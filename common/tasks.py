"""모델 출력(로짓) + meta.json → 원래 단위 값 / 등급 확률. numpy만 쓴다."""
import numpy as np


def decode(logits, meta_task_list, region):
    """크롭 하나의 출력 (D,) → {항목: 값}. 그 부위에 정의된 항목만 낸다 (다른 열은 학습된 적 없는 값).

    회귀: 원래 단위 값(float). 순서형: "등급 > k" 확률 (K-1,) — 여러 사진을 평균한 뒤 finalize로 등급을 정한다.
    """
    out = {}
    for t in meta_task_list:
        if region not in t["regions"]:
            continue
        z = np.asarray(logits[t["columns"][0]:t["columns"][1]], dtype=np.float64)
        if t["kind"] == "regression":
            value = z[0] * t["std"] + t["mean"]
            out[t["task"]] = float(np.expm1(value) if t["log"] else value)
        else:
            out[t["task"]] = 1 / (1 + np.exp(-z))
    return out


def finalize(meta_task, value):
    """평균한 값 → 최종 결과. 순서형은 {grade: 표시 이름, expected: 기대 등급(클래스 번호 척도)}."""
    if meta_task["kind"] == "regression":
        return float(value)
    probs = np.asarray(value)
    return dict(grade=meta_task["classes"][int((probs > 0.5).sum())], expected=float(probs.sum()))
