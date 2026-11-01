"""pipelines/sagemaker_pipeline — 파이프라인 **정의** 생성 검증. AWS는 호출하지 않는다 (Phase 5).

botocore의 모든 API 호출을 가로채 기록하고 실패시킨다(`BaseClient._make_api_call`). 그래도 `--dry-run` 정의가 만들어져야 한다 —
SDK가 시도하는 건 자격 증명·역할 확인(STS GetCallerIdentity, IAM SimulatePrincipalPolicy)뿐이고 실패해도 정의에는 영향이 없다.
생성·업로드·시작(CreatePipeline, PutObject, StartPipelineExecution …)은 시도조차 없어야 한다.
"""
import json
import os

import pytest

pytest.importorskip("sagemaker")  # SageMaker SDK v3 (requirements-pipeline.txt) — 서빙 CI 환경에는 없음 → 건너뜀

import botocore.client  # noqa: E402

from common.config import REPO_ROOT  # noqa: E402
from pipelines import sagemaker_pipeline as sp  # noqa: E402
from training.config import load_paths, load_yaml  # noqa: E402

READ_ONLY_IDENTITY_CALLS = {"GetCallerIdentity", "SimulatePrincipalPolicy"}  # 자격 증명·역할 확인 — 리소스를 만들지 않는다


class AwsBlocked(AssertionError):
    pass


@pytest.fixture(scope="module")
def aws_blocked():
    """모듈 전체: 가짜 자격 증명 + 모든 AWS API 호출 차단·기록. 실제 자격 증명(~/.aws, SSO)은 보지 않는다."""
    mp = pytest.MonkeyPatch()
    calls = []

    def blocked(self, operation_name, api_params):
        calls.append(operation_name)
        raise AwsBlocked(f"AWS 호출 시도: {operation_name}")

    for key, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                       "AWS_DEFAULT_REGION": "us-east-1", "AWS_EC2_METADATA_DISABLED": "true",
                       "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent", "AWS_CONFIG_FILE": "/nonexistent"}.items():
        mp.setenv(key, value)
    mp.delenv("AWS_PROFILE", raising=False)
    mp.delenv(sp.ROLE_ENV, raising=False)
    mp.setattr(botocore.client.BaseClient, "_make_api_call", blocked)
    yield calls
    mp.undo()


@pytest.fixture(scope="module")
def settings():
    return sp.load_settings()


@pytest.fixture(scope="module")
def definitions(aws_blocked, settings, tmp_path_factory):
    """dry-run 정의 JSON (Spot / 온디맨드). 코드 번들은 임시 폴더, S3 업로드는 no_aws 대역."""
    bundle = sp.build_source_bundle(tmp_path_factory.mktemp("bundle") / "source")
    session = sp.make_session(settings)
    out = {}
    with sp.no_aws(settings):
        for spot in (None, False):
            out[spot] = json.loads(sp.build_pipeline(settings, session, sp.DRY_RUN_ROLE, bundle, spot=spot).definition())
    return out


def steps_by_name(definition):
    steps = {s["Name"]: s for s in definition["Steps"]}
    for s in list(steps.values()):
        for inner in s.get("Arguments", {}).get("IfSteps", []):
            steps[inner["Name"]] = inner
    return steps


def flat_strings(obj):
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in flat_strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in flat_strings(v)]
    return [obj] if isinstance(obj, str) else []


# ---- 설정 ----

def test_settings_come_from_configs_not_code(settings):
    p, paths = load_yaml(sp.PIPELINE_CONFIG), load_paths()
    bucket = paths["s3"]["bucket"]
    assert settings["name"] == p["name"] and settings["region"] == paths["aws_region"] and settings["bucket"] == bucket
    assert settings["base_model"] == f"s3://{bucket}/{paths['s3']['models']}/{p['current_model']}/"
    assert settings["testset"] == f"s3://{bucket}/{paths['s3']['testset']}/{p['prepare']['testset_name']}/"
    assert settings["replay"] == f"s3://{bucket}/{paths['s3']['replay']}/{p['prepare']['replay_name']}/"
    assert settings["feedback"] == f"s3://{bucket}/{paths['s3']['feedback']}/"
    assert settings["artifacts"] == f"s3://{bucket}/{paths['s3']['artifacts']}/{p['name']}/"
    assert settings["artifacts_prefix"] == f"{paths['s3']['artifacts']}/{p['name']}" and settings["models_prefix"] == paths["s3"]["models"]
    assert settings["training"]["max_wait_seconds"] >= settings["training"]["max_run_seconds"]
    assert "axes" not in settings["condition"]  # 등록 조건 축은 설정으로 바꿀 수 없다 (항상 all)


