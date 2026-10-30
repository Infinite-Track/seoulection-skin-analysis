"""Prefect 주간 재학습 플로우 검증 — AWS 없이 (boto3 클라이언트 대역).

플로우 테스트는 Prefect 임시 서버(prefect_test_harness)로 돈다. 서버를 띄울 수 없는 환경(소켓 bind 금지 샌드박스)에서는
태스크를 .fn 으로 바꿔 Prefect 엔진 없이 같은 플로우 함수를 돈다 — 플로우 로직은 같은 코드이고 Prefect 통합만 빠진다.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from pipelines import new_data, prefect_flow

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


class FakeS3:
    """키 → (바이트, LastModified). list_objects_v2 페이지네이터·get_object·put_object(IfNoneMatch)·delete_object만."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts, self.deletes = [], []

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
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": Key}}, "GetObject")

        class Body:
            def read(_self):
                return self.objects[Key][0]
        return {"Body": Body()}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, **kw):
        if IfNoneMatch == "*" and Key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": Key}}, "PutObject")
        self.objects[Key] = (Body, datetime.now(timezone.utc))
        self.puts.append(Key)

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        self.deletes.append(Key)

    def json(self, key):
        return json.loads(self.objects[key][0]) if key in self.objects else None


class FakeSageMaker:
    """statuses 는 describe 마다 앞에서 하나씩 소비(마지막 값은 유지). started 는 start 요청 목록, executions 는 list 결과."""

    def __init__(self, statuses=("Executing", "Executing", "Succeeded"), steps=(), exists=True, executions=()):
        self.statuses, self.steps, self.exists = list(statuses), list(steps), exists
        self.started, self.executions, self.missing = [], list(executions), set()

    def start_pipeline_execution(self, **request):
        if not self.exists:
            raise ClientError({"Error": {"Code": "ResourceNotFound", "Message": "no pipeline"}}, "StartPipelineExecution")
        self.started.append(request)
        arn = ARN if len(self.started) == 1 else f"{ARN}-{len(self.started)}"
        self.executions.insert(0, {"PipelineExecutionArn": arn, "PipelineExecutionDisplayName": request.get("PipelineExecutionDisplayName"),
                                   "PipelineExecutionStatus": "Executing"})
        return {"PipelineExecutionArn": arn}

    def describe_pipeline_execution(self, PipelineExecutionArn):
        if PipelineExecutionArn in self.missing:
            raise ClientError({"Error": {"Code": "ResourceNotFound", "Message": "gone"}}, "DescribePipelineExecution")
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
    """진행 중 실행 기록 (다른 프로세스가 남긴 것처럼)."""
    rec = prefect_flow.new_active_record(T0 - timedelta(days=7), watermark, new_files, None)
    rec.update(reserved=(datetime.now(timezone.utc) - reserved_ago).isoformat(timespec="seconds"), owner="other-host:1",
               execution_arn=arn, started=rec["reserved"] if arn else None, **extra)
    return rec


def encoded(obj):
    return (json.dumps(obj).encode(), T0)


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
    with pytest.raises(RuntimeError, match="upsert"):
        prefect_flow.start_pipeline(FakeSageMaker(exists=False), "p")


def test_active_record_is_valid_token_and_display_name():
    rec = prefect_flow.new_active_record(None, T0, 5, {"Epochs": "2"})
    assert 32 <= len(rec["run_id"]) <= 128 and rec["run_id"].isalnum()                       # SageMaker ClientRequestToken 규격
    assert rec["display_name"].startswith("prefect-") and len(rec["display_name"]) <= 82     # PipelineExecutionDisplayName 규격
    assert all(c.isalnum() or c == "-" for c in rec["display_name"]) and rec["run_id"][:8] in rec["display_name"]
    assert rec["execution_arn"] is None and rec["since"] is None and rec["watermark"] == T0.isoformat() and rec["parameters"] == {"Epochs": "2"}
    assert rec["run_id"] != prefect_flow.new_active_record(None, T0, 5, None)["run_id"]


