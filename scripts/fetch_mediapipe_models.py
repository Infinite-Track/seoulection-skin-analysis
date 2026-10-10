"""mediapipe 모델 파일을 구글 공식 주소에서 받는다 (pip install처럼 설치 단계에서 한 번).

mediapipe 패키지(pip)는 실행 엔진만 있고 모델 파일은 따로 받아야 한다.
해시로 버전을 고정한다: 구글이 파일을 바꾸면 해시가 달라져 멈춘다 (전처리 결과가 달라지는 것을 막기 위해).
이미 받은 파일은 해시가 맞으면 건너뛴다. 표준 라이브러리만 쓴다.

사용: python -m scripts.fetch_mediapipe_models
"""
import argparse
import hashlib
import urllib.request

from common.config import load_yaml, resolve

MODELS = {
    "face_landmarker.task": (
        "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/"
        "face_landmarker.task",
        "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"),
    "selfie_multiclass_256x256.tflite": (
        "https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_multiclass_256x256/float32/latest/"
        "selfie_multiclass_256x256.tflite",
        "c6748b1253a99067ef71f7e26ca71096cd449baefa8f101900ea23016507e0e0"),
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="serving")
    args = ap.parse_args()
    out_dir = resolve(load_yaml(args.config)["mediapipe_models"])
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, (url, expected) in MODELS.items():
        path = out_dir / name
        if path.exists() and sha256(path) == expected:
            print(f"있음 {name}")
            continue
        tmp = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        actual = sha256(tmp)
        if actual != expected:
            tmp.unlink()
            raise SystemExit(f"{name} 해시 불일치 (받은 {actual}) — 구글이 파일을 바꿨을 수 있다. "
                             f"전처리 결과가 달라지므로 확인 후 MODELS의 해시를 갱신할 것")
        tmp.replace(path)
        print(f"받음 {name}")


if __name__ == "__main__":
    main()
