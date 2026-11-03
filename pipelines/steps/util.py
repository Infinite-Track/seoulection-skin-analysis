"""단계 스크립트 공통: 모델 산출물 풀기, S3 접두사 내려받기, NaN 없는 JSON 저장."""
import json
import math
import tarfile
import tempfile
from pathlib import Path

MODEL_TAR = "model.tar.gz"  # SageMaker Training 산출물 이름
SM_INPUT, SM_OUTPUT = "/opt/ml/processing/input", "/opt/ml/processing/output"


def unpack_model(path, workdir):
    """학습 산출물(model.tar.gz, 그 tar가 든 폴더, 또는 이미 풀린 폴더) → meta.json이 있는 폴더(Path).

    tar는 호출마다 workdir 아래 **새 폴더**에 푼다 — 후보·기준 모델이 둘 다 model.tar.gz여도 서로 덮어쓰지 않는다.
    """
    path = Path(path)
    if path.is_dir() and (path / "meta.json").is_file():
        return path
    tar = path if path.is_file() else path / MODEL_TAR
    if not tar.is_file():
        raise FileNotFoundError(f"{path}: {MODEL_TAR}도 meta.json도 없다")
    Path(workdir).mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(prefix=tar.stem.replace(".tar", "") + "-", dir=workdir))
    with tarfile.open(tar) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # filter 인자가 없는 옛 파이썬
            tf.extractall(dest)
    found = sorted(dest.rglob("meta.json"))
    if not found:
        raise FileNotFoundError(f"{tar}: 안에 meta.json이 없다")
    return found[0].parent


def s3_split(uri):
    """s3://bucket/prefix/ → (bucket, prefix)."""
    if not uri.startswith("s3://"):
        raise ValueError(f"S3 주소가 아니다: {uri}")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def download_prefix(uri, dest, client=None):
    """S3 접두사 아래 객체를 전부 dest에 받는다 (비어 있거나 없으면 0). 돌려주는 값: 받은 객체 수."""
    import boto3

    bucket, prefix = s3_split(uri)
    prefix = prefix.rstrip("/") + "/" if prefix else ""
    client = client or boto3.client("s3")
    dest, count = Path(dest), 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("/"):
                continue
            target = dest / obj["Key"][len(prefix):]
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, obj["Key"], str(target))
            count += 1
    return count


def without_nan(obj):
    """float NaN·inf → None (JSON 표준에 없어 SageMaker JsonGet이 못 읽는다)."""
    if isinstance(obj, dict):
        return {k: without_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [without_nan(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def dump_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(without_nan(obj), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return path
