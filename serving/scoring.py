"""부위별 측정값 → 점수(0~100, 높을수록 좋음)와 completed 메시지의 결과 필드.

모델은 점수를 내지 않는다. 아래 환산은 제품 결정(2026-10-15)이고, 원래 측정값은 metrics에 그대로 싣는다
(공식이 바뀌어도 받는 쪽이 metrics로 다시 계산할 수 있게).
  등급 항목(주름·모공·색소): 100 × (1 − 기대등급 / 최고등급)
  모공 개수·주름 Ra:         skin v2 학습 라벨의 1~99 백분위를 0~100으로 정규화한 뒤 뒤집기
  부위 점수 = 그 부위 항목 점수의 평균, 총점 = 부위 점수의 평균
  나이는 점수에 넣지 않고 skinAge(부위 평균)로 따로
"""
import numpy as np

# 모델 부위 이름 → 메시지 필드 (턱은 모델에 없다)
REGION_KEYS = {"forehead": "forehead", "glabella": "glabella", "l_eye": "leftEye", "r_eye": "rightEye",
               "l_cheek": "leftCheek", "r_cheek": "rightCheek"}
METRIC_KEYS = {"pore_count": "poreCount", "pore_grade": "poreGrade", "wrinkle_grade": "wrinkleGrade",
               "wrinkle_ra": "wrinkleRa", "pigment_grade": "pigmentGrade"}
# skin v2 학습 라벨(965명 × 좌우 = 1,930)의 1~99 백분위. 값이 클수록 나쁨
RANGE = {"pore_count": (94.0, 2113.0), "wrinkle_ra": (12.3, 37.8)}


def task_score(task, value):
    if isinstance(value, dict):  # 등급: expected는 0 ~ classes-1
        return 100.0 * (1.0 - value["expected"] / (value["classes"] - 1))
    lo, hi = RANGE[task]
    return 100.0 * (1.0 - float(np.clip((value - lo) / (hi - lo), 0.0, 1.0)))


def _metric(task, value):
    if isinstance(value, dict):
        return value["grade"]
    return round(value) if task == "pore_count" else round(value, 1)


def build_result(per_region):
    """per_region → {totalScore, regionScores, metrics, skinAge}. 크롭을 못 얻은 부위는 null."""
    region_scores, metrics, ages = {}, {}, []
    for region, key in REGION_KEYS.items():
        outs = per_region.get(region)
        if not outs:
            region_scores[key], metrics[key] = None, None
            continue
        scored = {t: v for t, v in outs.items() if t != "age"}
        region_scores[key] = round(float(np.mean([task_score(t, v) for t, v in scored.items()])))
        metrics[key] = {METRIC_KEYS[t]: _metric(t, v) for t, v in scored.items()}
        if "age" in outs:
            ages.append(outs["age"])
    present = [s for s in region_scores.values() if s is not None]
    return dict(totalScore=round(float(np.mean(present))) if present else None, regionScores=region_scores,
                metrics=metrics, skinAge=round(float(np.mean(ages)), 1) if ages else None)
