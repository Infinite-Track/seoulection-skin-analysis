"""고정 테스트셋·망각 방지(replay) 데이터를 S3에 올린다. **기본은 --dry-run** (목록·크기만 출력, AWS 호출 없음).

S3 구조 (configs/paths.yaml):
  s3://<bucket>/<s3.testset>/<name>/   crops.csv, labels.csv, crops/<person>/...   ← 테스트 인물만
  s3://<bucket>/<s3.replay>/<name>/    crops.csv, labels.csv, crops/<person>/...   ← replay 인물만
각 접두사가 그대로 SageMaker 채널(SM_CHANNEL_*)이 되도록 표준 데이터셋 형식을 유지한다.

인물 목록은 configs/splits/<name>_test.txt, <name>_replay.txt (scripts/make_splits.py). 올릴 csv는 그 인물로 걸러
<output_root>/upload/<testset|replay>/<name>/ 에 먼저 쓴다 (dry-run에서도 만들어 내용을 확인할 수 있다).

올리기 전에 완전성을 검사한다 (scripts/crop_index.check_dataset): 목록의 인물이 전부 images.csv에 있고, 대상 사진(기기 필터)마다
6부위 행이 중복 없이 있으며 ok 행의 PNG가 존재해야 한다. 하나라도 어긋나면 **아무것도 올리지 않고** 멈춘다
(고정 테스트셋이 축소된 채 게시되는 것을 막는다). testset·replay 둘 다 검사를 끝낸 뒤에야 업로드를 시작한다.

사용: python -m scripts.upload_datasets --dataset outputs/datasets/aihub_kr [--name aihub_kr] [--what testset|replay|both]
      실제 업로드: ... --execute   (boto3 기본 인증 체인. 키를 코드·설정에 넣지 않는다)
"""
import argparse
import csv
import json
from pathlib import Path

from common.config import resolve
from scripts.crop_index import check_dataset, read_csv
from training.config import CROPS_FILE, LABELS_FILE, load_paths, load_yaml, read_id_list

SPLITS_DIR = "configs/splits"


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def stage_subset(dataset_dir, people, devices, stage_dir):
    """인물 부분집합의 완전성을 검사하고 crops.csv·labels.csv를 stage_dir에 쓴 뒤, 올릴 (로컬 파일, 상대 키) 목록을 돌려준다.

    불완전하면 SystemExit — 이 함수가 통과한 것만 올린다."""
    crops, problems = check_dataset(dataset_dir, devices, people)
    if problems:
        raise SystemExit(f"{dataset_dir}: 인물 {len(people)}명 데이터가 불완전해 올리지 않는다\n  - " + "\n  - ".join(problems))
    labels = [r for r in read_csv(dataset_dir / LABELS_FILE) if r["person_id"] in people]
    no_label = sorted(people - {r["person_id"] for r in labels})
    if no_label:
        raise SystemExit(f"{dataset_dir}: 라벨 없는 인물 {len(no_label)}명: {no_label[:5]}")
    write_csv(stage_dir / CROPS_FILE, crops)
    write_csv(stage_dir / LABELS_FILE, labels)
    files = [(stage_dir / CROPS_FILE, CROPS_FILE), (stage_dir / LABELS_FILE, LABELS_FILE)]
    for r in crops:
        if r["status"] == "ok":
            files.append((dataset_dir / r["file"], r["file"]))
    missing = [str(f) for f, _ in files if not f.exists()]
    if missing:
        raise SystemExit(f"없는 파일 {len(missing)}개: {missing[:3]}")
    summary = dict(people=len({r["person_id"] for r in crops}), crop_rows=len(crops),
                   ok_crops=sum(r["status"] == "ok" for r in crops), label_rows=len(labels),
                   files=len(files), bytes=sum(f.stat().st_size for f, _ in files))
    return files, summary


def upload(files, bucket, prefix, execute, region):
    """files: [(로컬 경로, 상대 키)]. execute=False면 출력만."""
    print(f"  → s3://{bucket}/{prefix}/  ({len(files)}개)")
    for local, key in files[:3]:
        print(f"     {local.name:40s} → {prefix}/{key}")
    if len(files) > 3:
        print(f"     … 외 {len(files) - 3}개")
    if not execute:
        print("  (dry-run: 올리지 않음)")
        return
    import boto3
    client = boto3.client("s3", region_name=region)
    for i, (local, key) in enumerate(files, 1):
        client.upload_file(str(local), bucket, f"{prefix}/{key}")
        if i % 500 == 0 or i == len(files):
            print(f"  업로드 {i:,}/{len(files):,}", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dataset", required=True, help="crops.csv, labels.csv, crops/가 있는 데이터셋 폴더")
    ap.add_argument("--name", default="aihub_kr", help="S3 접두사 아래 폴더 이름 = configs/splits/<name>_*.txt")
    ap.add_argument("--what", choices=["testset", "replay", "both"], default="both")
    ap.add_argument("--dry-run", action="store_true", default=True, help="기본값. 목록만 출력")
    ap.add_argument("--execute", action="store_true", help="실제로 S3에 올린다 (dry-run 해제)")
    ap.add_argument("--data-config", default="configs/data.yaml", help="기기 필터 (대상 사진 범위)")
    args = ap.parse_args(argv)
    execute = args.execute

    paths, devices = load_paths(), load_yaml(args.data_config)["devices"]
    bucket, s3, region = paths["s3"]["bucket"], paths["s3"], paths.get("aws_region")
    dataset_dir = resolve(args.dataset)
    stage_root = resolve(paths["output_root"]) / "upload"
    targets = {"testset": (s3["testset"], f"{args.name}_test.txt"), "replay": (s3["replay"], f"{args.name}_replay.txt")}
    chosen = ["testset", "replay"] if args.what == "both" else [args.what]

    manifest, staged = {}, []
    print(f"{'실제 업로드' if execute else 'DRY-RUN'} — 버킷 {bucket}, 리전 {region}")
    for what in chosen:  # 1) 전부 검사·스테이징 — 하나라도 불완전하면 여기서 멈춘다 (아무것도 안 올림)
        prefix, list_name = targets[what]
        people = read_id_list(Path(SPLITS_DIR) / list_name)
        files, summary = stage_subset(dataset_dir, people, devices, stage_root / what / args.name)
        print(f"\n[{what}] {args.name}: 인물 {summary['people']}/{len(people)}, 크롭 행 {summary['crop_rows']:,} "
              f"(ok {summary['ok_crops']:,}), 라벨 {summary['label_rows']:,}행, 파일 {summary['files']:,}개, "
              f"{summary['bytes'] / 1e6:.0f} MB — 완전성 검사 통과")
        staged.append((what, prefix, list_name, files, summary))
    for what, prefix, list_name, files, summary in staged:  # 2) 업로드
        print(f"\n[{what}]")
        upload(files, bucket, f"{prefix}/{args.name}", execute, region)
        manifest[what] = dict(summary, s3=f"s3://{bucket}/{prefix}/{args.name}/", people_list=list_name)
    (stage_root / f"{args.name}_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n요약 저장 {stage_root / f'{args.name}_manifest.json'}")


if __name__ == "__main__":
    main()
