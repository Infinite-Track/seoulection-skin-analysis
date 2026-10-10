# models — 서빙·전처리에 쓰는 모델 파일

| 폴더 | 내용 | git |
|---|---|---|
| `mediapipe/` | `face_landmarker.task`, `selfie_multiclass_256x256.tflite` (구글 배포, 20MB) | 제외 — `python -m scripts.fetch_mediapipe_models`로 받음 (해시 고정) |
| `skin_v2/` | 피부 분석 모델 skin v2 (2026-10-04, 800명 학습). `metrics.json` = 고정 테스트 165명 성능 (점수 0.792) | 가중치 제외, 작은 파일만 포함 |

`skin_v2/skin.onnx`, `skin.onnx.data`(193MB)는 GitHub 파일 크기 제한(100MB) 때문에 git에 없다.
새로 클론했으면 받아서 `skin_v2/`에 넣고 해시를 확인한다:

```bash
cd models/skin_v2 && tr -d '\r' < SHA256SUMS.txt | shasum -a 256 -c
```
