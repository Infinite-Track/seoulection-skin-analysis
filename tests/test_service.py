import pytest

from serving.analyzer import INVALID_REQUEST, NO_FACE, AnalysisError
from serving.dummy import DummyAnalyzer
from serving.service import process_request

REQUEST = {"userId": "u1", "requestId": "r1",
           "images": {"front": "s3://b/f.jpg", "left": "s3://b/l.jpg", "right": "s3://b/r.jpg"}}


def no_fetch(uri, view):
    raise AssertionError("더미 모드에서는 S3를 읽으면 안 된다")


class FakeAnalyzer:
    needs_images = True
    model_version = "fake"

    def __init__(self, error=None):
        self.error, self.calls = error, []

    def analyze(self, images):
        self.calls.append(images)
        if self.error:
            raise self.error
        return DummyAnalyzer().analyze()


def test_dummy_succeeds_without_fetching_images():
    r = process_request(REQUEST, DummyAnalyzer(), no_fetch)
    assert r["status"] == "SUCCEEDED" and r["failureReason"] is None
    assert r["requestId"] == "r1" and r["modelVersion"] == "dummy"
    assert len(r["regionScores"]) == 6 and r["totalScore"] is not None


@pytest.mark.parametrize("payload", [{}, [], None, {"userId": "u1"}, {"requestId": "r1"}])
def test_missing_required_fields(payload):
    r = process_request(payload, DummyAnalyzer(), no_fetch)
    assert r["status"] == "FAILED" and r["failureReason"] == INVALID_REQUEST


def test_missing_image_uri():
    bad = {**REQUEST, "images": {"front": "s3://b/f.jpg"}}
    r = process_request(bad, FakeAnalyzer(), no_fetch)
    assert r["failureReason"] == INVALID_REQUEST and "left" in r["failureDetail"]


def test_fetch_not_found_maps_to_invalid_request():
    def fetch(uri, view):
        raise AnalysisError(INVALID_REQUEST, f"{view}: S3에 없음")
    r = process_request(REQUEST, FakeAnalyzer(), fetch)
    assert r["failureReason"] == INVALID_REQUEST and r["failureDetail"].startswith("front")


def test_model_not_loaded():
    r = process_request(REQUEST, None, no_fetch)
    assert r["failureReason"] == "MODEL_ERROR" and r["modelVersion"] is None


def test_analysis_error_and_unexpected_exception():
    fetch = lambda uri, view: "img"  # noqa: E731
    assert process_request(REQUEST, FakeAnalyzer(AnalysisError(NO_FACE, "front")), fetch)["failureReason"] == NO_FACE
    r = process_request(REQUEST, FakeAnalyzer(RuntimeError("boom")), fetch)
    assert r["status"] == "FAILED" and r["failureReason"] == "MODEL_ERROR"


def test_images_fetched_per_view_for_real_analyzer():
    analyzer = FakeAnalyzer()
    process_request(REQUEST, analyzer, lambda uri, view: f"img:{view}")
    assert analyzer.calls == [{"front": "img:front", "left": "img:left", "right": "img:right"}]
