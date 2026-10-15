"""MODEL_ENABLED=false일 때 쓰는 가짜 분석기. 모델 파일 없이 Kafka 흐름(요청 → 결과)만 확인하는 용도.

이미지를 보지 않고(needs_images=False → S3도 읽지 않는다) 고정값을 돌려준다.
model_version="dummy"로 표시돼 실제 결과와 구분된다. 운영에서 켜면 안 된다.
"""


def _grade(grade, expected, classes):
    return {"grade": grade, "expected": expected, "classes": classes}


class DummyAnalyzer:
    needs_images = False
    model_version = "dummy"

    def analyze(self, images=None):
        eye = {"wrinkle_grade": _grade("1", 1.0, 7), "wrinkle_ra": 20.0, "age": 30.0}
        cheek = {"pore_count": 800.0, "pore_grade": _grade("2", 1.0, 4), "pigment_grade": _grade("1", 1.0, 5),
                 "age": 30.0}
        per_region = {
            "forehead": {"wrinkle_grade": _grade("1", 1.0, 7), "pigment_grade": _grade("1", 1.0, 5), "age": 30.0},
            "glabella": {"wrinkle_grade": _grade("1", 1.0, 7), "age": 30.0},
            "l_eye": dict(eye), "r_eye": dict(eye),
            "l_cheek": dict(cheek), "r_cheek": dict(cheek),
        }
        return per_region, ["front", "left", "right"]
