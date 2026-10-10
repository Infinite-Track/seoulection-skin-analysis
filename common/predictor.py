"""배포 모델(skin.onnx + meta.json)로 추론.

사용:
    model = SkinPredictor(model_dir)
    outputs = model.predict_regions(region, [model_input, ...])   # 크롭별 {항목: 값 또는 확률}
    result = model.aggregate(outputs)                             # 같은 부위 여러 사진 평균 → 최종 값
"""
import json

import numpy as np
import onnxruntime as ort

from common import face_regions, transforms
from common.config import resolve
from common.tasks import decode, finalize


class SkinPredictor:
    def __init__(self, model_dir, providers=("CPUExecutionProvider",)):
        model_dir = resolve(model_dir)
        self.meta = json.loads((model_dir / "meta.json").read_text(encoding="utf-8"))
        self._check_input_contract(self.meta["input"])
        self.tasks = {t["task"]: t for t in self.meta["output"]["tasks"]}
        self.session = ort.InferenceSession(str(model_dir / "skin.onnx"), providers=list(providers))

    @staticmethod
    def _check_input_contract(spec):
        """meta.json의 입력 규격이 이 코드의 전처리와 같은지. 다르면 다른 입력을 넣게 되므로 바로 멈춘다."""
        canvas = {r: list(c) for r, c in face_regions.CANVAS.items()}
        assert spec["canvas"] == canvas, f"meta.json canvas {spec['canvas']} != 코드 {canvas}"
        assert spec["target_face_height"] == face_regions.TARGET_FACE_HEIGHT, "meta.json target_face_height 불일치"
        assert np.allclose(spec["pixel_mean"], transforms.PIXEL_MEAN), "meta.json pixel_mean 불일치"
        assert np.allclose(spec["pixel_std"], transforms.PIXEL_STD), "meta.json pixel_std 불일치"

    @property
    def mirror_right(self):
        return self.meta["input"]["mirror_right"]

    def model_input(self, crop, mask, face_h, region):
        return transforms.to_model_input(crop, mask, face_h, region, mirror_right=self.mirror_right)

    def predict_regions(self, region, inputs, batch_size=8):
        """같은 부위의 모델 입력 여러 개 → 크롭별 {항목: 값(회귀) 또는 "등급 > k" 확률(순서형)}."""
        outputs = []
        for start in range(0, len(inputs), batch_size):
            batch = np.stack(inputs[start:start + batch_size]).astype(np.float32)
            logits = self.session.run(None, {self.meta["input"]["name"]: batch})[0]
            outputs += [decode(row, self.meta["output"]["tasks"], region) for row in logits]
        return outputs

    def aggregate(self, outputs, final=True):
        """같은 부위 여러 사진의 출력 → 항목별 평균 (등급은 확률을 평균한 뒤 정한다)."""
        merged = {}
        for task in outputs[0]:
            mean = np.mean([o[task] for o in outputs], axis=0)
            merged[task] = finalize(self.tasks[task], mean) if final else mean
        return merged