def test_settings_reject_spot_wait_shorter_than_run(tmp_path):
    p = load_yaml(sp.PIPELINE_CONFIG)
    p["training"].update(spot=True, max_run_seconds=100, max_wait_seconds=50)
    bad = tmp_path / "pipeline.yaml"
    bad.write_text(json.dumps(p), encoding="utf-8")  # JSON은 YAML의 부분집합
    with pytest.raises(ValueError, match="max_wait_seconds"):
        sp.load_settings(str(bad))
    p["training"]["spot"] = False
    bad.write_text(json.dumps(p), encoding="utf-8")
    assert sp.load_settings(str(bad))["training"]["spot"] is False  # 온디맨드면 검사하지 않는다


def test_role_arn_from_env_or_dry_run_placeholder(monkeypatch, capsys):
    monkeypatch.delenv(sp.ROLE_ENV, raising=False)
    assert sp.role_arn(dry_run=True) == sp.DRY_RUN_ROLE and "경고" in capsys.readouterr().err
    with pytest.raises(SystemExit, match=sp.ROLE_ENV):
        sp.role_arn(dry_run=False)
    monkeypatch.setenv(sp.ROLE_ENV, "arn:aws:iam::123456789012:role/x")
    assert sp.role_arn(dry_run=False) == "arn:aws:iam::123456789012:role/x" == sp.role_arn(dry_run=True)
    assert "000000000000" in sp.DRY_RUN_ROLE  # 자리 표시 — 실제 계정 ID가 아니다


def test_parse_params():
    assert sp.parse_params(["Epochs=2", "MinFeedbackCrops=0", "X=a=b"]) == {"Epochs": "2", "MinFeedbackCrops": "0", "X": "a=b"}
    assert sp.parse_params([]) == {}
    with pytest.raises(SystemExit, match="이름=값"):
        sp.parse_params(["Epochs"])


# ---- 코드 번들 ----

def test_source_bundle_contains_code_step_scripts_and_container_requirements(tmp_path):
    bundle = sp.build_source_bundle(tmp_path / "source")
    for d in sp.BUNDLE_DIRS:
        assert (bundle / d).is_dir()
    for name in sp.STEP_SCRIPTS:  # Processing은 번들 루트의 스크립트를 실행한다
        assert (bundle / f"{name}.py").read_bytes() == (REPO_ROOT / "pipelines/steps" / f"{name}.py").read_bytes()
    assert (bundle / "requirements.txt").read_bytes() == (REPO_ROOT / sp.CONTAINER_REQUIREMENTS).read_bytes()
    names = {p.name for p in bundle.rglob("*")}
    assert "__pycache__" not in names and not any(n.endswith((".pyc", ".sh", ".log")) for n in names)
    assert not (bundle / "serving").exists() and not (bundle / "models").exists() and not (bundle / "outputs").exists()
    assert (bundle / "configs/tasks.yaml").is_file() and (bundle / "configs/splits/aihub_kr_test.txt").is_file()


# ---- 정의 ----

def test_definition_builds_without_any_aws_resource_call(definitions, aws_blocked):
    """정의 생성 중 시도된 API 호출은 자격 증명·역할 확인뿐이고(전부 차단됨), 생성·업로드·시작 호출은 없다."""
    assert set(aws_blocked) <= READ_ONLY_IDENTITY_CALLS, aws_blocked
    for definition in definitions.values():
        assert definition["Version"] == "2020-12-01" and len(definition["Steps"]) == 4


def test_parameters_and_defaults(definitions, settings):
    params = {p["Name"]: p for p in definitions[None]["Parameters"]}
    train, proc = settings["training"], settings["processing"]
    assert {n: p["DefaultValue"] for n, p in params.items()} == {
        "ProcessingInstanceType": proc["instance_type"], "TrainingInstanceType": train["instance_type"],
        "BaseModelUri": settings["base_model"], "TestsetUri": settings["testset"], "ReplayUri": settings["replay"],
        "FeedbackUri": settings["feedback"], "MinFeedbackCrops": settings["prepare"]["min_feedback_crops"], "Epochs": train["epochs"],
    }
    assert params["Epochs"]["Type"] == "Integer" and params["MinFeedbackCrops"]["Type"] == "Integer"
    assert all(p["Type"] == "String" for n, p in params.items() if n not in ("Epochs", "MinFeedbackCrops"))


