"""Prefect 주간 재학습 플로우 검증 — AWS 없이 (boto3 클라이언트 대역).

플로우 테스트는 Prefect 임시 서버(prefect_test_harness)로 돈다. 서버를 띄울 수 없는 환경(소켓 bind 금지 샌드박스)에서는
태스크를 .fn 으로 바꿔 Prefect 엔진 없이 같은 플로우 함수를 돈다 — 플로우 로직은 같은 코드이고 Prefect 통합만 빠진다.
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

pytest.importorskip("prefect")  # requirements-pipeline.txt — 서빙 CI 환경에는 없음 → 건너뜀

from pipelines import new_data, prefect_flow  # noqa: E402

T0 = datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc)  # 월요일 03:00 KST
ARN = "arn:aws:sagemaker:us-east-1:000000000000:pipeline/seoulection-skin-retrain/execution/exec123"
SETTINGS = dict(
    flow_name="t", deployment_name="weekly", cron="0 3 * * 1", timezone="Asia/Seoul",
    pipeline="seoulection-skin-retrain", region="us-east-1", bucket="bucket", artifacts_prefix="pipeline-artifacts/seoulection-skin-retrain",
    feedback="s3://bucket/feedback/", source="s3_feedback", min_new_files=3,
    marker_key="pipeline-artifacts/seoulection-skin-retrain/prefect/last_run.json",
    active_key="pipeline-artifacts/seoulection-skin-retrain/prefect/active_run.json", reserve_stale_seconds=600,
    poll_seconds=0, timeout_seconds=60,
)
MARKER, ACTIVE = SETTINGS["marker_key"], SETTINGS["active_key"]


def s3_error(code, op):
    return ClientError({"Error": {"Code": code, "Message": op}}, op)


class FakeS3:
    """키 → (바이트, LastModified). list_objects_v2 페이지네이터·get_object(ETag)·put_object(IfNoneMatch/IfMatch)·delete_object만.

    ETag = 본문 md5 (실제 S3 단일 PUT 과 같다) → 조건부 쓰기(If-Match)는 본문이 바뀌었으면 412.
    """

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts, self.deletes = [], []

    def etag(self, key):
        return '"' + hashlib.md5(self.objects[key][0]).hexdigest() + '"'

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        s3 = self

        class P:
            def paginate(self, Bucket, Prefix):
                items = sorted((k, v) for k, v in s3.objects.items() if k.startswith(Prefix))
                half = max(1, len(items) // 2)  # 두 페이지로 나눠 페이지네이션도 거친다
                for chunk in (items[:half], items[half:]):
                    yield {"Contents": [{"Key": k, "LastModified": v[1]} for k, v in chunk]} if chunk else {}
        return P()

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise s3_error("NoSuchKey", "GetObject")
        data = self.objects[Key][0]   # 읽은 시점의 본문 (그 뒤 다른 쓰기가 있어도 이 응답은 그대로)

        class Body:
            def read(_self):
                return data
        return {"Body": Body(), "ETag": self.etag(Key)}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, IfMatch=None, **kw):
        if IfNoneMatch == "*" and Key in self.objects:
            raise s3_error("PreconditionFailed", "PutObject")
        if IfMatch is not None:
            if Key not in self.objects:
                raise s3_error("NoSuchKey", "PutObject")
            if self.etag(Key) != IfMatch:
                raise s3_error("PreconditionFailed", "PutObject")
        self.objects[Key] = (Body, datetime.now(timezone.utc))
        self.puts.append(Key)

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        self.deletes.append(Key)

    def json(self, key):
        return json.loads(self.objects[key][0]) if key in self.objects else None


class FakeSageMaker:
    """statuses 는 describe 마다 앞에서 하나씩 소비(마지막 값은 유지). started 는 start **요청** 목록, executions 는 list 결과(= 생긴 실행).

    ClientRequestToken 멱등성: 같은 토큰의 start 요청은 새 실행을 만들지 않고 처음 받은 ARN 을 돌려준다(요청은 started 에 남는다).
    reject 에 오류 코드를 넣으면 start 가 그 ClientError 를 낸다(실행 없음).
    """

    def __init__(self, statuses=("Executing", "Executing", "Succeeded"), steps=(), exists=True, executions=()):
        self.statuses, self.steps, self.exists = list(statuses), list(steps), exists
        self.started, self.executions, self.missing = [], list(executions), set()
        self.tokens, self.created, self.reject = {}, 0, None

    def start_pipeline_execution(self, **request):
        if not self.exists:
            raise s3_error("ResourceNotFound", "StartPipelineExecution")
        if self.reject:
            raise s3_error(self.reject, "StartPipelineExecution")
        self.started.append(request)
        token = request.get("ClientRequestToken")
        if token in self.tokens:
            return {"PipelineExecutionArn": self.tokens[token]}
        self.created += 1
        arn = ARN if self.created == 1 else f"{ARN}-{self.created}"
        if token:
            self.tokens[token] = arn
        self.executions.insert(0, {"PipelineExecutionArn": arn, "PipelineExecutionDisplayName": request.get("PipelineExecutionDisplayName"),
                                   "PipelineExecutionStatus": "Executing"})
        return {"PipelineExecutionArn": arn}

    def describe_pipeline_execution(self, PipelineExecutionArn):
        if PipelineExecutionArn in self.missing:
            raise s3_error("ResourceNotFound", "DescribePipelineExecution")
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"PipelineExecutionStatus": status}

    def list_pipeline_executions(self, PipelineName, SortBy, SortOrder, CreatedAfter=None, NextToken=None):
        if NextToken is None and len(self.executions) > 1:
            return {"PipelineExecutionSummaries": self.executions[:1], "NextToken": "t"}
        return {"PipelineExecutionSummaries": self.executions[1:] if NextToken else self.executions}

    def list_pipeline_execution_steps(self, PipelineExecutionArn, SortOrder, NextToken=None):
        if NextToken is None and len(self.steps) > 1:
            return {"PipelineExecutionSteps": self.steps[:1], "NextToken": "t"}
        return {"PipelineExecutionSteps": self.steps[1:] if NextToken else self.steps}


def feedback_objects(n, start):
    return {f"feedback/2026-10/batch{i // 2}/f{i}.png": (b"x", start + timedelta(hours=i)) for i in range(n)}


STEPS_PASS = [
    {"StepName": "Prepare", "StepStatus": "Succeeded", "Metadata": {"ProcessingJob": {"Arn": "a"}}},
    {"StepName": "Train", "StepStatus": "Succeeded", "Metadata": {"TrainingJob": {"Arn": "b"}}},
    {"StepName": "Evaluate", "StepStatus": "Succeeded", "Metadata": {"ProcessingJob": {"Arn": "c"}}},
    {"StepName": "ModelBetterOrEqual", "StepStatus": "Succeeded", "Metadata": {"Condition": {"Outcome": "True"}}},
    {"StepName": "Register", "StepStatus": "Succeeded", "Metadata": {"ProcessingJob": {"Arn": "d"}}},
]
EVALUATION = dict(candidate=dict(score=0.81, model={"name": "skin"}), baseline=dict(score=0.80, model={"name": "skin v2"}),
                  failed_axes=0, total_axes=21, passed=True, testset=dict(name="aihub_kr", people=165),
                  axes=[dict(task="pore_grade", metric="qwk", passed=True)])
REGISTER = dict(version="skin_v3", s3_uri="s3://bucket/models/skin_v3/", model_name="skin v3",
                model_package_arn="arn:aws:sagemaker:us-east-1:000000000000:model-package/seoulection-skin/1")


def artifacts(evaluation=EVALUATION, register=REGISTER):
    base = f"{SETTINGS['artifacts_prefix']}/exec123/"
    objs = {base + prefect_flow.EVALUATION_KEY: (json.dumps(evaluation).encode(), T0)}
    if register:
        objs[base + prefect_flow.REGISTER_KEY] = (json.dumps(register).encode(), T0)
    return objs


def active_record(arn=ARN, reserved_ago=timedelta(hours=2), watermark=T0 + timedelta(hours=4), new_files=5, **extra):
    """진행 중 실행 기록 (다른 프로세스가 남긴 것처럼). ARN 이 있으면 started, 없으면 reserved."""
    rec = prefect_flow.new_active_record(T0 - timedelta(days=7), watermark, new_files, None)
    rec.update(state="started" if arn else "reserved", owner="other-host:1",
               reserved=(datetime.now(timezone.utc) - reserved_ago).isoformat(timespec="seconds"),
               execution_arn=arn, started=rec["reserved"] if arn else None, **extra)
    return rec


def encoded(obj):
    return (json.dumps(obj).encode(), T0)


def released(s3):
    """active_run.json 이 해제 묘비(state=released)인가 — 끝난 실행은 지우지 않고 묘비로 남긴다."""
    rec = s3.json(ACTIVE)
    return bool(rec) and rec["state"] == "released"


def call(task, *args, **kwargs):
    """태스크를 Prefect 엔진 없이 원함수로 부른다 (순수 함수 수준 테스트용)."""
    return getattr(task, "fn", task)(*args, **kwargs)


# ---- 신규 데이터 소스 ----

def test_s3_feedback_source_counts_only_files_after_since():
    s3 = FakeS3({**feedback_objects(6, T0 - timedelta(days=10)), "feedback/": (b"", T0), "feedback/2026-10/": (b"", T0),
                 "other/x.png": (b"", T0)})
    src = new_data.make_source("s3_feedback", "s3://bucket/feedback/", client=s3)
    full = src.check(None)
    assert (full.count, full.total) == (6, 6) and full.latest == T0 - timedelta(days=10, hours=-5)
    assert full.by_prefix == {"2026-10": 6}
    part = src.check(T0 - timedelta(days=10, hours=-2))  # f0, f1, f2 는 기준점 이하 → f3~f5 만 신규
    assert (part.count, part.total) == (3, 6) and part.since.tzinfo is not None
    assert part.to_dict()["latest"] == full.latest.isoformat()
    assert src.check(datetime(2030, 1, 1)).count == 0  # naive 는 UTC로


def test_make_source_rejects_unknown_and_recommend_api_is_todo():
    with pytest.raises(ValueError):
        new_data.make_source("nope", "s3://b/f/")
    with pytest.raises(NotImplementedError):
        new_data.make_source("recommend_api", "s3://b/f/")


# ---- 설정 ----

def test_flow_settings_come_from_configs_and_env(monkeypatch):
    monkeypatch.delenv(prefect_flow.MIN_NEW_ENV, raising=False)
    s = prefect_flow.load_flow_settings()
    assert s["cron"] == "0 3 * * 1" and s["timezone"] == "Asia/Seoul"  # 월요일 03:00 KST
    assert s["pipeline"] == "seoulection-skin-retrain" and s["feedback"].startswith("s3://") and s["feedback"].endswith("/feedback/")
    assert s["marker_key"] == f"{s['artifacts_prefix']}/prefect/last_run.json" and s["min_new_files"] == 1
    assert s["active_key"] == f"{s['artifacts_prefix']}/prefect/active_run.json" and s["reserve_stale_seconds"] > 0
    monkeypatch.setenv(prefect_flow.MIN_NEW_ENV, "25")
    assert prefect_flow.load_flow_settings()["min_new_files"] == 25
    assert prefect_flow.FLOW_NAME == s["flow_name"]


def test_parse_time():
    assert prefect_flow.parse_time(None) is None and prefect_flow.parse_time("") is None
    assert prefect_flow.parse_time("2026-10-19T18:00:00Z") == T0
    assert prefect_flow.parse_time("2026-10-20T03:00:00+09:00") == T0
    assert prefect_flow.parse_time("2026-10-19T18:00:00") == T0


# ---- 순수 함수 ----

def test_wait_pipeline_reports_status_changes_and_times_out():
    sm = FakeSageMaker(statuses=["Executing", "Executing", "Succeeded"])
    seen = []
    assert prefect_flow.wait_pipeline(sm, ARN, 0, 60, on_status=seen.append, sleep=lambda s: None) == "Succeeded"
    assert seen == ["Executing", "Succeeded"]
    with pytest.raises(TimeoutError, match="실행은 그대로 둔다"):
        prefect_flow.wait_pipeline(FakeSageMaker(statuses=["Executing"]), ARN, 0, 0, sleep=lambda s: None)


def test_start_pipeline_passes_parameters_token_and_explains_missing_pipeline():
    sm = FakeSageMaker()
    token = "a" * 32
    assert prefect_flow.start_pipeline(sm, "p", {"Epochs": 2, "MinFeedbackCrops": "0"}, display_name="x", client_request_token=token) == ARN
    assert sm.started[0]["PipelineParameters"] == [{"Name": "Epochs", "Value": "2"}, {"Name": "MinFeedbackCrops", "Value": "0"}]
    assert sm.started[0]["ClientRequestToken"] == token and sm.started[0]["PipelineExecutionDisplayName"] == "x"
    prefect_flow.start_pipeline(sm, "p", None)
    assert "PipelineParameters" not in sm.started[1] and "ClientRequestToken" not in sm.started[1]
    assert sm.started[1]["PipelineExecutionDisplayName"].startswith("prefect-")
    # 같은 토큰으로 다시 부르면 같은 ARN, 실행은 늘지 않는다 (대역이 SageMaker 멱등성을 흉내낸다)
    assert prefect_flow.start_pipeline(sm, "p", {"Epochs": 2, "MinFeedbackCrops": "0"}, display_name="x", client_request_token=token) == ARN
    assert len(sm.started) == 3 and sm.created == 2 and len(sm.executions) == 2
    with pytest.raises(prefect_flow.StartRejected, match="upsert"):
        prefect_flow.start_pipeline(FakeSageMaker(exists=False), "p")
    rejecting = FakeSageMaker()
    rejecting.reject = "ValidationException"
    with pytest.raises(prefect_flow.StartRejected, match="ValidationException"):
        prefect_flow.start_pipeline(rejecting, "p")
    rejecting.reject = "ThrottlingException"                      # 불확실한 오류는 그대로 ClientError
    with pytest.raises(ClientError):
        prefect_flow.start_pipeline(rejecting, "p")
    assert issubclass(prefect_flow.StartRejected, RuntimeError)


def test_active_record_is_valid_token_and_display_name():
    rec = prefect_flow.new_active_record(None, T0, 5, {"Epochs": "2"})
    assert 32 <= len(rec["run_id"]) <= 128 and rec["run_id"].isalnum()                       # SageMaker ClientRequestToken 규격
    assert rec["display_name"].startswith("prefect-") and len(rec["display_name"]) <= 82     # PipelineExecutionDisplayName 규격
    assert all(c.isalnum() or c == "-" for c in rec["display_name"]) and rec["run_id"][:8] in rec["display_name"]
    assert rec["state"] == "reserved" and rec["execution_arn"] is None and rec["since"] is None
    assert rec["watermark"] == T0.isoformat() and rec["parameters"] == {"Epochs": "2"}
    assert rec["run_id"] != prefect_flow.new_active_record(None, T0, 5, None)["run_id"]
    assert prefect_flow.is_active(rec) and prefect_flow.owns(rec, rec["run_id"]) and not prefect_flow.owns(rec, "x")
    tomb = prefect_flow.released_record(rec, "Succeeded")
    assert tomb["state"] == "released" and tomb["run_id"] == rec["run_id"] and tomb["status"] == "Succeeded"
    assert not prefect_flow.is_active(tomb) and not prefect_flow.owns(tomb, rec["run_id"])
    assert not prefect_flow.is_active(None) and prefect_flow.is_active({"run_id": "old-format-without-state"})


def test_conditional_write_and_cas_update():
    """write_json_s3: If-None-Match(없을 때만) / If-Match(ETag 그대로일 때만). update_json_s3: 읽기→결정→조건부 쓰기, 충돌 시 재시도."""
    s3 = FakeS3()
    assert prefect_flow.write_json_s3(s3, "b", ACTIVE, {"run_id": "1"}, if_none_match=True) == f"s3://b/{ACTIVE}"
    assert prefect_flow.write_json_s3(s3, "b", ACTIVE, {"run_id": "2"}, if_none_match=True) is False   # 이미 있으면 선점 실패
    obj, etag = prefect_flow.read_json_s3(s3, "b", ACTIVE, with_etag=True)
    assert obj == {"run_id": "1"} and etag == s3.etag(ACTIVE)
    assert prefect_flow.write_json_s3(s3, "b", ACTIVE, {"run_id": "3"}, if_match='"stale"') is False  # 남이 바꾼 뒤면 실패
    assert prefect_flow.write_json_s3(s3, "b", ACTIVE, {"run_id": "3"}, if_match=etag) and s3.json(ACTIVE) == {"run_id": "3"}
    assert prefect_flow.write_json_s3(s3, "b", "missing", {"x": 1}, if_match=etag) is False            # 지워진 객체에 If-Match → 실패
    assert prefect_flow.read_json_s3(s3, "b", "missing", with_etag=True) == (None, None)

    # CAS: 결정 함수가 None 이면 쓰지 않는다
    written, current = prefect_flow.update_json_s3(s3, "b", ACTIVE, lambda cur: None)
    assert written is None and current == {"run_id": "3"} and s3.puts.count(ACTIVE) == 2
    # CAS: 읽은 직후 다른 프로세스가 바꾸면(412) 다시 읽어 그 값 위에서 결정한다
    real_get, hits = s3.get_object, []

    def get_then_other_writes(Bucket, Key):
        resp = real_get(Bucket=Bucket, Key=Key)
        if not hits:
            hits.append(1)
            s3.put_object(Bucket=Bucket, Key=Key, Body=json.dumps({"run_id": "other"}).encode())
        return resp
    s3.get_object = get_then_other_writes
    written, current = prefect_flow.update_json_s3(s3, "b", ACTIVE, lambda cur: dict(cur, seen=cur["run_id"]))
    assert written == {"run_id": "other", "seen": "other"} and current == {"run_id": "other"} and s3.json(ACTIVE) == written
    # 계속 충돌하면 포기한다 (무한 반복 없음)
    s3.get_object = real_get
    s3.put_object = lambda **kw: (_ for _ in ()).throw(s3_error("ConditionalRequestConflict", "PutObject"))
    with pytest.raises(RuntimeError, match="연속 충돌"):
        prefect_flow.update_json_s3(s3, "b", ACTIVE, lambda cur: dict(cur or {}, n=1), attempts=3)


def test_find_execution_by_display_name_and_describe_missing():
    sm = FakeSageMaker(executions=[{"PipelineExecutionArn": "arn-a", "PipelineExecutionDisplayName": "prefect-a"},
                                   {"PipelineExecutionArn": "arn-b", "PipelineExecutionDisplayName": "prefect-b"}])
    assert prefect_flow.find_execution(sm, "p", "prefect-b", created_after=T0) == "arn-b"   # 2페이지 넘어서 찾는다
    assert prefect_flow.find_execution(sm, "p", "prefect-zzz") is None
    sm.missing.add("arn-a")
    assert prefect_flow.describe_status(sm, "arn-a") is None and prefect_flow.describe_status(sm, "arn-b") == "Executing"


def test_merge_marker_never_moves_watermark_backwards():
    newer = {"watermark": (T0 + timedelta(days=3)).isoformat(), "version": "skin_v4"}
    doc = prefect_flow.marker_document(T0, 5, ARN, {"status": "Succeeded", "passed": True, "register": {"version": "skin_v3"}})
    assert prefect_flow.merge_marker(newer, doc) is None                                              # 기존이 더 최근 → 쓰지 않음
    assert prefect_flow.merge_marker({"watermark": (T0 - timedelta(days=3)).isoformat()}, doc) == doc
    assert prefect_flow.merge_marker({"watermark": T0.isoformat()}, doc) == doc                        # 같으면 새 문서(같은 실행의 재마무리)
    assert prefect_flow.merge_marker(None, doc) == doc and prefect_flow.merge_marker({}, doc) == doc
    assert prefect_flow.merge_marker(newer, dict(doc, watermark=None)) is None


def test_summarize_reads_condition_evaluation_and_register():
    s3, sm = FakeS3(artifacts()), FakeSageMaker(steps=STEPS_PASS)
    summary = prefect_flow.summarize_execution(s3, sm, SETTINGS, ARN, "Succeeded")
    assert summary["passed"] is True and summary["register"]["version"] == "skin_v3"
    assert summary["evaluation"]["failed"] == [] and summary["steps"]["Register"] == "Succeeded"
    text = "\n".join(prefect_flow.format_summary(summary))
    assert "통과" in text and "skin_v3" in text and "0.8100" in text and "승인·배포는 사람" in text

    failed_eval = dict(EVALUATION, failed_axes=1, passed=False, axes=[dict(task="pore_grade", metric="mae", passed=False)])
    steps = STEPS_PASS[:3] + [dict(STEPS_PASS[3], Metadata={"Condition": {"Outcome": "False"}})]
    summary = prefect_flow.summarize_execution(FakeS3(artifacts(failed_eval, None)), FakeSageMaker(steps=steps), SETTINGS, ARN, "Succeeded")
    assert summary["passed"] is False and summary["register"] is None and summary["evaluation"]["failed"] == ["pore_grade.mae"]
    assert "불통과 → 등록 없음" in "\n".join(prefect_flow.format_summary(summary))

    broken = [dict(STEPS_PASS[0], StepStatus="Failed", FailureReason="신규 크롭 부족")]
    summary = prefect_flow.summarize_execution(FakeS3(), FakeSageMaker(steps=broken), SETTINGS, ARN, "Failed")
    assert summary["passed"] is None and summary["failures"] == {"Prepare": "신규 크롭 부족"}
    assert "판정 없음" in "\n".join(prefect_flow.format_summary(summary))


# ---- 플로우 ----

@pytest.fixture(scope="module")
def prefect_harness():
    """임시 Prefect 서버 (SQLite). 띄울 수 없으면(샌드박스 소켓 금지) None → 태스크를 .fn 으로 바꿔 돈다."""
    from prefect.testing.utilities import prefect_test_harness

    harness = prefect_test_harness()
    try:
        harness.__enter__()
    except (PermissionError, OSError):
        yield None
        return
    try:
        yield harness
    finally:
        harness.__exit__(None, None, None)


@pytest.fixture
def aws(monkeypatch, prefect_harness):
    """대역 클라이언트 + 설정(dict 공유 — 테스트가 timeout 등을 바꿀 수 있다). 테스트가 s3.objects / sm.statuses 를 채운다."""
    s3, sm, settings = FakeS3(), FakeSageMaker(), dict(SETTINGS)
    monkeypatch.setattr(prefect_flow, "make_clients", lambda region: (s3, sm))
    monkeypatch.setattr(prefect_flow, "load_flow_settings", lambda *a, **k: settings)
    if prefect_harness is None:  # Prefect 엔진 없이: 태스크 → 원함수, 로거 → logging
        for name in dir(prefect_flow):
            obj = getattr(prefect_flow, name)
            if name.endswith("_task") and hasattr(obj, "fn"):
                monkeypatch.setattr(prefect_flow, name, obj.fn)
        monkeypatch.setattr(prefect_flow, "get_run_logger", lambda: logging.getLogger("test-flow"))
    return s3, sm, settings


def run_flow(**kwargs):
    """Prefect 서버가 있으면 플로우로, 없으면(태스크가 .fn 원함수로 바뀐 상태) 플로우 함수 자체를 돈다."""
    if hasattr(prefect_flow.read_marker_task, "fn"):
        return prefect_flow.weekly_retrain(**kwargs)
    return prefect_flow.weekly_retrain.fn(**kwargs)


def test_flow_skips_when_below_minimum(aws):
    s3, sm, _ = aws
    s3.objects.update(feedback_objects(2, T0))
    result = run_flow(execute=True)
    assert result["status"] == "skipped" and result["new_data"]["count"] == 2 and result["min_new_files"] == 3
    assert sm.started == [] and s3.puts == []


def test_flow_dry_run_does_not_start_pipeline(aws):
    s3, sm, _ = aws
    s3.objects.update(feedback_objects(5, T0))
    result = run_flow()  # 기본 execute=False
    assert result["status"] == "dry_run" and result["new_data"]["count"] == 5 and sm.started == [] and s3.puts == []
    assert result["plan"] == ["예약 → 파이프라인 시작 (신규 5개)"] and "active" not in result


def test_flow_uses_marker_as_since_and_force_overrides_minimum(aws):
    s3, sm, _ = aws
    s3.objects.update(feedback_objects(5, T0))
    s3.objects[MARKER] = encoded({"watermark": (T0 + timedelta(hours=3)).isoformat()})
    result = run_flow()
    assert result["status"] == "dry_run" and result["new_data"]["count"] == 1 and result["since"] == (T0 + timedelta(hours=3)).isoformat()
    assert result["plan"] == ["신규 1개 < 최소 3개 → 시작하지 않음"] and s3.puts == []
    assert run_flow(force=True)["plan"] == ["예약 → 파이프라인 시작 (신규 1개 — force)"]
    result = run_flow(force=True, wait=False, execute=True, parameters={"Epochs": "2"})
    assert result["status"] == "started" and result["execution_arn"] == ARN
    assert sm.started[-1]["PipelineParameters"] == [{"Name": "Epochs", "Value": "2"}] and MARKER not in s3.puts  # 기다리지 않으면 기준점도 없음
    active = s3.json(ACTIVE)  # 예약 기록: 토큰·표시 이름이 start 요청과 같고 ARN이 채워져 있다
    assert active["state"] == "started" and active["execution_arn"] == ARN and active["run_id"] == sm.started[-1]["ClientRequestToken"]
    assert active["display_name"] == sm.started[-1]["PipelineExecutionDisplayName"] and active["parameters"] == {"Epochs": "2"}
    assert active["since"] == (T0 + timedelta(hours=3)).isoformat() and active["new_files"] == 1
    s3.delete_object(Bucket="bucket", Key=ACTIVE)   # 사람이 기록을 지운 경우도 다음 예약은 If-None-Match 로 된다
    assert run_flow(since=T0.isoformat())["new_data"]["count"] == 4  # --since 가 marker 보다 우선


def test_flow_executes_waits_logs_and_writes_marker(aws):
    s3, sm, _ = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts()})
    sm.statuses, sm.steps = ["Executing", "Executing", "Succeeded"], STEPS_PASS
    result = run_flow(execute=True)
    assert result["status"] == "Succeeded" and result["passed"] is True and result["previous"] is None
    assert result["register"]["version"] == "skin_v3" and result["evaluation"]["score"] == 0.81
    assert sm.started[0]["PipelineName"] == SETTINGS["pipeline"] and "PipelineParameters" not in sm.started[0]
    assert s3.puts == [ACTIVE, ACTIVE, MARKER, ACTIVE] and s3.deletes == []   # 예약 → ARN 기록 → 기준점 → 해제(묘비)
    tomb = s3.json(ACTIVE)
    assert tomb["state"] == "released" and tomb["execution_arn"] == ARN and tomb["status"] == "Succeeded"
    assert tomb["run_id"] == sm.started[0]["ClientRequestToken"]
    marker = s3.json(MARKER)
    assert marker["watermark"] == (T0 + timedelta(hours=4)).isoformat() and marker["version"] == "skin_v3" and marker["new_files"] == 5
    # 다음 주: 기준점 이후 신규가 없으면 건너뛴다 (묘비는 진행 중 실행이 아니다)
    assert run_flow(execute=True)["status"] == "skipped" and len(sm.started) == 1
    # 다음 예약은 묘비를 If-Match 로 덮어쓴다
    s3.objects.update(feedback_objects(3, T0 + timedelta(days=1)))
    sm.statuses = ["Executing", "Succeeded"]
    assert run_flow(execute=True)["status"] == "Succeeded" and len(sm.started) == 2 and released(s3)


def test_flow_fails_without_marker_when_pipeline_fails(aws):
    s3, sm, _ = aws
    s3.objects.update(feedback_objects(5, T0))
    sm.statuses, sm.steps = ["Executing", "Failed"], [dict(STEPS_PASS[0], StepStatus="Failed", FailureReason="boom")]
    with pytest.raises(RuntimeError, match="Failed"):
        run_flow(execute=True)
    assert MARKER not in s3.puts and len(sm.started) == 1
    assert released(s3) and s3.json(ACTIVE)["status"] == "Failed"  # 실패하면 예약을 해제해 다음 실행이 같은 데이터로 재시도할 수 있다
    sm.statuses = ["Executing", "Succeeded"]
    assert run_flow(execute=True)["status"] == "Succeeded" and len(sm.started) == 2  # 재시도 = 다음 실행에서 1회


# ---- 중복 실행 방지 (리뷰 Phase 4 #1) ----

def test_flow_second_call_while_running_attaches_instead_of_starting(aws):
    """완료 전 중복 호출: --no-wait 로 시작한 뒤 다시 부르면 같은 실행을 이어받고 start 는 1회."""
    s3, sm, _ = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts()})
    sm.statuses, sm.steps = ["Executing"], STEPS_PASS
    first = run_flow(execute=True, wait=False)
    assert first["status"] == "started" and len(sm.started) == 1
    again = run_flow(execute=True, wait=False)            # 아직 실행 중 → 새로 시작하지 않는다
    assert again["status"] == "in_progress" and again["execution_arn"] == ARN and len(sm.started) == 1
    forced = run_flow(execute=True, wait=False, force=True)   # force 도 진행 중 실행을 밀어내지 않는다
    assert forced["status"] == "in_progress" and len(sm.started) == 1
    # --no-wait 후 재호출(대기): 이어받아 마무리 → 기준점 저장·예약 해제 → 신규 0 → skipped
    sm.statuses = ["Executing", "Succeeded"]
    done = run_flow(execute=True)
    assert done["status"] == "skipped" and done["previous"]["status"] == "Succeeded" and done["previous"]["register"]["version"] == "skin_v3"
    assert len(sm.started) == 1 and released(s3)
    marker = s3.json(MARKER)
    assert marker["watermark"] == (T0 + timedelta(hours=4)).isoformat() and marker["new_files"] == 5 and marker["execution_arn"] == ARN
    assert done["new_data"]["count"] == 0 and done["since"] == marker["watermark"]


def test_flow_resumes_after_wait_timeout(aws):
    """대기 시간 초과 → 플로우 실패, 실행·예약은 그대로 → 다음 호출이 이어받아 마무리. start 1회."""
    s3, sm, settings = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts()})
    sm.statuses, sm.steps, settings["timeout_seconds"] = ["Executing"], STEPS_PASS, 0
    with pytest.raises(TimeoutError):
        run_flow(execute=True)
    assert len(sm.started) == 1 and s3.json(ACTIVE)["execution_arn"] == ARN and s3.json(ACTIVE)["state"] == "started" and MARKER not in s3.objects
    settings["timeout_seconds"], sm.statuses = 60, ["Executing", "Succeeded"]
    result = run_flow(execute=True)
    assert result["status"] == "skipped" and result["previous"]["status"] == "Succeeded" and len(sm.started) == 1
    assert s3.json(MARKER)["watermark"] == (T0 + timedelta(hours=4)).isoformat() and released(s3)


def test_flow_recovers_after_restart_with_arn_in_record(aws):
    """프로세스 재시작: 다른 프로세스가 남긴 기록(ARN 있음)을 이어받는다. 끝난 실행은 마무리만 하고 계속."""
    s3, sm, _ = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts(), ACTIVE: encoded(active_record())})
    sm.statuses, sm.steps = ["Succeeded"], STEPS_PASS
    result = run_flow(execute=True)
    assert result["previous"]["status"] == "Succeeded" and sm.started == []
    assert s3.json(MARKER)["watermark"] == (T0 + timedelta(hours=4)).isoformat() and result["status"] == "skipped" and released(s3)


def test_flow_recovers_arn_by_display_name_when_record_has_none(aws):
    """재시작(ARN 기록 전에 죽음): 표시 이름으로 실행을 찾아 기록에 채우고(If-Match) 이어받는다. start 0회."""
    s3, sm, _ = aws
    rec = active_record(arn=None)
    sm.executions = [{"PipelineExecutionArn": "arn-other", "PipelineExecutionDisplayName": "prefect-x"},
                     {"PipelineExecutionArn": ARN, "PipelineExecutionDisplayName": rec["display_name"]}]
    s3.objects.update({**feedback_objects(5, T0), **artifacts(), ACTIVE: encoded(rec)})
    sm.statuses, sm.steps = ["Executing", "Succeeded"], STEPS_PASS
    result = run_flow(execute=True, wait=False)
    assert result["status"] == "in_progress" and result["execution_arn"] == ARN and sm.started == []
    assert s3.json(ACTIVE)["execution_arn"] == ARN and s3.json(ACTIVE)["recovered"] and s3.json(ACTIVE)["state"] == "started"   # 기록 복구
    result = run_flow(execute=True)
    assert result["previous"]["status"] == "Succeeded" and sm.started == [] and released(s3)


def test_flow_handles_reservation_without_execution(aws):
    """예약만 있고 실행이 없을 때: 새 예약이면 다른 프로세스가 시작 중 → 시작 안 함. 오래됐으면 해제하고 새로 시작."""
    s3, sm, _ = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts()})
    sm.steps = STEPS_PASS
    s3.objects[ACTIVE] = encoded(active_record(arn=None, reserved_ago=timedelta(seconds=5)))
    result = run_flow(execute=True)
    assert result["status"] == "in_progress" and result["execution_arn"] is None and sm.started == [] and s3.puts == []
    # 오래된 예약(시작 전에 멈춤·죽음): 새 run_id 가 아니라 **그 예약의 토큰·표시 이름·파라미터 그대로** start 를 다시 보내 실행을 확보하고 이어받는다
    stale = active_record(arn=None, reserved_ago=timedelta(hours=3), parameters={"Epochs": "2"})
    s3.objects[ACTIVE] = encoded(stale)
    sm.statuses = ["Executing", "Succeeded"]
    result = run_flow(execute=True)
    assert result["previous"]["status"] == "Succeeded" and result["status"] == "skipped" and len(sm.started) == 1 and released(s3)
    req = sm.started[0]
    assert req["ClientRequestToken"] == stale["run_id"] and req["PipelineExecutionDisplayName"] == stale["display_name"]
    assert req["PipelineParameters"] == [{"Name": "Epochs", "Value": "2"}]
    tomb = s3.json(ACTIVE)
    assert tomb["run_id"] == stale["run_id"] and tomb["execution_arn"] == ARN and tomb["status"] == "Succeeded"
    assert s3.json(MARKER)["watermark"] == stale["watermark"]                       # 기준점은 그 예약이 셌던 값
    # 기록된 실행이 지워진 경우(확실한 종료)만 해제(abandoned)하고 새로 간다
    s3.objects[ACTIVE] = encoded(active_record(arn="arn-deleted", reserved_ago=timedelta(hours=3)))
    sm.missing.add("arn-deleted")
    assert run_flow(execute=True)["status"] == "skipped" and len(sm.started) == 1 and released(s3)
    assert s3.json(ACTIVE)["status"] == "abandoned" and s3.json(ACTIVE)["execution_arn"] == "arn-deleted"
    # 지워진 실행이라도 예약이 새 것이면 건드리지 않는다
    fresh = active_record(arn="arn-deleted", reserved_ago=timedelta(seconds=5))
    s3.objects[ACTIVE], s3.puts[:] = encoded(fresh), []
    result = run_flow(execute=True)
    assert result["status"] == "in_progress" and result["execution_arn"] == "arn-deleted" and s3.puts == [] and s3.json(ACTIVE) == fresh


def test_flow_previous_failed_run_is_cleared_then_retried_once(aws):
    """이전 실행이 실패로 끝나 있으면: 기록 해제·기준점 유지 → 같은 데이터로 이번 실행에서 1회 재시도."""
    s3, sm, _ = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts(), ACTIVE: encoded(active_record())})
    sm.statuses, sm.steps = ["Failed", "Executing", "Succeeded"], STEPS_PASS
    result = run_flow(execute=True)
    assert result["previous"]["status"] == "Failed" and result["status"] == "Succeeded"
    assert len(sm.started) == 1 and result["new_data"]["count"] == 5          # 기준점이 안 바뀌어 같은 5개를 다시 셌다
    assert s3.puts.count(MARKER) == 1 and released(s3)


def test_flow_does_not_start_when_reservation_is_taken_between_read_and_reserve(aws, monkeypatch):
    """읽을 때는 없었는데 예약 직전에 다른 프로세스가 선점(If-None-Match 412) → 시작하지 않는다."""
    s3, sm, _ = aws
    s3.objects.update(feedback_objects(5, T0))
    real, raced = prefect_flow.write_json_s3, []

    def racing_write(s3_, bucket, key, obj, if_none_match=False, if_match=None):
        if key == ACTIVE and (if_none_match or if_match) and not raced:
            raced.append(1)
            real(s3_, bucket, key, active_record(arn="arn-racer"))   # 다른 프로세스가 먼저
        return real(s3_, bucket, key, obj, if_none_match=if_none_match, if_match=if_match)

    monkeypatch.setattr(prefect_flow, "write_json_s3", racing_write)
    result = run_flow(execute=True)
    assert result["status"] == "in_progress" and result["execution_arn"] == "arn-racer" and sm.started == []


def test_flow_marker_does_not_regress_when_older_run_finishes_later(aws):
    """기준점 보호: 더 최근 기준점이 이미 저장돼 있으면 늦게 끝난 옛 실행이 그 문서를 건드리지 않는다."""
    s3, sm, _ = aws
    newer = {"watermark": (T0 + timedelta(days=10)).isoformat(), "version": "skin_v4"}
    s3.objects.update({**artifacts(), MARKER: encoded(newer), ACTIVE: encoded(active_record(watermark=T0 + timedelta(hours=4)))})
    sm.statuses, sm.steps = ["Succeeded"], STEPS_PASS
    result = run_flow(execute=True)
    assert result["previous"]["status"] == "Succeeded" and s3.json(MARKER) == newer and MARKER not in s3.puts and sm.started == []
    assert released(s3)


# ---- 소유권·원자성 (리뷰 Phase 4 재검토 R1) ----

def test_release_and_record_only_touch_own_record(aws):
    """해제·ARN 기록은 내 run_id 의 살아 있는 기록에만 (If-Match). 남의 기록·묘비는 건드리지 않고 False/현재값."""
    s3, _, settings = aws
    mine, theirs = active_record(), active_record()
    s3.objects[ACTIVE] = encoded(theirs)
    assert call(prefect_flow.release_active_task, settings, mine, "Succeeded") is False and s3.json(ACTIVE) == theirs and s3.puts == []
    # 소유권이 없으면 남의 기록이 아니라 내 기록을 돌려준다 (남의 run_id 를 들고 마무리하면 그 예약을 해제해 버린다)
    assert call(prefect_flow.record_active_task, settings, mine, recovered="x") == dict(mine, recovered="x") and s3.puts == []
    s3.objects[ACTIVE] = encoded(mine)
    assert call(prefect_flow.record_active_task, settings, mine, recovered="x")["recovered"] == "x" and s3.json(ACTIVE)["run_id"] == mine["run_id"]
    assert call(prefect_flow.release_active_task, settings, mine, "Succeeded") is True
    tomb = s3.json(ACTIVE)
    assert tomb["state"] == "released" and tomb["run_id"] == mine["run_id"] and tomb["status"] == "Succeeded" and tomb["execution_arn"] == ARN
    assert call(prefect_flow.release_active_task, settings, mine, "Succeeded") is False and s3.json(ACTIVE) == tomb   # 두 번째 해제는 무시
    s3.delete_object(Bucket="bucket", Key=ACTIVE)
    assert call(prefect_flow.release_active_task, settings, mine, "Succeeded") is False and ACTIVE not in s3.objects  # 지워졌으면 만들지 않음


def test_reserve_over_released_record_is_atomic(aws):
    """묘비 위 예약은 그 ETag 로 If-Match: 같은 묘비를 읽은 둘 중 하나만 성공한다."""
    s3, _, settings = aws
    s3.objects[ACTIVE] = encoded(prefect_flow.released_record(active_record(), "Succeeded"))
    first, second = (prefect_flow.new_active_record(None, T0, 3, None) for _ in range(2))
    real_get, raced = s3.get_object, []

    def get_then_first_reserves(Bucket, Key):
        resp = real_get(Bucket=Bucket, Key=Key)
        if Key == ACTIVE and not raced:
            raced.append(1)
            s3.put_object(Bucket=Bucket, Key=Key, Body=json.dumps(first).encode())   # 다른 프로세스가 같은 묘비 위에 먼저 예약
        return resp
    s3.get_object = get_then_first_reserves
    assert call(prefect_flow.reserve_run_task, settings, second) is False          # 내 If-Match 는 412 → 다시 읽으니 살아 있는 기록
    assert s3.json(ACTIVE)["run_id"] == first["run_id"] and s3.puts.count(ACTIVE) == 1
    s3.get_object = real_get
    assert call(prefect_flow.reserve_run_task, settings, second) is False          # 살아 있는 기록 위에는 예약 못 함
    s3.objects[ACTIVE] = encoded(prefect_flow.released_record(first, "Failed"))
    assert call(prefect_flow.reserve_run_task, settings, second) is True and s3.json(ACTIVE)["run_id"] == second["run_id"]


def test_write_marker_retries_on_conflict_and_stays_monotonic(aws):
    """기준점 CAS: 읽은 뒤 다른 프로세스가 더 최근 기준점을 쓰면 내 쓰기는 412 → 다시 읽어 더 최근 값을 유지한다."""
    s3, _, settings = aws
    s3.objects[MARKER] = encoded({"watermark": T0.isoformat(), "version": "skin_v2"})
    newer = {"watermark": (T0 + timedelta(days=3)).isoformat(), "version": "skin_v4"}
    doc = prefect_flow.marker_document(T0 + timedelta(hours=4), 5, ARN, {"status": "Succeeded", "passed": True, "register": {"version": "skin_v3"}})
    real_get, raced = s3.get_object, []

    def get_then_other_writes(Bucket, Key):
        resp = real_get(Bucket=Bucket, Key=Key)
        if Key == MARKER and not raced:
            raced.append(1)
            s3.put_object(Bucket=Bucket, Key=Key, Body=json.dumps(newer).encode())
        return resp
    s3.get_object = get_then_other_writes
    uri, current = call(prefect_flow.write_marker_task, settings, doc)
    assert uri is None and current == newer and s3.json(MARKER) == newer and s3.puts.count(MARKER) == 1   # 내 쓰기 없음
    # 반대로 다른 프로세스가 더 옛 기준점을 썼으면 내 값이 이긴다 (재시도 후 성공)
    raced.clear()
    s3.objects[MARKER] = encoded({"watermark": T0.isoformat(), "version": "skin_v2"})
    newer.update(watermark=(T0 - timedelta(days=3)).isoformat())
    uri, current = call(prefect_flow.write_marker_task, settings, doc)
    assert uri == f"s3://bucket/{MARKER}" and current == doc and s3.json(MARKER)["watermark"] == doc["watermark"]
    assert raced and s3.puts.count(MARKER) == 3   # 남의 쓰기(옛 값) → 내 412 → 재시도 성공


def test_flow_late_finisher_does_not_release_next_reservation(aws, monkeypatch):
    """교차 실행(R1): A·B 가 같은 끝난 실행 X 를 이어받는다. B 가 먼저 마무리·해제하고 새 실행 Y 를 예약·시작한 뒤
    A 가 늦게 마무리해도 Y 의 예약은 그대로다 → C 는 Y 를 이어받고 start 는 Y 1회뿐."""
    s3, sm, _ = aws
    x = active_record(watermark=T0 + timedelta(hours=1), new_files=2)   # X 기준점 T0+1h → 마무리 뒤 신규 f2~f4 = 3개 ≥ 최소 3
    s3.objects.update({**feedback_objects(5, T0), **artifacts(), ACTIVE: encoded(x)})
    sm.statuses, sm.steps = ["Succeeded", "Succeeded", "Executing"], STEPS_PASS   # A·B 의 X describe, 그 뒤 Y 는 Executing
    real_summarize, state = prefect_flow.summarize_task, {}

    def summarize_then_b_finishes(settings, arn, status):
        if "b" not in state:                      # A 가 X 를 요약하는 사이 B 가 X 마무리 → 해제 → 신규 3개 → Y 예약·시작
            state["b"] = None
            state["b"] = run_flow(execute=True, wait=False)
        return real_summarize(settings, arn, status)
    monkeypatch.setattr(prefect_flow, "summarize_task", summarize_then_b_finishes)

    a = run_flow(execute=True, wait=False)
    b = state["b"]
    assert b["previous"]["status"] == "Succeeded" and b["status"] == "started" and len(sm.started) == 1
    y_arn, y_token = b["execution_arn"], sm.started[0]["ClientRequestToken"]
    active = s3.json(ACTIVE)
    assert active["state"] == "started" and active["run_id"] == y_token and active["execution_arn"] == y_arn   # A 가 Y 예약을 지우지 않았다
    assert a["previous"]["status"] == "Succeeded" and a["status"] == "in_progress" and a["execution_arn"] == y_arn
    assert s3.json(MARKER)["watermark"] == (T0 + timedelta(hours=1)).isoformat()   # X 기준점 (Y 는 아직 안 끝남)
    c = run_flow(execute=True, wait=False)
    assert c["status"] == "in_progress" and c["execution_arn"] == y_arn and len(sm.started) == 1
    assert s3.json(ACTIVE)["run_id"] == y_token


# ---- 예약 복구는 같은 토큰으로 (리뷰 Phase 4 재검토 R3) ----

def test_stale_reservation_is_adopted_with_same_token_and_resumed_owner_gets_same_execution(aws):
    """리뷰 R3 재현: A 예약(ARN 없음) → A 멈춤(10분 초과) → B 가 복구 → A 재개해 자기 토큰으로 start.
    B 는 새 run_id 를 만들지 않고 A 의 토큰·표시 이름·파라미터 그대로 start 하므로 A 의 start 도 같은 실행을 받는다 → 실행 1개, 기록 ARN 일치."""
    s3, sm, settings = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts()})
    sm.statuses, sm.steps = ["Executing"], STEPS_PASS
    old = active_record(arn=None, reserved_ago=timedelta(hours=3), parameters={"Epochs": "2"})   # A 의 예약
    assert call(prefect_flow.reserve_run_task, settings, old) is True
    b = run_flow(execute=True, wait=False)                                      # B: 오래된 예약 → 같은 토큰으로 start → 이어받음
    assert b["status"] == "in_progress" and b["execution_arn"] == ARN and len(sm.started) == 1 and sm.created == 1
    rec = s3.json(ACTIVE)
    assert rec["run_id"] == old["run_id"] and rec["execution_arn"] == ARN and rec["state"] == "started" and rec["adopted_by"]
    a_arn = call(prefect_flow.start_pipeline_task, settings, old)               # A 재개: 자기(같은) 토큰으로 start
    assert a_arn == ARN and len(sm.started) == 2 and sm.created == 1 and len(sm.executions) == 1
    assert sm.started[0]["ClientRequestToken"] == sm.started[1]["ClientRequestToken"] == old["run_id"]
    assert sm.started[0]["PipelineParameters"] == sm.started[1]["PipelineParameters"] == [{"Name": "Epochs", "Value": "2"}]
    assert sm.started[0]["PipelineExecutionDisplayName"] == sm.started[1]["PipelineExecutionDisplayName"] == old["display_name"]
    rec = s3.json(ACTIVE)
    assert rec["execution_arn"] == ARN and rec["run_id"] == old["run_id"] and rec["state"] == "started"   # A 의 실행도 기록된 그 ARN
    sm.statuses = ["Executing", "Succeeded"]                                    # 마무리: 어느 호출이 끝내도 실행은 1개
    done = run_flow(execute=True)
    assert done["previous"]["status"] == "Succeeded" and done["status"] == "skipped" and sm.created == 1 and released(s3)
    assert s3.json(ACTIVE)["run_id"] == old["run_id"] and s3.json(MARKER)["watermark"] == old["watermark"]


def test_resumed_owner_after_delayed_start_response_joins_same_execution(aws):
    """A 의 start 요청은 SageMaker 에 닿아 실행이 생겼지만 응답이 늦어 ARN 을 기록하지 못한 상태.
    B 는 새 예약이라도 표시 이름으로 실행을 찾아 이어받고(start 없음), 뒤늦게 A 가 start 결과를 처리해도 같은 ARN 이다."""
    s3, sm, settings = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts()})
    sm.statuses, sm.steps = ["Executing"], STEPS_PASS
    rec = active_record(arn=None, reserved_ago=timedelta(minutes=1))
    assert call(prefect_flow.reserve_run_task, settings, rec) is True
    sm.start_pipeline_execution(PipelineName=settings["pipeline"], PipelineExecutionDisplayName=rec["display_name"],
                                ClientRequestToken=rec["run_id"])                # A 의 요청이 SageMaker 에 닿음
    b = run_flow(execute=True, wait=False)
    assert b["status"] == "in_progress" and b["execution_arn"] == ARN and len(sm.started) == 1 and sm.created == 1
    assert s3.json(ACTIVE)["execution_arn"] == ARN and s3.json(ACTIVE)["recovered"] and s3.json(ACTIVE)["state"] == "started"
    assert call(prefect_flow.start_pipeline_task, settings, rec) == ARN            # A 재개(같은 요청 재전송) → 같은 ARN
    assert len(sm.started) == 2 and sm.created == 1
    after = s3.json(ACTIVE)
    assert after["execution_arn"] == ARN and after["run_id"] == rec["run_id"] and after["recovered"] and after["state"] == "started"


def test_recovery_while_start_response_is_in_flight_keeps_single_execution(aws, monkeypatch):
    """B 의 start 요청이 처리된 뒤 응답이 오기 전에 C 가 돈다: C 는 ARN 없는 예약을 표시 이름으로 찾아 기록하고,
    B 의 늦은 ARN 기록은 같은 run_id 라 그 위에 쓴다 → 실행 1개, 같은 ARN."""
    s3, sm, _ = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts()})
    sm.statuses, sm.steps = ["Executing"], STEPS_PASS
    monkeypatch.setattr(prefect_flow, "start_pipeline_task", getattr(prefect_flow.start_pipeline_task, "fn", prefect_flow.start_pipeline_task))
    real_start, state = prefect_flow.start_pipeline, {}

    def start_then_c_runs(sm_, name, parameters=None, display_name=None, client_request_token=None):
        arn = real_start(sm_, name, parameters, display_name=display_name, client_request_token=client_request_token)
        if "c" not in state:
            state["c"] = None
            state["c"] = run_flow(execute=True, wait=False)
        return arn
    monkeypatch.setattr(prefect_flow, "start_pipeline", start_then_c_runs)
    b = run_flow(execute=True, wait=False)
    c = state["c"]
    assert b["status"] == "started" and c["status"] == "in_progress" and b["execution_arn"] == c["execution_arn"] == ARN
    assert len(sm.started) == 1 and sm.created == 1
    rec = s3.json(ACTIVE)
    assert rec["execution_arn"] == ARN and rec["state"] == "started" and rec["run_id"] == sm.started[0]["ClientRequestToken"] and rec["recovered"]


def test_rejected_start_releases_only_unstarted_own_reservation(aws):
    """확실한 거절(ValidationException 등)만 예약을 rejected 로 해제 → 다음 실행은 새 run_id. 불확실한 오류(스로틀링)는 예약 유지 →
    다음 실행이 같은 토큰으로 다시 시도. 이미 ARN 이 기록된(시작된) 기록은 거절이 와도 건드리지 않는다."""
    s3, sm, settings = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts()})
    sm.steps = STEPS_PASS
    sm.reject = "ValidationException"                                          # 1) 본 경로: 거절 → 해제(rejected) + 플로우 실패, 실행 0
    with pytest.raises(prefect_flow.StartRejected, match="ValidationException"):
        run_flow(execute=True)
    tomb = s3.json(ACTIVE)
    assert tomb["state"] == "released" and tomb["status"] == "rejected" and tomb["execution_arn"] is None and sm.created == 0
    sm.reject, sm.statuses = None, ["Executing", "Succeeded"]                  # 2) 다음 실행은 새 run_id
    assert run_flow(execute=True)["status"] == "Succeeded" and sm.created == 1 and sm.started[0]["ClientRequestToken"] != tomb["run_id"]
    s3.objects.update(feedback_objects(3, T0 + timedelta(days=1)))
    sm.reject = "ThrottlingException"                                           # 3) 불확실한 오류 → 예약 그대로
    with pytest.raises(ClientError):
        run_flow(execute=True)
    kept = s3.json(ACTIVE)
    assert kept["state"] == "reserved" and kept["execution_arn"] is None and sm.created == 1
    stale_kept = dict(kept, reserved=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"))
    s3.objects[ACTIVE], sm.reject, sm.statuses = encoded(stale_kept), None, ["Executing", "Succeeded"]
    result = run_flow(execute=True)                                             # 4) 그 예약을 같은 토큰으로 이어받는다
    assert result["previous"]["status"] == "Succeeded" and sm.started[-1]["ClientRequestToken"] == kept["run_id"] and sm.created == 2
    assert s3.json(ACTIVE)["run_id"] == kept["run_id"] and released(s3)
    started = active_record()                                                   # 5) 이미 시작된(ARN 있는) 기록은 거절이 와도 그대로
    s3.objects[ACTIVE], sm.reject = encoded(started), "ValidationException"
    with pytest.raises(prefect_flow.StartRejected):
        call(prefect_flow.start_pipeline_task, settings, started)
    assert s3.json(ACTIVE) == started
    stale = active_record(arn=None, reserved_ago=timedelta(hours=3))           # 6) ARN 없는 오래된 예약 복구 중 거절 → 그 예약만 rejected
    s3.objects[ACTIVE] = encoded(stale)
    with pytest.raises(prefect_flow.StartRejected):
        run_flow(execute=True)
    assert s3.json(ACTIVE)["status"] == "rejected" and s3.json(ACTIVE)["run_id"] == stale["run_id"] and sm.created == 2


# ---- dry-run 은 읽기만 (리뷰 Phase 4 재검토 R2) ----

def test_dry_run_is_read_only_for_every_active_state(aws, monkeypatch):
    """execute=False: 진행 중 / 끝남 / ARN 없는 예약(복구 가능·불가) 어느 경우에도 S3 쓰기·삭제·대기·시작이 없다."""
    s3, sm, _ = aws
    monkeypatch.setattr(prefect_flow, "wait_pipeline", lambda *a, **k: pytest.fail("dry-run 이 완료를 기다렸다"))
    s3.objects.update({**feedback_objects(5, T0), **artifacts()})
    sm.steps = STEPS_PASS

    def read_only(result):
        assert result["status"] == "dry_run" and s3.puts == [] and s3.deletes == [] and sm.started == [] and MARKER not in s3.objects
        return result

    # 진행 중 (기본 wait=True 여도 기다리지 않는다)
    running = active_record()
    s3.objects[ACTIVE], sm.statuses = encoded(running), ["Executing"]
    r = read_only(run_flow())
    assert r["active"]["execution_status"] == "Executing" and r["active"]["run_id"] == running["run_id"]
    assert r["plan"][0].startswith("진행 중 실행") and r["plan"][-1] == "위 처리 뒤 신규 데이터가 기준 이상이면 예약 → 시작"
    # 끝났지만 마무리 안 됨 → 기준점 저장·해제를 하지 않는다
    sm.statuses = ["Succeeded"]
    r = read_only(run_flow())
    assert r["active"]["execution_status"] == "Succeeded" and r["plan"][0].startswith("끝난 실행") and "기준점 저장" in r["plan"][0]
    assert s3.json(ACTIVE) == running
    # ARN 없는 오래된 예약, 실행도 못 찾음 → 같은 토큰으로 start 를 다시 보내지 않는다 (계획에만 적는다)
    stale = active_record(arn=None, reserved_ago=timedelta(hours=3))
    s3.objects[ACTIVE] = encoded(stale)
    r = read_only(run_flow())
    assert r["active"]["execution_status"] is None and r["plan"][0].startswith("실행 없는 예약") and "멱등" in r["plan"][0] and s3.json(ACTIVE) == stale
    # 기록된 실행이 지워짐 → 해제하지 않는다
    gone = active_record(arn="arn-deleted", reserved_ago=timedelta(hours=3))
    sm.missing.add("arn-deleted")
    s3.objects[ACTIVE] = encoded(gone)
    r = read_only(run_flow())
    assert r["active"]["execution_status"] is None and r["active"]["execution_arn"] == "arn-deleted"
    assert r["plan"][0].startswith("기록된 실행 arn-deleted 이 없다") and s3.json(ACTIVE) == gone
    # ARN 없지만 표시 이름으로 찾을 수 있음 → 결과에는 보여 주되 기록에 쓰지 않는다
    rec = active_record(arn=None)
    sm.executions, sm.statuses = [{"PipelineExecutionArn": ARN, "PipelineExecutionDisplayName": rec["display_name"]}], ["Executing"]
    s3.objects[ACTIVE] = encoded(rec)
    r = read_only(run_flow())
    assert r["active"]["execution_arn"] == ARN and s3.json(ACTIVE) == rec and s3.json(ACTIVE)["execution_arn"] is None
    # 묘비 + 기준 미달: active 없음, 계획은 "시작하지 않음"
    s3.objects[ACTIVE] = encoded(prefect_flow.released_record(rec, "Succeeded"))
    s3.objects[MARKER] = encoded({"watermark": (T0 + timedelta(hours=3)).isoformat()})
    s3.puts.clear()
    r = run_flow()
    assert r["status"] == "dry_run" and "active" not in r and r["plan"] == ["신규 1개 < 최소 3개 → 시작하지 않음"] and s3.puts == []


# ---- CLI check ----

def test_check_command_reports_marker_and_active(aws, capsys):
    s3, sm, settings = aws
    s3.objects.update({**feedback_objects(5, T0), ACTIVE: encoded(active_record())})
    assert prefect_flow.check_command(settings, None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 5 and out["enough"] is True and out["active"]["execution_arn"] == ARN and out["marker"] is None
    assert out["last_released"] is None
    s3.objects[ACTIVE] = encoded(prefect_flow.released_record(active_record(), "Succeeded"))
    assert prefect_flow.check_command(settings, "2030-01-01T00:00:00Z") == 3
    out = json.loads(capsys.readouterr().out)
    assert out["active"] is None and out["last_released"]["status"] == "Succeeded" and s3.puts == []
