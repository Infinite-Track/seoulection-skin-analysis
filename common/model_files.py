"""모델 파일 준비. MODEL_DIR에 파일이 다 있으면 그대로 쓰고, 없으면 MODEL_S3_URI에서 받는다.

S3 다운로드는 버킷이 아직 없어 실제로 돌려 보지 못했다 (자리만 마련). boto3 기본 인증 체인(EC2 IAM 역할)만 쓴다.
"""
import logging

from common.config import resolve

log = logging.getLogger("skin.model_files")
REQUIRED = ("meta.json", "skin.onnx", "skin.onnx.data")


def ensure_model_files(model_dir, s3_uri=None):
    """필요한 파일이 갖춰진 모델 폴더(Path)를 돌려준다. 없고 S3 주소도 없으면 FileNotFoundError."""
    d = resolve(model_dir)
    missing = [f for f in REQUIRED if not (d / f).exists()]
    if not missing:
        return d
    if not s3_uri:
        raise FileNotFoundError(f"{d}에 {missing} 없음 (MODEL_S3_URI도 설정되지 않음)")
    import boto3
    bucket, _, prefix = s3_uri[5:].partition("/")
    d.mkdir(parents=True, exist_ok=True)
    client = boto3.client("s3")
    for name in missing:
        key = f"{prefix.rstrip('/')}/{name}" if prefix else name
        log.info("S3에서 받는 중: s3://%s/%s → %s", bucket, key, d / name)
        client.download_file(bucket, key, str(d / name))
    return d
