"""Prefect 주간 재학습 플로우 — 매주 월요일 03:00 KST(configs/prefect.yaml schedule, 바꿀 수 있음).

흐름  진행 중 실행 이어받기 → 신규 데이터 확인 → 기준 미달이면 종료 → 실행 예약(active_run.json) → SageMaker 파이프라인 실행
      → 완료까지 대기 → 결과 로그(통과 여부·새 버전·지표) → 기준점 저장 → 예약 해제
      자동 배포 없음: Register 단계가 S3 models/skin_vN/ + Model Registry PendingManualApproval 까지만 하고, 승인·배포는 사람이 한다.

- 파이프라인은 `python -m pipelines.sagemaker_pipeline upsert` 로 미리 등록돼 있어야 한다 (이 플로우는 start·describe만 한다, boto3 sagemaker).
- 신규 데이터 확인은 pipelines/new_data.py 인터페이스. 현재 S3 feedback/ 의 지난 실행 이후 파일 수. 최소 건수는 환경변수
  MIN_NEW_FEEDBACK_FILES (없으면 configs/prefect.yaml new_data.min_new_files). 추천 서버 count·export API 연동은 TODO (new_data.py).
- "지난 실행" 기준점(watermark) = s3://<bucket>/<artifacts>/<파이프라인 이름>/prefect/last_run.json 의 watermark (그때 센 파일 중 가장 최근 시각).
  파이프라인이 Succeeded로 끝났을 때만 갱신 → 실패하면 다음 주에 같은 데이터를 다시 센다. watermark는 뒤로 가지 않는다. --since 로 수동 지정 가능.
- 중복 실행 방지(같은 폴더 prefect/active_run.json): 파이프라인을 시작하기 전에 이 기록을 `If-None-Match: *` 로 만든다 — 이미 있으면
  다른 실행이 진행 중이므로 시작하지 않는다. 기록에는 ClientRequestToken(run_id)·표시 이름·실행 ARN·그때의 기준점이 들어가고,
  플로우가 시작할 때 기록이 있으면 그 실행을 **이어받는다**(ARN describe → 진행 중이면 대기, 끝났으면 결과 로그·기준점 저장·기록 삭제 후
  신규 데이터를 다시 센다). --no-wait·대기 시간 초과·프로세스 재시작 뒤에도 같은 실행을 이어받고, start 는 한 번만 일어난다.
  ARN을 쓰기 전에 죽었으면 표시 이름으로 실행 목록에서 찾고, 그래도 없으면(reserve_stale_seconds 지난 뒤) 예약을 지우고 새로 간다.
  실행이 실패·중단으로 끝나면 기록을 지우고 기준점은 그대로 둔다 → 다음 실행(다음 주 스케줄 또는 수동 `run --execute`)이 같은 데이터로 재시도한다.
  --force 는 "최소 건수 미달이어도 시작"일 뿐, 진행 중 실행이 있으면 force 여도 새로 시작하지 않는다.
- 결과: Condition 단계 Outcome + 산출물 evaluation.json(점수·실패 축)·register.json(새 버전·S3·Model Package ARN)을 읽어 로그로 남긴다.

사용
  python -m pipelines.prefect_flow check [--since ISO]                 신규 데이터 수·기준점·진행 중 실행 (S3 읽기, Prefect 서버 없이)
  python -m pipelines.prefect_flow run [--execute] [--force] [--since ISO] [--param Epochs=2 ...] [--no-wait]
                                                                       로컬 1회 수동 실행. 기본은 dry-run(파이프라인을 시작하지 않음), --execute 로 실제 시작
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
def read_json_s3(s3, bucket, key):
    """S3 JSON 객체 → dict, 없으면 None."""
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    return json.loads(body)


def write_json_s3(s3, bucket, key, obj, if_none_match=False):
    """JSON 저장. if_none_match=True면 객체가 없을 때만 만들어진다(원자적) — 이미 있으면 False를 돌려준다."""
    kwargs = dict(Bucket=bucket, Key=key, Body=json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json")
    if if_none_match:
        kwargs["IfNoneMatch"] = "*"
    try:
        s3.put_object(**kwargs)
    except ClientError as e:
        if if_none_match and e.response["Error"]["Code"] in ("PreconditionFailed", "412", "ConditionalRequestConflict", "409"):
            return False
        raise
    return f"s3://{bucket}/{key}"


def delete_json_s3(s3, bucket, key):
    s3.delete_object(Bucket=bucket, Key=key)


def start_pipeline(sm, name, parameters=None, display_name=None, client_request_token=None):
    """파이프라인 실행 시작 → 실행 ARN. parameters: {이름: 값} (파이프라인 파라미터 덮어쓰기).

    client_request_token(32~128자)은 SageMaker 쪽 멱등 키 — 같은 토큰으로 다시 부르면 새 실행을 만들지 않는다.
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
        if e.response["Error"]["Code"] in ("ResourceNotFound", "ResourceNotFoundException"):
            raise RuntimeError(f"파이프라인 {name!r}이 없다 — 먼저 `python -m pipelines.sagemaker_pipeline upsert`") from e
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


