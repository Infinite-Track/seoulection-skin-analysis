"""training/export_onnx — 내보낸 결과가 models/skin_v2/와 같은 구성이고, reference.npz가 허용 오차 안에서 재현되는지 (Phase 5).

작은 백본(convnext_atto, 사전학습 없음)으로 train.py 형식의 model.pt를 만들어 export → 검사. 운영 v2 ONNX(git 밖, 로컬에만)가
있으면 그것도 같은 규칙(check_onnx.py = pipelines/steps/register.verify_model_dir)으로 reference.npz를 재현해 실측값을 남긴다.
"""
import hashlib
import json
import runpy
import shutil

import numpy as np
import pytest

from common import face_regions
from common.config import REPO_ROOT
from common.model_files import REQUIRED, ensure_model_files
from common.predictor import SkinPredictor
from common.training_targets import check_tasks_match, regression_stats

torch = pytest.importorskip("torch")
pytest.importorskip("timm")
pytest.importorskip("onnxscript")  # torch.onnx.export(dynamo=True)
ort = pytest.importorskip("onnxruntime")

from pipelines.steps import register  # noqa: E402
from training.config import TrainConfig, config_to_dict, load_tasks  # noqa: E402
from training.export_onnx import HASHED_FILES, ONNX_TOLERANCE, export, load_model, write_checksums  # noqa: E402
from training.model import SkinModel  # noqa: E402

V2_DIR = REPO_ROOT / "models/skin_v2"
V2_META = json.loads((V2_DIR / "meta.json").read_text(encoding="utf-8"))
V2_FILES = {"skin.onnx", "skin.onnx.data", "meta.json", "reference.npz", "check_onnx.py", "SHA256SUMS.txt"}  # + metrics.json(evaluate가 만든다)
TINY = "convnext_atto"


