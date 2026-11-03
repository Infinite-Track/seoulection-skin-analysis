"""crops.csv 공통: 안전한 저장(원자적 교체)과 완전성 검사. build_crops(재개)·upload_datasets(업로드 전)가 같이 쓴다.

"완전한 사진" = crops.csv에 그 사진의 6부위 행이 중복 없이 하나씩 있고, status가 아는 값이며, ok 행의 PNG가 실제로 있다.
status 어휘: ok / hidden_view / low_res / no_face / empty / read_error (hidden_view 이하도 정상적인 "처리 완료"다 —
아직 처리하지 않은 사진과 구분하기 위해 build_crops는 실패도 행으로 남긴다).
"""
import csv
import os
from collections import defaultdict
from pathlib import Path

from common.face_regions import REGION_NAMES
from training.config import CROPS_FILE

IMAGES_FILE = "images.csv"
COLUMNS = ["image_id", "person_id", "device", "view", "region", "status", "file", "width", "height", "skin_px", "face_h"]
STATUSES = {"ok", "hidden_view", "low_res", "no_face", "empty", "read_error"}


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv_atomic(path, rows, fieldnames):
    """같은 폴더의 임시 파일에 다 쓴 뒤 os.replace → 저장 도중 멈춰도 반쪽짜리 파일이 남지 않는다."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def expected_images(dataset_dir, devices, people=None):
    """images.csv에서 크롭 대상 사진(기기 필터, 인물 필터) → {image_id: 행}."""
    images = {}
    for r in read_csv(Path(dataset_dir) / IMAGES_FILE):
        if r["device"] in devices and (people is None or r["person_id"] in people):
            images[r["image_id"]] = r
    return images


def complete_image_ids(rows, dataset_dir):
    """crops.csv 행들 → (완전한 image_id 집합, {불완전 image_id: 이유}). 파일 존재까지 본다."""
    by_image = defaultdict(list)
    for r in rows:
        by_image[r["image_id"]].append(r)
    complete, problems = set(), {}
    for image_id, group in by_image.items():
        regions = [r["region"] for r in group]
        if sorted(regions) != sorted(REGION_NAMES):
            problems[image_id] = f"부위 행 {len(regions)}개 (기대 {len(REGION_NAMES)}, 중복/누락)"
            continue
        bad = [r["status"] for r in group if r["status"] not in STATUSES]
        if bad:
            problems[image_id] = f"모르는 status {bad}"
            continue
        missing = [r["file"] for r in group if r["status"] == "ok" and not (Path(dataset_dir) / r["file"]).is_file()]
        if missing:
            problems[image_id] = f"PNG 없음 {missing[:2]}"
            continue
        complete.add(image_id)
    return complete, problems


def check_dataset(dataset_dir, devices, people):
    """인물 집합에 대해 크롭이 완전한지 검사. (crops 행 목록, 문제 목록[str]) 반환. 문제가 없으면 빈 목록."""
    dataset_dir = Path(dataset_dir)
    expected = expected_images(dataset_dir, devices, people)
    rows = [r for r in read_csv(dataset_dir / CROPS_FILE) if r["person_id"] in people] if (dataset_dir / CROPS_FILE).exists() else []
    problems = []
    missing_people = sorted(people - {r["person_id"] for r in expected.values()})
    if missing_people:
        problems.append(f"images.csv에 없는 인물 {len(missing_people)}명: {missing_people[:5]}")
    complete, broken = complete_image_ids(rows, dataset_dir)
    unprocessed = sorted(set(expected) - complete - set(broken))
    if unprocessed:
        problems.append(f"아직 크롭하지 않은 사진 {len(unprocessed)}장: {unprocessed[:5]} (build_crops를 끝까지 돌릴 것)")
    if broken:
        items = list(broken.items())[:5]
        problems.append(f"불완전한 사진 {len(broken)}장: {items} (build_crops 재실행으로 복구)")
    extra = sorted({r["image_id"] for r in rows} - set(expected))
    if extra:
        problems.append(f"images.csv 대상이 아닌 사진의 행 {len(extra)}장: {extra[:5]}")
    return rows, problems
