"""Kafka 컨슈머 스레드: requested 토픽 → process_request → completed 토픽.

- 수동 커밋: 결과 발행이 확인된 뒤에만 커밋 (최소 1회 전달 — 잃지 않는다)
- 중복: 같은 requestId가 다시 오면 분석하지 않고 커밋만 한다. 최근 10,000건을 프로세스 메모리에 두므로
  재시작 직후나 컨테이너가 여러 개일 때는 못 거른다 → 받는 쪽도 requestId로 걸러야 한다
- 분석 실패: FAILED 메시지를 발행하고 커밋한다 (재시도하지 않는다)
- 복구 불가(결과 발행 실패, 예상 못 한 예외): 커밋하지 않고 프로세스에 SIGTERM → Docker가 재시작 → 마지막 커밋부터 다시
- 종료(stop): 처리 중인 건을 마치고 커밋한 뒤 close
- 토픽을 만들지 않는다 (allow.auto.create.topics=false). 프로듀서 쪽 자동 생성은 브로커 설정으로 막아야 한다
"""
import json
import logging
import os
import signal
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

from serving.logging_setup import request_id_var
from serving.service import process_request

log = logging.getLogger("skin.worker")


def consumer_config(s):
    return {"bootstrap.servers": s.kafka_bootstrap_servers, "group.id": s.kafka_group_id,
            "enable.auto.commit": False, "auto.offset.reset": "earliest", "allow.auto.create.topics": False}


def producer_config(s):
    return {"bootstrap.servers": s.kafka_bootstrap_servers, "acks": "all", "enable.idempotence": True,
            "message.timeout.ms": 30_000}


class RecentIds:
    """최근 requestId 집합 (크기 고정)."""

    def __init__(self, maxlen=10_000):
        self._ids, self.maxlen = OrderedDict(), maxlen

    def seen(self, key):
        """이미 있었으면 True. 없었으면 넣고 False."""
        if key in self._ids:
            self._ids.move_to_end(key)
            return True
        self._ids[key] = None
        if len(self._ids) > self.maxlen:
            self._ids.popitem(last=False)
        return False


def _default_fatal():
    os.kill(os.getpid(), signal.SIGTERM)  # uvicorn이 받아 정상 종료 → --restart always가 재시작


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class KafkaWorker(threading.Thread):
    """get_analyzer: 현재 분석기를 돌려주는 함수(모델 미로드면 None). fetch: (s3 uri, view) → 이미지.
    consumer_factory / producer_factory / on_fatal은 테스트에서 바꿔 끼운다."""

    def __init__(self, settings, get_analyzer, fetch, consumer_factory=None, producer_factory=None, on_fatal=None):
        super().__init__(name="kafka-worker", daemon=True)
        self.s, self.get_analyzer, self.fetch = settings, get_analyzer, fetch
        self._consumer_factory, self._producer_factory = consumer_factory, producer_factory
        self._on_fatal = on_fatal or _default_fatal
        self._stop = threading.Event()
        self._recent = RecentIds()
        self.status = {"alive": False, "assigned_partitions": 0, "last_poll_at": None, "last_error": None,
                       "processed": 0, "duplicates": 0}

    def stop(self, timeout=30):
        self._stop.set()
        self.join(timeout)

    def run(self):
        try:
            self._run()
        except Exception as e:  # noqa: BLE001
            self.status["last_error"] = f"{type(e).__name__}: {e}"
            log.exception("컨슈머 스레드 종료 — 프로세스를 재시작한다")
            self._on_fatal()
        finally:
            self.status["alive"] = False

    def _run(self):
        if self._consumer_factory is None:
            from confluent_kafka import Consumer, Producer
            self._consumer_factory, self._producer_factory = Consumer, Producer
        consumer = self._consumer_factory(consumer_config(self.s))
        producer = self._producer_factory(producer_config(self.s))
        consumer.subscribe([self.s.kafka_topic_requested])
        self.status["alive"] = True
        log.info("컨슈머 시작: %s → %s (group %s)", self.s.kafka_topic_requested, self.s.kafka_topic_completed,
                 self.s.kafka_group_id)
        try:
            while not self._stop.is_set():
                msg = consumer.poll(1.0)
                self.status["last_poll_at"] = _now()
                self.status["assigned_partitions"] = len(consumer.assignment())
                if msg is None:
                    continue
                if msg.error():
                    self.status["last_error"] = str(msg.error())
                    log.error("Kafka 오류: %s", msg.error())
                    continue
                self._handle(msg, consumer, producer)
        finally:
            consumer.close()
            log.info("컨슈머 종료")

    def _handle(self, msg, consumer, producer):
        try:
            payload = json.loads(msg.value())
        except (ValueError, TypeError):
            payload = {}
        request_id = payload.get("requestId") if isinstance(payload, dict) else None
        token = request_id_var.set(request_id or "-")
        try:
            if request_id and self._recent.seen(request_id):
                self.status["duplicates"] += 1
                log.info("중복 요청 — 건너뜀")
                consumer.commit(msg, asynchronous=False)
                return
            response = process_request(payload, self.get_analyzer(), self.fetch)
            key = str(payload.get("userId") or "") if isinstance(payload, dict) else ""
            if not self._publish(producer, key, response):
                raise RuntimeError("결과 발행 실패 (커밋하지 않음)")
            consumer.commit(msg, asynchronous=False)  # 발행 확인 뒤에만 커밋
            self.status["processed"] += 1
            log.info("처리 완료 status=%s reason=%s", response["status"], response["failureReason"])
        finally:
            request_id_var.reset(token)

    def _publish(self, producer, key, response):
        """completed 메시지를 보내고 브로커 확인(acks=all)까지 기다린다. 실패하면 False."""
        delivery = {}

        def on_delivery(err, _msg):
            delivery["err"] = err

        producer.produce(self.s.kafka_topic_completed, key=key.encode(),
                         value=json.dumps(response, ensure_ascii=False).encode(), on_delivery=on_delivery)
        remaining = producer.flush(timeout=60)
        err = delivery.get("err")
        if remaining or err:
            self.status["last_error"] = f"발행 실패: {err or f'{remaining}건 미전송'}"
            log.error(self.status["last_error"])
            return False
        return True

    @staticmethod
    def sleep(seconds):  # 테스트에서 바꿔 끼우기 쉽게
        time.sleep(seconds)
