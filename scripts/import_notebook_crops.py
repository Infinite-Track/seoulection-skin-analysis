"""skin v2 노트북이 만든 크롭(face_analysis_AI/data/01.원천데이터 + index.csv) → 표준 데이터셋의 crops.csv + crops/.

build_crops.py와 같은 결과를 내지만(같은 common/ 전처리 — 3명 84크롭 픽셀 단위 동일 확인) 다시 계산하지 않고
기존 파일을 하드링크(같은 볼륨) 또는 복사로 가져온다. 노트북 index.csv의 status 어휘(ok/hidden_view/low_res/no_face/empty)는 그대로다.

사용: python -m scripts.import_notebook_crops --src /path/to/face_analysis_AI/data --dataset outputs/datasets/aihub_kr
"""
import argparse
import csv
import os
import shutil
from collections import Counter
from pathlib import Path

from common.config import resolve
from training.config import CROPS_FILE

COLUMNS = ["image_id", "person_id", "device", "view", "region", "status", "file", "width", "height", "skin_px", "face_h"]
DEVICES = {"스마트폰": "smartphone", "스마트패드": "smartpad"}
INDEX, CROP_DIR = "index.csv", "01.원천데이터"


def link_or_copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", required=True, help="노트북 data 폴더 (index.csv, 01.원천데이터/)")
    ap.add_argument("--dataset", required=True, help="표준 데이터셋 폴더 (crops.csv, crops/를 만든다)")
    args = ap.parse_args(argv)
    src, dataset = resolve(args.src), resolve(args.dataset)
    dataset.mkdir(parents=True, exist_ok=True)

    rows = []
    with open(src / INDEX, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            row = dict(image_id=r["stem"], person_id=r["subject"], device=DEVICES[r["device"]], view=r["view"],
                       region=r["region"], status=r["status"], file="", width=r["width"], height=r["height"],
                       skin_px=r["skin_px"], face_h=r["face_h"])
            if r["status"] == "ok":
                rel = Path("crops") / r["subject"] / f"{r['stem']}_{r['region']}.png"
                link_or_copy(src / CROP_DIR / r["file"], dataset / rel)
                row["file"] = str(rel)
            rows.append(row)
    with open(dataset / CROPS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{CROPS_FILE} {len(rows):,}행, 사람 {len({r['person_id'] for r in rows})}, status {dict(Counter(r['status'] for r in rows))}")
    print(f"→ {dataset}")


if __name__ == "__main__":
    main()
