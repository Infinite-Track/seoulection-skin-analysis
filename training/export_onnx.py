"""체크포인트(model.pt) → 배포 폴더. 파일 구성은 models/skin_v2/와 같다:

  skin.onnx, skin.onnx.data   연산 그래프 + 가중치 (서빙 필수)
  meta.json                   입력 규격 + 출력 해석법 (서빙 필수. common/predictor.py가 읽는다)
  reference.npz               고정 seed 난수 입력의 출력 — 다른 머신에서 같은 모델인지 확인용
  check_onnx.py               위 셋을 검사하는 스크립트 (models/skin_v2/check_onnx.py와 같은 파일)
  SHA256SUMS.txt              위 5개 파일의 해시
(metrics.json은 training/evaluate.py가 만든다)

내보낸 뒤 부위별 입력 크기 전부에 대해 ONNX와 PyTorch 출력이 같은지 확인한다.

사용: python -m training.export_onnx --checkpoint outputs/models/<run>/model.pt [--out DIR] [--name "skin"] [--version 2026-11-01]
"""
import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import onnxruntime as ort
import torch

from common import face_regions, transforms
from common.model_files import REQUIRED
from common.training_targets import meta_tasks
from training.config import TrainConfig, config_from_dict
from training.model import SkinModel

TEMPLATE_DIR = Path(__file__).parent / "templates"
CHECK_SCRIPT = "check_onnx.py"
HASHED_FILES = ("skin.onnx", "skin.onnx.data", "meta.json", CHECK_SCRIPT, "reference.npz")  # v2 SHA256SUMS와 같은 순서
ONNX_TOLERANCE = 1e-3  # check_onnx.py와 같은 기준


def load_model(checkpoint):
    saved = torch.load(checkpoint, map_location="cpu", weights_only=False)
    cfg = config_from_dict(saved["cfg"])
    net = SkinModel.from_config(cfg, saved["tasks"], pretrained=False, freeze_stages=0)
    net.load_state_dict(saved["model"])
    net.eval()
    return net, saved, cfg


def export(checkpoint, out_dir, name=None, version=None, opset=None, reference_seed=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    net, saved, cfg = load_model(checkpoint)
    defaults = TrainConfig()
    name = name or cfg.model_name
    version = version or datetime.now().strftime("%Y-%m-%d")
    opset = opset or cfg.onnx_opset or defaults.onnx_opset
    reference_seed = reference_seed if reference_seed is not None else cfg.reference_seed

    # 1) ONNX. 부위마다 입력 크기가 달라 세로·가로를 동적으로 둔다. 더미 입력은 가장 큰 캔버스(볼)
    onnx_path = out_dir / "skin.onnx"
    width, height = face_regions.CANVAS["l_cheek"]
    torch.onnx.export(net, torch.randn(1, 3, height, width), str(onnx_path), input_names=["crop"],
                      output_names=["logits"],
                      dynamic_axes={"crop": {0: "batch", 2: "height", 3: "width"}, "logits": {0: "batch"}},
                      opset_version=opset, dynamo=True, external_data=True)
    missing = [f for f in REQUIRED if f != "meta.json" and not (out_dir / f).exists()]
    if missing:
        raise RuntimeError(f"ONNX 내보내기 결과에 {missing}가 없다")

    # 2) 부위별 크기 전부에서 ONNX vs PyTorch
    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(0)
    worst = 0.0
    for w, h in face_regions.CANVAS.values():
        x = rng.standard_normal((2, 3, h, w), dtype=np.float32)
        with torch.no_grad():
            expected = net(torch.from_numpy(x)).numpy()
        worst = max(worst, float(np.abs(session.run(None, {"crop": x})[0] - expected).max()))
    if worst >= ONNX_TOLERANCE:
        raise RuntimeError(f"ONNX 출력이 PyTorch와 다르다 (최대 차이 {worst:.2e})")

    # 3) meta.json (skin v2와 같은 형식)
    meta = {
        "version": version,
        "model": {"name": name, "backbone": cfg.backbone, "params": net.num_params(),
                  "trained_on": f"{saved.get('people')}명 {saved.get('datasets', [])}, 각도 {cfg.views}, {cfg.epochs}에폭",
                  "parent": saved.get("parent")},
        "input": {
            "name": "crop", "shape": "(배치, 3, 세로, 가로) float32",
            "canvas": {region: list(size) for region, size in face_regions.CANVAS.items()},  # (가로, 세로)
            "target_face_height": face_regions.TARGET_FACE_HEIGHT,
            "mirror_right": cfg.mirror_right,
            # float32 상수를 그대로 쓰면 0.48500001430511475처럼 찍힌다 → v2와 같은 6자리 십진수로
            "pixel_mean": np.round(transforms.PIXEL_MEAN.astype(np.float64), 6).tolist(),
            "pixel_std": np.round(transforms.PIXEL_STD.astype(np.float64), 6).tolist(),
            "mask_fill": 0,
        },
        "output": {
            "name": "logits",
            "regression": "값 = 로짓 * std + mean, log가 true면 다시 exp(값) - 1",
            "ordinal": "로짓 k개 → 시그모이드 → 0.5를 넘는 개수 = 등급 번호(classes의 순서), 확률의 합 = 기대 등급(연속값)",
            "aggregate": "같은 부위의 여러 사진은 회귀 값·등급 확률을 평균한 뒤 등급을 정한다",
            "tasks": meta_tasks(saved["tasks"], saved["stats"]),
        },
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4) reference.npz — check_onnx.py와 같은 규칙: 하나의 rng에서 부위 순서대로 (1,3,H,W) 연속 추출
    reference = {"seed": reference_seed, "regions": np.array(list(face_regions.CANVAS))}
    rng = np.random.default_rng(reference_seed)
    for region, (w, h) in face_regions.CANVAS.items():
        x = rng.standard_normal((1, 3, h, w), dtype=np.float32)
        reference[region] = session.run(None, {"crop": x})[0]
    np.savez(out_dir / "reference.npz", **reference)

    # 5) check_onnx.py + SHA256SUMS.txt
    shutil.copyfile(TEMPLATE_DIR / CHECK_SCRIPT, out_dir / CHECK_SCRIPT)
    lines = [f"{hashlib.sha256((out_dir / f).read_bytes()).hexdigest()}  {f}" for f in HASHED_FILES]
    (out_dir / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    size = (out_dir / "skin.onnx.data").stat().st_size / 1e6
    print(f"내보내기 {out_dir} | {name} {version} | 가중치 {size:.0f} MB | ONNX vs PyTorch 최대 차이 {worst:.1e}", flush=True)
    return out_dir


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--checkpoint", required=True, help="model.pt")
    ap.add_argument("--out", help="기본: 체크포인트와 같은 폴더")
    ap.add_argument("--name", help="meta.json model.name (기본: 체크포인트 cfg의 model_name)")
    ap.add_argument("--version", help="meta.json version (기본: 오늘 날짜)")
    args = ap.parse_args(argv)
    export(args.checkpoint, args.out or Path(args.checkpoint).parent, name=args.name, version=args.version)


if __name__ == "__main__":
    main()