def test_step_order_images_and_wiring(definitions, settings):
    steps = steps_by_name(definitions[None])
    assert [s["Name"] for s in definitions[None]["Steps"]] == ["Prepare", "Train", "Evaluate", "ModelBetterOrEqual"]
    assert {steps[n]["Type"] for n in ("Prepare", "Evaluate", "Register")} == {"Processing"} and steps["Train"]["Type"] == "Training"
    fw = settings["framework"]
    for name in ("Prepare", "Evaluate", "Register"):
        image = steps[name]["Arguments"]["AppSpecification"]["ImageUri"]
        assert f"pytorch-training:{fw['version']}-cpu-{fw['py_version']}" in image and "ecr" in image  # AWS 제공 컨테이너
        assert steps[name]["Arguments"]["ProcessingResources"]["ClusterConfig"]["InstanceType"] == {"Get": "Parameters.ProcessingInstanceType"}
    train = steps["Train"]["Arguments"]
    assert f"pytorch-training:{fw['version']}-gpu-{fw['py_version']}" in train["AlgorithmSpecification"]["TrainingImage"]
    assert train["ResourceConfig"]["InstanceType"] == {"Get": "Parameters.TrainingInstanceType"}

    # Prepare: 테스트셋·replay 채널 + feedback 주소·최소 건수 인자 → 실행별 산출물 경로
    prepare = steps["Prepare"]["Arguments"]
    inputs = {i["InputName"]: i["S3Input"]["S3Uri"] for i in prepare["ProcessingInputs"]}
    assert inputs["testset"] == {"Get": "Parameters.TestsetUri"} and inputs["replay"] == {"Get": "Parameters.ReplayUri"}
    args = prepare["AppSpecification"]["ContainerArguments"]
    assert args[:2] == ["--feedback", {"Get": "Parameters.FeedbackUri"}] and "--min-feedback-crops" in args
    outputs = {o["OutputName"]: o["S3Output"]["S3Uri"]["Std:Join"]["Values"] for o in prepare["ProcessingOutputConfig"]["Outputs"]}
    assert outputs["train"] == [settings["artifacts"].rstrip("/"), {"Get": "Execution.PipelineExecutionId"}, "prepare", "train"]

    # Train: 입력 train = Prepare 출력, base_model = 현재 운영 모델 파라미터. 하이퍼파라미터는 설정에서
    channels = {c["ChannelName"]: c["DataSource"]["S3DataSource"]["S3Uri"] for c in train["InputDataConfig"]}
    assert channels["train"] == {"Get": "Steps.Prepare.ProcessingOutputConfig.Outputs['train'].S3Output.S3Uri"}
    assert channels["base_model"] == {"Get": "Parameters.BaseModelUri"}
    hp = train["HyperParameters"]
    assert hp["config"] == settings["training"]["config"] and hp["epochs"] == {"Get": "Parameters.Epochs"}
    assert hp["checkpoint-dir"] == settings["training"]["checkpoint_local_path"] == train["CheckpointConfig"]["LocalPath"]
    assert hp["num-workers"] == str(settings["training"]["hyperparameters"]["num-workers"])
    assert train["CheckpointConfig"]["S3Uri"]["Std:Join"]["Values"][-2:] == ["train", "checkpoints"]
    assert train["OutputDataConfig"]["CompressionType"] == "GZIP"

    # Evaluate: 새 모델(Train 산출물) vs 현재 모델, 같은 테스트셋, 축은 항상 all
    evaluate = steps["Evaluate"]["Arguments"]
    inputs = {i["InputName"]: i["S3Input"]["S3Uri"] for i in evaluate["ProcessingInputs"]}
    assert inputs["candidate"] == {"Get": "Steps.Train.ModelArtifacts.S3ModelArtifacts"}
    assert inputs["baseline"] == {"Get": "Parameters.BaseModelUri"} and inputs["testset"] == {"Get": "Parameters.TestsetUri"}
    assert evaluate["AppSpecification"]["ContainerArguments"][:4] == ["--axes", "all", "--tolerance", str(settings["condition"]["tolerance"])]

    # Condition: failed_axes == 0 이면 Register, 아니면 아무것도 (불통과는 정상 종료)
    cond = steps["ModelBetterOrEqual"]["Arguments"]
    [c] = cond["Conditions"]
    assert c["Type"] == "Equals" and c["RightValue"] == 0 and c["LeftValue"]["Std:JsonGet"]["Path"] == "failed_axes"
    assert c["LeftValue"]["Std:JsonGet"]["PropertyFile"] == {"Get": f"Steps.Evaluate.PropertyFiles.{sp.EVALUATION_REPORT}"}
    assert [s["Name"] for s in cond["IfSteps"]] == ["Register"] and cond["ElseSteps"] == []

    # Register: 버킷·접두사·버전 접두사·그룹·이름 형식은 설정에서, 실행 ARN·베이스 모델은 실행 변수·파라미터
    reg = steps["Register"]["Arguments"]["AppSpecification"]["ContainerArguments"]
    pairs = dict(zip(reg[::2], reg[1::2]))
    assert pairs["--bucket"] == settings["bucket"] and pairs["--models-prefix"] == settings["models_prefix"]
    assert pairs["--version-prefix"] == settings["register"]["version_prefix"] and pairs["--model-package-group"] == settings["group"]
    assert pairs["--region"] == settings["region"] and pairs["--model-name-format"] == settings["register"]["model_name_format"]
    assert pairs["--pipeline-execution-arn"] == {"Get": "Execution.PipelineExecutionArn"} and pairs["--base-model-uri"] == {"Get": "Parameters.BaseModelUri"}
    inputs = {i["InputName"]: i["S3Input"]["S3Uri"] for i in steps["Register"]["Arguments"]["ProcessingInputs"]}
    assert inputs["candidate"] == {"Get": "Steps.Train.ModelArtifacts.S3ModelArtifacts"}
    assert inputs["evaluation"] == {"Get": "Steps.Evaluate.ProcessingOutputConfig.Outputs['evaluation'].S3Output.S3Uri"}


