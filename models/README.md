# models — 모델 파일 (git·Docker 이미지 밖)

| 폴더 | 내용 | 어디에 |
|---|---|---|
| `skin_v2/` | `skin.onnx`, `skin.onnx.data`(193MB), `meta.json`, `SHA256SUMS.txt`, `metrics.json`(고정 테스트 165명 점수 0.792) | 가중치 2개는 git·이미지 제외. 이 폴더를 컨테이너에 마운트(`MODEL_DIR=/models/skin_v2`). S3 버킷이 생기면 `MODEL_S3_URI`로 대체 |
| `mediapipe/` | `face_landmarker.task`, `selfie_multiclass_256x256.tflite` (구글 배포, 20MB) | git 제외. `python -m scripts.fetch_mediapipe_models`로 받음(해시 고정). Docker 빌드 때 이미지에 포함 |

새 환경에 가중치를 옮긴 뒤 해시 확인:
```bash
cd models/skin_v2 && tr -d '\r' < SHA256SUMS.txt | sha256sum -c     # Mac은 shasum -a 256 -c
```
