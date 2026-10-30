"""신규 데이터 확인 인터페이스 — Prefect 플로우(pipelines/prefect_flow.py)가 "이번 주 재학습할 만큼 새 데이터가 있나"를 묻는 곳.

NewDataSource.check(since) → NewDataReport. 플로우는 report.count를 최소 건수(환경변수 MIN_NEW_FEEDBACK_FILES)와 비교하고,
report.latest를 다음 실행의 기준점(since)으로 저장한다. 소스는 configs/prefect.yaml new_data.source 로 고른다.

구현
  s3_feedback    S3FeedbackSource     s3://<bucket>/feedback/ 아래 객체 중 since 이후(LastModified > since)에 올라온 파일 수.
                                      폴더 표시 객체(키가 /로 끝남)는 세지 않는다. 형식 검사는 하지 않는다 — 표준 데이터셋 형식 여부는
                                      파이프라인 Prepare 단계가 판단하고 라벨 있는 크롭 수(MinFeedbackCrops)로 다시 거른다.
  recommend_api  RecommendServerSource TODO: 추천 서버의 피드백 count API로 신규 건수를 묻고, export API로 feedback/에 표준 형식으로
                                      내보내게 한다 (주소·인증은 환경변수). 라벨 출처가 정해지면 구현. 지금은 NotImplementedError.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from pipelines.steps.util import s3_split


@dataclass(frozen=True)
class NewDataReport:
    source: str                       # 소스 이름 (s3_feedback 등)
    location: str                     # 어디를 봤는지 (S3 주소, API 주소)
    since: datetime | None            # 기준점. None이면 전체가 신규
    count: int                        # 기준점 이후 신규 파일 수  ← 플로우가 최소 건수와 비교
    total: int                        # 기준점과 무관한 전체 파일 수
    latest: datetime | None           # 가장 최근 파일 시각 → 다음 기준점
    by_prefix: dict = field(default_factory=dict)  # 신규 파일 수를 첫 하위 폴더별로 (예: {"2026-10": 42})

    def to_dict(self):
        d = asdict(self)
        for k in ("since", "latest"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


class NewDataSource(Protocol):
    name: str

    def check(self, since: datetime | None) -> NewDataReport: ...


def as_utc(value):
    """naive datetime은 UTC로 본다 (S3 LastModified는 aware UTC)."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class S3FeedbackSource:
    """S3 feedback/ 접두사의 신규 파일 수. client는 boto3 S3 클라이언트 (테스트에서는 대역)."""

    name = "s3_feedback"

    def __init__(self, uri, client=None):
        self.uri = uri
        self.bucket, prefix = s3_split(uri)
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("s3")
        return self._client

    def check(self, since):
        since = as_utc(since)
        count = total = 0
        latest = None
        by_prefix = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                total += 1
                modified = as_utc(obj["LastModified"])
                latest = modified if latest is None or modified > latest else latest
                if since is None or modified > since:
                    count += 1
                    head = key[len(self.prefix):].split("/", 1)[0]
                    by_prefix[head] = by_prefix.get(head, 0) + 1
        return NewDataReport(source=self.name, location=self.uri, since=since, count=count, total=total, latest=latest,
                             by_prefix=dict(sorted(by_prefix.items())))


class RecommendServerSource:
    """TODO(추천 서버 연동): count API(기준점 이후 피드백 건수) + export API(feedback/에 표준 데이터셋 형식으로 내보내기).

    주소·토큰은 환경변수(예: RECOMMEND_API_URL, RECOMMEND_API_TOKEN)로 받고, check()는 count API 응답을 NewDataReport로 바꾼 뒤
    export API를 호출해 파이프라인 Prepare가 읽을 feedback/<날짜>/ 폴더를 만들게 한다. 라벨 출처(Phase 2 Q3)가 정해지면 구현.
    """

    name = "recommend_api"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("recommend_api 소스는 아직 없다 — configs/prefect.yaml new_data.source: s3_feedback 을 쓸 것")

    def check(self, since):  # pragma: no cover - 구현 전
        raise NotImplementedError


SOURCES = {S3FeedbackSource.name: S3FeedbackSource, RecommendServerSource.name: RecommendServerSource}


def make_source(name, feedback_uri, client=None) -> NewDataSource:
    """configs/prefect.yaml new_data.source 이름 → 소스 객체."""
    if name not in SOURCES:
        raise ValueError(f"모르는 신규 데이터 소스 {name!r} (가능: {sorted(SOURCES)})")
    return SOURCES[name](feedback_uri, client=client)
