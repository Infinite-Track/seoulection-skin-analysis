# serving — AI 피부분석 서비스 (EC2, Docker)

한 프로세스에서 **Kafka 컨슈머**(스레드)와 **HTTP API**가 같이 돈다. 진입점 `python -m serving.main`.

`skin.analysis.requested.v1` → S3에서 사진 3장 → mediapipe 14크롭 → skin v2 ONNX → 부위 6개 점수 → `skin.analysis.completed.v1`

## 파일

| 파일 | 역할 |
|---|---|
| `main.py` | 진입점. 설정·로깅 → uvicorn |
| `api.py` | lifespan(모델 로드, 컨슈머 시작/종료), `GET /health`, `POST /analyze` |
| `worker.py` | Kafka 컨슈머 스레드: 수동 커밋, requestId 중복 제거, 발행 확인, 복구 불가 시 프로세스 재시작 |
| `service.py` | 요청 → completed 메시지. **메시지 형식은 이 파일 맨 위** |
| `analyzer.py` | 사진 3장 → 부위별 측정값 (mediapipe + ONNX, 한 번에 하나씩) |
| `scoring.py` | 측정값 → 0~100 점수. **환산 공식은 이 파일 맨 위** |
| `images.py` | S3 다운로드(IAM 역할), 이미지 디코딩 |
| `dummy.py` | `MODEL_ENABLED=false`용 가짜 분석기 |
| `settings.py` | 환경변수 → Settings (`.env.example` 참고) |
| `logging_setup.py` | stdout, 모든 줄에 request_id |

## 설정

전부 환경변수. `cp .env.example .env` 해서 채운다. 코드·이미지에 주소·경로·키를 넣지 않는다.

## 실행

로컬 (API만, Kafka 없이):
```bash
cp .env.example .env   # KAFKA_ENABLED=false, MODEL_DIR=models/skin_v2, MEDIAPIPE_MODELS_DIR=models/mediapipe 로
python -m scripts.fetch_mediapipe_models
python -m serving.main                      # http://localhost:8000/docs
```

EC2 (Docker, 컨테이너 1개). 가중치는 이미지에 없으니 `models/skin_v2/`에 `skin.onnx`, `skin.onnx.data`를 넣고 마운트한다 (`models/README.md`):
```bash
docker build -t skin-analysis .
docker run -d --restart always --env-file .env -p 8000:8000 --stop-timeout 30 \
    -v "$(pwd)/models/skin_v2:/models/skin_v2:ro" --name skin-analysis skin-analysis
docker logs -f skin-analysis
curl localhost:8000/health
docker exec skin-analysis python -m scripts.send_test_message --wait 30   # Kafka 연동 확인
```

## `/health`

```json
{"status": "ok" | "degraded",
 "model": {"loaded": true, "version": "skin v2 2026-10-04", "error": null},
 "kafka": {"enabled": true, "alive": true, "assigned_partitions": 1, "last_poll_at": "...", "last_error": null,
           "processed": 12, "duplicates": 0}}
```
degraded면 503. 모델을 못 올려도, Kafka 스레드가 죽어도 서비스는 떠 있고 여기서 드러난다.

## 동작 보장

- **요청을 잃지 않는다**: 결과 발행이 브로커에서 확인(acks=all)된 뒤에만 커밋. 발행 실패·예외 시 커밋 없이 프로세스를 끝내고 Docker가 재시작 → 마지막 커밋부터 재소비
- **같은 requestId는 두 번 분석하지 않는다** (프로세스당 최근 10,000건). 재시작 직후·컨테이너 여러 개일 때는 못 거르니 받는 쪽도 requestId로 걸러야 한다
- **SIGTERM(docker stop)**: 처리 중인 건을 마치고 커밋한 뒤 종료
- **토픽을 만들지 않는다.** 브로커의 `auto.create.topics.enable`은 꺼 둘 것 (프로듀서 쪽 자동 생성은 클라이언트가 못 막는다)