def test_reserve_is_atomic_and_release_deletes():
    s3 = FakeS3()
    assert prefect_flow.write_json_s3(s3, "b", ACTIVE, {"run_id": "1"}, if_none_match=True) == f"s3://b/{ACTIVE}"
    assert prefect_flow.write_json_s3(s3, "b", ACTIVE, {"run_id": "2"}, if_none_match=True) is False   # 이미 있으면 선점 실패
    assert s3.json(ACTIVE) == {"run_id": "1"}
    prefect_flow.delete_json_s3(s3, "b", ACTIVE)
    assert prefect_flow.read_json_s3(s3, "b", ACTIVE) is None
    assert prefect_flow.write_json_s3(s3, "b", ACTIVE, {"run_id": "3"}, if_none_match=True)


def test_find_execution_by_display_name_and_describe_missing():
    sm = FakeSageMaker(executions=[{"PipelineExecutionArn": "arn-a", "PipelineExecutionDisplayName": "prefect-a"},
                                   {"PipelineExecutionArn": "arn-b", "PipelineExecutionDisplayName": "prefect-b"}])
    assert prefect_flow.find_execution(sm, "p", "prefect-b", created_after=T0) == "arn-b"   # 2페이지 넘어서 찾는다
    assert prefect_flow.find_execution(sm, "p", "prefect-zzz") is None
    sm.missing.add("arn-a")
    assert prefect_flow.describe_status(sm, "arn-a") is None and prefect_flow.describe_status(sm, "arn-b") == "Executing"


def test_merge_marker_never_moves_watermark_backwards():
    newer = {"watermark": (T0 + timedelta(days=3)).isoformat()}
    doc = prefect_flow.marker_document(T0, 5, ARN, {"status": "Succeeded", "passed": True, "register": {"version": "skin_v3"}})
    merged = prefect_flow.merge_marker(newer, doc)
    assert merged["watermark"] == newer["watermark"] and merged["watermark_kept"] is True and merged["version"] == "skin_v3"
    assert prefect_flow.merge_marker({"watermark": (T0 - timedelta(days=3)).isoformat()}, doc) == doc
    assert prefect_flow.merge_marker(None, doc) == doc
    assert prefect_flow.merge_marker(newer, dict(doc, watermark=None))["watermark"] == newer["watermark"]


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


def test_flow_uses_marker_as_since_and_force_overrides_minimum(aws):
    s3, sm, _ = aws
    s3.objects.update(feedback_objects(5, T0))
    s3.objects[MARKER] = encoded({"watermark": (T0 + timedelta(hours=3)).isoformat()})
    result = run_flow()
    assert result["status"] == "skipped" and result["new_data"]["count"] == 1 and result["since"] == (T0 + timedelta(hours=3)).isoformat()
    result = run_flow(force=True, wait=False, execute=True, parameters={"Epochs": "2"})
    assert result["status"] == "started" and result["execution_arn"] == ARN
    assert sm.started[-1]["PipelineParameters"] == [{"Name": "Epochs", "Value": "2"}] and MARKER not in s3.puts  # 기다리지 않으면 기준점도 없음
    active = s3.json(ACTIVE)  # 예약 기록: 토큰·표시 이름이 start 요청과 같고 ARN이 채워져 있다
    assert active["execution_arn"] == ARN and active["run_id"] == sm.started[-1]["ClientRequestToken"]
    assert active["display_name"] == sm.started[-1]["PipelineExecutionDisplayName"] and active["parameters"] == {"Epochs": "2"}
    assert active["since"] == (T0 + timedelta(hours=3)).isoformat() and active["new_files"] == 1
    s3.delete_object(Bucket="bucket", Key=ACTIVE)
    assert run_flow(since=T0.isoformat())["new_data"]["count"] == 4  # --since 가 marker 보다 우선


