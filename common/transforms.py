"""부위 크롭 → 모델 입력 (3, H, W) float32.

순서: r_ 부위 좌우 반전 → 배율 통일·캔버스 → 픽셀 정규화 → 다각형 밖 0 (skin v2 학습 때와 같은 순서)
"""
import numpy as np

from common.face_regions import normalize_crop

PIXEL_MEAN = np.array([0.485, 0.456, 0.406], np.float32)  # ImageNet 사전학습 기준
PIXEL_STD = np.array([0.229, 0.224, 0.225], np.float32)


def to_model_input(crop, mask, face_h, region, mirror_right=True):
    """crop: RGB uint8 원본 해상도, mask: 피부 1 / 나머지 0, face_h: 그 사진의 얼굴 세로 길이(px)."""
    if mirror_right and region.startswith("r_"):
        crop, mask = crop[:, ::-1].copy(), mask[:, ::-1].copy()
    crop, mask = normalize_crop(crop, mask, face_h, region)
    image = (crop.astype(np.float32) / 255 - PIXEL_MEAN) / PIXEL_STD
    # 정규화 후 0은 '평균 색'이다. 다각형 밖을 0으로 두면 새까만 테두리 대신 무난한 회색이 된다
    image *= mask[..., None].astype(np.float32)
    return np.ascontiguousarray(image.transpose(2, 0, 1))
