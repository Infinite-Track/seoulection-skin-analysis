# configs — 경로·항목 정의·학습 설정 (yaml)

코드에 경로·버킷·하이퍼파라미터를 적지 않는다. 전부 여기서 읽는다.

| 파일 | 내용 | 읽는 곳 |
|---|---|---|
| `paths.yaml` | 로컬 산출물 루트, mediapipe 모델 폴더, S3 버킷·접두사, 리전 | `training.config.load_paths`, Phase 2~4 스크립트 |
| `tasks.yaml` | 모델이 예측하는 항목 정의 = **출력 열 규격**. 바꾸면 새 모델(파인튜닝 불가) | `training.config.load_tasks` |
| `train.yaml` | 학습 하이퍼파라미터 (기본값은 skin v2와 같다). 우선순위: `TrainConfig` 기본값 < 이 파일 < CLI | `training.config.config_from_args` |
| `splits/` | 고정 테스트셋 person_id 목록 (한 줄에 하나, `#` 주석). Phase 2 `make_splits.py`가 만든다 | `training.dataset.load_rows`, `pipelines/steps/prepare.py`·`evaluate.py` (테스트셋 대조) |
| `pipeline.yaml` | SageMaker 파이프라인: 이름, 컨테이너 버전, 인스턴스, Spot, 현재 운영 모델, 신규 데이터 최소 건수, 비교 축, 버전 접두사 | `pipelines/sagemaker_pipeline.py`, `pipelines/steps/*` (기본값) |
| `train_pipeline.yaml` | 파이프라인 Training 단계의 학습 설정 (데이터셋 `train` = Prepare 출력, 시작점 = `SM_CHANNEL_BASE_MODEL`) | `training.train` (컨테이너 안) |
| `prefect.yaml` | Prefect 주간 플로우: 플로우·배포 이름, cron(월 03:00 KST)·시간대, 신규 데이터 소스·최소 건수(환경변수 `MIN_NEW_FEEDBACK_FILES` 우선)·기준점 파일, 완료 대기 간격·상한 | `pipelines/prefect_flow.py` |

비밀값(키·계정 ID)은 넣지 않는다. 역할 ARN은 환경변수 `SAGEMAKER_ROLE_ARN`.
