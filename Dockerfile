# AI 피부분석 서비스 이미지. 한 프로세스에서 Kafka 컨슈머 + HTTP API (serving/main.py).
#   빌드: docker build -t skin-analysis .
#   실행: docker run -d --restart always --env-file .env -p 8000:8000 --stop-timeout 30 \
#           -v "$(pwd)/models/skin_v2:/models/skin_v2:ro" --name skin-analysis skin-analysis
#
# 이미지에 넣지 않는 것: skin v2 가중치(MODEL_DIR로 마운트하거나 MODEL_S3_URI로 받음), .env
# 이미지에 넣는 것: mediapipe 모델 (구글 배포 파일, 해시 고정 — pip 패키지처럼 의존성으로 취급)
FROM python:3.11-slim

# opencv·mediapipe가 필요로 하는 시스템 라이브러리. 이미지는 linux/amd64 (운영 EC2가 x86. Mac에서는 --platform linux/amd64).
# mediapipe 0.10.35는 GPU를 안 써도 시작 시 libEGL.so.1·libGLESv2.so.2를 로드한다 (없으면 OSError로 죽음)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libegl1 libgles2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 패키지를 코드보다 먼저 설치: 코드만 바뀌면 이 층은 캐시돼 재빌드가 빠르다
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ common/
COPY scripts/ scripts/
COPY serving/ serving/

ENV MEDIAPIPE_MODELS_DIR=/app/models/mediapipe
RUN python -m scripts.fetch_mediapipe_models

# 컨테이너 안에서는 root로 돌리지 않는다
RUN useradd --create-home app && chown -R app /app
USER app

# .env가 덮어쓴다. API_PORT를 바꾸면 -p 와 EXPOSE도 맞출 것
ENV PYTHONUNBUFFERED=1 \
    MODEL_DIR=/models/skin_v2 \
    API_HOST=0.0.0.0 \
    API_PORT=8000
EXPOSE 8000

# exec 형식: 셸을 거치지 않아 SIGTERM이 python에 직접 간다 → uvicorn 정상 종료 → 컨슈머가 처리 중인 건을 마치고 멈춤
CMD ["python", "-m", "serving.main"]
