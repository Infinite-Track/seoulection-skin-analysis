"""SageMaker 재학습 파이프라인 — 정의·등록·실행 (SageMaker Python SDK v3, AWS 제공 PyTorch 컨테이너, 커스텀 이미지 없음).

단계                                   스크립트 (번들 루트에서 실행)          역할
  Prepare   Processing (CPU)            pipelines/steps/prepare.py           고정 테스트셋 검증, replay + feedback → 학습 데이터셋 train/
  Train     Training (GPU, Spot 옵션)   pipelines/steps/train.py             현재 운영 모델(BaseModelUri)에서 이어 학습 → model.tar.gz
  Evaluate  Processing (CPU)            pipelines/steps/evaluate.py          새 모델 vs 현재 모델, 같은 고정 테스트셋 → evaluation.json
  Condition                                                                  failed_axes == 0 (모든 축에서 현재 모델 이상)
  Register  Processing (CPU, 통과 시)   pipelines/steps/register.py          S3 models/skin_vN/ (서빙 형식 그대로) + Model Registry PendingManualApproval

설정: configs/pipeline.yaml (이름·컨테이너·인스턴스·기본값), configs/paths.yaml (버킷·접두사·리전), 환경변수 SAGEMAKER_ROLE_ARN (실행 역할).
파라미터 (실행 때 바꿀 수 있음): ProcessingInstanceType, TrainingInstanceType, BaseModelUri, TestsetUri, ReplayUri, FeedbackUri,
                                 MinFeedbackCrops, Epochs
산출물: s3://<bucket>/<s3.artifacts>/<name>/<실행 id>/{prepare,train,evaluation,register}/, 코드 번들은 …/<name>/code/

사용
  python -m pipelines.sagemaker_pipeline definition --dry-run [--out FILE]   AWS 호출 없이 정의 JSON 출력 (검토용)
  python -m pipelines.sagemaker_pipeline upsert [--no-spot]                  파이프라인 생성·갱신 — 코드 번들을 S3에 올린다 (사람이 실행)
  python -m pipelines.sagemaker_pipeline start [--param Epochs=2 ...]        1회 실행 시작 (Phase 4 Prefect 플로우도 start_execution을 쓴다)

코드 번들: common/, training/, configs/, scripts/, pipelines/ + 단계 스크립트 복사본(루트) + requirements.txt(= pipelines/requirements-container.txt).
SageMaker Processing은 번들 루트의 스크립트만 실행하므로 pipelines/steps/*.py를 루트에도 복사한다. 번들은 upsert 때마다 새로 올라간다.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from common.config import REPO_ROOT
from training.config import DEFAULT_PATHS, load_paths, load_yaml

PIPELINE_CONFIG = "configs/pipeline.yaml"
ROLE_ENV = "SAGEMAKER_ROLE_ARN"
BUNDLE_DIRS = ("common", "training", "configs", "scripts", "pipelines")
BUNDLE_IGNORE = ("__pycache__", "*.pyc", ".DS_Store", "*.sh", "*.log")
CONTAINER_REQUIREMENTS = "pipelines/requirements-container.txt"
STEP_SCRIPTS = ("prepare", "train", "evaluate", "register")
EVALUATION_REPORT = "EvaluationReport"
DRY_RUN_ROLE = "arn:aws:iam::000000000000:role/DRY-RUN-SAGEMAKER_ROLE_ARN"  # 정의 검토용 자리 표시 (실제 계정 아님)


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def load_settings(pipeline_config=PIPELINE_CONFIG, paths_config=DEFAULT_PATHS):
    """configs/pipeline.yaml + configs/paths.yaml → 한 dict. S3 주소는 여기서만 만든다."""
    p, paths = load_yaml(pipeline_config), load_paths(paths_config)
    bucket, s3 = paths["s3"]["bucket"], paths["s3"]

    def uri(prefix, *parts):
        return "s3://" + "/".join([bucket, prefix.strip("/"), *parts]) + "/"

    train = p["training"]
    if train["spot"] and train["max_wait_seconds"] < train["max_run_seconds"]:
        raise ValueError("configs/pipeline.yaml: training.max_wait_seconds는 max_run_seconds 이상이어야 한다")
    return dict(
        name=p["name"], region=paths["aws_region"], bucket=bucket, group=p["model_package_group"],
        artifacts_prefix=f"{s3['artifacts'].strip('/')}/{p['name']}", artifacts=uri(s3["artifacts"], p["name"]),
        models_prefix=s3["models"], base_model=uri(s3["models"], p["current_model"]),
        testset=uri(s3["testset"], p["prepare"]["testset_name"]), replay=uri(s3["replay"], p["prepare"]["replay_name"]),
        feedback=uri(s3["feedback"]),
        framework=p["framework"], processing=p["processing"], training=train, prepare=p["prepare"],
        condition=p["condition"], register=p["register"],
    )


def role_arn(dry_run=False):
    role = os.environ.get(ROLE_ENV)
    if role:
        return role
    if dry_run:
        print(f"경고: {ROLE_ENV} 없음 — dry-run이라 자리 표시 역할을 쓴다", file=sys.stderr)
        return DRY_RUN_ROLE
    raise SystemExit(f"환경변수 {ROLE_ENV}(SageMaker 실행 역할 ARN)이 필요하다")


def build_source_bundle(dest):
    """컨테이너에 올릴 코드 번들 폴더를 만든다 (레포의 일부만, 모델·데이터·산출물 제외)."""
    dest = Path(dest)
    ignore = shutil.ignore_patterns(*BUNDLE_IGNORE)
    for d in BUNDLE_DIRS:
        shutil.copytree(REPO_ROOT / d, dest / d, ignore=ignore, dirs_exist_ok=True)
    for name in STEP_SCRIPTS:  # Processing은 번들 루트의 스크립트를 실행한다 → 루트에도 복사
        shutil.copyfile(REPO_ROOT / "pipelines" / "steps" / f"{name}.py", dest / f"{name}.py")
    shutil.copyfile(REPO_ROOT / CONTAINER_REQUIREMENTS, dest / "requirements.txt")
    return dest


# ---------------------------------------------------------------------------
# 파이프라인 정의
# ---------------------------------------------------------------------------
def make_session(settings):
    import boto3
    from sagemaker.core.workflow.pipeline_context import PipelineSession

    boto_session = boto3.Session(region_name=settings["region"])
    return PipelineSession(boto_session=boto_session, default_bucket=settings["bucket"],
                           default_bucket_prefix=settings["artifacts_prefix"])


def build_pipeline(settings, session, role, source_dir, spot=None):
    """정의만 만든다 (AWS 리소스 생성 없음 — 단, definition()/upsert()는 코드 번들을 S3에 올린다)."""
    from sagemaker.core import image_uris
    from sagemaker.core.processing import FrameworkProcessor
    from sagemaker.core.shapes.shapes import (CheckpointConfig, OutputDataConfig, ProcessingInput, ProcessingOutput,
                                              ProcessingS3Input, ProcessingS3Output, StoppingCondition)
    from sagemaker.core.workflow.conditions import ConditionEquals
    from sagemaker.core.workflow.execution_variables import ExecutionVariables
    from sagemaker.core.workflow.functions import Join, JsonGet
    from sagemaker.core.workflow.parameters import ParameterInteger, ParameterString
    from sagemaker.core.workflow.properties import PropertyFile
    from sagemaker.mlops.workflow.condition_step import ConditionStep
    from sagemaker.mlops.workflow.pipeline import Pipeline
    from sagemaker.mlops.workflow.steps import ProcessingStep, TrainingStep
    from sagemaker.train import ModelTrainer
    from sagemaker.train.configs import Compute, InputData, SourceCode

    name, region, bucket = settings["name"], settings["region"], settings["bucket"]
    fw, proc_cfg, train_cfg = settings["framework"], settings["processing"], settings["training"]
    spot = train_cfg["spot"] if spot is None else spot
    source_dir = str(source_dir)

    # 파라미터 — 기본값은 configs/pipeline.yaml·paths.yaml
    processing_instance = ParameterString("ProcessingInstanceType", default_value=proc_cfg["instance_type"])
    training_instance = ParameterString("TrainingInstanceType", default_value=train_cfg["instance_type"])
    base_model_uri = ParameterString("BaseModelUri", default_value=settings["base_model"])
    testset_uri = ParameterString("TestsetUri", default_value=settings["testset"])
    replay_uri = ParameterString("ReplayUri", default_value=settings["replay"])
    feedback_uri = ParameterString("FeedbackUri", default_value=settings["feedback"])
    min_feedback = ParameterInteger("MinFeedbackCrops", default_value=settings["prepare"]["min_feedback_crops"])
    epochs = ParameterInteger("Epochs", default_value=train_cfg["epochs"])

    artifacts = settings["artifacts"].rstrip("/")

    def run_uri(*parts):  # 실행마다 다른 산출물 경로
        return Join(on="/", values=[artifacts, ExecutionVariables.PIPELINE_EXECUTION_ID, *parts])

    # 컨테이너 이미지는 정의 시점에 정한다 (파라미터로는 CPU/GPU를 알 수 없다) → 인스턴스 파라미터는 같은 프로세서 종류 안에서만 바꿀 것
    image = {kind: image_uris.retrieve("pytorch", region, version=fw["version"], py_version=fw["py_version"],
                                       instance_type=cfg["instance_type"], image_scope="training")
             for kind, cfg in (("cpu", proc_cfg), ("gpu", train_cfg))}

    def processor(job):
        return FrameworkProcessor(image_uri=image["cpu"], role=role, instance_count=1, instance_type=processing_instance,
                                  command=["python3"], volume_size_in_gb=proc_cfg["volume_size_gb"],
                                  max_runtime_in_seconds=proc_cfg["max_runtime_seconds"], base_job_name=f"{name}-{job}",
                                  code_location=f"{artifacts}/code", sagemaker_session=session)

    def s3_in(channel, uri):
        return ProcessingInput(input_name=channel, s3_input=ProcessingS3Input(
            s3_uri=uri, local_path=f"/opt/ml/processing/input/{channel}", s3_data_type="S3Prefix", s3_input_mode="File",
            s3_data_distribution_type="FullyReplicated"))

    def s3_out(channel, uri):
        return ProcessingOutput(output_name=channel, s3_output=ProcessingS3Output(
            s3_uri=uri, local_path=f"/opt/ml/processing/output/{channel}", s3_upload_mode="EndOfJob"))

    def run(job, inputs, outputs, arguments):
        return processor(job).run(code=f"{job}.py", source_dir=source_dir, requirements="requirements.txt",
                                  inputs=inputs, outputs=outputs, arguments=arguments)

    # 1) Prepare — feedback은 비어 있을 수 있어 채널이 아니라 주소로 넘긴다 (스크립트가 boto3로 내려받는다)
    prepare = ProcessingStep(
        name="Prepare",
        step_args=run("prepare",
                      inputs=[s3_in("testset", testset_uri), s3_in("replay", replay_uri)],
                      outputs=[s3_out("train", run_uri("prepare", "train")), s3_out("prepare", run_uri("prepare", "manifest"))],
                      arguments=["--feedback", feedback_uri, "--testset-name", settings["prepare"]["testset_name"],
                                 "--min-feedback-crops", Join(on="", values=[min_feedback])]),
    )
    train_data = prepare.properties.ProcessingOutputConfig.Outputs["train"].S3Output.S3Uri

    # 2) Train — 현재 운영 모델에서 이어 학습. Spot이면 체크포인트(last.pt)로 재개
    checkpoint_local = train_cfg["checkpoint_local_path"]
    hyperparameters = {"config": train_cfg["config"], "epochs": epochs, "checkpoint-dir": checkpoint_local,
                       **{str(k): v for k, v in (train_cfg.get("hyperparameters") or {}).items()}}
    trainer = ModelTrainer(
        training_image=image["gpu"], role=role, sagemaker_session=session, base_job_name=f"{name}-train",
        input_s3_key_prefix="code",  # default_bucket_prefix(artifacts) 아래 code/<job>/input/{code,sm_drivers}
        source_code=SourceCode(source_dir=source_dir, requirements="requirements.txt", entry_script="train.py"),
        compute=Compute(instance_type=training_instance, instance_count=1, volume_size_in_gb=train_cfg["volume_size_gb"],
                        enable_managed_spot_training=spot),
        stopping_condition=StoppingCondition(max_runtime_in_seconds=train_cfg["max_run_seconds"],
                                             **({"max_wait_time_in_seconds": train_cfg["max_wait_seconds"]} if spot else {})),
        output_data_config=OutputDataConfig(s3_output_path=run_uri("train"), compression_type="GZIP"),
        checkpoint_config=CheckpointConfig(s3_uri=run_uri("train", "checkpoints"), local_path=checkpoint_local),
        hyperparameters=hyperparameters,
    )
    train = TrainingStep(
        name="Train",
        step_args=trainer.train(input_data_config=[InputData(channel_name="train", data_source=train_data),
                                                   InputData(channel_name="base_model", data_source=base_model_uri)]),
    )
    model_tar = train.properties.ModelArtifacts.S3ModelArtifacts

    # 3) Evaluate — 새 모델 vs 현재 모델, 같은 고정 테스트셋
    cond = settings["condition"]
    report = PropertyFile(name=EVALUATION_REPORT, output_name="evaluation", path="evaluation.json")
    evaluate = ProcessingStep(
        name="Evaluate",
        step_args=run("evaluate",
                      inputs=[s3_in("candidate", model_tar), s3_in("baseline", base_model_uri), s3_in("testset", testset_uri)],
                      outputs=[s3_out("evaluation", run_uri("evaluation"))],
                      arguments=["--axes", cond["axes"], "--tolerance", str(cond["tolerance"]),
                                 "--testset-name", settings["prepare"]["testset_name"]]),
        property_files=[report],
    )

    # 5) Register — 통과했을 때만
    reg = settings["register"]
    register_args = ["--bucket", bucket, "--models-prefix", settings["models_prefix"], "--version-prefix", reg["version_prefix"],
                     "--model-package-group", settings["group"], "--region", region,
                     "--pipeline-execution-arn", ExecutionVariables.PIPELINE_EXECUTION_ARN, "--base-model-uri", base_model_uri]
    if reg.get("model_name_format"):
        register_args += ["--model-name-format", reg["model_name_format"]]
    register = ProcessingStep(
        name="Register",
        step_args=run("register",
                      inputs=[s3_in("candidate", model_tar),
                              s3_in("evaluation", evaluate.properties.ProcessingOutputConfig.Outputs["evaluation"].S3Output.S3Uri)],
                      outputs=[s3_out("register", run_uri("register"))],
                      arguments=register_args),
    )

    # 4) Condition — 모든 축에서 현재 모델 이상 (evaluation.json failed_axes == 0). 불통과면 등록 없이 정상 종료
    condition = ConditionStep(
        name="ModelBetterOrEqual",
        conditions=[ConditionEquals(left=JsonGet(step_name=evaluate.name, property_file=report, json_path="failed_axes"), right=0)],
        if_steps=[register], else_steps=[],
    )

    return Pipeline(
        name=name,
        parameters=[processing_instance, training_instance, base_model_uri, testset_uri, replay_uri, feedback_uri, min_feedback, epochs],
        steps=[prepare, train, evaluate, condition],
        sagemaker_session=session,
        pipeline_experiment_config=None,
    )


# ---------------------------------------------------------------------------
# dry-run: S3 업로드·역할 검증을 대역으로 바꿔 AWS 없이 정의를 만든다
# ---------------------------------------------------------------------------
@contextmanager
def no_aws(settings):
    from unittest import mock

    from sagemaker.core.helper.session_helper import Session

    os.environ.setdefault("SAGEMAKER_VALIDATE_ROLE", "false")
    fake_prefix = f"s3://{settings['bucket']}/{settings['artifacts_prefix']}/dry-run"
    with mock.patch.object(Session, "default_bucket", lambda self: settings["bucket"]), \
         mock.patch.object(Session, "upload_data", lambda self, path, bucket=None, key_prefix="data", **kw: f"s3://{bucket}/{key_prefix}"), \
         mock.patch("sagemaker.core.s3.client.S3Uploader.upload_string_as_file_body",
                    lambda body, desired_s3_uri, kms_key=None, sagemaker_session=None, **kw: desired_s3_uri), \
         mock.patch("sagemaker.core.s3.client.S3Uploader.upload",
                    lambda local_path, desired_s3_uri, kms_key=None, sagemaker_session=None, **kw: desired_s3_uri or fake_prefix):
        yield


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_params(items):
    params = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"--param 형식은 이름=값: {item}")
        params[key] = value
    return params


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pipeline-config", default=PIPELINE_CONFIG)
    ap.add_argument("--paths-config", default=DEFAULT_PATHS)
    sub = ap.add_subparsers(dest="command", required=True)
    d = sub.add_parser("definition", help="정의 JSON 출력")
    d.add_argument("--dry-run", action="store_true", help="AWS 호출 없음 (S3 업로드·역할 검증 생략)")
    d.add_argument("--no-spot", action="store_true")
    d.add_argument("--out", help="JSON 저장 경로 (기본: 표준 출력)")
    u = sub.add_parser("upsert", help="파이프라인 생성·갱신 (코드 번들 S3 업로드 포함)")
    u.add_argument("--no-spot", action="store_true", help="학습을 온디맨드로")
    u.add_argument("--description", default="Seoulection skin 모델 주간 재학습")
    s = sub.add_parser("start", help="실행 시작")
    s.add_argument("--param", action="append", default=[], help="파라미터 덮어쓰기 이름=값 (예: Epochs=2)")
    s.add_argument("--name", help="실행 표시 이름")
    args = ap.parse_args(argv)

    settings = load_settings(args.pipeline_config, args.paths_config)
    os.environ.setdefault("AWS_DEFAULT_REGION", settings["region"])  # SDK가 import 중에 만드는 boto3 클라이언트도 리전이 필요하다
    session = make_session(settings)

    if args.command == "start":
        from sagemaker.mlops.workflow.pipeline import Pipeline

        execution = Pipeline(name=settings["name"], sagemaker_session=session).start(
            parameters=parse_params(args.param) or None, execution_display_name=args.name)
        print(execution.arn)
        return

    dry_run = args.command == "definition" and args.dry_run
    role = role_arn(dry_run=dry_run)
    spot = False if args.no_spot else None
    with tempfile.TemporaryDirectory(prefix="skin-pipeline-") as tmp:
        bundle = build_source_bundle(Path(tmp) / "source")
        if dry_run:
            with no_aws(settings):
                definition = build_pipeline(settings, session, role, bundle, spot=spot).definition()
        else:
            pipeline = build_pipeline(settings, session, role, bundle, spot=spot)
            if args.command == "upsert":
                response = pipeline.upsert(role_arn=role, description=args.description)
                print(f"등록됨: {response['PipelineArn']}")
                return
            definition = pipeline.definition()
    if args.out:
        Path(args.out).write_text(definition, encoding="utf-8")
        print(f"저장 {args.out}")
    else:
        print(json.dumps(json.loads(definition), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
