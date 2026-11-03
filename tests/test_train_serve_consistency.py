"""학습 전처리와 서빙 전처리가 **같은 입력에 같은 결과**를 내는지 (Phase 5).

학습 경로: training/dataset.RegionDataset — 크롭 PNG → common.crop_files.load_region_png → common.transforms.to_model_input
서빙 경로: common/predictor.SkinPredictor.model_input — 크롭 → 같은 to_model_input (mirror_right는 meta.json 값)
타깃:      common/training_targets.encode_target(학습 정변환) ↔ common/tasks.decode·finalize(서빙 역변환)

서빙 쪽은 ONNX 세션 없이 입력 계약만 확인한다 — SkinPredictor를 운영 모델 `models/skin_v2/meta.json`(git에 있음)으로
만들되 세션은 열지 않는다. 내보낸 모델로 세션까지 여는 검증은 tests/test_export_reference.py.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from common import face_regions, tasks as serving_tasks, transforms
from common.config import REPO_ROOT
from common.crop_files import load_region_png, save_region_png
from common.predictor import SkinPredictor
from common.training_targets import (check_tasks_match, encode_target, grade_to_class, meta_tasks, output_layout,
                                     regression_stats)

pytest.importorskip("yaml")  # training/config.py (requirements-train.txt). 서빙 CI 환경(requirements.txt)에는 없음 → 건너뜀
torch = pytest.importorskip("torch")  # training/dataset.py

from training import dataset as training_dataset  # noqa: E402
from training.config import TrainConfig, load_tasks, load_yaml  # noqa: E402
from training.dataset import RegionDataset  # noqa: E402

V2_META = json.loads((REPO_ROOT / "models/skin_v2/meta.json").read_text(encoding="utf-8"))
REGIONS = list(face_regions.CANVAS)
# 부위별 합성 크롭 (세로, 가로, 얼굴 세로 길이). 확대(face_h < 900)·축소(> 900), 캔버스보다 큰 쪽(가운데 잘라냄)·작은 쪽(0 채움)이 섞이게
CROP_SPECS = {
    "forehead": (300, 700, 1000.0),   # 축소 0.9 → 630x270: 가로는 잘라내고 세로는 채운다
    "glabella": (150, 150, 700.0),    # 확대 1.29 → 193x193: 양쪽 다 잘라낸다
    "l_eye": (200, 150, 900.0),       # 배율 1: 세로 200 < 224 채움, 가로 150 < 192 채움
    "r_eye": (260, 230, 950.0),       # 축소 + 반전
    "l_cheek": (640, 560, 1200.0),    # 축소 0.75 → 420x480: 둘 다 채움
    "r_cheek": (500, 450, 800.0),     # 확대 1.125 → 506x563 + 반전
}


def make_crop(region, seed=0):
    """원본 해상도 RGB 크롭 + 피부 마스크(타원). 마스크 밖은 0 — common.face_regions.crop_polygon 결과와 같은 꼴."""
    h, w, face_h = CROP_SPECS[region]
    rng = np.random.default_rng([seed, REGIONS.index(region)])
    crop = rng.integers(16, 256, (h, w, 3), dtype=np.uint8)  # BLACK_LEVEL(15)보다 밝게 — 눈 가림 막대 판정과 무관
    mask = np.zeros((h, w), np.uint8)
    import cv2
    cv2.ellipse(mask, (w // 2 + 5, h // 2 - 3), (int(w * 0.45), int(h * 0.4)), 15, 0, 360, 1, -1)
    return crop * mask[..., None], mask, face_h


def serving_predictor(meta=V2_META):
    """ONNX 세션 없이 입력 계약만 가진 SkinPredictor (model_input·_check_input_contract는 세션을 쓰지 않는다)."""
    p = SkinPredictor.__new__(SkinPredictor)
    p.meta = meta
    p._check_input_contract(meta["input"])
    p.tasks = {t["task"]: t for t in meta["output"]["tasks"]}
    return p


@pytest.fixture(scope="module")
def crops_dir(tmp_path_factory):
    """부위별 합성 크롭을 학습 데이터 형식(RGBA PNG)으로 저장한 폴더."""
    root = tmp_path_factory.mktemp("crops")
    for region in REGIONS:
        crop, mask, _ = make_crop(region)
        save_region_png(root / f"{region}.png", crop, mask)
    return root


def rows_for(crops_dir, regions=REGIONS):
    """RegionDataset 행 (training.dataset.load_rows가 만드는 것과 같은 키)."""
    return [dict(subject="t/p1", person_id="p1", region=r, view="F", device="phone", file=str(crops_dir / f"{r}.png"),
                 face_h=CROP_SPECS[r][2], targets={}) for r in regions]


# ---- 같은 함수를 쓰는가 (재구현 금지) ----

def test_training_uses_common_preprocessing_functions_not_copies(monkeypatch):
    assert training_dataset.to_model_input is transforms.to_model_input
    assert training_dataset.load_region_png is load_region_png
    # 서빙도 같은 함수를 부르고, mirror_right는 meta.json 값을 넘긴다
    calls = []
    monkeypatch.setattr(transforms, "to_model_input", lambda *a, **k: calls.append((a, k)) or "sentinel")
    p = serving_predictor()
    assert p.model_input("crop", "mask", 900.0, "l_eye") == "sentinel"
    assert calls == [(("crop", "mask", 900.0, "l_eye"), {"mirror_right": V2_META["input"]["mirror_right"]})]


def test_v2_meta_input_contract_equals_code_and_train_config():
    """서빙 모델 meta.json의 입력 규격 = common 코드 상수 = 학습 설정 기본값."""
    cfg = TrainConfig()
    spec = V2_META["input"]
    assert spec["canvas"] == {r: list(c) for r, c in face_regions.CANVAS.items()}
    assert spec["target_face_height"] == face_regions.TARGET_FACE_HEIGHT
    assert np.allclose(spec["pixel_mean"], transforms.PIXEL_MEAN) and np.allclose(spec["pixel_std"], transforms.PIXEL_STD)
    assert spec["mirror_right"] is cfg.mirror_right and spec["mask_fill"] == 0
    # 입력 품질 기준도 서빙 기본값(serving/settings.py)과 같다
    from serving.settings import load_settings

    serving = load_settings()  # conftest가 Kafka·모델 없는 환경변수를 넣는다
    assert serving.min_skin_px == cfg.min_skin_px == 20_000
    assert serving.min_pixels == load_yaml("configs/data.yaml")["min_pixels"] == 1_000_000


def test_tasks_yaml_matches_v2_output_spec():
    """configs/tasks.yaml(학습 출력 규격)이 운영 모델 meta.json의 output.tasks와 열·클래스 순서·등급 구간·log까지 같다."""
    _, tasks = load_tasks()
    check_tasks_match(V2_META["output"]["tasks"], tasks, what="models/skin_v2/meta.json")
    layout = output_layout(tasks)
    for t in V2_META["output"]["tasks"]:
        assert [layout[t["task"]].start, layout[t["task"]].stop] == t["columns"]


# ---- 크롭 → 모델 입력 ----

def test_png_roundtrip_preserves_crop_and_mask(crops_dir):
    for region in REGIONS:
        crop, mask, _ = make_crop(region)
        loaded_crop, loaded_mask = load_region_png(crops_dir / f"{region}.png")
        assert np.array_equal(loaded_crop, crop) and np.array_equal(loaded_mask, mask)
        assert loaded_crop.dtype == np.uint8 and set(np.unique(loaded_mask)) <= {0, 1}


@pytest.mark.parametrize("region", REGIONS)
def test_dataset_item_equals_serving_model_input(crops_dir, region):
    """학습 Dataset(증강 없음)이 만든 텐서 == 서빙 SkinPredictor.model_input (같은 PNG·face_h·부위). 비트 단위로 같다."""
    _, tasks = load_tasks()
    row = rows_for(crops_dir, [region])[0]
    ds = RegionDataset([row], {}, TrainConfig(), tasks, train=False)
    image, targets, valid, index = ds[0]
    served = serving_predictor().model_input(*load_region_png(row["file"]), row["face_h"], region)
    assert image.dtype == torch.float32 and index == 0 and valid.sum() == 0
    w, h = face_regions.CANVAS[region]
    assert tuple(image.shape) == (3, h, w) == served.shape
    assert np.array_equal(image.numpy(), served)
    # r_ 부위는 좌우 반전됐다: 반전 없이 만든 입력과 다르고, 가로를 뒤집으면 가까워진다 (캔버스 정렬 오프셋 때문에 완전 동일은 아님)
    unmirrored = transforms.to_model_input(*load_region_png(row["file"]), row["face_h"], region, mirror_right=False)
    if region.startswith("r_"):
        assert not np.array_equal(served, unmirrored)
    else:
        assert np.array_equal(served, unmirrored)
    canvas_mask = served.any(axis=0)  # 다각형 밖은 0 (정규화 뒤 마스크 곱)
    assert canvas_mask.any() and not canvas_mask.all()  # 피부가 있고, 다각형 밖(0)도 있다


def test_augmentation_disabled_equals_serving_and_enabled_differs(crops_dir):
    """증강 값이 0이면 학습 입력 == 서빙 입력(회전 0·배율 1 warpAffine는 항등). 기본 증강은 입력을 바꾸되 모양·마스크 규칙은 유지."""
    _, tasks = load_tasks()
    rows = rows_for(crops_dir)
    off = TrainConfig(aug_rotate=0.0, aug_scale=0.0, aug_bright=0.0)
    on = TrainConfig()
    served = serving_predictor()
    for i, row in enumerate(rows):
        expected = served.model_input(*load_region_png(row["file"]), row["face_h"], row["region"])
        assert np.array_equal(RegionDataset(rows, {}, off, tasks, train=True)[(3, i)][0].numpy(), expected)
        augmented = RegionDataset(rows, {}, on, tasks, train=True)[(3, i)][0].numpy()
        assert augmented.shape == expected.shape and not np.array_equal(augmented, expected)
        # 같은 (seed, 에폭, 행)이면 같은 증강, 에폭이 다르면 다른 증강 (재개 재현성)
        assert np.array_equal(RegionDataset(rows, {}, on, tasks, train=True)[(3, i)][0].numpy(), augmented)
        assert not np.array_equal(RegionDataset(rows, {}, on, tasks, train=True)[(4, i)][0].numpy(), augmented)


# ---- 타깃 정변환(학습) ↔ 역변환(서빙) ----

def test_target_encode_matches_serving_decode_and_finalize():
    _, tasks = load_tasks()
    stats = regression_stats(tasks, {"pore_count": [100, 777, 2000], "wrinkle_ra": [10, 21.9, 30], "age": [20, 33.3, 60]})
    meta_list = meta_tasks(tasks, stats)
    layout = output_layout(tasks)
    width = sum(s.stop - s.start for s in layout.values())
    assert width == 16 == V2_META["output"]["tasks"][-1]["columns"][1]

    # 회귀: 원래 단위 → 학습 척도 → (서빙) 원래 단위. log(pore_count)·비log(age) 둘 다
    originals = {"pore_count": 777.0, "wrinkle_ra": 21.9, "age": 33.3}
    logits = np.zeros(width)
    for task, value in originals.items():
        logits[layout[task]] = encode_target(tasks[task], value, stats[task])
    for region in REGIONS:
        decoded = serving_tasks.decode(logits, meta_list, region)
        assert set(decoded) == {t for t, s in tasks.items() if region in s["regions"]}  # 그 부위 항목만 (load_rows와 같은 규칙)
        for task, value in originals.items():
            if task in decoded:
                assert decoded[task] == pytest.approx(value, abs=1e-6)

    # 순서형: 원래 등급 → 클래스 번호(학습) ↔ CORAL 로짓 → 등급 이름(서빙)
    for task, spec in tasks.items():
        if spec["kind"] != "ordinal":
            continue
        region = spec["regions"][0]
        for k, (name, grades) in enumerate(spec["classes"].items()):
            for grade in grades:
                assert encode_target(spec, grade) == float(grade_to_class(spec, grade)) == k
            levels = np.where(np.arange(len(spec["classes"]) - 1) < k, 5.0, -5.0)  # "등급 > j" 로짓: j < k 만 참
            logits = np.zeros(width)
            logits[layout[task]] = levels
            probs = serving_tasks.decode(logits, meta_list, region)[task]
            final = serving_tasks.finalize(meta_list[list(tasks).index(task)], probs)
            assert final["grade"] == name and final["expected"] == pytest.approx(k, abs=0.05)
        with pytest.raises(ValueError):
            grade_to_class(spec, 99)


def test_dataset_targets_use_encode_target(crops_dir):
    """Dataset이 내는 정답 텐서 = encode_target (항목 순서 = tasks.yaml 순서), 정답 없는 칸은 valid 0."""
    _, tasks = load_tasks()
    stats = regression_stats(tasks, {"pore_count": [100, 500], "wrinkle_ra": [10, 30], "age": [20, 60]})
    row = dict(rows_for(crops_dir, ["l_cheek"])[0], targets={"pore_count": 500.0, "pore_grade": 4, "age": 40.0})
    _, targets, valid, _ = RegionDataset([row], stats, TrainConfig(), tasks, train=False)[0]
    for t, (task, spec) in enumerate(tasks.items()):
        if task in row["targets"]:
            assert valid[t] == 1 and targets[t].item() == pytest.approx(encode_target(spec, row["targets"][task], stats.get(task)))
        else:
            assert valid[t] == 0 and targets[t] == 0
    assert Path(row["file"]).is_file()
