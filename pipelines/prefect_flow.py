"""Prefect 주간 재학습 플로우 — 매주 월요일 03:00 KST(configs/prefect.yaml schedule, 바꿀 수 있음).

흐름  진행 중 실행 이어받기 → 신규 데이터 확인 → 기준 미달이면 종료 → 실행 예약(active_run.json) → SageMaker 파이프라인 실행
      → 완료까지 대기 → 결과 로그(통과 여부·새 버전·지표) → 기준점 저장 → 예약 해제
      자동 배포 없음: Register 단계가 S3 models/skin_vN/ + Model Registry PendingManualApproval 까지만 하고, 승인·배포는 사람이 한다.

- 파이프라인은 `python -m pipelines.sagemaker_pipeline upsert` 로 미리 등록돼 있어야 한다 (이 플로우는 start·describe만 한다, boto3 sagemaker).
- 신규 데이터 확인은 pipelines/new_data.py 인터페이스. 현재 S3 feedback/ 의 지난 실행 이후 파일 수. 최소 건수는 환경변수
  MIN_NEW_FEEDBACK_FILES (없으면 configs/prefect.yaml new_data.min_new_files). 추천 서버 count·export API 연동은 TODO (new_data.py).
- "지난 실행" 기준점(watermark) = s3://<bucket>/<artifacts>/<파이프라인 이름>/prefect/last_run.json 의 watermark (그때 센 파일 중 가장 최근 시각).
  파이프라인이 Succeeded로 끝났을 때만 갱신 → 실패하면 다음 주에 같은 데이터를 다시 센다. watermark는 뒤로 가지 않는다. --since 로 수동 지정 가능.
- 중복 실행 방지(같은 폴더 prefect/active_run.json): 파이프라인을 시작하기 전에 이 기록을 **예약**한다 — 기록이 없으면 `If-None-Match: *`,
  끝난 기록(state=released)이 있으면 그 ETag 로 `If-Match` 덮어쓰기. 둘 다 원자적이라 하나만 성공하고, 다른 실행이 진행 중(reserved/started)이면
  시작하지 않는다. 기록에는 state·ClientRequestToken(run_id)·표시 이름·실행 ARN·그때의 기준점이 들어가고, 플로우가 시작할 때 진행 중 기록이 있으면
  그 실행을 **이어받는다**(ARN describe → 진행 중이면 대기, 끝났으면 결과 로그·기준점 저장·기록 해제 후 신규 데이터를 다시 센다).
  --no-wait·대기 시간 초과·프로세스 재시작 뒤에도 같은 실행을 이어받고, start 는 한 번만 일어난다.
- 시작 여부가 불확실한 예약(ARN 없음): ARN을 쓰기 전에 죽었거나 멈춘 경우. 먼저 표시 이름으로 실행 목록에서 찾고, 그래도 없으면
  reserve_stale_seconds 가 지난 뒤 **그 예약의 run_id(ClientRequestToken)·표시 이름·파라미터 그대로 start 를 다시 요청**해 실행을 확보한다
  (SageMaker 멱등성: 원래 요청이 이미 닿았으면 같은 실행 ARN, 아니면 새 실행 1개). 새 run_id 로 바꾸지 않으므로 멈춰 있던 원래 프로세스가
  재개해 자기 토큰으로 start 해도 같은 실행이 돌아온다 — 실행은 1개만 생긴다. 새 run_id 로 다시 가는 경우는 확실한 종료만:
  기록된 실행 ARN 이 더는 없거나(지워짐), SageMaker 가 start 를 확실히 거절(ValidationException 등 → 예약 해제 status=rejected)한 때.
- 소유권: 기록을 바꾸는 모든 쓰기(ARN 기록·해제·예약)는 "읽은 run_id 가 같고 ETag 가 그대로일 때만"(`If-Match`) 이루어진다 — 같은 실행을
  여러 호출이 이어받아 마무리해도, 먼저 끝낸 호출이 해제하고 새 실행을 예약한 뒤 늦은 호출이 그 새 예약을 건드리지 못한다(412 → 다시 읽고
  run_id 가 다르면 손을 뗀다). 기록은 지우지 않고 state=released 묘비로 남긴다(조건부 삭제 대신 조건부 덮어쓰기). 기준점(last_run.json)도
  같은 방식으로 읽은 ETag 에 조건부 쓰기 → 기존 watermark 가 더 최근이면 쓰지 않는다(단조 증가).
  실행이 실패·중단으로 끝나면 기록을 해제하고 기준점은 그대로 둔다 → 다음 실행(다음 주 스케줄 또는 수동 `run --execute`)이 같은 데이터로 재시도한다.
  --force 는 "최소 건수 미달이어도 시작"일 뿐, 진행 중 실행이 있으면 force 여도 새로 시작하지 않는다.
- dry-run(execute=False, `run` 기본값)은 **읽기만** 한다: 기록·실행 상태·기준점·신규 데이터를 읽고 "execute 였다면 무엇을 했을지"를 로그로 남긴다.
  대기·ARN 복구 기록·기준점 갱신·해제·시작 모두 하지 않는다.
- 결과: Condition 단계 Outcome + 산출물 evaluation.json(점수·실패 축)·register.json(새 버전·S3·Model Package ARN)을 읽어 로그로 남긴다.

사용
  python -m pipelines.prefect_flow check [--since ISO]                 신규 데이터 수·기준점·진행 중 실행 (S3 읽기, Prefect 서버 없이)
  python -m pipelines.prefect_flow run [--execute] [--force] [--since ISO] [--param Epochs=2 ...] [--no-wait]
                                                                       로컬 1회 수동 실행. 기본은 dry-run(S3 읽기와 계획 로그만), --execute 로 실제 시작
  python -m pipelines.prefect_flow serve [--paused] [--cron C --timezone TZ]
                                                                       스케줄 배포 등록 + 실행 프로세스 (계속 떠 있음). Prefect 서버/Cloud는 PREFECT_API_URL

환경변수: AWS 자격증명(IAM 역할·프로필), MIN_NEW_FEEDBACK_FILES, PREFECT_API_URL(없으면 로컬 임시 서버).
"""
import argparse
import json
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError
from prefect import flow, get_run_logger, task
from prefect.cache_policies import NO_CACHE

