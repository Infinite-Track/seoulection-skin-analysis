"""데이터셋 사진 → 부위 크롭 (표준 데이터셋의 crops.csv + crops/). 전처리는 전부 common/ (서빙과 같은 코드).

입력: <dataset>/images.csv (scripts/prepare_aihub.py 등 어댑터 산출물) + 이미지 루트 (path의 기준 폴더)
출력: <dataset>/crops/{person_id}/{image_id}_{region}.png   RGBA (RGB = 원본 해상도 크롭, 알파 = 피부 마스크)
      <dataset>/crops.csv  사진 1장 × 부위 6 = 6행
        status: ok / hidden_view(측면에서 가려지는 쪽) / low_res / no_face / empty / read_error
        face_h: 얼굴 세로 길이(px) — 학습·서빙에서 배율을 맞추는 기준

기기 필터·최소 해상도는 configs/data.yaml. 중간에 멈춰도 다시 실행하면 **완전한** 사진(6부위 행 + ok PNG 존재, scripts/crop_index.py)만
건너뛰고, 부분 행만 남은 사진은 그 행을 버리고 다시 처리한다. crops.csv는 임시 파일에 쓴 뒤 원자적으로 교체한다.
워커마다 mediapipe 모델을 한 번 띄운다 (스레드 안전하지 않아 프로세스로 나눈다).

사용: python -m scripts.build_crops --dataset outputs/datasets/aihub_kr --image-root $AIHUB_RAW_ROOT
                                   [--persons configs/splits/aihub_kr_test.txt] [--workers 4] [--limit N]
"""
import argparse
import os
import time
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

from common import face_regions as fr
from common.config import resolve
from common.crop_files import save_region_png
from scripts.crop_index import COLUMNS, complete_image_ids, expected_images, read_csv, write_csv_atomic
from training.config import CROPS_FILE, load_paths, load_yaml, read_id_list

MEDIAPIPE_ENV = "MEDIAPIPE_MODELS_DIR"  # 서빙과 같은 환경변수. 없으면 configs/paths.yaml mediapipe_models

_worker = {}


def _init_worker(models_dir, dataset_dir, image_root, min_pixels):
    _worker.update(landmarker=fr.create_landmarker(models_dir), segmenter=fr.create_segmenter(models_dir),
                   dataset_dir=Path(dataset_dir), image_root=Path(image_root), min_pixels=min_pixels)


def process_image(image):
    """사진 1장 → crops.csv 행 6개. 저장하지 못한 부위도 이유(status)를 남긴다."""
    base = {k: image[k] for k in ("image_id", "person_id", "device", "view")}
    bgr = fr.read_image(_worker["image_root"] / image["path"])
    if bgr is None:
        return [dict(base, region=r, status="read_error") for r in fr.REGION_NAMES]
    if bgr.shape[0] * bgr.shape[1] < _worker["min_pixels"]:
        return [dict(base, region=r, status="low_res", width=bgr.shape[1], height=bgr.shape[0]) for r in fr.REGION_NAMES]

    face_h, regions = fr.extract_regions(bgr, image["view"], _worker["landmarker"], _worker["segmenter"])
    if face_h is None:
        return [dict(base, region=r, status="no_face") for r in fr.REGION_NAMES]

    base["face_h"] = round(face_h, 1)
    out_dir = _worker["dataset_dir"] / "crops" / image["person_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for region, result in regions.items():
        if isinstance(result, str):
            rows.append(dict(base, region=region, status=result))
            continue
        crop, mask = result
        file = out_dir / f"{image['image_id']}_{region}.png"
        save_region_png(file, crop, mask)
        rows.append(dict(base, region=region, status="ok", file=str(file.relative_to(_worker["dataset_dir"])),
                         width=crop.shape[1], height=crop.shape[0], skin_px=int(mask.sum())))
    return rows


def write_csv(path, rows):
    write_csv_atomic(path, rows, COLUMNS)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dataset", required=True, help="images.csv가 있는 데이터셋 폴더 (여기에 crops.csv, crops/를 만든다)")
    ap.add_argument("--image-root", default=os.environ.get("AIHUB_RAW_ROOT"),
                    help="images.csv path의 기준 폴더 (기본: 환경변수 AIHUB_RAW_ROOT)")
    ap.add_argument("--persons", help="이 파일에 있는 person_id만 처리 (예: configs/splits/aihub_kr_test.txt)")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--limit", type=int, help="이번 실행에서 처리할 사진 수 상한")
    ap.add_argument("--data-config", default="configs/data.yaml")
    args = ap.parse_args(argv)
    if not args.image_root:
        ap.error("--image-root 또는 환경변수 AIHUB_RAW_ROOT가 필요하다")

    cfg, paths = load_yaml(args.data_config), load_paths()
    models_dir = os.environ.get(MEDIAPIPE_ENV) or paths["mediapipe_models"]
    dataset_dir, image_root = resolve(args.dataset), resolve(args.image_root)
    index_path = dataset_dir / CROPS_FILE

    people = read_id_list(args.persons) if args.persons else None
    images = list(expected_images(dataset_dir, cfg["devices"], people).values())
    existing = read_csv(index_path) if index_path.exists() else []
    done, broken = complete_image_ids(existing, dataset_dir)
    if broken:
        print(f"불완전한 사진 {len(broken)}장은 행을 버리고 다시 처리한다: {list(broken.items())[:3]}", flush=True)
    done_rows = [r for r in existing if r["image_id"] in done]  # 완전한 사진의 행만 유지 (부분 행·고아 행 제거)
    todo = [r for r in images if r["image_id"] not in done][:args.limit]
    print(f"대상 {len(images):,}장 (완료 {sum(r['image_id'] in done for r in images):,}), 이번에 처리 {len(todo):,}장, "
          f"워커 {args.workers}", flush=True)

    rows, started = list(done_rows), time.time()
    init = (models_dir, dataset_dir, image_root, cfg["min_pixels"])
    with Pool(args.workers, initializer=_init_worker, initargs=init) as pool:
        for i, result in enumerate(pool.imap(process_image, todo, chunksize=4), 1):
            rows += result
            if i % 200 == 0 or i == len(todo):
                write_csv(index_path, rows)
                elapsed = time.time() - started
                print(f"  {i:,}/{len(todo):,}  {elapsed / i:.2f}초/장, 남은 약 {elapsed / i * (len(todo) - i) / 60:.0f}분",
                      flush=True)
    if not todo:
        write_csv(index_path, rows)  # 처리할 게 없어도 정리된(부분 행 제거) 목록을 쓴다
    print("status:", dict(Counter(r["status"] for r in rows)))


if __name__ == "__main__":
    main()
