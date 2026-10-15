"""요청 토픽에 테스트 메시지 1건을 발행한다 (EC2에서 Kafka 연동 확인용). 설정은 .env의 KAFKA_*.

사용: python -m scripts.send_test_message [--user-id u-test] [--request-id ...]
          [--front s3://...] [--left s3://...] [--right s3://...] [--wait 30]
  --wait N  결과 토픽에서 같은 requestId의 completed 메시지를 N초까지 기다려 출력한다 (서비스가 떠 있어야 함)
토픽을 만들지 않는다: 토픽이 없으면 실패한다 (브로커의 auto.create.topics.enable이 꺼져 있어야 한다).
MODEL_ENABLED=false로 서비스를 띄우면 S3에 사진이 없어도 더미 결과(SUCCEEDED)가 돌아온다.
"""
import argparse
import json
import time
import uuid

from confluent_kafka import Consumer, Producer

from serving.settings import load_settings
from serving.worker import consumer_config, producer_config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", default="u-test")
    ap.add_argument("--request-id", default=f"test-{uuid.uuid4().hex[:8]}")
    ap.add_argument("--front", default="s3://example-bucket/test/front.jpg")
    ap.add_argument("--left", default="s3://example-bucket/test/left.jpg")
    ap.add_argument("--right", default="s3://example-bucket/test/right.jpg")
    ap.add_argument("--wait", type=int, default=0, help="completed 메시지를 기다릴 초")
    args = ap.parse_args()
    s = load_settings()

    consumer = None
    if args.wait:
        # 발행하기 전에 결과 토픽을 구독해 둔다 (latest라 그 뒤에 오는 것만 본다)
        consumer = Consumer({**consumer_config(s), "group.id": f"skin-test-{uuid.uuid4().hex[:8]}",
                             "auto.offset.reset": "latest"})
        consumer.subscribe([s.kafka_topic_completed])
        deadline = time.time() + 10
        while not consumer.assignment() and time.time() < deadline:
            consumer.poll(0.5)
        if not consumer.assignment():
            raise SystemExit(f"결과 토픽 {s.kafka_topic_completed}에 파티션을 못 받았다 (토픽이 있는지 확인)")

    payload = dict(userId=args.user_id, requestId=args.request_id,
                   images=dict(front=args.front, left=args.left, right=args.right))
    delivery = {}
    producer = Producer(producer_config(s))
    producer.produce(s.kafka_topic_requested, key=args.user_id.encode(), value=json.dumps(payload).encode(),
                     on_delivery=lambda err, msg: delivery.update(err=err, partition=msg.partition(),
                                                                  offset=msg.offset()))
    producer.flush(timeout=30)
    if delivery.get("err"):
        raise SystemExit(f"발행 실패: {delivery['err']}")
    print(f"발행됨 → {s.kafka_topic_requested} partition={delivery['partition']} offset={delivery['offset']}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if consumer is None:
        return
    print(f"\n{s.kafka_topic_completed}에서 requestId={args.request_id} 대기 (최대 {args.wait}초)…")
    deadline = time.time() + args.wait
    try:
        while time.time() < deadline:
            msg = consumer.poll(1.0)
            if msg is None or msg.error():
                continue
            try:
                result = json.loads(msg.value())
            except ValueError:
                continue
            if isinstance(result, dict) and result.get("requestId") == args.request_id:
                print("받음:")
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
        raise SystemExit("시간 안에 결과가 오지 않았다 (서비스 로그 확인)")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
