"""Prefect 주간 재학습 플로우 검증 — AWS 없이 (boto3 클라이언트 대역, Prefect 임시 서버)."""
import json
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
    marker_key="pipeline-artifacts/seoulection-skin-retrain/prefect/last_run.json", poll_seconds=0, timeout_seconds=60,
)


class FakeS3:
    """키 → (바이트, LastModified). list_objects_v2 페이지네이터·get_object·put_object만."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []

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

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = (Body, datetime.now(timezone.utc))
        self.puts.append(Key)


class FakeSageMaker:
    def __init__(self, statuses=("Executing", "Executing", "Succeeded"), steps=(), exists=True):
        self.statuses, self.steps, self.exists = list(statuses), list(steps), exists
        self.started = []

    def start_pipeline_execution(self, **request):
        if not self.exists:
            raise ClientError({"Error": {"Code": "ResourceNotFound", "Message": "no pipeline"}}, "StartPipelineExecution")
        self.started.append(request)
        return {"PipelineExecutionArn": ARN}

    def describe_pipeline_execution(self, PipelineExecutionArn):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"PipelineExecutionStatus": status}

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


def test_start_pipeline_passes_parameters_and_explains_missing_pipeline():
    sm = FakeSageMaker()
    assert prefect_flow.start_pipeline(sm, "p", {"Epochs": 2, "MinFeedbackCrops": "0"}, display_name="x") == ARN
    assert sm.started[0]["PipelineParameters"] == [{"Name": "Epochs", "Value": "2"}, {"Name": "MinFeedbackCrops", "Value": "0"}]
    prefect_flow.start_pipeline(sm, "p", None)
    assert "PipelineParameters" not in sm.started[1] and sm.started[1]["PipelineExecutionDisplayName"].startswith("prefect-")
    with pytest.raises(RuntimeError, match="upsert"):
        prefect_flow.start_pipeline(FakeSageMaker(exists=False), "p")


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


# ---- 플로우 (Prefect 임시 서버) ----

@pytest.fixture(scope="module")
def prefect_harness():
    """임시 Prefect 서버 (SQLite). 플로우를 실제로 돌리는 테스트만 쓴다."""
    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness():
        yield


@pytest.fixture
def aws(monkeypatch, prefect_harness):
    """대역 클라이언트 + 설정. 테스트가 s3.objects / sm.statuses 를 채운다."""
    s3, sm = FakeS3(), FakeSageMaker()
    monkeypatch.setattr(prefect_flow, "make_clients", lambda region: (s3, sm))
    monkeypatch.setattr(prefect_flow, "load_flow_settings", lambda *a, **k: dict(SETTINGS))
    return s3, sm


def test_flow_skips_when_below_minimum(aws):
    s3, sm = aws
    s3.objects.update(feedback_objects(2, T0))
    result = prefect_flow.weekly_retrain(execute=True)
    assert result["status"] == "skipped" and result["new_data"]["count"] == 2 and result["min_new_files"] == 3
    assert sm.started == [] and s3.puts == []


def test_flow_dry_run_does_not_start_pipeline(aws):
    s3, sm = aws
    s3.objects.update(feedback_objects(5, T0))
    result = prefect_flow.weekly_retrain()  # 기본 execute=False
    assert result["status"] == "dry_run" and result["new_data"]["count"] == 5 and sm.started == []


def test_flow_uses_marker_as_since_and_force_overrides_minimum(aws):
    s3, sm = aws
    s3.objects.update(feedback_objects(5, T0))
    s3.objects[SETTINGS["marker_key"]] = (json.dumps({"watermark": (T0 + timedelta(hours=3)).isoformat()}).encode(), T0)
    result = prefect_flow.weekly_retrain()
    assert result["status"] == "skipped" and result["new_data"]["count"] == 1 and result["since"] == (T0 + timedelta(hours=3)).isoformat()
    result = prefect_flow.weekly_retrain(force=True, wait=False, execute=True, parameters={"Epochs": "2"})
    assert result["status"] == "started" and result["execution_arn"] == ARN
    assert sm.started[-1]["PipelineParameters"] == [{"Name": "Epochs", "Value": "2"}] and s3.puts == []  # 기다리지 않으면 기준점도 없음
    assert prefect_flow.weekly_retrain(since=T0.isoformat())["new_data"]["count"] == 4  # --since 가 marker 보다 우선


def test_flow_executes_waits_logs_and_writes_marker(aws):
    s3, sm = aws
    s3.objects.update({**feedback_objects(5, T0), **artifacts()})
    sm.statuses, sm.steps = ["Executing", "Executing", "Succeeded"], STEPS_PASS
    result = prefect_flow.weekly_retrain(execute=True)
    assert result["status"] == "Succeeded" and result["passed"] is True
    assert result["register"]["version"] == "skin_v3" and result["evaluation"]["score"] == 0.81
    assert sm.started[0]["PipelineName"] == SETTINGS["pipeline"] and "PipelineParameters" not in sm.started[0]
    assert s3.puts == [SETTINGS["marker_key"]]
    marker = json.loads(s3.objects[SETTINGS["marker_key"]][0])
    assert marker["watermark"] == (T0 + timedelta(hours=4)).isoformat() and marker["version"] == "skin_v3" and marker["new_files"] == 5
    # 다음 주: 기준점 이후 신규가 없으면 건너뛴다
    assert prefect_flow.weekly_retrain(execute=True)["status"] == "skipped"


def test_flow_fails_without_marker_when_pipeline_fails(aws):
    s3, sm = aws
    s3.objects.update(feedback_objects(5, T0))
    sm.statuses, sm.steps = ["Executing", "Failed"], [dict(STEPS_PASS[0], StepStatus="Failed", FailureReason="boom")]
    with pytest.raises(RuntimeError, match="Failed"):
        prefect_flow.weekly_retrain(execute=True)
    assert s3.puts == [] and len(sm.started) == 1