def new_active_record(since, watermark, new_files, parameters):
    """active_run.json 내용 (시작 전). run_id = SageMaker ClientRequestToken, display_name = 실행 표시 이름 (ARN 복구용)."""
    run_id, now = uuid.uuid4().hex, now_utc()
    return dict(run_id=run_id, display_name=f"prefect-{now:%Y%m%d-%H%M%S}-{run_id[:8]}", reserved=iso(now),
                owner=f"{socket.gethostname()}:{os.getpid()}", since=iso(since), watermark=iso(watermark), new_files=new_files,
                parameters=parameters or None, execution_arn=None, started=None)


def marker_document(watermark, new_files, arn, summary):
    """last_run.json 내용. watermark = 그 실행이 센 파일 중 가장 최근 시각 (파일이 없었으면 이전 기준점)."""
    return dict(watermark=iso(watermark), updated=iso(now_utc()),
                execution_arn=arn, status=summary["status"], passed=summary.get("passed"),
                new_files=new_files, version=(summary.get("register") or {}).get("version"))


def merge_marker(existing, document):
    """기준점은 뒤로 가지 않는다: 기존 watermark가 더 최근이면 그 값을 유지한다 (나머지 필드는 새 값)."""
    old, new = parse_time((existing or {}).get("watermark")), parse_time(document.get("watermark"))
    if old and (new is None or old > new):
        return dict(document, watermark=existing["watermark"], watermark_kept=True)
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
    s3, _ = make_clients(settings["region"])
    return read_json_s3(s3, settings["bucket"], settings["active_key"])


@task(name="resolve-active", retries=2, retry_delay_seconds=30, cache_policy=NO_CACHE)
def resolve_active_task(settings, active):
    """기록의 실행 ARN(없으면 표시 이름으로 찾아 기록에 채움)과 현재 상태 → (arn, status). 실행이 없으면 (None, None)."""
    s3, sm = make_clients(settings["region"])
    arn = active.get("execution_arn")
    if not arn:
        reserved = parse_time(active.get("reserved"))
        arn = find_execution(sm, settings["pipeline"], active["display_name"],
                             created_after=reserved - FIND_EXECUTION_MARGIN if reserved else None)
        if arn:
            write_json_s3(s3, settings["bucket"], settings["active_key"], dict(active, execution_arn=arn, recovered=iso(now_utc())))
    if not arn:
        return None, None
    status = describe_status(sm, arn)
    return (arn, status) if status else (None, None)


@task(name="release-active", retries=2, retry_delay_seconds=30, cache_policy=NO_CACHE)
def release_active_task(settings):
    s3, _ = make_clients(settings["region"])
    delete_json_s3(s3, settings["bucket"], settings["active_key"])


