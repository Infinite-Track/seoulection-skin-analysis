"""사진 3장(정면·좌·우) → 얼굴 전체 6개 항목. Kafka·S3와 무관한 핵심 로직.

흐름: 사진별 mediapipe 부위 분할 (정면 6 + 좌 4 + 우 4 = 최대 14크롭)
     → 크롭별 모델 출력 → 부위별로 사진 평균 → 얼굴 전체로 부위 평균 → 항목별 최종 값

- 정면 사진은 필수다 (이마·미간은 정면에서만 제대로 보인다). 측면은 얼굴을 못 찾으면 빼고 계속한다.
- 등급 항목은 확률을 평균한 뒤 등급을 정한다 (skin v2 평가와 같은 규칙).

로컬 확인: python -m serving.analyzer 정면.jpg 좌.jpg 우.jpg
"""
import argparse
import json
from collections import defaultdict

import numpy as np

from common import face_regions as fr
from common.config import load_yaml
from common.predictor import SkinPredictor
from common.tasks import finalize

VIEWS = {"front": "F", "left": "L", "right": "R"}  # 메시지 키 → 학습 데이터의 각도 코드


class AnalysisError(Exception):
    """사용자에게 다시 찍어 달라고 할 수 있는 실패. code는 스킨서버가 분기할 값."""

    def __init__(self, code, view=None):
        super().__init__(f"{code} ({view})" if view else code)
        self.code, self.view = code, view


class SkinAnalyzer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.predictor = SkinPredictor(cfg["model_dir"])
        self.landmarker = fr.create_landmarker(cfg["mediapipe_models"])
        self.segmenter = fr.create_segmenter(cfg["mediapipe_models"])

    @property
    def model_version(self):
        m = self.predictor.meta
        return f"{m['model']['name']} {m['version']}"

    def analyze(self, images):
        """images: {"front"|"left"|"right": BGR 배열 또는 None} → (결과 {항목: 값}, 쓴 사진 목록)"""
        inputs = defaultdict(list)  # 부위 → 모델 입력들
        used = []
        for key, view in VIEWS.items():
            bgr = images.get(key)
            if bgr is None:
                raise AnalysisError("image_unreadable", key)
            if bgr.shape[0] * bgr.shape[1] < self.cfg["min_pixels"]:
                raise AnalysisError("low_resolution", key)
            face_h, regions = fr.extract_regions(bgr, view, self.landmarker, self.segmenter)
            if face_h is None:
                if key == "front":
                    raise AnalysisError("no_face", key)
                continue  # 측면은 없어도 정면으로 분석할 수 있다
            used.append(key)
            for region, result in regions.items():
                if isinstance(result, tuple) and result[1].sum() >= self.cfg["min_skin_px"]:
                    inputs[region].append(self.predictor.model_input(*result, face_h, region))

        # 부위별: 여러 사진의 출력 평균 (등급은 확률 평균)
        per_region = {region: self.predictor.aggregate(self.predictor.predict_regions(region, xs), final=False)
                      for region, xs in inputs.items()}
        return self.face_level(per_region), used

    def face_level(self, per_region):
        """부위별 값 → 얼굴 전체 값. 지금 규칙: 항목이 있는 부위들의 단순 평균.

        팀 논의로 바뀔 수 있는 부분 (예: 주름은 가장 심한 부위, 모공 개수는 양 볼 합).
        """
        result = {}
        for task, meta_task in self.predictor.tasks.items():
            values = [outs[task] for outs in per_region.values() if task in outs]
            if not values:
                result[task] = None  # 해당 부위 크롭을 하나도 못 얻음
                continue
            final = finalize(meta_task, np.mean(values, axis=0))
            result[task] = round(final, 2) if isinstance(final, float) else \
                dict(grade=final["grade"], expected=round(final["expected"], 2))
        return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("front")
    ap.add_argument("left")
    ap.add_argument("right")
    ap.add_argument("--config", default="serving")
    args = ap.parse_args()
    analyzer = SkinAnalyzer(load_yaml(args.config))
    images = {key: fr.read_image(getattr(args, key)) for key in VIEWS}
    result, used = analyzer.analyze(images)
    print(json.dumps(dict(model_version=analyzer.model_version, used_views=used, results=result),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
