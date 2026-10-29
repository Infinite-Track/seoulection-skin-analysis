"""항목(task) 정의 → 학습 타깃·출력 열 배치·meta.json의 output.tasks. numpy만 쓴다.

학습 전용 모듈이다 (서빙은 import하지 않는다). 서빙의 역변환(common/tasks.py decode·finalize)과 짝이 맞아야 해서
같은 폴더에 둔다:
  회귀:   원래 단위 → (log1p) → (x - mean) / std      ↔ decode:   z * std + mean → (expm1)
  순서형: 원래 등급 → 합친 클래스 번호 k               ↔ finalize: (p > 0.5).sum() → classes[k]

tasks: configs/tasks.yaml의 tasks (training.config.load_tasks). dict 순서가 출력 열 순서다.
"""
import numpy as np


def output_layout(tasks):
    """항목 → 출력 열 범위(slice). 회귀는 1열, 순서형은 (클래스 수 - 1)열."""
    layout, start = {}, 0
    for task, spec in tasks.items():
        width = len(spec["classes"]) - 1 if spec["kind"] == "ordinal" else 1
        layout[task] = slice(start, start + width)
        start += width
    return layout


def output_width(tasks):
    return sum(s.stop - s.start for s in output_layout(tasks).values())


def grade_to_class(spec, grade):
    """원래 등급 → 합친 클래스 번호. 예: 모공 등급 5 → "≥4" → 3"""
    for index, grades in enumerate(spec["classes"].values()):
        if int(grade) in grades:
            return index
    raise ValueError(f"정의되지 않은 등급: {grade}")


def to_train_scale(spec, value):
    """회귀 정답을 표준화 전 학습 척도로 (치우친 값은 log1p)."""
    return float(np.log1p(value)) if spec.get("log") else float(value)


def regression_stats(tasks, values):
    """회귀 항목의 (mean, std). values: {항목: [원래 단위 값, ...]}.

    skin v2와 같은 규칙: 학습 **크롭 행** 단위로 모은 값(사진이 많은 사람이 더 반영됨, 테스트 인물 제외)을
    log 항목은 log1p 한 뒤 np.mean / np.std(ddof=0). std가 0이면 1.0.
    """
    stats = {}
    for task, spec in tasks.items():
        if spec["kind"] != "regression" or not values.get(task):
            continue
        scaled = np.array([to_train_scale(spec, v) for v in values[task]], dtype=np.float64)
        stats[task] = (float(scaled.mean()), float(scaled.std()) or 1.0)
    return stats


def encode_target(spec, value, stats=None):
    """정답 하나를 학습 척도로. 회귀는 (log →) 표준화, 순서형은 클래스 번호. stats = (mean, std)."""
    if spec["kind"] == "ordinal":
        return float(grade_to_class(spec, value))
    mean, std = stats
    return (to_train_scale(spec, value) - mean) / std


def meta_tasks(tasks, stats):
    """meta.json의 output.tasks. 서빙(common/predictor.py)은 이것만 보고 출력을 해석한다."""
    layout, out = output_layout(tasks), []
    for task, spec in tasks.items():
        cols = layout[task]
        entry = dict(task=task, kind=spec["kind"], columns=[cols.start, cols.stop], regions=list(spec["regions"]))
        if spec["kind"] == "regression":
            mean, std = stats[task]
            entry.update(mean=mean, std=std, log=bool(spec.get("log", False)))
        else:
            entry.update(classes=list(spec["classes"]), grades={k: list(v) for k, v in spec["classes"].items()})
        out.append(entry)
    return out


def task_signature(tasks):
    """출력 규격 비교용: 항목 순서·종류·부위·클래스 **순서**·등급 구간·log. 베이스 체크포인트와 같아야 파인튜닝할 수 있다.

    classes는 dict가 아니라 (이름, 등급 목록) 리스트로 넣는다 — dict 동등 비교는 순서를 무시해서
    클래스 순서를 뒤집어도 같다고 판정하기 때문이다 (순서가 바뀌면 같은 등급이 다른 클래스 번호가 된다).
    """
    return [(task, spec["kind"], list(spec["regions"]),
             [(str(k), [int(g) for g in v]) for k, v in spec.get("classes", {}).items()], bool(spec.get("log", False)))
            for task, spec in tasks.items()]


def meta_signature(meta_task_list):
    """meta.json의 output.tasks → task_signature와 같은 형식. 내보낸 모델과 평가·학습에 쓰는 tasks가 같은지 비교용."""
    out = []
    for t in meta_task_list:
        classes = [(str(name), [int(g) for g in t["grades"][name]]) for name in t["classes"]] if t["kind"] == "ordinal" else []
        out.append((t["task"], t["kind"], list(t["regions"]), classes, bool(t.get("log", False))))
    return out


def check_tasks_match(meta_task_list, tasks, what="meta.json"):
    """내보낸 모델(meta.json)과 tasks의 출력 규격이 다르면 ValueError. 다른 클래스 번호 체계로 평가·학습하는 것을 막는다."""
    expected, actual = task_signature(tasks), meta_signature(meta_task_list)
    if expected != actual:
        diff = [(e[0] if e else a[0]) for e, a in zip(expected, actual) if e != a] or ["항목 수"]
        raise ValueError(f"{what}의 항목 정의가 tasks와 다르다 (다른 곳: {diff}) → 같은 tasks.yaml을 쓰거나 처음부터 학습할 것")