def test_flow_executes_waits_logs_and_writes_marker(aws):
    s3, sm, _ = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts()})
    sm.statuses, sm.steps = ["Executing", "Executing", "Succeeded"], STEPS_PASS
    result = run_flow(execute=True)
    assert result["status"] == "Succeeded" and result["passed"] is True and result["previous"] is None
    assert result["register"]["version"] == "skin_v3" and result["evaluation"]["score"] == 0.81
    assert sm.started[0]["PipelineName"] == SETTINGS["pipeline"] and "PipelineParameters" not in sm.started[0]
    assert s3.puts == [ACTIVE, ACTIVE, MARKER] and s3.deletes == [ACTIVE] and ACTIVE not in s3.objects  # 예약 → ARN 기록 → 기준점 → 해제
    marker = s3.json(MARKER)
    assert marker["watermark"] == (T0 + timedelta(hours=4)).isoformat() and marker["version"] == "skin_v3" and marker["new_files"] == 5
    # 다음 주: 기준점 이후 신규가 없으면 건너뛴다
    assert run_flow(execute=True)["status"] == "skipped" and len(sm.started) == 1


def test_flow_fails_without_marker_when_pipeline_fails(aws):
    s3, sm, _ = aws
    s3.objects.update(feedback_objects(5, T0))
    sm.statuses, sm.steps = ["Executing", "Failed"], [dict(STEPS_PASS[0], StepStatus="Failed", FailureReason="boom")]
    with pytest.raises(RuntimeError, match="Failed"):
        run_flow(execute=True)
    assert MARKER not in s3.puts and len(sm.started) == 1
    assert ACTIVE not in s3.objects  # 실패하면 예약을 지워 다음 실행이 같은 데이터로 재시도할 수 있다
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
    assert len(sm.started) == 1 and ACTIVE not in s3.objects
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
    assert len(sm.started) == 1 and s3.json(ACTIVE)["execution_arn"] == ARN and MARKER not in s3.objects
    settings["timeout_seconds"], sm.statuses = 60, ["Executing", "Succeeded"]
    result = run_flow(execute=True)
    assert result["status"] == "skipped" and result["previous"]["status"] == "Succeeded" and len(sm.started) == 1
    assert s3.json(MARKER)["watermark"] == (T0 + timedelta(hours=4)).isoformat() and ACTIVE not in s3.objects


def test_flow_recovers_after_restart_with_arn_in_record(aws):
    """프로세스 재시작: 다른 프로세스가 남긴 기록(ARN 있음)을 이어받는다. 끝난 실행은 마무리만 하고 계속."""
    s3, sm, _ = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts(), ACTIVE: encoded(active_record())})
    sm.statuses, sm.steps = ["Succeeded"], STEPS_PASS
    result = run_flow(execute=True)
    assert result["previous"]["status"] == "Succeeded" and sm.started == []
    assert s3.json(MARKER)["watermark"] == (T0 + timedelta(hours=4)).isoformat() and result["status"] == "skipped"


def test_flow_recovers_arn_by_display_name_when_record_has_none(aws):
    """재시작(ARN 기록 전에 죽음): 표시 이름으로 실행을 찾아 기록에 채우고 이어받는다. start 0회."""
    s3, sm, _ = aws
    rec = active_record(arn=None)
    sm.executions = [{"PipelineExecutionArn": "arn-other", "PipelineExecutionDisplayName": "prefect-x"},
                     {"PipelineExecutionArn": ARN, "PipelineExecutionDisplayName": rec["display_name"]}]
    s3.objects.update({**feedback_objects(5, T0), **artifacts(), ACTIVE: encoded(rec)})
    sm.statuses, sm.steps = ["Executing", "Succeeded"], STEPS_PASS
    result = run_flow(execute=True, wait=False)
    assert result["status"] == "in_progress" and result["execution_arn"] == ARN and sm.started == []
    assert s3.json(ACTIVE)["execution_arn"] == ARN and s3.json(ACTIVE)["recovered"]      # 기록 복구
    result = run_flow(execute=True)
    assert result["previous"]["status"] == "Succeeded" and sm.started == [] and ACTIVE not in s3.objects


