"""사진 3장(정면·좌·우) → 부위 6개별 항목 값. Kafka·S3·HTTP와 무관한 핵심 로직.

흐름: 사진별 mediapipe 부위 분할 (정면 6 + 좌 4 + 우 4 = 최대 14크롭)
     → 크롭별 모델 출력 → 같은 부위의 사진들을 평균 → 부위별 최종 값
     (점수 환산은 serving/scoring.py)

- 정면 사진은 필수다 (이마·미간은 정면에서만 제대로 보인다). 측면은 얼굴을 못 찾으면 빼고 계속한다.
- 등급 항목은 확률을 평균한 뒤 등급을 정한다 (skin v2 평가와 같은 규칙).
- mediapipe는 스레드 안전하지 않아서 분석은 한 번에 하나씩 돈다 (lock). 처리량은 컨테이너 수로 늘린다.

출력 per_region: {부위: {항목: float | {"grade": str, "expected": float, "classes": int}}}
  부위 forehead, glabella, l_eye, r_eye, l_cheek, r_cheek — 크롭을 못 얻은 부위는 빠진다.
"""
import threading
from collections import defaultdict

from common import face_regions as fr
from common.predictor import SkinPredictor

VIEWS = {"front": "F", "left": "L", "right": "R"}  # 메시지 키 → 학습 데이터의 각도 코드

# completed 메시지의 failureReason 값
NO_FACE, LOW_QUALITY, INVALID_REQUEST, MODEL_ERROR = "NO_FACE", "LOW_QUALITY", "INVALID_REQUEST", "MODEL_ERROR"


class AnalysisError(Exception):
    """분석할 수 없는 요청. reason은 failureReason 값, detail은 failureDetail(디버깅용)."""

    def __init__(self, reason, detail):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


class SkinAnalyzer:
    needs_images = True  # False인 분석기(더미)는 이미지를 받지 않는다

    def __init__(self, model_dir, mediapipe_models_dir, min_pixels, min_skin_px):
        self.min_pixels, self.min_skin_px = min_pixels, min_skin_px
        self.predictor = SkinPredictor(model_dir)
        self.landmarker = fr.create_landmarker(mediapipe_models_dir)
        self.segmenter = fr.create_segmenter(mediapipe_models_dir)
        self._lock = threading.Lock()

    @property
    def model_version(self):
        m = self.predictor.meta
        return f"{m['model']['name']} {m['version']}"

    def analyze(self, images):
        """images: {"front"|"left"|"right": BGR 배열 또는 None} → (per_region, 쓴 사진 목록)"""
        with self._lock:
            return self._analyze(images)

    def _analyze(self, images):
        inputs = defaultdict(list)  # 부위 → 모델 입력들
        used = []
        for key, view in VIEWS.items():
            bgr = images.get(key)
            if bgr is None:
                raise AnalysisError(LOW_QUALITY, f"{key}: 이미지를 읽을 수 없음")
            if bgr.shape[0] * bgr.shape[1] < self.min_pixels:
                raise AnalysisError(LOW_QUALITY, f"{key}: 해상도 {bgr.shape[1]}x{bgr.shape[0]} < {self.min_pixels}px")
            face_h, regions = fr.extract_regions(bgr, view, self.landmarker, self.segmenter)
            if face_h is None:
                if key == "front":
                    raise AnalysisError(NO_FACE, "front: 얼굴을 찾지 못함")
                continue  # 측면은 없어도 정면으로 분석할 수 있다
            used.append(key)
            for region, result in regions.items():
                if isinstance(result, tuple) and result[1].sum() >= self.min_skin_px:
                    inputs[region].append(self.predictor.model_input(*result, face_h, region))

        per_region = {}
        for region, xs in inputs.items():
            merged = self.predictor.aggregate(self.predictor.predict_regions(region, xs))  # 사진 평균 → 최종 값
            per_region[region] = {
                task: value if isinstance(value, float)
                else {**value, "classes": len(self.predictor.tasks[task]["classes"])}
                for task, value in merged.items()
            }
        return per_region, used
