import cv2
import numpy as np
from fastapi.testclient import TestClient

from serving.api import app


def png():
    return cv2.imencode(".png", np.zeros((8, 8, 3), np.uint8))[1].tobytes()


def files():
    return {view: (f"{view}.png", png(), "image/png") for view in ("front", "left", "right")}


def test_health_dummy_mode_kafka_disabled():
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["model"]["version"] == "dummy" and body["kafka"] == {"enabled": False}


def test_analyze_dummy_returns_completed_shape():
    with TestClient(app) as client:
        r = client.post("/analyze", data={"userId": "u1"}, files=files())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "SUCCEEDED" and body["requestId"].startswith("http-")
    assert set(body) == {"requestId", "status", "failureReason", "failureDetail", "totalScore", "regionScores",
                         "metrics", "skinAge", "modelVersion"}
    assert body["modelVersion"] == "dummy"


def test_analyze_keeps_given_request_id():
    with TestClient(app) as client:
        r = client.post("/analyze", data={"userId": "u1", "requestId": "r-9"}, files=files())
    assert r.json()["requestId"] == "r-9"


def test_degraded_when_model_missing(monkeypatch):
    monkeypatch.setenv("MODEL_ENABLED", "true")  # MODEL_DIR은 /nonexistent → 로드 실패해도 서비스는 떠야 한다
    with TestClient(app) as client:
        health = client.get("/health")
        analyze = client.post("/analyze", data={"userId": "u1"}, files=files())
    assert health.status_code == 503 and health.json()["status"] == "degraded"
    assert health.json()["model"]["loaded"] is False and "FileNotFoundError" in health.json()["model"]["error"]
    assert analyze.json()["status"] == "FAILED" and analyze.json()["failureReason"] == "MODEL_ERROR"
