# AI 서비스 이미지 (이미지 하나로 Kafka 컨슈머와 HTTP API 둘 다 띄운다).
#   빌드:   docker build -t skin-analysis .
#   컨슈머: docker run -d --restart=always --name skin-consumer skin-analysis
#   API:    docker run -d --restart=always --name skin-api -p 8000:8000 skin-analysis uvicorn serving.api:app --host 0.0.0.0 --port 8000
#   설정 바꾸기: -e SKIN_CONFIG=serving.prod  (configs/serving.prod.yaml)
#
# 모델 파일: mediapipe 모델은 빌드 때 받아 이미지에 고정한다. skin v2 가중치(models/skin_v2/skin.onnx*)는
# 빌드하는 PC에 있어야 한다 (git 제외 → models/README.md). 이후 MLOps에서 S3로 옮길 때 이 부분만 바뀐다.
FROM python:3.12-slim

# opencv·mediapipe가 필요로 하는 시스템 라이브러리.
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
COPY configs/ configs/
COPY serving/ serving/
COPY models/ models/
RUN python -m scripts.fetch_mediapipe_models \
    && test -f models/skin_v2/skin.onnx.data || (echo "models/skin_v2/skin.onnx.data 없음 — models/README.md 참고" && exit 1)

# 컨테이너 안에서는 root로 돌리지 않는다
RUN useradd --create-home app && chown -R app /app
USER app

ENV PYTHONUNBUFFERED=1 \
    SKIN_CONFIG=serving

# serving/api.py(HTTP)로 띄울 때 쓰는 포트. 기본 CMD(컨슈머)는 포트를 열지 않는다
EXPOSE 8000

# exec 형식(JSON 배열): 셸을 거치지 않아 SIGTERM이 python에 직접 간다 → consumer.py의 안전 종료가 동작한다.
# 설정 파일은 SKIN_CONFIG 환경변수로 고른다 (consumer.py가 --config 기본값으로 읽는다)
CMD ["python", "-m", "serving.consumer"]