@task(name="check-new-data", retries=2, retry_delay_seconds=30, cache_policy=NO_CACHE)
def check_new_data_task(settings, since):
    s3, _ = make_clients(settings["region"])
    return make_source(settings["source"], settings["feedback"], client=s3).check(since)


@task(name="reserve-run", cache_policy=NO_CACHE)
def reserve_run_task(settings, record):
    """active_run.json 을 If-None-Match:* 로 만든다 → 만들어졌으면 True, 이미 있으면 False (다른 실행이 선점)."""
    s3, _ = make_clients(settings["region"])
    return bool(write_json_s3(s3, settings["bucket"], settings["active_key"], record, if_none_match=True))


@task(name="start-pipeline", cache_policy=NO_CACHE)
def start_pipeline_task(settings, parameters, record):
    """예약 기록의 표시 이름·토큰으로 시작하고, 기록에 ARN을 채운다 (우리가 만든 기록이므로 조건 없이 덮어쓴다)."""
    s3, sm = make_clients(settings["region"])
    arn = start_pipeline(sm, settings["pipeline"], parameters, display_name=record["display_name"], client_request_token=record["run_id"])
    write_json_s3(s3, settings["bucket"], settings["active_key"], dict(record, execution_arn=arn, started=iso(now_utc())))
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
    """기준점 저장 — 기존 watermark가 더 최근이면 유지(merge_marker)."""
    s3, _ = make_clients(settings["region"])
    document = merge_marker(read_json_s3(s3, settings["bucket"], settings["marker_key"]), document)
    return write_json_s3(s3, settings["bucket"], settings["marker_key"], document), document


def _finish(settings, log, arn, status, watermark, new_files):
    """종료된 실행 마무리: 요약 로그 → Succeeded면 기준점 저장 → active_run.json 삭제. 요약을 돌려준다."""
    summary = summarize_task(settings, arn, status)
    for line in format_summary(summary):
        log.info(line)
    if status == "Succeeded":
        uri, document = write_marker_task(settings, marker_document(watermark, new_files, arn, summary))
        log.info("기준점 저장 %s (watermark %s%s)", uri, document["watermark"], " — 기존 값이 더 최근이라 유지" if document.get("watermark_kept") else "")
    else:
        log.error("파이프라인 실행이 %s로 끝났다: %s — 기준점은 그대로 (다음 실행이 같은 데이터로 재시도한다)", status, arn)
    release_active_task(settings)
    return summary


