"""환경변수 → Settings. 설정은 전부 여기서만 읽는다 (yaml 없음, 코드에 주소·경로 하드코딩 없음).

로컬: .env.example을 .env로 복사해 채운다. 컨테이너: docker run --env-file .env.
필수 값이 비어 있으면 시작할 때 바로 멈춘다 (어떤 변수가 없는지 메시지로 알려줌).
"""
import os
from dataclasses import dataclass

_REQUIRED = object()


def _env(name, default=_REQUIRED, cast=str):
    raw = os.environ.get(name, "").strip()
    if not raw:
        if default is _REQUIRED:
            raise RuntimeError(f"환경변수 {name}가 없다 (.env.example 참고)")
        return default
    return cast(raw)


def _bool(raw):
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    kafka_enabled: bool
    kafka_bootstrap_servers: str
    kafka_group_id: str
    kafka_topic_requested: str
    kafka_topic_completed: str
    api_host: str
    api_port: int
    model_enabled: bool
    model_dir: str
    model_s3_uri: str | None
    mediapipe_models_dir: str
    min_pixels: int
    min_skin_px: int
    log_level: str


def load_settings() -> Settings:
    kafka_enabled = _env("KAFKA_ENABLED", True, _bool)
    kafka = _REQUIRED if kafka_enabled else ""  # Kafka를 끄면(로컬 API만) Kafka 변수는 없어도 된다
    return Settings(
        kafka_enabled=kafka_enabled,
        kafka_bootstrap_servers=_env("KAFKA_BOOTSTRAP_SERVERS", kafka),
        kafka_group_id=_env("KAFKA_GROUP_ID", kafka),
        kafka_topic_requested=_env("KAFKA_TOPIC_REQUESTED", kafka),
        kafka_topic_completed=_env("KAFKA_TOPIC_COMPLETED", kafka),
        api_host=_env("API_HOST", "0.0.0.0"),
        api_port=_env("API_PORT", 8000, int),
        model_enabled=_env("MODEL_ENABLED", True, _bool),
        model_dir=_env("MODEL_DIR"),
        model_s3_uri=_env("MODEL_S3_URI", None),
        mediapipe_models_dir=_env("MEDIAPIPE_MODELS_DIR"),
        min_pixels=_env("MIN_PIXELS", 1_000_000, int),
        min_skin_px=_env("MIN_SKIN_PX", 20_000, int),
        log_level=_env("LOG_LEVEL", "INFO"),
    )
