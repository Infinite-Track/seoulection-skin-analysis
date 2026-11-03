"""Register 단계 (Processing, CPU — Condition 통과 시에만): 새 모델을 S3 models/<prefix><N>/에 **서빙 형식 그대로** 저장하고
Model Registry에 PendingManualApproval로 등록한다. 배포는 하지 않는다 (승인·배포는 사람).

버전 N: S3 <models-prefix>/ 아래 <version-prefix><k>/ 폴더의 최댓값 + 1. 모든 객체를 If-None-Match: * 로 올려 기존 객체는 절대
덮어쓰지 않는다 — 다른 실행이 먼저 같은 N을 차지했으면(412) N을 올려 다시 시도한다. 첫 객체(meta.json)가 자리 선점이다.

올리는 파일: meta.json, skin.onnx, skin.onnx.data (서빙 필수) + reference.npz, check_onnx.py, SHA256SUMS.txt (models/skin_v2/와 같은 구성)
            + model.pt, train_history.json (다음 파인튜닝 시작점) + metrics.json, evaluation.json (평가 결과)
올리기 전에 SHA256SUMS.txt와 reference.npz로 산출물이 손상되지 않았는지 확인한다 (check_onnx.py와 같은 검사, 어긋나면 멈춤).
--model-name-format이 있으면 meta.json model.name을 "skin v3"처럼 바꾸고 SHA256SUMS.txt를 다시 쓴다 (서빙 modelVersion = "<name> <version>").

사용: python -m pipelines.steps.register --candidate <model.tar.gz|폴더> --evaluation <폴더> --bucket B --models-prefix models
                                        [--version-prefix skin_v] [--model-package-group G] [--out DIR] [--dry-run]
--dry-run: 검증과 계획 출력만, AWS 호출 없음.
"""
from pipelines.steps.bootstrap import system_libs  # noqa: I001 (common을 import하기 전에)

system_libs()

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

from common.model_files import REQUIRED  # noqa: E402
from pipelines.steps.util import SM_INPUT, SM_OUTPUT, dump_json, unpack_model  # noqa: E402
from training.config import load_yaml  # noqa: E402
from training.export_onnx import HASHED_FILES, ONNX_TOLERANCE, write_checksums  # noqa: E402

PIPELINE_CONFIG = "configs/pipeline.yaml"
CHECKSUMS = "SHA256SUMS.txt"
EXTRA_FILES = ("model.pt", "train_history.json")  # 있으면 같이 올린다
EVALUATION_FILES = ("metrics.json", "evaluation.json")
MAX_VERSION_RETRIES = 10


