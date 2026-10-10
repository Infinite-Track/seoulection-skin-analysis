"""HTTP 입구 (테스트·프론트 확인용). Kafka 없이 사진 3장을 올리면 바로 결과를 돌려준다.

분석 로직은 consumer.py와 같은 SkinAnalyzer를 쓴다. 응답 JSON도 analysis.completed 메시지와 같은 모양이다.
  GET  /health                       서버·모델 상태
  POST /analyze  (multipart/form-data: user_id, front, left, right)  → 결과 JSON
실행: uvicorn serving.api:app --host 0.0.0.0 --port 8000   (설정 파일은 환경변수 SKIN_CONFIG, 기본 serving)
문서: http://localhost:8000/docs
"""
import logging
import os
from contextlib import asynccontextmanager

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool

from common.config import load_yaml
from serving.analyzer import VIEWS, AnalysisError, SkinAnalyzer

log = logging.getLogger("skin-api")
state = {}


@asynccontextmanager
async def lifespan(app):
    cfg = load_yaml(os.environ.get("SKIN_CONFIG", "serving"))
    state["analyzer"] = SkinAnalyzer(cfg)  # 모델 로드는 서버 시작 때 한 번
    log.info("시작: 모델 %s", state["analyzer"].model_version)
    yield


app = FastAPI(title="Seoulection skin analysis", lifespan=lifespan)


def _decode(upload: UploadFile):
    data = np.frombuffer(upload.file.read(), np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None  # 못 읽으면 None → analyze가 image_unreadable


def _analyze(user_id, uploads):
    """consumer.handle()과 같은 응답 모양. 동기 함수라 스레드풀에서 돌린다 (분석 중에도 /health가 응답하도록)."""
    analyzer = state["analyzer"]
    response = dict(user_id=user_id, request_id=None, status="failed", model_version=analyzer.model_version,
                    used_views=[], results=None, error=None)
    try:
        images = {key: _decode(uploads[key]) for key in VIEWS}
        response["results"], response["used_views"] = analyzer.analyze(images)
        response["status"] = "ok"
    except AnalysisError as e:
        response["error"] = dict(code=e.code, view=e.view)
    except Exception:  # noqa: BLE001
        log.exception("분석 실패 user_id=%s", user_id)
        response["error"] = dict(code="internal_error", view=None)
    return response


@app.get("/health")
def health():
    return {"status": "ok", "model_version": state["analyzer"].model_version}


@app.post("/analyze")
async def analyze(user_id: str = Form(...), front: UploadFile = File(...), left: UploadFile = File(...),
                  right: UploadFile = File(...)):
    return await run_in_threadpool(_analyze, user_id, {"front": front, "left": left, "right": right})