def test_flow_handles_reservation_without_execution(aws):
    """예약만 있고 실행이 없을 때: 새 예약이면 다른 프로세스가 시작 중 → 시작 안 함. 오래됐으면 지우고 새로 시작."""
    s3, sm, _ = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts()})
    sm.steps = STEPS_PASS
    s3.objects[ACTIVE] = encoded(active_record(arn=None, reserved_ago=timedelta(seconds=5)))
    result = run_flow(execute=True)
    assert result["status"] == "in_progress" and result["execution_arn"] is None and sm.started == []
    s3.objects[ACTIVE] = encoded(active_record(arn=None, reserved_ago=timedelta(hours=3)))   # 시작 전에 죽은 예약
    sm.statuses = ["Executing", "Succeeded"]
    result = run_flow(execute=True)
    assert result["status"] == "Succeeded" and len(sm.started) == 1 and ACTIVE not in s3.objects
    # 기록의 실행이 지워진 경우도 같다
    s3.objects[ACTIVE] = encoded(active_record(arn="arn-deleted", reserved_ago=timedelta(hours=3)))
    sm.missing.add("arn-deleted")
    assert run_flow(execute=True)["status"] == "skipped" and len(sm.started) == 1 and ACTIVE not in s3.objects


def test_flow_previous_failed_run_is_cleared_then_retried_once(aws):
    """이전 실행이 실패로 끝나 있으면: 기록 삭제·기준점 유지 → 같은 데이터로 이번 실행에서 1회 재시도."""
    s3, sm, _ = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts(), ACTIVE: encoded(active_record())})
    sm.statuses, sm.steps = ["Failed", "Executing", "Succeeded"], STEPS_PASS
    result = run_flow(execute=True)
    assert result["previous"]["status"] == "Failed" and result["status"] == "Succeeded"
    assert len(sm.started) == 1 and result["new_data"]["count"] == 5          # 기준점이 안 바뀌어 같은 5개를 다시 셌다
    assert s3.puts.count(MARKER) == 1 and ACTIVE not in s3.objects


def test_flow_does_not_start_when_reservation_is_taken_between_read_and_reserve(aws, monkeypatch):
    """읽을 때는 없었는데 예약 직전에 다른 프로세스가 선점(If-None-Match 412) → 시작하지 않는다."""
    s3, sm, _ = aws
    s3.objects.update(feedback_objects(5, T0))
    real = prefect_flow.write_json_s3

    def racing_write(s3_, bucket, key, obj, if_none_match=False):
        if if_none_match and key == ACTIVE:
            real(s3_, bucket, key, active_record(arn="arn-racer"), if_none_match=True)   # 다른 프로세스가 먼저
        return real(s3_, bucket, key, obj, if_none_match=if_none_match)

    monkeypatch.setattr(prefect_flow, "write_json_s3", racing_write)
    result = run_flow(execute=True)
    assert result["status"] == "in_progress" and result["execution_arn"] == "arn-racer" and sm.started == []


def test_flow_marker_does_not_regress_when_older_run_finishes_later(aws):
    """기준점 보호: 더 최근 기준점이 이미 저장돼 있으면 늦게 끝난 옛 실행이 그 값을 되돌리지 않는다."""
    s3, sm, _ = aws
    newer = (T0 + timedelta(days=10)).isoformat()
    s3.objects.update({**artifacts(), MARKER: encoded({"watermark": newer, "version": "skin_v4"}),
                       ACTIVE: encoded(active_record(watermark=T0 + timedelta(hours=4)))})
    sm.statuses, sm.steps = ["Succeeded"], STEPS_PASS
    result = run_flow(execute=True)
    marker = s3.json(MARKER)
    assert result["previous"]["status"] == "Succeeded" and marker["watermark"] == newer and marker["watermark_kept"] is True
    assert marker["version"] == "skin_v3" and marker["execution_arn"] == ARN and sm.started == []


# ---- CLI check ----

def test_check_command_reports_marker_and_active(aws, capsys):
    s3, sm, settings = aws
    s3.objects.update({**feedback_objects(5, T0), ACTIVE: encoded(active_record())})
    assert prefect_flow.check_command(settings, None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 5 and out["enough"] is True and out["active"]["execution_arn"] == ARN and out["marker"] is None
    assert prefect_flow.check_command(settings, "2030-01-01T00:00:00Z") == 3