def test_spot_and_on_demand_definitions(definitions, settings):
    train_cfg = settings["training"]
    spot = steps_by_name(definitions[None])["Train"]["Arguments"]
    assert spot["EnableManagedSpotTraining"] is True
    assert spot["StoppingCondition"] == {"MaxRuntimeInSeconds": train_cfg["max_run_seconds"], "MaxWaitTimeInSeconds": train_cfg["max_wait_seconds"]}
    on_demand = steps_by_name(definitions[False])["Train"]["Arguments"]
    assert on_demand["EnableManagedSpotTraining"] is False
    assert on_demand["StoppingCondition"] == {"MaxRuntimeInSeconds": train_cfg["max_run_seconds"]}


def test_definition_has_no_secrets_and_only_configured_bucket(definitions, settings):
    strings = flat_strings(definitions[None])
    assert not any(s.startswith("AKIA") or "aws_secret" in s.lower() for s in strings)
    roles = {s for s in strings if s.startswith("arn:aws:iam::")}
    assert roles == {sp.DRY_RUN_ROLE}  # dry-run 자리 표시 역할만 — 실제 계정 ID 없음
    s3_uris = {s for s in strings if s.startswith("s3://")}
    assert s3_uris and all(u.startswith(f"s3://{settings['bucket']}/") for u in s3_uris)


# ---- CLI ----

def test_cli_definition_dry_run_writes_json_and_uses_env_role(aws_blocked, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(sp.ROLE_ENV, "arn:aws:iam::123456789012:role/sagemaker-exec")
    out = tmp_path / "definition.json"
    before = len(aws_blocked)
    sp.main(["definition", "--dry-run", "--no-spot", "--out", str(out)])
    assert set(aws_blocked[before:]) <= READ_ONLY_IDENTITY_CALLS
    definition = json.loads(out.read_text(encoding="utf-8"))
    steps = steps_by_name(definition)
    assert steps["Train"]["Arguments"]["RoleArn"] == "arn:aws:iam::123456789012:role/sagemaker-exec"
    assert steps["Train"]["Arguments"]["EnableManagedSpotTraining"] is False
    assert "저장" in capsys.readouterr().out and os.environ.get("SAGEMAKER_VALIDATE_ROLE") == "false"


def test_cli_definition_without_dry_run_needs_role(aws_blocked, monkeypatch):
    monkeypatch.delenv(sp.ROLE_ENV, raising=False)
    with pytest.raises(SystemExit, match=sp.ROLE_ENV):
        sp.main(["definition"])


def test_cli_start_passes_parameters_and_display_name(aws_blocked, monkeypatch, capsys):
    """start는 정의를 다시 만들지 않고 등록된 파이프라인 이름으로 실행만 시작한다 (SDK Pipeline.start 대역)."""
    import sagemaker.mlops.workflow.pipeline as pipeline_module

    calls = []

    class FakePipeline:
        def __init__(self, name, sagemaker_session):
            calls.append(("init", name))

        def start(self, parameters=None, execution_display_name=None):
            calls.append(("start", parameters, execution_display_name))
            return type("E", (), {"arn": "arn:aws:sagemaker:us-east-1:000000000000:pipeline/p/execution/1"})()

    monkeypatch.setattr(pipeline_module, "Pipeline", FakePipeline)
    sp.main(["start", "--param", "Epochs=2", "--param", "MinFeedbackCrops=0", "--name", "manual-check"])
    assert calls == [("init", sp.load_settings()["name"]), ("start", {"Epochs": "2", "MinFeedbackCrops": "0"}, "manual-check")]
    assert capsys.readouterr().out.strip().endswith("/execution/1")
    sp.main(["start"])
    assert calls[-1] == ("start", None, None)