def reference_diff(model_dir):
    """check_onnx.py와 같은 규칙: 하나의 rng(seed)에서 부위 순서대로 입력을 뽑아 reference.npz와 비교 → 최대 차이."""
    model_dir = REPO_ROOT / model_dir
    reference = np.load(model_dir / "reference.npz")
    meta = json.loads((model_dir / "meta.json").read_text(encoding="utf-8"))
    session = ort.InferenceSession(str(model_dir / "skin.onnx"), providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(int(reference["seed"]))
    worst = 0.0
    for region in reference["regions"]:
        width, height = meta["input"]["canvas"][str(region)]
        x = rng.standard_normal((1, 3, height, width), dtype=np.float32)
        worst = max(worst, float(np.abs(session.run(None, {"crop": x})[0] - reference[str(region)]).max()))
    return worst


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    """작은 체크포인트(train.py가 저장하는 형식) → export → (체크포인트 경로, 내보낸 폴더, 체크포인트 dict)."""
    _, tasks = load_tasks()
    torch.manual_seed(2026)
    net = SkinModel(tasks, backbone=TINY, pretrained=False, freeze_stages=0, head_hidden=16,
                    threshold_init={"pore_grade": [1.0, 0.0, -1.0]})
    stats = regression_stats(tasks, {"pore_count": [100, 500, 2000], "wrinkle_ra": [10, 20, 30], "age": [20, 40, 60]})
    cfg = TrainConfig(backbone=TINY, head_hidden=16, freeze_stages=0, model_name="skin", reference_seed=2026)
    root = tmp_path_factory.mktemp("export")
    checkpoint = root / "model.pt"
    saved = {"model": net.state_dict(), "stats": stats, "l0": {t: 1.0 for t in tasks}, "cfg": config_to_dict(cfg), "tasks": tasks,
             "people": 3, "crops": 10, "datasets": ["tiny"], "parent": None, "history": [], "created": "2026-11-01T00:00:00"}
    torch.save(saved, checkpoint)
    out = export(checkpoint, root / "out", name="skin", version="2026-11-01")
    return checkpoint, out, saved


def test_export_file_set_and_checksums_match_models_skin_v2(exported):
    _, out, _ = exported
    assert {p.name for p in out.iterdir()} == V2_FILES
    assert all((out / f).stat().st_size > 0 for f in V2_FILES)
    # SHA256SUMS.txt: v2와 같은 파일 순서, 해시가 실제 파일과 일치
    lines = (out / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    assert [l.split()[1] for l in lines] == list(HASHED_FILES)
    assert [l.split()[1] for l in (V2_DIR / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()] == list(HASHED_FILES)
    for expected, name in (l.split() for l in lines):
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == expected
    # check_onnx.py는 v2 폴더의 것과 같은 파일
    assert hashlib.sha256((out / "check_onnx.py").read_bytes()).hexdigest() == hashlib.sha256((V2_DIR / "check_onnx.py").read_bytes()).hexdigest()


def test_meta_json_input_contract_and_output_spec_match_v2(exported):
    _, out, saved = exported
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["input"] == V2_META["input"]  # 서빙 입력 계약 완전 동일 (캔버스·배율·정규화 상수·반전·mask_fill)
    assert meta["version"] == "2026-11-01" and meta["model"]["name"] == "skin" and meta["model"]["backbone"] == TINY
    assert meta["model"]["parent"] is None and "3명" in meta["model"]["trained_on"]
    assert {k for k in meta["output"]} == {k for k in V2_META["output"]}
    _, tasks = load_tasks()
    check_tasks_match(meta["output"]["tasks"], tasks, what="exported meta.json")  # 열·클래스 순서·등급 구간·log
    for mine, v2 in zip(meta["output"]["tasks"], V2_META["output"]["tasks"]):
        assert {k: v for k, v in mine.items() if k not in ("mean", "std")} == {k: v for k, v in v2.items() if k not in ("mean", "std")}
        if mine["kind"] == "regression":
            assert (mine["mean"], mine["std"]) == tuple(saved["stats"][mine["task"]])
    # float32 상수가 0.48500001430511475 식으로 찍히지 않는다
    assert meta["input"]["pixel_mean"] == [0.485, 0.456, 0.406] and meta["input"]["pixel_std"] == [0.229, 0.224, 0.225]


def test_reference_npz_reproduces_within_tolerance(exported):
    """reference.npz = 내보낸 ONNX의 고정 seed 출력. check_onnx.py 규칙으로 다시 뽑으면 허용 오차(1e-3) 안 — 실측값을 출력한다."""
    _, out, _ = exported
    reference = np.load(out / "reference.npz")
    assert int(reference["seed"]) == TrainConfig().reference_seed == 2026
    assert list(reference["regions"]) == list(face_regions.CANVAS)
    assert all(reference[r].shape == (1, 16) and reference[r].dtype == np.float32 for r in face_regions.CANVAS)
    worst = reference_diff(out)
    print(f"\n내보낸 모델 reference.npz 재현 최대 차이 {worst:.2e}")
    assert worst < ONNX_TOLERANCE
    # Register 단계의 산출물 검증도 같은 규칙으로 통과한다 (해시 + reference)
    meta, worst_register = register.verify_model_dir(out)
    assert meta["model"]["name"] == "skin" and worst_register == worst


def test_pytorch_checkpoint_matches_exported_reference(exported):
    """체크포인트의 PyTorch 모델이 같은 입력에 reference.npz와 허용 오차 안의 출력을 낸다 (ONNX 변환이 모델을 바꾸지 않았다)."""
    checkpoint, out, saved = exported
    net, loaded, cfg = load_model(checkpoint)
    assert cfg.backbone == TINY and net.hparams["freeze_stages"] == 0 and loaded["stats"] == saved["stats"]
    reference = np.load(out / "reference.npz")
    rng = np.random.default_rng(int(reference["seed"]))
    worst = 0.0
    with torch.no_grad():
        for region, (w, h) in face_regions.CANVAS.items():
            x = rng.standard_normal((1, 3, h, w), dtype=np.float32)
            worst = max(worst, float(np.abs(net(torch.from_numpy(x)).numpy() - reference[region]).max()))
    print(f"\nPyTorch vs reference.npz 최대 차이 {worst:.2e}")
    assert worst < ONNX_TOLERANCE


def test_exported_check_onnx_script_passes(exported, capsys):
    _, out, _ = exported
    runpy.run_path(str(out / "check_onnx.py"), run_name="__main__")
    text = capsys.readouterr().out
    assert "통과: 최대 차이" in text and "확인 필요" not in text and "불일치" not in text
    assert [l.split()[-1] for l in text.splitlines() if l.startswith("   OK ")] == list(HASHED_FILES)  # 해시 5/5 OK
    assert "모델 로드 OK" in text


def test_serving_loads_exported_model(exported):
    """서빙 로더(common/model_files)와 추론기(common/predictor)가 내보낸 폴더를 그대로 쓴다."""
    _, out, _ = exported
    assert ensure_model_files(out) == out and all((out / f).is_file() for f in REQUIRED)
    model = SkinPredictor(out)  # 입력 계약 assert 포함
    assert model.mirror_right is True and set(model.tasks) == {t["task"] for t in V2_META["output"]["tasks"]}
    rng = np.random.default_rng(0)
    region = "l_cheek"
    w, h = face_regions.CANVAS[region]
    outputs = model.predict_regions(region, [rng.standard_normal((3, h, w), dtype=np.float32) for _ in range(3)], batch_size=2)
    assert len(outputs) == 3 and set(outputs[0]) == {"pore_count", "pore_grade", "pigment_grade", "age"}
    final = model.aggregate(outputs)
    assert isinstance(final["age"], float) and final["pore_grade"]["grade"] in model.tasks["pore_grade"]["classes"]
    assert final["pore_count"] >= 0  # expm1 역변환 — 개수는 음수가 될 수 없다


def test_tampering_is_detected_and_rename_keeps_verification(exported, tmp_path):
    _, out, _ = exported
    # Register의 이름 변경(skin → skin v7)은 meta.json·SHA256SUMS만 바꾸고 검증은 계속 통과
    renamed = shutil.copytree(out, tmp_path / "renamed")
    assert register.rename_model(renamed, "{name} v{n}", 7) == "skin v7"
    meta, _ = register.verify_model_dir(renamed)
    assert meta["model"]["name"] == "skin v7"
    sums = {l.split()[1]: l.split()[0] for l in (renamed / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()}
    original = {l.split()[1]: l.split()[0] for l in (out / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()}
    assert sums["meta.json"] != original["meta.json"] and {k: v for k, v in sums.items() if k != "meta.json"} == {k: v for k, v in original.items() if k != "meta.json"}
    # 가중치 1바이트 변조 → 해시 불일치
    damaged = shutil.copytree(out, tmp_path / "damaged")
    data = bytearray((damaged / "skin.onnx.data").read_bytes())
    data[len(data) // 2] ^= 0xFF
    (damaged / "skin.onnx.data").write_bytes(bytes(data))
    with pytest.raises(RuntimeError, match="해시 불일치"):
        register.verify_model_dir(damaged)
    # 해시는 맞지만 reference가 다른 모델의 것 → 출력 불일치
    swapped = shutil.copytree(out, tmp_path / "swapped")
    ref = dict(np.load(swapped / "reference.npz"))
    for region in face_regions.CANVAS:
        ref[region] = ref[region] + 0.01
    np.savez(swapped / "reference.npz", **ref)
    write_checksums(swapped)
    with pytest.raises(RuntimeError, match="reference.npz"):
        register.verify_model_dir(swapped)


@pytest.mark.skipif(not (V2_DIR / "skin.onnx.data").is_file(), reason="운영 v2 가중치(skin.onnx.data)는 git 밖 — 로컬에만")
def test_production_skin_v2_reproduces_its_reference():
    """운영 모델 폴더 자체도 같은 규칙으로 검증된다 (SHA256SUMS + reference.npz). 실측값을 출력한다."""
    meta, worst = register.verify_model_dir(V2_DIR)
    print(f"\nmodels/skin_v2 reference.npz 재현 최대 차이 {worst:.2e}")
    assert meta["model"]["name"] == "skin v2" and worst < ONNX_TOLERANCE
