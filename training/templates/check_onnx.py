"""맥(또는 다른 PC)에서 v2 배포 모델이 PC와 같은 결과를 내는지 확인한다.

사용: 압축을 푼 폴더에서
    pip install onnxruntime numpy
    python check_onnx.py
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import onnxruntime as ort

HERE = Path(__file__).parent

# 1) 전송 중 파일이 깨지지 않았는지 (PC에서 잰 해시와 비교)
print("1) 파일 해시")
for line in (HERE / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
    expected, name = line.split()
    actual = hashlib.sha256((HERE / name).read_bytes()).hexdigest()
    print(f"   {'OK ' if actual == expected else '불일치'} {name}")
    assert actual == expected, f"{name}이 손상됐다. 다시 받을 것"

# 2) 모델 로드 (skin.onnx와 skin.onnx.data가 같은 폴더에 있어야 한다)
session = ort.InferenceSession(str(HERE / "skin.onnx"), providers=["CPUExecutionProvider"])
print(f"2) 모델 로드 OK (onnxruntime {ort.__version__})")

# 3) PC와 같은 입력(고정 seed)을 넣어 같은 출력이 나오는지
reference = np.load(HERE / "reference.npz")
meta = json.loads((HERE / "meta.json").read_text(encoding="utf-8"))
rng = np.random.default_rng(int(reference["seed"]))
print("3) PC 출력과 비교")
worst = 0.0
for region in reference["regions"]:
    width, height = meta["input"]["canvas"][str(region)]
    x = rng.standard_normal((1, 3, height, width), dtype=np.float32)
    output = session.run(None, {"crop": x})[0]
    diff = float(np.abs(output - reference[str(region)]).max())
    worst = max(worst, diff)
    print(f"   {str(region):9s} {width}x{height}  최대 차이 {diff:.2e}")
print(f"\n{'통과' if worst < 1e-3 else '확인 필요'}: 최대 차이 {worst:.2e} (기준 1e-3 미만이면 같은 모델로 본다)")
