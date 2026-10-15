"""진입점: python -m serving.main

환경변수(.env)로 설정을 읽고 uvicorn을 띄운다. API와 Kafka 컨슈머 스레드는 api.lifespan이 시작한다.
uvicorn에 log_config=None을 줘서 로깅은 logging_setup 하나로 통일한다 (stdout, request_id).
"""
import uvicorn

from serving.api import app
from serving.logging_setup import setup_logging
from serving.settings import load_settings


def main():
    s = load_settings()
    setup_logging(s.log_level)
    uvicorn.run(app, host=s.api_host, port=s.api_port, log_config=None)


if __name__ == "__main__":
    main()
