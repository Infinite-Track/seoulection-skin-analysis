"""Kafka 컨슈머: analysis.requested → 피부 분석 → analysis.completed.

받는 메시지 (팀 확정 전 가정):
  {"user_id": "...", "images": {"front": "s3://버킷/키.jpg", "left": "...", "right": "..."},
   "request_id": "... (있으면 그대로 돌려준다)"}
보내는 메시지:
  {"user_id", "request_id", "status": "ok" | "failed", "model_version", "used_views",
   "results": {항목: 값 | {grade, expected}} | null,
   "error": {"code", "view"} | null}
실패도 analysis.completed로 보낸다 (status=failed). 스킨서버가 code로 "다시 찍어 주세요" 등을 분기한다.
  code: image_not_found / image_unreadable / low_resolution / no_face / bad_request / internal_error

처리 후 오프셋을 커밋한다 (최소 1회 전달) → 같은 요청이 두 번 처리될 수 있으니 받는 쪽은 request_id로 중복을 거른다.

사용: python -m serving.consumer [--config serving]
"""
import argparse
import json
import logging
import os
import signal

import numpy as np

from common import face_regions as fr
from common.config import load_yaml
from serving.analyzer import VIEWS, AnalysisError, SkinAnalyzer

log = logging.getLogger("skin-consumer")


class ImageFetcher:
    """s3://버킷/키 또는 로컬 경로 → BGR 배열. 없으면 AnalysisError("image_not_found")."""

    def __init__(self, region):
        self.region, self._s3 = region, None

    def __call__(self, uri, view):
        if uri.startswith("s3://"):
            import boto3
            from botocore.exceptions import ClientError
            self._s3 = self._s3 or boto3.client("s3", region_name=self.region)
            bucket, key = uri[5:].split("/", 1)
            try:
                data = self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            except ClientError as e:
                raise AnalysisError("image_not_found", view) from e
            return fr.cv2.imdecode(np.frombuffer(data, np.uint8), fr.cv2.IMREAD_COLOR)
        try:
            return fr.read_image(uri)
        except FileNotFoundError as e:
            raise AnalysisError("image_not_found", view) from e


def handle(payload, analyzer, fetch):
    """요청 메시지(dict) → 응답 메시지(dict). 어떤 경우에도 응답을 만든다."""
    response = dict(user_id=payload.get("user_id"), request_id=payload.get("request_id"), status="failed",
                    model_version=analyzer.model_version, used_views=[], results=None, error=None)
    try:
        uris = payload.get("images") or {}
        if not payload.get("user_id") or any(not uris.get(k) for k in VIEWS):
            raise AnalysisError("bad_request")
        images = {key: fetch(uris[key], key) for key in VIEWS}
        response["results"], response["used_views"] = analyzer.analyze(images)
        response["status"] = "ok"
    except AnalysisError as e:
        response["error"] = dict(code=e.code, view=e.view)
    except Exception:  # noqa: BLE001 — 한 요청의 오류로 컨슈머가 죽으면 안 된다
        log.exception("분석 실패 user_id=%s", payload.get("user_id"))
        response["error"] = dict(code="internal_error", view=None)
    return response


def main():
    from confluent_kafka import Consumer, Producer

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.environ.get("SKIN_CONFIG", "serving"),
                    help="configs/<이름>.yaml. 기본: 환경변수 SKIN_CONFIG 또는 serving")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_yaml(args.config)
    kcfg = cfg["kafka"]
    analyzer = SkinAnalyzer(cfg)
    fetch = ImageFetcher(cfg["s3"]["region"])
    consumer = Consumer({**kcfg["client"], "group.id": kcfg["group_id"], "enable.auto.commit": False,
                         "auto.offset.reset": "earliest"})
    producer = Producer(kcfg["client"])
    consumer.subscribe([kcfg["topic_requested"]])
    log.info("시작: 모델 %s, %s → %s", analyzer.model_version, kcfg["topic_requested"], kcfg["topic_completed"])

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while running:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                log.error("Kafka 오류: %s", msg.error())
                continue
            try:
                payload = json.loads(msg.value())
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            response = handle(payload, analyzer, fetch)
            producer.produce(kcfg["topic_completed"], key=str(response["user_id"] or ""),
                             value=json.dumps(response, ensure_ascii=False).encode())
            producer.flush()
            consumer.commit(msg, asynchronous=False)  # 응답을 보낸 뒤에 커밋
            log.info("user_id=%s status=%s %s", response["user_id"], response["status"], response["error"] or "")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
