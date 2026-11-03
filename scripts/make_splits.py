"""인물 단위 분할. 고정 테스트셋(와 망각 방지 replay 인물)의 person_id 목록을 configs/splits/에 저장한다.

같은 사람의 사진은 정답이 같아서 사진 단위로 나누면 점수가 부풀려진다 → 항상 person_id로 나눈다.
테스트 목록은 한 번 만들면 바꾸지 않는다 (바꾸면 이전 모델과 점수를 비교할 수 없다). 파일이 있으면 --force 없이는 덮어쓰지 않는다.

세 가지 일:
  --import FILE   기존 목록(txt 한 줄 하나 / json {"subjects": [{"subject_id": ...}]})을 테스트셋으로 채택
                  aihub_kr은 skin v2가 쓴 165명(face_analysis_AI/ailabtools/test_set/test_subjects_165.json)을 그대로 쓴다
  --fraction F    데이터셋 인물의 F 비율을 seed로 뽑아 테스트셋으로 (새 데이터셋). 기본 seed·비율은 configs/data.yaml
  --replay N      테스트 인물을 뺀 나머지에서 N명을 seed로 뽑아 replay 목록 저장 (configs/splits/<name>_replay.txt)
  --check         저장된 목록이 데이터셋 인물과 맞는지(존재·겹침 없음) 검사

사용: python -m scripts.make_splits --name aihub_kr --dataset outputs/datasets/aihub_kr --import <json> [--replay 100] [--check]
"""
import argparse
import csv
import json
import random
from pathlib import Path

from common.config import resolve
from training.config import load_yaml, read_id_list

SPLITS_DIR = "configs/splits"


def dataset_people(dataset_dir):
    """images.csv(없으면 labels.csv)의 person_id 집합."""
    for name in ("images.csv", "labels.csv"):
        path = resolve(dataset_dir) / name
        if path.exists():
            with open(path, newline="", encoding="utf-8") as f:
                return {r["person_id"] for r in csv.DictReader(f)}
    raise FileNotFoundError(f"{dataset_dir}에 images.csv/labels.csv가 없다")


def load_id_file(path):
    p = resolve(path)
    if p.suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(s["subject_id"]) for s in data["subjects"]}
    return read_id_list(p)


def write_ids(path, ids, header_lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"# {line}" for line in header_lines) + "\n" + "\n".join(sorted(ids)) + "\n"
    path.write_text(body, encoding="utf-8")


def check(name, people, test_path, replay_path=None):
    test = read_id_list(test_path)
    problems = []
    unknown = test - people
    if unknown:
        problems.append(f"테스트 목록에 데이터셋에 없는 사람 {len(unknown)}명: {sorted(unknown)[:5]}")
    if not test:
        problems.append("테스트 목록이 비어 있다")
    if replay_path and replay_path.exists():
        replay = read_id_list(replay_path)
        if replay & test:
            problems.append(f"replay와 테스트가 겹친다: {sorted(replay & test)[:5]}")
        if replay - people:
            problems.append(f"replay 목록에 데이터셋에 없는 사람 {len(replay - people)}명")
    print(f"[{name}] 데이터셋 {len(people)}명 / 테스트 {len(test)}명 / 학습 가능 {len(people - test)}명"
          + (f" / replay {len(read_id_list(replay_path))}명" if replay_path and replay_path.exists() else ""))
    if problems:
        raise SystemExit("검사 실패:\n  " + "\n  ".join(problems))
    print("검사 통과: 테스트 인물이 모두 데이터셋에 있고, replay와 겹치지 않는다")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--name", default="aihub_kr", help="데이터셋 이름 → configs/splits/<name>_test.txt")
    ap.add_argument("--dataset", required=True, help="images.csv 또는 labels.csv가 있는 데이터셋 폴더")
    ap.add_argument("--import", dest="import_file", help="기존 테스트 인물 목록 (txt/json)")
    ap.add_argument("--fraction", type=float, help="새로 나눌 때의 테스트 비율 (기본: configs/data.yaml split.test_fraction)")
    ap.add_argument("--seed", type=int, help="기본: configs/data.yaml split.seed")
    ap.add_argument("--replay", type=int, help="replay 인물 수 (기본: configs/data.yaml replay.people). 0이면 만들지 않음")
    ap.add_argument("--check", action="store_true", help="저장된 목록 검사만")
    ap.add_argument("--force", action="store_true", help="기존 목록 덮어쓰기 (테스트셋이 바뀌면 이전 점수와 비교 불가)")
    ap.add_argument("--data-config", default="configs/data.yaml")
    args = ap.parse_args(argv)

    cfg = load_yaml(args.data_config)
    people = dataset_people(args.dataset)
    test_path = resolve(SPLITS_DIR) / f"{args.name}_test.txt"
    replay_path = resolve(SPLITS_DIR) / f"{args.name}_replay.txt"

    if args.check:
        check(args.name, people, test_path, replay_path)
        return

    if args.import_file or args.fraction is not None or not test_path.exists():
        if test_path.exists() and not args.force:
            raise SystemExit(f"{test_path}가 이미 있다. 테스트셋은 바꾸지 않는다 (정말 바꾸려면 --force)")
        if args.import_file:
            test = load_id_file(args.import_file)
            unknown = test - people
            if unknown:
                raise SystemExit(f"가져온 목록에 데이터셋에 없는 사람 {len(unknown)}명: {sorted(unknown)[:5]}")
            header = [f"{args.name} 고정 테스트셋 (person_id). 학습·튜닝·에폭 선택에 절대 쓰지 않는다.",
                      f"출처: {Path(args.import_file).name} 에서 가져옴 ({len(test)}명). 이 파일이 원본이다 — 바꾸면 이전 모델과 비교 불가."]
        else:
            seed = args.seed if args.seed is not None else cfg["split"]["seed"]
            fraction = args.fraction if args.fraction is not None else cfg["split"]["test_fraction"]
            count = max(1, round(len(people) * fraction))
            test = set(random.Random(seed).sample(sorted(people), count))
            header = [f"{args.name} 고정 테스트셋 (person_id). 학습·튜닝·에폭 선택에 절대 쓰지 않는다.",
                      f"인물 단위 무작위 분할: seed {seed}, 비율 {fraction} → {len(test)}명 / 전체 {len(people)}명. 바꾸면 이전 모델과 비교 불가."]
        write_ids(test_path, test, header)
        print(f"테스트 목록 저장 {test_path} ({len(test)}명)")

    replay_n = args.replay if args.replay is not None else cfg["replay"]["people"]
    if replay_n:
        test = read_id_list(test_path)
        pool = sorted(people - test)
        if replay_path.exists() and not args.force:
            print(f"replay 목록 유지 {replay_path} ({len(read_id_list(replay_path))}명)")
        else:
            seed = cfg["replay"]["seed"]
            replay = set(random.Random(seed).sample(pool, min(replay_n, len(pool))))
            write_ids(replay_path, replay, [f"{args.name} 망각 방지(replay) 인물. 테스트 인물 제외, seed {seed}, {len(replay)}명 / 학습 {len(pool)}명."])
            print(f"replay 목록 저장 {replay_path} ({len(replay)}명)")
    check(args.name, people, test_path, replay_path)


if __name__ == "__main__":
    main()