@flow(name=FLOW_NAME)
def weekly_retrain(execute: bool = False, force: bool = False, since: str | None = None,
                   parameters: dict | None = None, wait: bool = True) -> dict:
    """주간 재학습. execute=False면 신규 데이터 확인까지만(dry-run). force=True면 기준 미달이어도 시작. since는 기준점 수동 지정(ISO).

    parameters는 파이프라인 파라미터 덮어쓰기 ({"Epochs": "2"}), wait=False면 시작만 하고 끝낸다.
    진행 중 실행(active_run.json)이 있으면 새로 시작하지 않고 그 실행을 이어받는다 (force 여도).
    """
    log = get_run_logger()
    settings = load_flow_settings()
    result = dict(pipeline=settings["pipeline"], previous=None)

    # 1) 진행 중(또는 끝났지만 마무리 안 된) 실행 이어받기
    active = read_active_task(settings)
    if active:
        arn, status = resolve_active_task(settings, active)
        if arn is None:
            age = (now_utc() - (parse_time(active.get("reserved")) or now_utc())).total_seconds()
            if age < settings["reserve_stale_seconds"]:
                log.info("다른 프로세스가 %s 전에 예약하고 시작 중이다(%s) → 이번에는 시작하지 않는다", f"{age:.0f}초", active.get("owner"))
                return dict(result, status="in_progress", execution_arn=None, active=active)
            log.warning("예약만 있고 실행이 없다(%s, %.0f초 전) → 예약을 지우고 계속한다", active.get("owner"), age)
            release_active_task(settings)
        else:
            log.info("이전 실행 이어받음 %s (상태 %s, 예약 %s, 기준점 %s, 신규 %s개)",
                     arn, status, active.get("reserved"), active.get("since") or "없음", active.get("new_files"))
            if status not in TERMINAL:
                if not wait:
                    return dict(result, status="in_progress", execution_arn=arn, active=active)
                status = wait_pipeline_task(settings, arn)   # TimeoutError 면 기록을 남겨 다음 실행이 다시 이어받는다
            result["previous"] = _finish(settings, log, arn, status,
                                         parse_time(active.get("watermark")) or parse_time(active.get("since")), active.get("new_files"))

    # 2) 신규 데이터 확인
    marker = None if since else read_marker_task(settings)
    since_dt = parse_time(since) if since else parse_time((marker or {}).get("watermark"))
    report = check_new_data_task(settings, since_dt)
    log.info("신규 데이터(%s, %s): 기준점 %s 이후 %d개 / 전체 %d개 %s | 최소 %d개",
             report.source, report.location, since_dt.isoformat() if since_dt else "없음(전체)", report.count, report.total,
             report.by_prefix or "", settings["min_new_files"])
    result.update(since=since_dt.isoformat() if since_dt else None, new_data=report.to_dict(), min_new_files=settings["min_new_files"])

    if report.count < settings["min_new_files"] and not force:
        log.info("신규 데이터 %d개 < 최소 %d개 → 이번 주는 재학습하지 않는다", report.count, settings["min_new_files"])
        return dict(result, status="skipped")
    if not execute:
        log.info("dry-run: 파이프라인 %s 을 시작하지 않는다 (실제 실행은 execute=True / --execute). 파라미터 %s",
                 settings["pipeline"], parameters or {})
        return dict(result, status="dry_run")

    # 3) 예약 → 시작 (예약이 선점돼 있으면 시작하지 않는다)
    record = new_active_record(since_dt, report.latest or since_dt, report.count, parameters)
    if not reserve_run_task(settings, record):
        other = read_active_task(settings) or {}
        log.info("다른 실행이 예약을 선점했다(%s, 실행 %s) → 이번에는 시작하지 않는다", other.get("owner"), other.get("execution_arn") or "시작 중")
        return dict(result, status="in_progress", execution_arn=other.get("execution_arn"), active=other)
    arn = start_pipeline_task(settings, parameters, record)
    log.info("파이프라인 %s 시작: %s (표시 이름 %s, 파라미터 %s)", settings["pipeline"], arn, record["display_name"], parameters or {})
    if not wait:
        return dict(result, status="started", execution_arn=arn)

    # 4) 대기 → 결과 로그 → 기준점 저장 → 예약 해제
    try:
        status = wait_pipeline_task(settings, arn)
    except TimeoutError:
        log.warning("대기 시간 초과 — 실행 %s 과 active_run.json 은 그대로 둔다. 다음 실행이 이어받는다", arn)
        raise
    summary = _finish(settings, log, arn, status, report.latest or since_dt, report.count)
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
    active = read_json_s3(s3, settings["bucket"], settings["active_key"])
    since_dt = parse_time(since) if since else parse_time((marker or {}).get("watermark"))
    report = make_source(settings["source"], settings["feedback"], client=s3).check(since_dt)
    enough = report.count >= settings["min_new_files"]
    print(json.dumps(dict(report.to_dict(), marker=marker, active=active, min_new_files=settings["min_new_files"], enough=enough),
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
    r = sub.add_parser("run", help="로컬 1회 수동 실행 (기본 dry-run). 진행 중 실행이 있으면 새로 시작하지 않고 이어받는다")
    r.add_argument("--execute", action="store_true", help="실제로 파이프라인을 시작한다 (없으면 신규 데이터 확인까지만)")
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
