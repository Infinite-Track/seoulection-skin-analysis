"""Kafka 없이 컨슈머 루프를 검증한다. Consumer/Producer를 가짜로 바꿔 끼우고 run()을 직접 호출한다."""
import json

from serving.dummy import DummyAnalyzer
from serving.settings import Settings
from serving.worker import KafkaWorker, RecentIds

SETTINGS = Settings(kafka_enabled=True, kafka_bootstrap_servers="b:9092", kafka_group_id="g",
                    kafka_topic_requested="req", kafka_topic_completed="done", api_host="0.0.0.0", api_port=8000,
                    model_enabled=False, model_dir="/x", model_s3_uri=None, mediapipe_models_dir="/x",
                    min_pixels=1, min_skin_px=1, log_level="INFO")


def request(request_id, user_id="u1"):
    return json.dumps({"userId": user_id, "requestId": request_id,
                       "images": {"front": "s3://b/f", "left": "s3://b/l", "right": "s3://b/r"}}).encode()


class FakeMessage:
    def __init__(self, value):
        self._value = value

    def value(self):
        return self._value

    def error(self):
        return None


class FakeConsumer:
    """메시지를 다 주고 나면 워커를 멈춘다."""

    def __init__(self, messages, worker_ref):
        self.messages, self.worker_ref = list(messages), worker_ref
        self.events, self.closed = [], False

    def subscribe(self, topics):
        self.events.append(("subscribe", topics))

    def assignment(self):
        return [0]

    def poll(self, timeout):
        if not self.messages:
            self.worker_ref["worker"]._stop.set()
            return None
        return FakeMessage(self.messages.pop(0))

    def commit(self, msg, asynchronous):
        self.events.append(("commit", json.loads(msg.value())["requestId"] if msg.value().startswith(b"{") else None))

    def close(self):
        self.closed = True


class FakeProducer:
    def __init__(self, events, fail=False):
        self.events, self.fail = events, fail

    def produce(self, topic, key, value, on_delivery):
        self.events.append(("produce", topic, key.decode(), json.loads(value)))
        on_delivery("delivery failed" if self.fail else None, None)

    def flush(self, timeout):
        return 0


def run_worker(messages, fail_publish=False):
    ref, events, fatal = {}, [], []
    consumer = FakeConsumer(messages, ref)
    consumer.events = events
    worker = KafkaWorker(SETTINGS, get_analyzer=DummyAnalyzer, fetch=None,
                         consumer_factory=lambda cfg: consumer,
                         producer_factory=lambda cfg: FakeProducer(events, fail=fail_publish),
                         on_fatal=lambda: fatal.append(True))
    ref["worker"] = worker
    worker.run()
    return worker, consumer, events, fatal


def test_recent_ids_is_bounded_lru():
    r = RecentIds(maxlen=2)
    assert not r.seen("a") and not r.seen("b")
    assert r.seen("a")  # 다시 보면 True, 최근 것으로 갱신된다
    r.seen("c")  # 가장 오래 안 본 b가 밀려난다
    assert r.seen("a")
    assert not r.seen("b")


def test_publish_then_commit_and_duplicate_skipped():
    worker, consumer, events, fatal = run_worker([request("r1"), request("r2", "u2"), request("r1")])
    produces = [e for e in events if e[0] == "produce"]
    commits = [e for e in events if e[0] == "commit"]
    assert [p[3]["requestId"] for p in produces] == ["r1", "r2"]  # 중복 r1은 발행 안 함
    assert [p[2] for p in produces] == ["u1", "u2"]  # 키 = userId
    assert [c[1] for c in commits] == ["r1", "r2", "r1"]  # 중복도 커밋은 한다
    assert events.index(produces[0]) < events.index(commits[0])  # 발행 뒤 커밋
    assert all(p[1] == "done" for p in produces) and events[0] == ("subscribe", ["req"])
    assert worker.status["processed"] == 2 and worker.status["duplicates"] == 1
    assert consumer.closed and not fatal and worker.status["alive"] is False


def test_invalid_json_gets_failed_response_and_commit():
    _, _, events, fatal = run_worker([b"not json"])
    produce = next(e for e in events if e[0] == "produce")
    assert produce[3]["status"] == "FAILED" and produce[3]["failureReason"] == "INVALID_REQUEST"
    assert ("commit", None) in events and not fatal


def test_publish_failure_does_not_commit_and_is_fatal():
    worker, consumer, events, fatal = run_worker([request("r1")], fail_publish=True)
    assert not [e for e in events if e[0] == "commit"]
    assert fatal == [True] and consumer.closed
    assert "발행 실패" in worker.status["last_error"]
