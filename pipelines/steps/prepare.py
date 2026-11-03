"""Prepare 단계 (Processing, CPU): 고정 테스트셋을 검증하고 replay + feedback(신규) 데이터를 학습용 표준 데이터셋 하나로 합친다.

입력 — 전부 표준 데이터셋 형식 (crops.csv + labels.csv + 크롭 PNG, training/dataset.py 맨 위):
  --testset   S3 datasets/testset/<name>/ (채널)  검사만 한다 — 인물 목록이 configs/splits/<name>_test.txt와 같은지, 6부위 행·PNG가 다 있는지.
                                                      학습에는 쓰지 않고, 여기 인물은 replay·feedback에서 제외한다 (누수 방지)
  --replay    S3 datasets/replay/<name>/ (채널)    망각 방지 데이터. 테스트 인물이 섞여 있으면 멈춘다 (분할이 깨진 것)
  --feedback  S3 feedback/ (주소 또는 폴더)        신규 데이터. 비어 있어도 되므로 채널이 아니라 boto3로 내려받는다.
                                                      안에서 crops.csv + labels.csv가 있는 폴더(깊이 2까지)를 각각 하나의 데이터셋으로 본다.
                                                      그 외 파일은 세기만 한다 (라벨 출처가 정해지면 scripts/prepare_*.py 어댑터로 변환해 올린다)
출력:
  --out       합친 학습 데이터셋: crops.csv, labels.csv, <source>/crops/... (person_id·image_id는 "<source>:<원래 값>" — 소스 간 충돌 방지)
              source = "replay" 또는 "feedback/<feedback 루트 기준 상대 경로>" (예: feedback/2026-10/batch1 — 마지막 폴더 이름만 쓰면
              2026-10/batch1과 2026-11/batch1이 충돌한다). 소스 ID·출력 파일·image_id·라벨 키가 겹치면 덮어쓰지 않고 멈춘다
  --manifest  manifest.json (소스별 인물·크롭 수, 제외 인물, 테스트셋 검증 결과)

라벨 있는 feedback 크롭이 --min-feedback-crops보다 적으면 멈춘다 (0이면 replay만으로도 진행 — 파이프라인 점검용).
Training 단계는 --out을 데이터셋 "train"으로 읽는다 (configs/train_pipeline.yaml, SM_CHANNEL_TRAIN).

사용: python -m pipelines.steps.prepare --testset DIR --replay DIR --feedback DIR|s3://... --out DIR --manifest FILE [--testset-name aihub_kr]
"""
from pipelines.steps.bootstrap import system_libs  # noqa: I001 (common을 import하기 전에)

system_libs()

import argparse  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from common.config import resolve  # noqa: E402
from pipelines.steps.util import SM_INPUT, SM_OUTPUT, download_prefix, dump_json  # noqa: E402
from scripts.crop_index import COLUMNS, complete_image_ids, read_csv, write_csv_atomic  # noqa: E402
from training.config import CROPS_FILE, LABELS_FILE, load_yaml, read_id_list  # noqa: E402

PIPELINE_CONFIG = "configs/pipeline.yaml"
SPLITS_DIR = "configs/splits"
LABEL_COLUMNS = ["person_id", "region", "task", "value"]
FEEDBACK_DEPTH = 2  # feedback/ 아래 이 깊이까지 데이터셋 폴더를 찾는다


def load_dataset(root):
    """표준 데이터셋 폴더 → (crops 행, labels 행). 형식이 아니면 SystemExit."""
    root = Path(root)
    for name in (CROPS_FILE, LABELS_FILE):
        if not (root / name).is_file():
            raise SystemExit(f"{root}: {name} 없음 (표준 데이터셋 형식이 아니다)")
    crops, labels = read_csv(root / CROPS_FILE), read_csv(root / LABELS_FILE)
    if crops and (missing := set(COLUMNS) - set(crops[0])):
        raise SystemExit(f"{root}/{CROPS_FILE}: 열 {sorted(missing)} 없음")
    if labels and (missing := set(LABEL_COLUMNS) - set(labels[0])):
        raise SystemExit(f"{root}/{LABELS_FILE}: 열 {sorted(missing)} 없음")
    return crops, labels


