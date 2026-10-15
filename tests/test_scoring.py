from serving.dummy import DummyAnalyzer
from serving.scoring import REGION_KEYS, build_result, task_score


def test_grade_score_bounds():
    assert task_score("wrinkle_grade", {"grade": "0", "expected": 0.0, "classes": 7}) == 100
    assert task_score("wrinkle_grade", {"grade": "6", "expected": 6.0, "classes": 7}) == 0
    assert task_score("pore_grade", {"grade": "2", "expected": 1.5, "classes": 4}) == 50


def test_count_score_uses_percentile_range_and_clips():
    assert task_score("pore_count", 94.0) == 100
    assert task_score("pore_count", 2113.0) == 0
    assert task_score("pore_count", 10.0) == 100  # p01 아래는 100으로 고정
    assert task_score("wrinkle_ra", 50.0) == 0  # p99 위는 0으로 고정


def test_build_result_covers_six_regions():
    per_region, _ = DummyAnalyzer().analyze()
    r = build_result(per_region)
    assert set(r["regionScores"]) == set(REGION_KEYS.values())
    assert all(isinstance(v, int) for v in r["regionScores"].values())
    assert isinstance(r["totalScore"], int)
    assert r["skinAge"] == 30.0
    assert r["metrics"]["leftCheek"] == {"poreCount": 800, "poreGrade": "2", "pigmentGrade": "1"}
    assert "age" not in str(r["metrics"])


def test_missing_region_is_null_and_excluded_from_total():
    per_region, _ = DummyAnalyzer().analyze()
    del per_region["l_eye"], per_region["r_eye"]
    r = build_result(per_region)
    assert r["regionScores"]["leftEye"] is None and r["metrics"]["rightEye"] is None
    present = [v for v in r["regionScores"].values() if v is not None]
    assert len(present) == 4 and r["totalScore"] == round(sum(present) / 4)


def test_no_regions_gives_null_total():
    assert build_result({})["totalScore"] is None
