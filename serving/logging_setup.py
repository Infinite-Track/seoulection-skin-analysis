"""로그: stdout, 한 줄 형식, 모든 줄에 request_id.

request_id_var에 넣어 두면 같은 스레드·태스크 안의 모든 로그에 자동으로 붙는다 (없으면 '-').
uvicorn 로그도 루트 핸들러로 올라와 같은 형식으로 찍힌다 (main.py가 uvicorn에 log_config=None을 준다).
"""
import contextvars
import logging
import sys

request_id_var = contextvars.ContextVar("request_id", default="-")

FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True


def setup_logging(level="INFO"):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(FORMAT))
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
