"""부위 크롭 파일 (RGBA PNG) 저장·로드. RGB = 원본 해상도 크롭, 알파 = 피부 마스크 (피부 255, 나머지 0).

skin v2 학습 데이터(face_analysis_AI/data/01.원천데이터)와 같은 형식이다.
학습·데이터 준비 전용 — 서빙은 사진에서 바로 크롭하므로 쓰지 않는다.
"""
import cv2
import numpy as np

from common.face_regions import read_image


def save_region_png(path, crop, mask):
    """crop: RGB uint8, mask: 피부 1(또는 >0) / 나머지 0. 한글 경로도 되게 바이트로 쓴다."""
    rgba = np.dstack([crop, (mask > 0).astype(np.uint8) * 255])
    ok, buf = cv2.imencode(".png", cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
    if not ok:
        raise OSError(f"PNG 인코딩 실패: {path}")
    buf.tofile(str(path))


def load_region_png(path):
    """save_region_png로 저장한 파일 → (crop RGB uint8, mask 0/1 uint8)."""
    rgba = read_image(path, cv2.IMREAD_UNCHANGED)
    if rgba is None:
        raise FileNotFoundError(f"크롭을 읽지 못했다: {path}")
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError(f"RGBA PNG가 아니다 (shape {rgba.shape}): {path}")
    return cv2.cvtColor(rgba[..., :3], cv2.COLOR_BGR2RGB), (rgba[..., 3] > 0).astype(np.uint8)
