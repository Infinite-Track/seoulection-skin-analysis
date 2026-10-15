"""HTTP 입구 + 프로세스 수명 관리. 한 프로세스에서 Kafka 컨슈머(스레드)와 HTTP API가 같이 돈다.

  GET  /health   모델·Kafka 상태. 둘 다 정상이면 200, 아니면 503 (degraded)
  POST /analyze  multipart: userId, requestId(선택), front, left, right
                 → completed 메시지와 같은 JSON (테스트·프론트 확인용. Kafka와 같은 분석 코드)
실행: python -m serving.main   문서: http://<host>:<port>/docs

시작 순서(lifespan): 설정 → 모델 로드(실패해도 죽지 않고 degraded) → Kafka 컨슈머 스레드.
종료(SIGTERM → uvicorn → lifespan): 컨슈머가 처리 중인 건을 마치고 커밋한 뒤 멈춘다.
"""
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from common.model_files import ensure_model_files
from serving.analyzer import SkinAnalyzer
from serving.dummy import DummyAnalyzer
from serving.images import S3ImageFetcher, decode_image
from serving.logging_setup import request_id_var
from serving.service import VIEWS, analyze_images
from serving.settings import load_settings
from serving.worker import KafkaWorker

log = logging.getLogger("skin.api")
state = {"settings": None, "analyzer": None, "model_error": None, "worker": None}


def load_analyzer(s):
    """(분석기, 오류 메시지). 모델을 못 올리면 (None, 이유) — 서비스는 뜨고 /health가 503을 낸다."""
    if not s.model_enabled:
        log.warning("MODEL_ENABLED=false → 더미 분석기 (실제 결과 아님)")
        return DummyAnalyzer(), None
    try:
        model_dir = ensure_model_files(s.model_dir, s.model_s3_uri)
        analyzer = SkinAnalyzer(model_dir, s.mediapipe_models_dir, s.min_pixels, s.min_skin_px)
        log.info("모델 로드: %s", analyzer.model_version)
        return analyzer, None
    except Exception as e:  # noqa: BLE001
        log.exception("모델 로드 실패 → degraded")
        return None, f"{type(e).__name__}: {e}"


@asynccontextmanager
async def lifespan(app):
    s = load_settings()
    state.update(settings=s, worker=None)
    state["analyzer"], state["model_error"] = load_analyzer(s)
    if s.kafka_enabled:
        worker = KafkaWorker(s, get_analyzer=lambda: state["analyzer"], fetch=S3ImageFetcher())
        worker.start()
        state["worker"] = worker
    yield
    if state["worker"] is not None:
        state["worker"].stop()


app = FastAPI(title="Seoulection skin analysis", lifespan=lifespan)


@app.get("/health")
def health():
    analyzer, worker = state["analyzer"], state["worker"]
    model = {"loaded": analyzer is not None, "version": analyzer.model_version if analyzer else None,
             "error": state["model_error"]}
    kafka = {"enabled": False} if worker is None else {"enabled": True, **worker.status}
    ok = model["loaded"] and (worker is None or worker.status["alive"])
    return JSONResponse({"status": "ok" if ok else "degraded", "model": model, "kafka": kafka},
                        status_code=200 if ok else 503)


@app.post("/analyze")
async def analyze(userId: str = Form(...), requestId: str | None = Form(None), front: UploadFile = File(...),
                  left: UploadFile = File(...), right: UploadFile = File(...)):
    request_id = requestId or f"http-{uuid.uuid4().hex[:12]}"
    token = request_id_var.set(request_id)
    try:
        analyzer = state["analyzer"]
        images = None
        if analyzer is None or analyzer.needs_images:
            uploads = dict(zip(VIEWS, (front, left, right)))
            images = {view: decode_image(await uploads[view].read()) for view in VIEWS}
        return await run_in_threadpool(analyze_images, request_id, images, analyzer)  # 분석 중에도 /health가 응답
    finally:
        request_id_var.reset(token)