from pipelines.new_data import as_utc, make_source
from pipelines.sagemaker_pipeline import PIPELINE_CONFIG, load_settings, parse_params
from training.config import DEFAULT_PATHS, load_yaml

PREFECT_CONFIG = "configs/prefect.yaml"
MIN_NEW_ENV = "MIN_NEW_FEEDBACK_FILES"
TERMINAL = ("Succeeded", "Failed", "Stopped")
EVALUATION_KEY = "evaluation/evaluation.json"   # 파이프라인 산출물 (pipelines/sagemaker_pipeline.py run_uri 와 같은 구조)
REGISTER_KEY = "register/register.json"
FIND_EXECUTION_MARGIN = timedelta(hours=1)      # 예약 시각보다 이만큼 앞부터 실행 목록을 찾는다 (시계 차이 여유)
FLOW_NAME = load_yaml(PREFECT_CONFIG)["flow_name"]


# ---------------------------------------------------------------------------
# 설정·클라이언트
# ---------------------------------------------------------------------------
def load_flow_settings(prefect_config=PREFECT_CONFIG, pipeline_config=PIPELINE_CONFIG, paths_config=DEFAULT_PATHS):
    """configs/prefect.yaml + 파이프라인 설정(이름·리전·버킷·feedback 주소) → 한 dict. 최소 건수는 환경변수가 우선."""
    p, s = load_yaml(prefect_config), load_settings(pipeline_config, paths_config)
    env_min = os.environ.get(MIN_NEW_ENV, "").strip()
    return dict(
        flow_name=p["flow_name"], deployment_name=p["deployment_name"],
        cron=p["schedule"]["cron"], timezone=p["schedule"]["timezone"],
        pipeline=s["name"], region=s["region"], bucket=s["bucket"], artifacts_prefix=s["artifacts_prefix"], feedback=s["feedback"],
        source=p["new_data"]["source"], min_new_files=int(env_min or p["new_data"]["min_new_files"]),
        marker_key=f"{s['artifacts_prefix']}/{p['new_data']['marker_key'].strip('/')}",
        active_key=f"{s['artifacts_prefix']}/{p['new_data']['active_key'].strip('/')}",
        reserve_stale_seconds=float(p["new_data"]["reserve_stale_seconds"]),
        poll_seconds=float(p["wait"]["poll_seconds"]), timeout_seconds=float(p["wait"]["timeout_seconds"]),
    )


def make_clients(region):
    """(s3, sagemaker) boto3 클라이언트. 테스트는 이 함수를 대역으로 바꾼다."""
    import boto3

    session = boto3.Session(region_name=region)
    return session.client("s3"), session.client("sagemaker")


def parse_time(value):
    """ISO 8601 문자열 → aware UTC datetime (시간대 없으면 UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return as_utc(value)
    return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


# ---------------------------------------------------------------------------
# 순수 함수 (Prefect 없이 테스트 가능)
# ---------------------------------------------------------------------------
MISSING_CODES = ("NoSuchKey", "404", "NotFound")
CONDITION_FAILED_CODES = ("PreconditionFailed", "412", "ConditionalRequestConflict", "409")
CAS_ATTEMPTS = 8


def read_json_s3(s3, bucket, key, with_etag=False):
    """S3 JSON 객체 → dict, 없으면 None. with_etag=True면 (dict, ETag) — ETag는 조건부 쓰기(If-Match)에 쓴다."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] in MISSING_CODES:
            return (None, None) if with_etag else None
        raise
    obj = json.loads(resp["Body"].read())
    return (obj, resp.get("ETag")) if with_etag else obj


