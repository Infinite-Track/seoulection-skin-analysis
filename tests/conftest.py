"""테스트 공통: Kafka·모델 없이 돌아가는 환경변수. 개별 테스트가 monkeypatch로 덮어쓴다."""
import pytest

BASE_ENV = {
    "KAFKA_ENABLED": "false",
    "KAFKA_BOOTSTRAP_SERVERS": "",
    "KAFKA_GROUP_ID": "",
    "KAFKA_TOPIC_REQUESTED": "",
    "KAFKA_TOPIC_COMPLETED": "",
    "MODEL_ENABLED": "false",
    "MODEL_DIR": "/nonexistent/skin_v2",
    "MODEL_S3_URI": "",
    "MEDIAPIPE_MODELS_DIR": "/nonexistent/mediapipe",
}


@pytest.fixture(autouse=True)
def base_env(monkeypatch):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