def verify_model_dir(model_dir):
    """서빙 파일 존재 + SHA256SUMS.txt 일치 + reference.npz 재현 (check_onnx.py와 같은 규칙). 어긋나면 RuntimeError."""
    import onnxruntime as ort

    model_dir = Path(model_dir)
    missing = [f for f in (*REQUIRED, *HASHED_FILES, CHECKSUMS) if not (model_dir / f).is_file()]
    if missing:
        raise RuntimeError(f"{model_dir}: 파일 없음 {missing}")
    for line in (model_dir / CHECKSUMS).read_text(encoding="utf-8").splitlines():
        expected, name = line.split()
        actual = hashlib.sha256((model_dir / name).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"{model_dir}/{name}: 해시 불일치 (손상)")
    reference = np.load(model_dir / "reference.npz")
    meta = json.loads((model_dir / "meta.json").read_text(encoding="utf-8"))
    session = ort.InferenceSession(str(model_dir / "skin.onnx"), providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(int(reference["seed"]))
    worst = 0.0
    for region in reference["regions"]:
        width, height = meta["input"]["canvas"][str(region)]
        x = rng.standard_normal((1, 3, height, width), dtype=np.float32)
        worst = max(worst, float(np.abs(session.run(None, {"crop": x})[0] - reference[str(region)]).max()))
    if worst >= ONNX_TOLERANCE:
        raise RuntimeError(f"{model_dir}: reference.npz와 출력이 다르다 (최대 차이 {worst:.2e})")
    return meta, worst


def rename_model(model_dir, name_format, n):
    """meta.json model.name → name_format.format(name=기본 이름, n=N). 기본 이름은 끝의 ' vK'를 뗀 것. 해시 파일을 다시 쓴다."""
    meta_path = Path(model_dir) / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    base = re.sub(r"\s+v\d+$", "", meta["model"]["name"])
    meta["model"]["name"] = name_format.format(name=base, n=n)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    write_checksums(model_dir)
    return meta["model"]["name"]


def existing_versions(s3, bucket, models_prefix, version_prefix):
    """S3 <models_prefix>/ 바로 아래 <version_prefix><k>/ 폴더의 k 목록."""
    prefix = models_prefix.strip("/") + "/"
    pattern = re.compile(rf"^{re.escape(prefix)}{re.escape(version_prefix)}(\d+)/$")
    versions = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            if m := pattern.match(cp["Prefix"]):
                versions.append(int(m.group(1)))
    return sorted(versions)


def put_new_object(s3, bucket, key, path):
    """If-None-Match: * 로 올린다 → 이미 있으면 412 PreconditionFailed. 돌려주는 값: 올렸으면 True, 이미 있으면 False."""
    from botocore.exceptions import ClientError

    try:
        with open(path, "rb") as f:
            s3.put_object(Bucket=bucket, Key=key, Body=f, IfNoneMatch="*")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("PreconditionFailed", "412"):
            return False
        raise


def upload_version(s3, bucket, models_prefix, version_prefix, start, files, before_attempt=None, claim_file="meta.json"):
    """비어 있는 <version_prefix><N>/ 을 찾아 files를 올린다. claim_file이 먼저 — 선점에 실패(412)하면 N을 올린다. → (N, s3 uri).

    before_attempt(n): 번호가 정해질 때마다 먼저 부른다 (meta.json 이름을 그 번호로 바꾸는 데 쓴다)."""
    ordered = [claim_file] + [f for f in files if f != claim_file]
    for n in range(start, start + MAX_VERSION_RETRIES):
        if before_attempt:
            before_attempt(n)
        prefix = f"{models_prefix.strip('/')}/{version_prefix}{n}/"
        if not put_new_object(s3, bucket, prefix + claim_file, files[claim_file]):
            print(f"  {prefix} 는 이미 있다 → 다음 번호", flush=True)
            continue
        for name in ordered[1:]:
            if not put_new_object(s3, bucket, prefix + name, files[name]):
                raise RuntimeError(f"s3://{bucket}/{prefix}{name} 가 이미 있다 — 폴더가 부분적으로 차 있다. 사람이 확인할 것")
        return n, f"s3://{bucket}/{prefix}"
    raise RuntimeError(f"{version_prefix}{start}부터 {MAX_VERSION_RETRIES}개 번호가 전부 차 있다")


def ensure_model_package_group(sm, group):
    from botocore.exceptions import ClientError

    try:
        sm.describe_model_package_group(ModelPackageGroupName=group)
    except ClientError as e:
        if "does not exist" not in str(e) and e.response["Error"]["Code"] not in ("ResourceNotFound", "ValidationException"):
            raise
        sm.create_model_package_group(ModelPackageGroupName=group,
                                      ModelPackageGroupDescription="Seoulection skin 모델 (서빙 형식은 S3 models/skin_vN/)")
        print(f"  Model Package Group 생성: {group}", flush=True)


def register_package(sm, group, version, s3_uri, evaluation, execution_arn, model_name, client_token=None):
    """PendingManualApproval 모델 패키지. 서빙은 SageMaker 엔드포인트가 아니라 EC2(ONNX)라 추론 컨테이너는 지정하지 않는다."""
    cand, base = evaluation["candidate"], evaluation["baseline"]
    metadata = {k: v for k, v in dict(
        version=version, s3_uri=s3_uri, model_name=model_name, score=f"{cand['score']:.6f}",
        baseline_model=str(base["model"].get("name", "")), baseline_score=f"{base['score']:.6f}",
        testset=f"{evaluation['testset']['name']} {evaluation['testset']['people']}명",
        pipeline_execution_arn=execution_arn or "",
    ).items() if v}
    request = dict(
        ModelPackageGroupName=group,
        ModelPackageDescription=f"{version} | 점수 {cand['score']:.4f} (현재 {base['score']:.4f}) | {s3_uri}",
        ModelApprovalStatus="PendingManualApproval",
        CustomerMetadataProperties=metadata,
        ModelMetrics={"ModelQuality": {"Statistics": {"ContentType": "application/json", "S3Uri": s3_uri + "evaluation.json"}}},
    )
    if client_token:
        request["ClientToken"] = re.sub(r"[^a-zA-Z0-9-]", "-", client_token)[:36]
    return sm.create_model_package(**request)["ModelPackageArn"]


def main(argv=None):
    defaults = load_yaml(PIPELINE_CONFIG)["register"]
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--candidate", default=f"{SM_INPUT}/candidate", help="새 모델: model.tar.gz, 그 폴더, 또는 풀린 폴더")
    ap.add_argument("--evaluation", default=f"{SM_INPUT}/evaluation", help="Evaluate 출력 폴더 (evaluation.json, metrics.json)")
    ap.add_argument("--out", default=f"{SM_OUTPUT}/register", help="register.json 출력 폴더")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--models-prefix", required=True, help="예: models (paths.yaml s3.models)")
    ap.add_argument("--version-prefix", default=defaults["version_prefix"])
    ap.add_argument("--model-name-format", default=defaults.get("model_name_format"), help="meta.json model.name 형식. 비우면 그대로")
    ap.add_argument("--model-package-group", help="Model Registry 그룹. 비우면 등록 생략")
    ap.add_argument("--region", default=None)
    ap.add_argument("--pipeline-execution-arn", default=None)
    ap.add_argument("--base-model-uri", default=None, help="기록용: 파인튜닝 시작점")
    ap.add_argument("--dry-run", action="store_true", help="검증·계획만, AWS 호출 없음")
    args = ap.parse_args(argv)

    evaluation = json.loads((Path(args.evaluation) / "evaluation.json").read_text(encoding="utf-8"))
    if not evaluation.get("passed") or evaluation.get("failed_axes", 1) != 0:
        raise SystemExit(f"evaluation.json이 통과가 아니다 (failed_axes={evaluation.get('failed_axes')}) → 등록하지 않는다")

    with tempfile.TemporaryDirectory(prefix="register-") as tmp:
        model_dir = unpack_model(args.candidate, tmp)
        if not model_dir.is_relative_to(tmp):  # 이미 풀린 폴더를 받았으면 복사본에서 작업한다 (meta.json을 고치므로)
            model_dir = Path(shutil.copytree(model_dir, Path(tmp) / "model"))
        meta, worst = verify_model_dir(model_dir)
        print(f"산출물 검증 통과: {meta['model']['name']} {meta['version']} (reference 최대 차이 {worst:.1e})", flush=True)
        for name in EVALUATION_FILES:
            shutil.copyfile(Path(args.evaluation) / name, model_dir / name)
        files = {f: model_dir / f for f in (*HASHED_FILES, CHECKSUMS, *EXTRA_FILES, *EVALUATION_FILES) if (model_dir / f).is_file()}

        result = dict(created=datetime.now(timezone.utc).isoformat(timespec="seconds"), dry_run=args.dry_run,
                      bucket=args.bucket, models_prefix=args.models_prefix, version_prefix=args.version_prefix,
                      base_model_uri=args.base_model_uri, pipeline_execution_arn=args.pipeline_execution_arn,
                      score=evaluation["candidate"]["score"], baseline_score=evaluation["baseline"]["score"],
                      files=sorted(files), model_version_date=meta["version"])
        if args.dry_run:
            name = (args.model_name_format or "{name}").format(name=re.sub(r"\s+v\d+$", "", meta["model"]["name"]), n="N")
            result.update(version=f"{args.version_prefix}N", model_name=name, model_package_arn=None)
            print(f"DRY-RUN: s3://{args.bucket}/{args.models_prefix.strip('/')}/{args.version_prefix}N/ 에 {len(files)}개 파일, "
                  f"model.name '{name}', 그룹 {args.model_package_group or '(등록 생략)'}", flush=True)
        else:
            import boto3

            s3 = boto3.client("s3", region_name=args.region)
            versions = existing_versions(s3, args.bucket, args.models_prefix, args.version_prefix)
            start = (max(versions) + 1) if versions else 1
            print(f"기존 버전 {versions or '없음'} → {args.version_prefix}{start}부터 시도", flush=True)
            named = {"model_name": meta["model"]["name"]}

            def before_attempt(n):  # 번호가 정해질 때마다 meta.json 이름을 그 번호로 (해시 파일도 다시 씀)
                if args.model_name_format:
                    named["model_name"] = rename_model(model_dir, args.model_name_format, n)

            got, s3_uri = upload_version(s3, args.bucket, args.models_prefix, args.version_prefix, start, files, before_attempt)
            model_name, version = named["model_name"], f"{args.version_prefix}{got}"
            print(f"저장 {s3_uri} ({len(files)}개 파일)", flush=True)
            arn = None
            if args.model_package_group:
                sm = boto3.client("sagemaker", region_name=args.region)
                ensure_model_package_group(sm, args.model_package_group)
                token = f"{(args.pipeline_execution_arn or '').rsplit('/', 1)[-1]}-{got}" if args.pipeline_execution_arn else None
                arn = register_package(sm, args.model_package_group, version, s3_uri, evaluation, args.pipeline_execution_arn,
                                       model_name, client_token=token)
                print(f"Model Registry 등록 (PendingManualApproval): {arn}", flush=True)
            result.update(version=version, s3_uri=s3_uri, model_name=model_name, model_package_arn=arn)

    dump_json(Path(args.out) / "register.json", result)
    print(f"저장 {Path(args.out) / 'register.json'}", flush=True)


if __name__ == "__main__":
    main()