def write_json_s3(s3, bucket, key, obj, if_none_match=False, if_match=None):
    """JSON 저장 → s3 URI. 조건부 쓰기(원자적): if_none_match=True면 객체가 없을 때만, if_match=ETag면 그 ETag 그대로일 때만 쓴다.

    조건이 맞지 않으면(이미 있음 / 누가 바꿈 / 지워짐) False 를 돌려준다. 둘 다 없으면 무조건 덮어쓴다.
    """
    kwargs = dict(Bucket=bucket, Key=key, Body=json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json")
    if if_none_match:
        kwargs["IfNoneMatch"] = "*"
    if if_match:
        kwargs["IfMatch"] = if_match
    try:
        s3.put_object(**kwargs)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if (if_none_match or if_match) and (code in CONDITION_FAILED_CODES or (if_match and code in MISSING_CODES)):
            return False
        raise
    return f"s3://{bucket}/{key}"


def update_json_s3(s3, bucket, key, decide, attempts=CAS_ATTEMPTS):
    """읽기 → decide(현재 값) → 읽은 ETag 로 조건부 쓰기(compare-and-swap). 누가 사이에 바꿨으면(412/409) 다시 읽어 반복한다.

    decide(current) 가 None 을 돌려주면 쓰지 않는다(조건 불일치 — 예: 내 run_id 가 아니다, 기존 기준점이 더 최근이다).
    반환 (쓴 값 또는 None, 마지막으로 읽은 값). 객체가 없으면 If-None-Match:* 로 만든다.
    """
    for _ in range(attempts):
        current, etag = read_json_s3(s3, bucket, key, with_etag=True)
        new = decide(current)
        if new is None:
            return None, current
        if write_json_s3(s3, bucket, key, new, if_none_match=etag is None, if_match=etag):
            return new, current
    raise RuntimeError(f"s3://{bucket}/{key} 조건부 쓰기가 {attempts}회 연속 충돌했다 — 다른 프로세스가 계속 바꾸고 있다")


def is_active(record):
    """예약 기록이 살아 있나(reserved/started). released 묘비나 없음은 False. state 없는 옛 기록은 살아 있는 것으로 본다."""
    return bool(record) and record.get("state") != "released"


def owns(record, run_id):
    return is_active(record) and record.get("run_id") == run_id


class StartRejected(RuntimeError):
    """SageMaker 가 시작 요청을 **확실히** 거절했다(실행이 생기지 않았다) — 요청 내용·권한·한도 문제. 이 예약은 해제해도 된다.

    네트워크 오류·스로틀링·5xx 는 여기 들어가지 않는다(실행이 생겼는지 모름 → 예약을 그대로 두고 같은 토큰으로 다시 시도).
    """


REJECTED_CODES = ("ValidationException", "ResourceNotFound", "ResourceNotFoundException", "ResourceLimitExceeded",
                  "AccessDeniedException", "ResourceInUse")


def start_pipeline(sm, name, parameters=None, display_name=None, client_request_token=None):
    """파이프라인 실행 시작 → 실행 ARN. parameters: {이름: 값} (파이프라인 파라미터 덮어쓰기).

    client_request_token(32~128자)은 SageMaker 쪽 멱등 키 — 같은 토큰(같은 요청)으로 다시 부르면 새 실행을 만들지 않고 같은 ARN 을 돌려준다.
    확실한 거절(파이프라인 없음·요청 오류·권한·한도)은 StartRejected, 그 밖(스로틀링·5xx)은 ClientError 그대로.
    """
    request = dict(PipelineName=name,
                   PipelineExecutionDisplayName=display_name or f"prefect-{now_utc():%Y%m%d-%H%M%S}")
    if parameters:
        request["PipelineParameters"] = [dict(Name=str(k), Value=str(v)) for k, v in parameters.items()]
    if client_request_token:
        request["ClientRequestToken"] = client_request_token
    try:
        return sm.start_pipeline_execution(**request)["PipelineExecutionArn"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ResourceNotFound", "ResourceNotFoundException"):
            raise StartRejected(f"파이프라인 {name!r}이 없다 — 먼저 `python -m pipelines.sagemaker_pipeline upsert`") from e
        if code in REJECTED_CODES:
            raise StartRejected(f"파이프라인 {name!r} 시작 거절 {code}: {e.response['Error'].get('Message')}") from e
        raise


def describe_status(sm, arn):
    """실행 상태 문자열. 실행이 없으면(지워졌으면) None."""
    try:
        return sm.describe_pipeline_execution(PipelineExecutionArn=arn)["PipelineExecutionStatus"]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ResourceNotFound", "ResourceNotFoundException"):
            return None
        raise


def find_execution(sm, name, display_name, created_after=None):
    """표시 이름으로 실행 ARN 찾기 (예약 뒤 ARN을 기록하기 전에 죽은 경우 복구용). 없으면 None."""
    token = None
    while True:
        kwargs = dict(PipelineName=name, SortBy="CreationTime", SortOrder="Descending")
        if created_after:
            kwargs["CreatedAfter"] = created_after
        if token:
            kwargs["NextToken"] = token
        page = sm.list_pipeline_executions(**kwargs)
        for s in page.get("PipelineExecutionSummaries", []):
            if s.get("PipelineExecutionDisplayName") == display_name:
                return s["PipelineExecutionArn"]
        token = page.get("NextToken")
        if not token:
            return None


def wait_pipeline(sm, arn, poll_seconds, timeout_seconds, on_status=None, sleep=time.sleep):
    """종료 상태(Succeeded/Failed/Stopped)가 될 때까지 describe. 시간 초과면 TimeoutError (실행은 멈추지 않는다)."""
    deadline, last = time.monotonic() + timeout_seconds, None
    while True:
        status = sm.describe_pipeline_execution(PipelineExecutionArn=arn)["PipelineExecutionStatus"]
        if status != last and on_status:
            on_status(status)
        last = status
        if status in TERMINAL:
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{timeout_seconds:.0f}초 안에 끝나지 않았다 (마지막 상태 {status}): {arn} — 실행은 그대로 둔다")
        sleep(poll_seconds)


def execution_steps(sm, arn):
    """실행의 단계 목록 → [{name, status, outcome(Condition이면 bool), failure}] 시작 순서대로."""
    steps, token = [], None
    while True:
        kwargs = dict(PipelineExecutionArn=arn, SortOrder="Ascending")
        if token:
            kwargs["NextToken"] = token
        page = sm.list_pipeline_execution_steps(**kwargs)
        for s in page.get("PipelineExecutionSteps", []):
            outcome = (s.get("Metadata") or {}).get("Condition", {}).get("Outcome")
            steps.append(dict(name=s["StepName"], status=s.get("StepStatus"),
                              outcome=None if outcome is None else str(outcome).lower() == "true",
                              failure=s.get("FailureReason")))
        token = page.get("NextToken")
        if not token:
            return steps


def summarize_execution(s3, sm, settings, arn, status):
    """실행 결과 요약: 단계 상태, Condition 결과, evaluation.json(점수·실패 축), register.json(새 버전)."""
    exec_id = arn.rsplit("/", 1)[-1]
    base = f"{settings['artifacts_prefix']}/{exec_id}/"
    steps = execution_steps(sm, arn)
    evaluation = read_json_s3(s3, settings["bucket"], base + EVALUATION_KEY)
    register = read_json_s3(s3, settings["bucket"], base + REGISTER_KEY)
    condition = next((s for s in steps if s["outcome"] is not None), None)
    summary = dict(status=status, execution_arn=arn, execution_id=exec_id,
                   steps={s["name"]: s["status"] for s in steps},
                   failures={s["name"]: s["failure"] for s in steps if s["failure"]},
                   passed=condition["outcome"] if condition else None, evaluation=None, register=None)
    if evaluation:
        summary["evaluation"] = dict(
            score=evaluation["candidate"]["score"], baseline_score=evaluation["baseline"]["score"],
            model=evaluation["candidate"].get("model"), baseline_model=evaluation["baseline"].get("model"),
            failed_axes=evaluation.get("failed_axes"), total_axes=evaluation.get("total_axes"),
            failed=[f"{a['task']}.{a['metric']}" for a in evaluation.get("axes", []) if not a.get("passed")],
            testset=evaluation.get("testset"))
        if summary["passed"] is None:
            summary["passed"] = bool(evaluation.get("passed"))
    if register:
        summary["register"] = {k: register.get(k) for k in ("version", "s3_uri", "model_name", "model_package_arn")}
    return summary


def format_summary(summary):
    """요약 → 로그 줄 목록 (통과 여부, 새 버전, 지표)."""
    lines = [f"파이프라인 {summary['status']}: {summary['execution_arn']}"]
    lines += [f"  단계 {name}: {status}" for name, status in summary["steps"].items()]
    lines += [f"  실패 사유 {name}: {reason}" for name, reason in summary["failures"].items()]
    ev = summary.get("evaluation")
    if ev:
        lines.append(f"  평가 점수 새 모델 {ev['score']:.4f} vs 현재 {ev['baseline_score']:.4f} | "
                     f"축 {ev['total_axes']}개 중 실패 {ev['failed_axes']}개")
        lines += [f"    ✗ {axis}" for axis in ev["failed"]]
    passed = summary.get("passed")
    lines.append("  조건: " + ("통과 (모든 축에서 현재 모델 이상)" if passed else "불통과 → 등록 없음" if passed is False else "판정 없음 (Evaluate까지 가지 않음)"))
    reg = summary.get("register")
    if reg:
        lines.append(f"  새 버전 {reg['version']} ({reg['model_name']}) → {reg['s3_uri']}")
        lines.append(f"  Model Registry PendingManualApproval: {reg['model_package_arn'] or '(등록 생략)'} — 승인·배포는 사람이 한다")
    return lines


def owner_tag():
    return f"{socket.gethostname()}:{os.getpid()}"


def new_active_record(since, watermark, new_files, parameters):
    """active_run.json 내용 (시작 전, state=reserved). run_id = SageMaker ClientRequestToken, display_name = 실행 표시 이름 (ARN 복구용)."""
    run_id, now = uuid.uuid4().hex, now_utc()
    return dict(state="reserved", run_id=run_id, display_name=f"prefect-{now:%Y%m%d-%H%M%S}-{run_id[:8]}", reserved=iso(now),
                owner=owner_tag(), since=iso(since), watermark=iso(watermark), new_files=new_files,
                parameters=parameters or None, execution_arn=None, started=None)


def released_record(record, status):
    """해제 묘비(state=released): 무엇이 어떻게 끝났는지만 남긴다. 다음 예약이 이 ETag 로 덮어쓴다."""
    return dict(state="released", run_id=record.get("run_id"), display_name=record.get("display_name"),
                execution_arn=record.get("execution_arn"), status=status, reserved=record.get("reserved"),
                released=iso(now_utc()), released_by=owner_tag())


def marker_document(watermark, new_files, arn, summary):
    """last_run.json 내용. watermark = 그 실행이 센 파일 중 가장 최근 시각 (파일이 없었으면 이전 기준점)."""
    return dict(watermark=iso(watermark), updated=iso(now_utc()),
                execution_arn=arn, status=summary["status"], passed=summary.get("passed"),
                new_files=new_files, version=(summary.get("register") or {}).get("version"))


def merge_marker(existing, document):
    """기준점은 뒤로 가지 않는다: 기존 watermark가 더 최근이면 None(쓰지 않음 — 기존 문서 그대로), 아니면 새 문서."""
    old, new = parse_time((existing or {}).get("watermark")), parse_time(document.get("watermark"))
    if old and (new is None or old > new):
        return None
    return document


# ---------------------------------------------------------------------------
# Prefect 태스크·플로우
# ---------------------------------------------------------------------------
@task(name="read-marker", retries=2, retry_delay_seconds=30, cache_policy=NO_CACHE)
def read_marker_task(settings):
    s3, _ = make_clients(settings["region"])
    return read_json_s3(s3, settings["bucket"], settings["marker_key"])


@task(name="read-active", retries=2, retry_delay_seconds=30, cache_policy=NO_CACHE)
def read_active_task(settings):
    """살아 있는 예약 기록(reserved/started), 없거나 released 묘비면 None."""
    s3, _ = make_clients(settings["region"])
    record = read_json_s3(s3, settings["bucket"], settings["active_key"])
    return record if is_active(record) else None


@task(name="locate-active", retries=2, retry_delay_seconds=30, cache_policy=NO_CACHE)
def locate_active_task(settings, active):
    """기록의 실행을 찾는다 (읽기만) → (arn, status, recovered). ARN이 기록에 없으면 표시 이름으로 실행 목록에서 찾는다(recovered=True).

    - 기록에 ARN 이 있는데 그 실행이 없다(지워짐) → (arn, None, False): 시작은 확실히 끝났고 실행만 없다.
    - ARN 도 없고 표시 이름으로도 못 찾음 → (None, None, False): 시작했는지 불확실 (예약만 있음).
    """
    _, sm = make_clients(settings["region"])
    arn, recovered = active.get("execution_arn"), False
    if not arn:
        reserved = parse_time(active.get("reserved"))
        arn = find_execution(sm, settings["pipeline"], active["display_name"],
                             created_after=reserved - FIND_EXECUTION_MARGIN if reserved else None)
        recovered = bool(arn)
    if not arn:
        return None, None, False
    status = describe_status(sm, arn)
    if status is None:
        return (None, None, False) if recovered else (arn, None, False)
    return arn, status, recovered


@task(name="record-active", retries=2, retry_delay_seconds=30, cache_policy=NO_CACHE)
def record_active_task(settings, active, **fields):
    """내 기록(run_id 같고 살아 있음)에만 필드를 덧붙인다 — 읽은 ETag 로 If-Match.

    돌려주는 값: 쓴 기록. 소유권이 없으면(다른 호출이 해제·재예약) S3 기록이 아니라 **내가 들고 있던 기록**(+필드)을 돌려준다 —
    남의 run_id 를 들고 마무리하면 그 예약을 해제해 버릴 수 있기 때문.
    """
    s3, _ = make_clients(settings["region"])
    run_id = active["run_id"]
    written, _ = update_json_s3(s3, settings["bucket"], settings["active_key"],
                                lambda cur: dict(cur, **fields) if owns(cur, run_id) else None)
    return written or dict(active, **fields)


def release_record(s3, settings, run_id, status, only_unstarted=False):
    """내 기록(run_id 같고 살아 있음)만 released 묘비로 바꾼다(If-Match) → True. only_unstarted=True 면 ARN 이 기록되지 않은 때만."""
    def decide(cur):
        if not owns(cur, run_id) or (only_unstarted and cur.get("execution_arn")):
            return None
        return released_record(cur, status)
    written, _ = update_json_s3(s3, settings["bucket"], settings["active_key"], decide)
    return written is not None


@task(name="release-active", retries=2, retry_delay_seconds=30, cache_policy=NO_CACHE)
def release_active_task(settings, active, status):
    """내 기록만 released 묘비로 바꾼다(If-Match). 다른 호출이 먼저 해제했거나 새 예약이 들어왔으면(run_id 다름) 건드리지 않고 False."""
    s3, _ = make_clients(settings["region"])
    return release_record(s3, settings, active["run_id"], status)


@task(name="check-new-data", retries=2, retry_delay_seconds=30, cache_policy=NO_CACHE)
def check_new_data_task(settings, since):
    s3, _ = make_clients(settings["region"])
    return make_source(settings["source"], settings["feedback"], client=s3).check(since)


@task(name="reserve-run", cache_policy=NO_CACHE)
def reserve_run_task(settings, record):
    """active_run.json 예약: 없으면 If-None-Match:*, released 묘비면 그 ETag 로 If-Match 덮어쓰기 → True. 살아 있는 기록이 있으면 False.

    예약 사이의 경합(둘이 같은 묘비를 읽고 동시에 덮어쓰기)은 412 로 한쪽만 성공한다 — 진 쪽은 다시 읽고, 살아 있는 기록이면 False.
    """
    s3, _ = make_clients(settings["region"])
    written, _ = update_json_s3(s3, settings["bucket"], settings["active_key"], lambda cur: None if is_active(cur) else record)
    return written is not None


@task(name="start-pipeline", cache_policy=NO_CACHE)
def start_pipeline_task(settings, record, **fields):
    """예약 기록의 run_id(ClientRequestToken)·표시 이름·파라미터 **그대로** 시작을 요청하고, 내 기록에 ARN·state=started 를 채운다(If-Match).

    요청 인자는 전부 기록에서 오므로 같은 기록으로 몇 번을 불러도 같은 요청이다 → SageMaker 멱등성으로 같은 실행 ARN 이 돌아오고
    실행은 1개만 생긴다 (예약 뒤 멈춘 프로세스가 재개해도, 그 사이 다른 호출이 이 예약을 이어받아 시작했어도). 기록이 이미 남의 것이면
    (run_id 다름) 기록은 건드리지 않는다. SageMaker 가 확실히 거절하면(StartRejected) 실행이 없으므로 ARN 없는 내 예약을 해제(status=rejected)하고
    예외를 그대로 올린다 — 다음 실행이 새 run_id 로 간다. 불확실한 오류(스로틀링·5xx)는 예약을 그대로 둔다.
    """
    s3, sm = make_clients(settings["region"])
    run_id = record["run_id"]
    try:
        arn = start_pipeline(sm, settings["pipeline"], record.get("parameters"), display_name=record["display_name"], client_request_token=run_id)
    except StartRejected:
        release_record(s3, settings, run_id, "rejected", only_unstarted=True)
        raise
    update_json_s3(s3, settings["bucket"], settings["active_key"],
                   lambda cur: dict(cur, state="started", execution_arn=arn, started=cur.get("started") or iso(now_utc()), **fields)
                   if owns(cur, run_id) else None)
    return arn


@task(name="wait-pipeline", cache_policy=NO_CACHE)
def wait_pipeline_task(settings, arn):
    log = get_run_logger()
    _, sm = make_clients(settings["region"])
    return wait_pipeline(sm, arn, settings["poll_seconds"], settings["timeout_seconds"],
                         on_status=lambda s: log.info("파이프라인 상태 %s", s))


@task(name="summarize", retries=2, retry_delay_seconds=30, cache_policy=NO_CACHE)
def summarize_task(settings, arn, status):
    s3, sm = make_clients(settings["region"])
    return summarize_execution(s3, sm, settings, arn, status)


@task(name="write-marker", retries=2, retry_delay_seconds=30, cache_policy=NO_CACHE)
def write_marker_task(settings, document):
    """기준점 저장 — 읽은 ETag 로 조건부 쓰기(update_json_s3). 기존 watermark가 더 최근이면 쓰지 않는다(merge_marker → None).

    돌려주는 값: (썼으면 s3 URI / 아니면 None, 저장돼 있는 문서).
    """
    s3, _ = make_clients(settings["region"])
    written, current = update_json_s3(s3, settings["bucket"], settings["marker_key"], lambda cur: merge_marker(cur, document))
    return (f"s3://{settings['bucket']}/{settings['marker_key']}" if written else None), (written or current)


def _finish(settings, log, active, arn, status, watermark, new_files):
    """종료된 실행 마무리: 요약 로그 → Succeeded면 기준점 저장(단조) → 내 기록만 해제(소유권 If-Match). 요약을 돌려준다."""
    summary = summarize_task(settings, arn, status)
    for line in format_summary(summary):
        log.info(line)
    if status == "Succeeded":
        uri, document = write_marker_task(settings, marker_document(watermark, new_files, arn, summary))
        if uri:
            log.info("기준점 저장 %s (watermark %s)", uri, document["watermark"])
        else:
            log.info("기준점은 기존 값이 더 최근이라 그대로 둔다 (watermark %s, 실행 %s)", document.get("watermark"), document.get("execution_arn"))
    else:
        log.error("파이프라인 실행이 %s로 끝났다: %s — 기준점은 그대로 (다음 실행이 같은 데이터로 재시도한다)", status, arn)
    if release_active_task(settings, active, status):
        log.info("예약 해제 (run_id %s)", active.get("run_id"))
    else:
        log.info("예약 기록은 다른 호출이 이미 해제했거나 새 예약이 들어왔다(run_id %s) → 건드리지 않는다", active.get("run_id"))
    return summary


def _dry_run_plan(active, status, report, min_new_files, force):
    """execute 였다면 무엇을 했을지 (로그·결과용)."""
    steps = []
    if active and status and status not in TERMINAL:
        steps.append(f"진행 중 실행 {active.get('execution_arn') or active.get('display_name')}({status}) 을 이어받아 완료까지 대기")
    elif active and status:
        steps.append(f"끝난 실행 {active.get('execution_arn')}({status}) 마무리: 결과 로그 → {'기준점 저장 → ' if status == 'Succeeded' else ''}예약 해제 → 신규 데이터 다시 세기")
    elif active and active.get("execution_arn"):
        steps.append(f"기록된 실행 {active['execution_arn']} 이 없다(지워짐): 예약이 reserve_stale_seconds 지났으면 해제, 아니면 시작하지 않음")
    elif active:
        steps.append(f"실행 없는 예약({active.get('owner')}, {active.get('reserved')}) 확인: reserve_stale_seconds 지났으면 "
                     "같은 run_id(ClientRequestToken)·표시 이름·파라미터로 시작을 다시 요청해 실행 확보(멱등), 아니면 시작하지 않음")
    if report.count < min_new_files and not force:
        steps.append(f"신규 {report.count}개 < 최소 {min_new_files}개 → 시작하지 않음")
    elif active:
        steps.append("위 처리 뒤 신규 데이터가 기준 이상이면 예약 → 시작")
    else:
        steps.append(f"예약 → 파이프라인 시작 (신규 {report.count}개{' — force' if report.count < min_new_files else ''})")
    return steps


@flow(name=FLOW_NAME)
def weekly_retrain(execute: bool = False, force: bool = False, since: str | None = None,
                   parameters: dict | None = None, wait: bool = True) -> dict:
    """주간 재학습. execute=False면 읽기만 하는 dry-run(기록·실행 상태·기준점·신규 데이터 확인 + 계획 로그, S3 쓰기·대기 없음).

    force=True면 기준 미달이어도 시작. since는 기준점 수동 지정(ISO). parameters는 파이프라인 파라미터 덮어쓰기 ({"Epochs": "2"}),
    wait=False면 시작만 하고 끝낸다. 진행 중 실행(active_run.json)이 있으면 새로 시작하지 않고 그 실행을 이어받는다 (force 여도).
    """
    log = get_run_logger()
    settings = load_flow_settings()
    result = dict(pipeline=settings["pipeline"], previous=None)

    # 1) 진행 중(또는 끝났지만 마무리 안 된) 실행 — dry-run 은 상태만 읽고, execute 는 이어받는다
    active = read_active_task(settings)
    active_status = None
    if active:
        arn, active_status, recovered = locate_active_task(settings, active)
        if not execute:
            result["active"] = dict(active, execution_arn=arn or active.get("execution_arn"), execution_status=active_status)
            log.info("dry-run: 예약 기록 있음 (run_id %s, 실행 %s, 상태 %s, 예약 %s, %s) — 읽기만 한다",
                     active.get("run_id"), arn or "없음", active_status or "없음", active.get("reserved"), active.get("owner"))
        else:
            if active_status is None:   # 실행을 못 찾음: 예약만 있음(시작했는지 불확실) 또는 기록된 실행이 지워짐(확실한 종료)
                age = (now_utc() - (parse_time(active.get("reserved")) or now_utc())).total_seconds()
                if age < settings["reserve_stale_seconds"]:
                    log.info("다른 프로세스가 %s 전에 예약하고 시작 중이다(%s) → 이번에는 시작하지 않는다", f"{age:.0f}초", active.get("owner"))
                    return dict(result, status="in_progress", execution_arn=arn, active=active)
                if arn:
                    log.warning("기록된 실행 %s 이 없다(지워짐; %s, %.0f초 전) → 예약을 해제하고 계속한다", arn, active.get("owner"), age)
                    release_active_task(settings, active, "abandoned")
                else:
                    # 새 run_id 로 바꾸지 않는다: 원래 프로세스가 재개해 자기 토큰으로 start 해도 같은 실행이 돌아와야 한다
                    log.warning("예약만 있고 실행을 찾지 못했다(%s, %.0f초 전) → 같은 run_id·표시 이름·파라미터로 시작을 다시 요청해 "
                                "실행을 확보한다 (멱등: 이미 시작됐으면 그 실행, 아니면 새 실행 1개)", active.get("owner"), age)
                    arn = start_pipeline_task(settings, active, adopted=iso(now_utc()), adopted_by=owner_tag())
                    arn, active_status, _ = locate_active_task(settings, dict(active, execution_arn=arn))
                    if active_status is None:
                        raise RuntimeError(f"시작 요청으로 받은 실행 {arn} 을 describe 할 수 없다 — 기록(run_id {active.get('run_id')})은 그대로 둔다")
                    active = dict(active, execution_arn=arn, state="started")
            elif recovered:
                active = record_active_task(settings, active, execution_arn=arn, state="started", recovered=iso(now_utc()))
            if active_status is not None:
                log.info("이전 실행 이어받음 %s (상태 %s, 예약 %s, 기준점 %s, 신규 %s개)",
                         arn, active_status, active.get("reserved"), active.get("since") or "없음", active.get("new_files"))
                status = active_status
                if status not in TERMINAL:
                    if not wait:
                        return dict(result, status="in_progress", execution_arn=arn, active=active)
                    status = wait_pipeline_task(settings, arn)   # TimeoutError 면 기록을 남겨 다음 실행이 다시 이어받는다
                result["previous"] = _finish(settings, log, active, arn, status,
                                             parse_time(active.get("watermark")) or parse_time(active.get("since")), active.get("new_files"))

    # 2) 신규 데이터 확인
    marker = None if since else read_marker_task(settings)
    since_dt = parse_time(since) if since else parse_time((marker or {}).get("watermark"))
    report = check_new_data_task(settings, since_dt)
    log.info("신규 데이터(%s, %s): 기준점 %s 이후 %d개 / 전체 %d개 %s | 최소 %d개",
             report.source, report.location, since_dt.isoformat() if since_dt else "없음(전체)", report.count, report.total,
             report.by_prefix or "", settings["min_new_files"])
    result.update(since=since_dt.isoformat() if since_dt else None, new_data=report.to_dict(), min_new_files=settings["min_new_files"])

    if not execute:
        plan = _dry_run_plan(active, active_status, report, settings["min_new_files"], force)
        log.info("dry-run: 파이프라인 %s 을 시작하지 않는다 (실제 실행은 execute=True / --execute). 파라미터 %s. execute 였다면: %s",
                 settings["pipeline"], parameters or {}, " → ".join(plan))
        return dict(result, status="dry_run", plan=plan)
    if report.count < settings["min_new_files"] and not force:
        log.info("신규 데이터 %d개 < 최소 %d개 → 이번 주는 재학습하지 않는다", report.count, settings["min_new_files"])
        return dict(result, status="skipped")

    # 3) 예약 → 시작 (예약이 선점돼 있으면 시작하지 않는다)
    record = new_active_record(since_dt, report.latest or since_dt, report.count, parameters)
    if not reserve_run_task(settings, record):
        other = read_active_task(settings) or {}
        log.info("다른 실행이 예약을 선점했다(%s, 실행 %s) → 이번에는 시작하지 않는다", other.get("owner"), other.get("execution_arn") or "시작 중")
        return dict(result, status="in_progress", execution_arn=other.get("execution_arn"), active=other)
    arn = start_pipeline_task(settings, record)   # 확실한 거절(StartRejected)이면 예약을 해제하고 예외 → 다음 실행은 새 run_id
    log.info("파이프라인 %s 시작: %s (표시 이름 %s, 파라미터 %s)", settings["pipeline"], arn, record["display_name"], parameters or {})
    if not wait:
        return dict(result, status="started", execution_arn=arn)

    # 4) 대기 → 결과 로그 → 기준점 저장 → 예약 해제
    try:
        status = wait_pipeline_task(settings, arn)
    except TimeoutError:
        log.warning("대기 시간 초과 — 실행 %s 과 active_run.json 은 그대로 둔다. 다음 실행이 이어받는다", arn)
        raise
    summary = _finish(settings, log, record, arn, status, report.latest or since_dt, report.count)
    result.update(summary)
    if status != "Succeeded":
        raise RuntimeError(f"파이프라인 실행이 {status}로 끝났다: {arn} (기준점은 갱신하지 않음 — 다음 실행이 같은 데이터로 재시도한다)")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def check_command(settings, since):
    """Prefect 없이 신규 데이터·기준점·진행 중 실행 기록을 본다 (S3 읽기)."""
    s3, _ = make_clients(settings["region"])
    marker = read_json_s3(s3, settings["bucket"], settings["marker_key"]) if since is None else None
    record = read_json_s3(s3, settings["bucket"], settings["active_key"])
    since_dt = parse_time(since) if since else parse_time((marker or {}).get("watermark"))
    report = make_source(settings["source"], settings["feedback"], client=s3).check(since_dt)
    enough = report.count >= settings["min_new_files"]
    print(json.dumps(dict(report.to_dict(), marker=marker, active=record if is_active(record) else None,
                          last_released=None if is_active(record) else record, min_new_files=settings["min_new_files"], enough=enough),
                     ensure_ascii=False, indent=2))
    return 0 if enough else 3


def serve_command(settings, paused=False):
    """스케줄 배포를 등록하고 이 프로세스에서 실행한다 (계속 떠 있음). 스케줄 실행은 execute=True.

    global_limit=1 은 Prefect 쪽 보조 장치(같은 배포의 플로우 실행 동시 1개) — 중복 시작 방지의 본체는 S3 active_run.json 예약이다.
    """
    from prefect.schedules import Cron

    print(f"배포 {settings['flow_name']}/{settings['deployment_name']}: cron '{settings['cron']}' ({settings['timezone']}), "
          f"파이프라인 {settings['pipeline']}, 최소 신규 {settings['min_new_files']}개{' — 일시정지 상태로 등록' if paused else ''}")
    weekly_retrain.serve(
        name=settings["deployment_name"], schedule=Cron(settings["cron"], timezone=settings["timezone"]),
        parameters={"execute": True}, paused=paused, global_limit=1, tags=["seoulection", "skin", "retrain"],
        description=f"Seoulection skin 주간 재학습 — SageMaker 파이프라인 {settings['pipeline']} (승인·배포는 사람)",
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="신규 데이터 수·기준점·진행 중 실행 기록 (S3 읽기, Prefect 없이). 기준 이상이면 종료 코드 0, 미만이면 3")
    c.add_argument("--since", help="기준점 ISO 8601 (기본: S3 last_run.json watermark)")
    r = sub.add_parser("run", help="로컬 1회 수동 실행 (기본 dry-run: S3 읽기와 계획 로그만). --execute 면 진행 중 실행이 있을 때 새로 시작하지 않고 이어받는다")
    r.add_argument("--execute", action="store_true", help="실제로 실행한다 (없으면 기록·실행 상태·신규 데이터를 읽고 계획만 로그 — 쓰기·대기 없음)")
    r.add_argument("--force", action="store_true", help="신규 데이터가 기준 미달이어도 시작 (진행 중 실행이 있으면 그래도 새로 시작하지 않음)")
    r.add_argument("--since", help="기준점 ISO 8601 (기본: S3 last_run.json watermark)")
    r.add_argument("--param", action="append", default=[], help="파이프라인 파라미터 이름=값 (예: Epochs=2, MinFeedbackCrops=0)")
    r.add_argument("--no-wait", action="store_true", help="시작만 하고 완료를 기다리지 않는다 (다음 run/스케줄이 이어받아 마무리한다)")
    s = sub.add_parser("serve", help="스케줄 배포 등록 + 실행 프로세스 (계속 떠 있음)")
    s.add_argument("--paused", action="store_true", help="스케줄을 일시정지 상태로 등록")
    s.add_argument("--cron", help="configs/prefect.yaml schedule.cron 덮어쓰기")
    s.add_argument("--timezone", help="configs/prefect.yaml schedule.timezone 덮어쓰기")
    args = ap.parse_args(argv)

    settings = load_flow_settings()
    if args.command == "check":
        return check_command(settings, args.since)
    if args.command == "serve":
        settings.update({k: v for k, v in (("cron", args.cron), ("timezone", args.timezone)) if v})
        return serve_command(settings, paused=args.paused)
    result = weekly_retrain(execute=args.execute, force=args.force, since=args.since,
                            parameters=parse_params(args.param) or None, wait=not args.no_wait)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
