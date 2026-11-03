"""AI-Hub 한국인 피부상태 측정 데이터(원본) → 표준 데이터셋의 목록 두 개 (images.csv, labels.csv).

이미지는 열지 않고 파일명과 라벨 JSON만 읽는다. 크롭은 scripts/build_crops.py.

표준 데이터셋 형식 (training/dataset.py 맨 위와 같다. 다른 데이터 소스는 이 두 파일을 만드는 어댑터만 새로 쓰면 된다):
  images.csv  image_id, person_id, device, view, path(이미지 루트 기준 상대 경로), source_split
  labels.csv  person_id, region, task, value   (원래 단위. region·task 이름은 configs/tasks.yaml)

라벨은 사람 단위로 같은 값이 모든 사진의 JSON에 복사돼 있어서(Phase 0 점검: 불일치 0건) 스마트폰 정면 JSON만 읽는다 — skin v2와 같다.
피부나이의 정답은 실제 나이(info.age). 6부위 모두에 같은 값을 넣고 추론 때 부위별 예측을 평균한다.

사용: AIHUB_RAW_ROOT=<.../3.개방데이터/1.데이터> python -m scripts.prepare_aihub [--out outputs/datasets/aihub_kr]
"""
import argparse
import csv
import json
import os
import re
from collections import Counter
from pathlib import Path

from common.config import resolve
from training.config import load_paths

RAW_ROOT_ENV = "AIHUB_RAW_ROOT"
SPLITS = ("Training", "Validation")
IMAGE_DIR, LABEL_DIR = "01.원천데이터", "02.라벨링데이터"
DEVICE_DIRS = {"1. 디지털카메라": "digital_camera", "2. 스마트패드": "smartpad", "3. 스마트폰": "smartphone"}
LABEL_DEVICE_DIR = "3. 스마트폰"  # 라벨은 이 기기의 정면 JSON에서만 읽는다
IMAGE_RE = re.compile(r"^(?P<person>\d{4})_(?P<dev>\d{2})_(?P<view>[A-Za-z]+\d*)$")  # 0002_03_F

# 부위 → (라벨 JSON 부위 번호 facepart, [(항목, JSON 섹션, 키)])
FIELDS = {
    "forehead": (1, [("wrinkle_grade", "annotations", "forehead_wrinkle"),
                     ("pigment_grade", "annotations", "forehead_pigmentation")]),
    "glabella": (2, [("wrinkle_grade", "annotations", "glabellus_wrinkle")]),
    "l_eye": (3, [("wrinkle_grade", "annotations", "l_perocular_wrinkle"),
                  ("wrinkle_ra", "equipment", "l_perocular_wrinkle_Ra")]),
    "r_eye": (4, [("wrinkle_grade", "annotations", "r_perocular_wrinkle"),
                  ("wrinkle_ra", "equipment", "r_perocular_wrinkle_Ra")]),
    "l_cheek": (5, [("pore_grade", "annotations", "l_cheek_pore"),
                    ("pore_count", "equipment", "l_cheek_pore"),  # 같은 키가 등급(annotations)과 개수(equipment)에 다 있다
                    ("pigment_grade", "annotations", "l_cheek_pigmentation")]),
    "r_cheek": (6, [("pore_grade", "annotations", "r_cheek_pore"),
                    ("pore_count", "equipment", "r_cheek_pore"),
                    ("pigment_grade", "annotations", "r_cheek_pigmentation")]),
}


def raw_root():
    root = os.environ.get(RAW_ROOT_ENV)
    if not root:
        raise SystemExit(f"환경변수 {RAW_ROOT_ENV}가 없다 (AI-Hub 원본의 '1.데이터' 폴더)")
    root = Path(root)
    if not (root / SPLITS[0] / IMAGE_DIR).is_dir():
        raise SystemExit(f"{root} 아래에 {SPLITS[0]}/{IMAGE_DIR}가 없다")
    return root


def build_images(root):
    rows = []
    for split in SPLITS:
        for dev_dir, device in DEVICE_DIRS.items():
            for p in sorted((root / split / IMAGE_DIR / dev_dir).glob("*/*.jpg")):
                m = IMAGE_RE.match(p.stem)
                if not m:
                    raise ValueError(f"파일명 규칙 위반: {p}")
                rows.append(dict(image_id=p.stem, person_id=m["person"], device=device, view=m["view"],
                                 path=str(p.relative_to(root)), source_split=split))
    return rows


def build_labels(root):
    rows = []
    for split in SPLITS:
        for person_dir in sorted((root / split / LABEL_DIR / LABEL_DEVICE_DIR).iterdir()):
            if not person_dir.is_dir():
                continue
            pid, age = person_dir.name, None
            for region, (part, fields) in FIELDS.items():
                label = json.loads((person_dir / f"{pid}_03_F_{part:02d}.json").read_text(encoding="utf-8"))
                age = label["info"]["age"]
                for task, section, key in fields:
                    value = (label.get(section) or {}).get(key)
                    if value is not None:
                        rows.append(dict(person_id=pid, region=region, task=task, value=float(value)))
            rows += [dict(person_id=pid, region=region, task="age", value=float(age)) for region in FIELDS]
    return rows


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", help="기본: <output_root>/datasets/aihub_kr")
    args = ap.parse_args(argv)
    root = raw_root()
    out = resolve(args.out or Path(load_paths()["output_root"]) / "datasets" / "aihub_kr")
    out.mkdir(parents=True, exist_ok=True)

    images, labels = build_images(root), build_labels(root)
    write_csv(out / "images.csv", images, ["image_id", "person_id", "device", "view", "path", "source_split"])
    write_csv(out / "labels.csv", labels, ["person_id", "region", "task", "value"])

    people = {r["person_id"] for r in images}
    print(f"images.csv  {len(images):,}장, {len(people)}명, 기기 {dict(Counter(r['device'] for r in images))}, "
          f"각도 {dict(Counter(r['view'] for r in images))}")
    print(f"labels.csv  {len(labels):,}행, {len({r['person_id'] for r in labels})}명, 항목 {dict(Counter(r['task'] for r in labels))}")
    missing = people - {r["person_id"] for r in labels}
    if missing:
        print(f"경고: 라벨 없는 사람 {len(missing)}명 {sorted(missing)[:5]}…")
    print(f"→ {out}\n이미지 루트(build_crops --image-root): {root}")


if __name__ == "__main__":
    main()
