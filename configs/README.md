# configs — 경로·항목 정의·학습 설정 (yaml)

코드에 경로·버킷·하이퍼파라미터를 적지 않는다. 전부 여기서 읽는다.

| 파일 | 내용 | 읽는 곳 |
|---|---|---|
| `paths.yaml` | 로컬 산출물 루트, mediapipe 모델 폴더, S3 버킷·접두사, 리전 | `training.config.load_paths`, Phase 2~4 스크립트 |
| `tasks.yaml` | 모델이 예측하는 항목 정의 = **출력 열 규격**. 바꾸면 새 모델(파인튜닝 불가) | `training.config.load_tasks` |
| `train.yaml` | 학습 하이퍼파라미터 (기본값은 skin v2와 같다). 우선순위: `TrainConfig` 기본값 < 이 파일 < CLI | `training.config.config_from_args` |
| `splits/` | 고정 테스트셋 person_id 목록 (한 줄에 하나, `#` 주석). Phase 2 `make_splits.py`가 만든다 | `training.dataset.load_rows` |

비밀값(키·계정 ID)은 넣지 않는다. 역할 ARN은 환경변수 `SAGEMAKER_ROLE_ARN`.
