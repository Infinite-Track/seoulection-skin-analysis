# serving — Kafka 컨슈머 AI 서비스 (EC2)

`analysis.requested` → S3 사진 3장 → mediapipe 14크롭 → skin v2 ONNX → 얼굴 전체 6개 항목 → `analysis.completed`

- `analyzer.py` — 핵심 로직 (Kafka·S3 없이 동작). 로컬 확인: `python -m serving.analyzer 정면.jpg 좌.jpg 우.jpg`
- `consumer.py` — Kafka 루프 + S3 다운로드. 메시지 형식은 파일 맨 위 설명 참고. 실행: `python -m serving.consumer`
- `api.py` — HTTP 입구 (테스트·프론트 확인용). 사진 3장을 업로드하면 Kafka 없이 바로 결과. 실행: `uvicorn serving.api:app --port 8000` → http://localhost:8000/docs
- 처음 한 번: `python -m scripts.fetch_mediapipe_models` (mediapipe 모델 받기)
- 설정: `configs/serving.yaml` (모델 경로, Kafka, S3). 패키지: 루트 `requirements.txt`

## Docker (운영 배포)

이미지는 하나(`skin-analysis`), 컨테이너는 역할별로 둘(`skin-consumer`, `skin-api`).

```bash
docker build -t skin-analysis .                                            # 빌드 (skin v2 가중치가 models/skin_v2/에 있어야 함)
docker run -d --restart=always --name skin-consumer skin-analysis          # Kafka 컨슈머 (CMD 기본값)
docker run -d --restart=always --name skin-api -p 8000:8000 skin-analysis \
    uvicorn serving.api:app --host 0.0.0.0 --port 8000                     # HTTP API
docker run ... -e SKIN_CONFIG=serving.prod skin-analysis                   # 다른 설정 파일 (configs/serving.prod.yaml)
docker logs -f skin-consumer                                               # 로그
docker stop skin-consumer                                                  # SIGTERM → 처리 중인 요청을 마치고 종료
```