def completeness_problems(root, crops, labels):
    """6부위 행·status·ok PNG 존재(scripts/crop_index)와 라벨 유무 검사 → 문제 목록."""
    _, broken = complete_image_ids(crops, root)
    problems = [f"{image_id}: {why}" for image_id, why in list(broken.items())[:10]]
    if len(broken) > 10:
        problems.append(f"… 외 불완전한 사진 {len(broken) - 10}장")
    unlabeled = sorted({r["person_id"] for r in crops} - {r["person_id"] for r in labels})
    if unlabeled:
        problems.append(f"라벨 없는 인물 {len(unlabeled)}명: {unlabeled[:5]}")
    return problems


def check_testset(root, name):
    """고정 테스트셋 검증 → (테스트 인물 집합, 요약). 목록(configs/splits/<name>_test.txt)과 다르거나 불완전하면 SystemExit."""
    crops, labels = load_dataset(root)
    people = {r["person_id"] for r in crops}
    split_file = resolve(SPLITS_DIR) / f"{name}_test.txt"
    problems = completeness_problems(root, crops, labels)
    if split_file.exists():
        expected = read_id_list(split_file)
        if people != expected:
            problems.append(f"인물이 {split_file.name}과 다르다: 목록에만 {sorted(expected - people)[:5]} ({len(expected - people)}명), "
                            f"데이터에만 {sorted(people - expected)[:5]} ({len(people - expected)}명)")
    else:
        print(f"경고: {split_file} 없음 — 테스트셋 폴더의 인물 전원을 고정 테스트셋으로 본다", flush=True)
    if not people:
        problems.append("테스트 인물이 없다")
    if problems:
        raise SystemExit(f"테스트셋 {root} 검증 실패:\n  - " + "\n  - ".join(problems))
    summary = dict(dir=str(root), people=len(people), crop_rows=len(crops), ok_crops=sum(r["status"] == "ok" for r in crops),
                   label_rows=len(labels), split_file=str(split_file) if split_file.exists() else None)
    return people, summary


def find_datasets(root, max_depth=FEEDBACK_DEPTH):
    """root 아래에서 crops.csv + labels.csv가 있는 폴더를 찾는다 → (폴더 목록, 그 밖의 파일 수)."""
    root = Path(root)
    if not root.is_dir():
        return [], 0
    found, other = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        if CROPS_FILE in filenames and LABELS_FILE in filenames:
            found.append(Path(dirpath))
            dirnames[:] = []
            continue
        other += len(filenames)
        if depth >= max_depth:
            dirnames[:] = []
    return sorted(found), other


def feedback_source(feedback_root, dataset_dir):
    """feedback 데이터셋 폴더 → 소스 ID "feedback/<feedback 루트 기준 상대 경로>" (루트 자체가 데이터셋이면 "feedback")."""
    rel = Path(dataset_dir).resolve().relative_to(Path(feedback_root).resolve()).as_posix()
    return "feedback" if rel == "." else f"feedback/{rel}"


