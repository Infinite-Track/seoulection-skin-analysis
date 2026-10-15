"""요청 → completed 메시지. Kafka 워커(worker.py)와 HTTP API(api.py)가 같이 쓴다.

requested 메시지 (skin.analysis.requested.v1):
  {"userId": "...", "requestId": "...", "images": {"front": "s3://...", "left": "s3://...", "right": "s3://..."}}
completed 메시지 (skin.analysis.completed.v1, 2026-10-15 합의):
  requestId, status (SUCCEEDED | FAILED),
  failureReason (NO_FACE | LOW_QUALITY | INVALID_REQUEST | MODEL_ERROR | null), failureDetail (디버깅용 | null),
  totalScore, regionScores {forehead, glabella, leftEye, rightEye, leftCheek, rightCheek},
  metrics {부위: {poreCount, poreGrade, wrinkleGrade, wrinkleRa, pigmentGrade}}, skinAge, modelVersion
어떤 입력에도 예외를 내지 않고 메시지를 만든다 (실패는 status=FAILED로).
"""
import logging

from serving.analyzer import INVALID_REQUEST, LOW_QUALITY, MODEL_ERROR, AnalysisError
from serving.scoring import build_result

log = logging.getLogger("skin.service")
VIEWS = ("front", "left", "right")


def failed(request_id, reason, detail, analyzer):
    return dict(requestId=request_id, status="FAILED", failureReason=reason, failureDetail=detail, totalScore=None,
                regionScores=None, metrics=None, skinAge=None,
                modelVersion=analyzer.model_version if analyzer is not None else None)


def analyze_images(request_id, images, analyzer):
    """디코딩된 이미지 {front, left, right} → completed 메시지. analyzer가 None이면(모델 미로드) MODEL_ERROR."""
    if analyzer is None:
        return failed(request_id, MODEL_ERROR, "모델이 로드되지 않음", None)
    try:
        per_region, used = analyzer.analyze(images)
        result = build_result(per_region)
        if result["totalScore"] is None:
            raise AnalysisError(LOW_QUALITY, "분석 가능한 부위가 없음")
        log.info("SUCCEEDED total=%s used_views=%s", result["totalScore"], used)
        return dict(requestId=request_id, status="SUCCEEDED", failureReason=None, failureDetail=None, **result,
                    modelVersion=analyzer.model_version)
    except AnalysisError as e:
        log.info("FAILED %s", e)
        return failed(request_id, e.reason, e.detail, analyzer)
    except Exception:  # noqa: BLE001 — 한 요청의 오류로 서비스가 죽으면 안 된다
        log.exception("분석 중 예외")
        return failed(request_id, MODEL_ERROR, "분석 중 예외", analyzer)


def process_request(payload, analyzer, fetch):
    """requested 메시지(dict) → completed 메시지(dict). 필수 필드 검사 → S3에서 사진 → 분석."""
    request_id = payload.get("requestId") if isinstance(payload, dict) else None
    try:
        if not isinstance(payload, dict) or not payload.get("userId") or not request_id:
            raise AnalysisError(INVALID_REQUEST, "userId, requestId는 필수")
        images = None
        if analyzer is not None and analyzer.needs_images:
            uris = payload.get("images") or {}
            missing = [v for v in VIEWS if not uris.get(v)]
            if missing:
                raise AnalysisError(INVALID_REQUEST, f"images.{', images.'.join(missing)} 없음")
            images = {v: fetch(uris[v], v) for v in VIEWS}
    except AnalysisError as e:
        log.info("FAILED %s", e)
        return failed(request_id, e.reason, e.detail, analyzer)
    except Exception:  # noqa: BLE001
        log.exception("요청 처리 중 예외")
        return failed(request_id, MODEL_ERROR, "요청 처리 중 예외", analyzer)
    return analyze_images(request_id, images, analyzer)
