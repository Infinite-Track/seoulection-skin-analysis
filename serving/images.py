"""이미지 가져오기·디코딩. S3는 boto3 기본 인증 체인(EC2 IAM 역할, AWS_REGION)만 쓴다 — 키를 코드·설정에 넣지 않는다."""
import cv2
import numpy as np

from serving.analyzer import INVALID_REQUEST, AnalysisError


def decode_image(data):
    """JPEG/PNG 바이트 → BGR 배열. 못 읽으면 None (분석기가 LOW_QUALITY로 처리)."""
    arr = np.frombuffer(data, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR) if arr.size else None


class S3ImageFetcher:
    def __init__(self):
        self._client = None

    def __call__(self, uri, view):
        if not uri.startswith("s3://"):
            raise AnalysisError(INVALID_REQUEST, f"{view}: s3:// 경로가 아님")
        if self._client is None:
            import boto3
            self._client = boto3.client("s3")
        from botocore.exceptions import ClientError
        bucket, _, key = uri[5:].partition("/")
        try:
            data = self._client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "NoSuchBucket", "404"):
                raise AnalysisError(INVALID_REQUEST, f"{view}: S3에 없음 ({uri})") from e
            raise  # 권한 등 서버 쪽 문제 → MODEL_ERROR로 올라간다
        return decode_image(data)