def link_or_copy(src, dst):
    """src → dst (같은 볼륨이면 하드링크). dst가 이미 있으면 덮어쓰지 않고 멈춘다 — 소스 간 파일 충돌은 데이터 손상이다."""
    if dst.exists():
        raise SystemExit(f"출력 파일 충돌: {dst} 이미 있음 (소스 ID가 겹친다)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)  # 같은 볼륨이면 복사 없이
    except OSError:
        shutil.copyfile(src, dst)


def merge_source(source, root, crops, labels, out, exclude):
    """소스 하나를 out에 넣는다 (exclude 인물 제외, ok 크롭 PNG 복사). → (crops 행, labels 행, 요약)."""
    root, out = Path(root), Path(out)
    excluded = sorted({r["person_id"] for r in crops} & exclude)
    rows, copied = [], 0
    for r in crops:
        if r["person_id"] in exclude:
            continue
        row = dict(r, person_id=f"{source}:{r['person_id']}", image_id=f"{source}:{r['image_id']}", file=f"{source}/{r['file']}")
        if r["status"] == "ok":
            link_or_copy(root / r["file"], out / row["file"])
            copied += 1
        rows.append(row)
    label_rows = [dict(r, person_id=f"{source}:{r['person_id']}") for r in labels if r["person_id"] not in exclude]
    summary = dict(dir=str(root), people=len({r["person_id"] for r in rows}), crop_rows=len(rows), ok_crops=copied,
                   label_rows=len(label_rows), excluded_test_people=excluded)
    return rows, label_rows, summary


def merge_conflicts(sources, rows, labels):
    """합친 결과의 충돌 목록: 소스 ID 중복, 출력 파일·(image_id, region) 중복, 라벨 키(person_id, region, task) 중복. 비어 있어야 정상."""
    problems = []
    dupes = sorted({s for s in sources if sources.count(s) > 1})
    if dupes:
        problems.append(f"소스 ID 중복: {dupes}")
    for name, keys in (("출력 파일", [r["file"] for r in rows if r["status"] == "ok"]),  # 실패 행은 file이 비어 있다
                       ("크롭 (image_id, region)", [(r["image_id"], r["region"]) for r in rows]),
                       ("라벨 키 (person_id, region, task)", [(r["person_id"], r["region"], r["task"]) for r in labels])):
        seen, dup = set(), set()
        for k in keys:
            (dup if k in seen else seen).add(k)
        if dup:
            problems.append(f"{name} 중복 {len(dup)}건: {sorted(map(str, dup))[:3]}")
    return problems


def ok_crops(crops, exclude):
    return sum(r["status"] == "ok" and r["person_id"] not in exclude for r in crops)


def main(argv=None):
    defaults = load_yaml(PIPELINE_CONFIG)["prepare"]
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--testset", default=f"{SM_INPUT}/testset", help="고정 테스트셋 폴더 (검사만)")
    ap.add_argument("--replay", default=f"{SM_INPUT}/replay", help="망각 방지 데이터 폴더")
    ap.add_argument("--feedback", default=None, help="신규 데이터: 폴더 또는 s3://… (기본: 없음 → 신규 0건)")
    ap.add_argument("--out", default=f"{SM_OUTPUT}/train", help="합친 학습 데이터셋 폴더 (비어 있어야 한다)")
    ap.add_argument("--manifest", default=f"{SM_OUTPUT}/prepare/manifest.json")
    ap.add_argument("--testset-name", default=defaults["testset_name"], help="configs/splits/<name>_test.txt와 대조")
    ap.add_argument("--min-feedback-crops", type=int, default=defaults["min_feedback_crops"],
                    help="라벨 있는 신규 ok 크롭 최소 수. 미달이면 멈춘다 (0: replay만으로 진행)")
    ap.add_argument("--overwrite", action="store_true", help="--out이 비어 있지 않아도 지우고 다시 만든다")
    args = ap.parse_args(argv)

    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        if not args.overwrite:
            raise SystemExit(f"{out}이 비어 있지 않다 (--overwrite로 지우고 다시 만들 수 있다)")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    # 1) 고정 테스트셋 — 검사만, 인물은 제외 목록
    test_people, test_summary = check_testset(args.testset, args.testset_name)
    print(f"테스트셋 {args.testset}: {test_summary['people']}명, ok 크롭 {test_summary['ok_crops']:,}개 — 검증 통과", flush=True)

    # 2) replay — 테스트 인물이 섞여 있으면 분할이 깨진 것이므로 멈춘다
    replay_crops, replay_labels = load_dataset(args.replay)
    problems = completeness_problems(args.replay, replay_crops, replay_labels)
    leaked = sorted({r["person_id"] for r in replay_crops} & test_people)
    if leaked:
        problems.append(f"테스트 인물 {len(leaked)}명이 replay에 있다: {leaked[:5]} (scripts/make_splits.py --check)")
    if problems:
        raise SystemExit(f"replay {args.replay} 검증 실패:\n  - " + "\n  - ".join(problems))

    # 3) feedback — 폴더 또는 S3. 표준 형식 데이터셋만 쓰고 나머지는 센다
    with tempfile.TemporaryDirectory(prefix="feedback-") as tmp:
        feedback_root, downloaded = args.feedback, None
        if feedback_root and feedback_root.startswith("s3://"):
            downloaded = download_prefix(feedback_root, tmp)
            print(f"feedback {feedback_root}: 객체 {downloaded:,}개 내려받음", flush=True)
            feedback_root = tmp
        datasets, unrecognized = find_datasets(feedback_root) if feedback_root else ([], 0)
        feedback = []
        for d in datasets:
            crops, labels = load_dataset(d)
            problems = completeness_problems(d, crops, labels)
            if problems:
                raise SystemExit(f"feedback 데이터셋 {d} 검증 실패:\n  - " + "\n  - ".join(problems))
            feedback.append((feedback_source(feedback_root, d), d, crops, labels))
        feedback_ok = sum(ok_crops(crops, test_people) for _, _, crops, _ in feedback)
        print(f"feedback: 데이터셋 {len(feedback)}개, 라벨 있는 ok 크롭 {feedback_ok:,}개 (형식 밖 파일 {unrecognized:,}개)", flush=True)
        if feedback_ok < args.min_feedback_crops:
            raise SystemExit(f"신규 ok 크롭 {feedback_ok}개 < 최소 {args.min_feedback_crops}개 → 학습하지 않는다")

        # 4) 합치기 (replay 먼저). 소스 ID가 겹치면 파일·라벨이 섞이므로 복사 전에 멈춘다
        inputs = [("replay", Path(args.replay), replay_crops, replay_labels), *feedback]
        if conflicts := merge_conflicts([s for s, *_ in inputs], [], []):
            raise SystemExit("합치기 실패:\n  - " + "\n  - ".join(conflicts))
        all_rows, all_labels, sources = [], [], {}
        for source, root, crops, labels in inputs:
            rows, label_rows, summary = merge_source(source, root, crops, labels, out, test_people)
            if summary["excluded_test_people"]:
                print(f"경고: {source}에서 테스트 인물 {len(summary['excluded_test_people'])}명 제외", flush=True)
            all_rows += rows
            all_labels += label_rows
            sources[source] = summary
            print(f"  {source:32s} {summary['people']:4d}명  ok 크롭 {summary['ok_crops']:6,}개  라벨 {summary['label_rows']:6,}행", flush=True)

    if conflicts := merge_conflicts(list(sources), all_rows, all_labels):
        raise SystemExit("합친 데이터셋에 충돌이 있다 (소스 안의 중복 포함):\n  - " + "\n  - ".join(conflicts))
    if not any(r["status"] == "ok" for r in all_rows):
        raise SystemExit("학습할 ok 크롭이 없다")
    write_csv_atomic(out / CROPS_FILE, all_rows, COLUMNS)
    write_csv_atomic(out / LABELS_FILE, all_labels, LABEL_COLUMNS)

    manifest = dict(created=datetime.now(timezone.utc).isoformat(timespec="seconds"), out=str(out),
                    testset=dict(test_summary, name=args.testset_name), sources=sources,
                    feedback=dict(uri=args.feedback, downloaded_objects=downloaded, datasets=len(feedback), ok_crops=feedback_ok,
                                  unrecognized_files=unrecognized, min_feedback_crops=args.min_feedback_crops),
                    total=dict(people=len({r["person_id"] for r in all_rows}), crop_rows=len(all_rows),
                               ok_crops=sum(r["status"] == "ok" for r in all_rows), label_rows=len(all_labels)))
    dump_json(args.manifest, manifest)
    print(f"학습 데이터셋 {out}: {manifest['total']['people']}명, ok 크롭 {manifest['total']['ok_crops']:,}개 → {args.manifest}", flush=True)


if __name__ == "__main__":
    main()
