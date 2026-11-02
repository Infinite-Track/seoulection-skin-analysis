# MLOps — 재학습 파이프라인 운영 안내

skin 모델(ConvNeXt, `models/skin_v2/`)을 **주 1회** 새 데이터로 이어 학습하고, 고정 테스트셋에서 현재 모델 이상일 때만 새 버전을 S3와
Model Registry에 올린다. **배포는 자동이 아니다** — 승인·배포는 사람이 한다 (§6).

```
Prefect (월 03:00 KST)                SageMaker Pipeline  seoulection-skin-retrain                       사람
─────────────────────                ──────────────────────────────────────────────────────────          ──────────────
S3 feedback/ 신규 파일 수 확인  ──▶   Prepare ─▶ Train ─▶ Evaluate ─▶ Condition ─┬─▶ Register ──▶  S3 models/skin_vN/
  (기준 미달이면 종료)                (CPU)     (GPU,Spot)  (CPU)   모든 축 ≥ 현재   │                 Model Registry PendingManualApproval
                                                                                  └─▶ (불통과) 등록 없이 정상 종료      ──▶ 검토 → 승인 → EC2 배포
```

| 구성 요소 | 코드 | 설정 |
|---|---|---|
| 학습·내보내기·평가 | `training/` ([README](../training/README.md)) | `configs/train.yaml`, `configs/tasks.yaml` |
| 데이터 준비·업로드 | `scripts/` ([README](../scripts/README.md)) | `configs/data.yaml`, `configs/splits/` |
| SageMaker 파이프라인 | `pipelines/sagemaker_pipeline.py`, `pipelines/steps/` ([README](../pipelines/README.md)) | `configs/pipeline.yaml`, `configs/train_pipeline.yaml`, `configs/paths.yaml` |
| Prefect 주간 플로우 | `pipelines/prefect_flow.py`, `pipelines/new_data.py` | `configs/prefect.yaml` |
| 서빙 (EC2) | `serving/`, `common/` — 이 작업에서 수정하지 않았다 | `.env` |

전처리(크롭·캔버스·정규화·타깃 변환)는 학습과 서빙이 **같은 `common/` 코드**를 import한다. 학습 쪽에서 복사·재구현하지 않는다.

## 1. 설치

| 어디서 | 명령 | 들어가는 것 |
|---|---|---|
| 서빙 (EC2, CI) | `pip install -r requirements.txt` | numpy·opencv·mediapipe·onnxruntime·boto3·Kafka·FastAPI |
| 로컬 학습·평가·데이터 준비 | `pip install -r requirements.txt -r requirements-train.txt` | + torch 2.14·timm·onnx·onnxscript·PyYAML·scipy·scikit-learn |
| 파이프라인 등록·Prefect 워커 | `pip install -r requirements.txt -r requirements-pipeline.txt` | + sagemaker SDK v3·prefect 3 (학습 패키지는 불필요) |
| 테스트 | `pip install -r requirements-dev.txt` | pytest·pyflakes (전체 테스트는 위 셋 다 필요, §8) |
| SageMaker 컨테이너 | 자동 — `pipelines/requirements-container.txt` | AWS PyTorch 컨테이너 위에 서빙과 같은 numpy·opencv·mediapipe·onnxruntime + 학습 패키지 |

mediapipe 모델 파일: `python -m scripts.fetch_mediapipe_models` (→ `models/mediapipe/`, 해시 고정).

## 2. 환경변수

코드에 경로·버킷·역할·인스턴스 타입·하이퍼파라미터를 적지 않는다. 값은 `configs/*.yaml`이고, 환경에 따라 달라지는 것만 환경변수다.
예시 파일: [`.env.training.example`](../.env.training.example) (서빙 컨테이너용 `.env.example`과 별개).

| 변수 | 읽는 곳 | 의미 |
|---|---|---|
| `SAGEMAKER_ROLE_ARN` | `pipelines/sagemaker_pipeline.py` | SageMaker 실행 역할. `upsert`와 dry-run 아닌 `definition`에 필수. `definition --dry-run`은 없으면 자리 표시 역할을 쓴다 |
| `AWS_PROFILE` 등 boto3 자격 증명 | 전부 | 액세스 키를 코드·설정에 넣지 않는다. 리전은 `configs/paths.yaml aws_region`을 `AWS_DEFAULT_REGION`으로 넣는다 |
| `SAGEMAKER_VALIDATE_ROLE` | SageMaker SDK | `false`면 `upsert` 때의 `iam:SimulatePrincipalPolicy` 역할 검사를 생략. `--dry-run`은 자동으로 `false` |
| `MIN_NEW_FEEDBACK_FILES` | `pipelines/prefect_flow.py` | 이보다 feedback 신규 파일이 적으면 파이프라인을 시작하지 않는다. 없으면 `configs/prefect.yaml new_data.min_new_files`(1) |
| `PREFECT_API_URL`, `PREFECT_API_KEY` | Prefect | `serve`가 붙을 서버/Cloud. 없으면 로컬 임시 서버 |
| `AIHUB_RAW_ROOT` | `scripts/prepare_aihub.py`, `build_crops.py` | AI-Hub 원본 "1.데이터" 폴더 (원본은 S3에 올리지 않는다) |
| `MEDIAPIPE_MODELS_DIR` | `scripts/build_crops.py`, `fetch_mediapipe_models.py`, 서빙 | mediapipe 모델 폴더. 없으면 `configs/paths.yaml mediapipe_models` |
| `SM_CHANNEL_<데이터셋 이름>`, `SM_CHANNEL_BASE_MODEL`, `SM_MODEL_DIR` | `training/train.py`, `training/dataset.py` | SageMaker가 컨테이너에 넣는다. 있으면 `configs` 경로 대신 쓰고, 없으면(로컬) `configs`·CLI 경로 |
| `MODEL_DIR`, `MODEL_S3_URI` | 서빙 `serving/settings.py` | 배포 때 바꾸는 값 (§6) |

## 3. S3 구조

버킷·접두사는 `configs/paths.yaml`(`s3:`), 리전 `us-east-1`.

```
s3://seoulection-prod-skin-images/
├── models/
│   ├── skin_v2/                     현재 운영 모델 (서빙 형식) + 체크포인트 deploy_model.pt(v2 노트북 이름) — 파인튜닝 시작점·비교 기준
│   └── skin_vN/                     Register가 만든 새 버전 (N = 기존 최댓값 + 1, 덮어쓰기 없음)
│       meta.json skin.onnx skin.onnx.data reference.npz check_onnx.py SHA256SUMS.txt   ← 서빙 형식 (models/skin_v2/와 같다)
│       model.pt train_history.json                                                      ← 다음 파인튜닝 시작점
│       metrics.json evaluation.json                                                     ← 고정 테스트셋 지표, 현재 모델과의 축별 비교
├── datasets/testset/aihub_kr/       고정 테스트셋 165명 (crops.csv, labels.csv, crops/…) — 학습에 절대 쓰지 않는다
├── datasets/replay/aihub_kr/        망각 방지 데이터 100명 (같은 형식)
├── feedback/                        신규 데이터. 표준 데이터셋 형식 폴더(crops.csv + labels.csv + crops/, 깊이 2까지)만 학습에 쓴다
│   └── 2026-11/batch1/…
└── pipeline-artifacts/seoulection-skin-retrain/
    ├── code/…                       upsert 때 올라간 코드 번들 (common/ training/ configs/ scripts/ pipelines/ + 단계 스크립트)
    ├── <실행 id>/prepare/{train,manifest}/   합친 학습 데이터셋, manifest.json
    ├── <실행 id>/train/model.tar.gz          model.pt + 서빙 파일;  train/checkpoints/last.pt (Spot 재개용)
    ├── <실행 id>/evaluation/                 evaluation.json (Condition이 failed_axes를 읽는다), metrics.json, metrics_baseline.json
    ├── <실행 id>/register/register.json      새 버전 이름·S3 주소·Model Package ARN
    └── prefect/last_run.json, active_run.json   Prefect 기준점(watermark), 진행 중 실행 예약 기록
```

표준 데이터셋 형식(`crops.csv`, `labels.csv`, `crops/<person>/<image_id>_<region>.png`)은 `training/dataset.py` 맨 위에 있다.

## 4. 실행 방법

### 4.1 로컬 학습·내보내기·평가 (`training/`)

```bash
python -m training.train                                                   # 처음부터: 인물 80:20 분할 + 조기 종료(patience 6, 최대 15에폭), 최고 에폭 가중치
python -m training.train --val-fraction 0 --epochs 6                       # skin v2 배포 학습 그대로 (검증 없이 6에폭 고정)
python -m training.train --init-from outputs/models/<run>/model.pt --epochs 3 --lr-head 1e-4   # 파인튜닝
python -m training.export_onnx --checkpoint outputs/models/<run>/model.pt  # model.pt → 서빙 파일 6개 (models/skin_v2/와 같은 구성)
python -m training.evaluate --model-dir outputs/models/<run>               # 고정 테스트셋 → metrics.json
python outputs/models/<run>/check_onnx.py                                  # 다른 머신에서 같은 모델인지 (해시 + reference.npz)
```

하이퍼파라미터 우선순위: `TrainConfig` 기본값(모델·손실·학습률 = skin v2, 검증 분할 `val_fraction` 0.2·`patience` 6·`epochs` 15 상한) < `configs/train.yaml` < CLI(`--lr-head 1e-4`처럼). 전체 목록은 `python -m training.train --help`.
학습은 학습 인물을 인물 단위로 80:20 나눠 에폭마다 검증 점수(평가와 같은 지표)를 재고, `patience`만큼 개선이 없으면 멈춘 뒤 최고 에폭 가중치를 내보낸다. 검증 인물은 학습 데이터 안의 분할이고 고정 테스트셋과 별개다(aihub_kr: 테스트 165명 제외 → 640/160). 파이프라인의 `Epochs` 파라미터는 이 상한이다. 조기 종료 직후 끊긴 체크포인트를 재개하면 추가 학습 없이 최고 모델만 다시 저장하고, 유효한 검증 지표가 없으면 멈춘다(`--eval-min-groups`·`--val-fraction` 조정).
동작 확인은 `--epochs 1 --max-batches 20 --l0-batches 5 --num-workers 0`.

### 4.2 데이터 준비 (`scripts/`) — 처음 한 번, 또는 새 데이터 소스

```bash
export AIHUB_RAW_ROOT=/path/to/028.한국인_피부상태_측정_데이터/3.개방데이터/1.데이터
python -m scripts.prepare_aihub                                            # images.csv, labels.csv
python -m scripts.make_splits --dataset outputs/datasets/aihub_kr --import /path/to/test_subjects_165.json   # configs/splits/
python -m scripts.build_crops --dataset outputs/datasets/aihub_kr --workers 6                                # crops.csv + crops/
python -m scripts.upload_datasets --dataset outputs/datasets/aihub_kr            # dry-run (기본): 올릴 목록·크기만
python -m scripts.upload_datasets --dataset outputs/datasets/aihub_kr --execute  # 실제 업로드 (사람이 실행)
```

### 4.3 SageMaker 파이프라인 등록·수동 실행 (`pipelines/sagemaker_pipeline.py`)

```bash
export SAGEMAKER_ROLE_ARN=arn:aws:iam::<계정>:role/<실행 역할>
python -m pipelines.sagemaker_pipeline definition --dry-run --out /tmp/def.json   # AWS 리소스 없이 정의 JSON (검토용)
python -m pipelines.sagemaker_pipeline upsert [--no-spot]                          # 생성·갱신 — 코드 번들을 S3에 올린다 (코드를 바꾸면 다시)
python -m pipelines.sagemaker_pipeline start --param Epochs=2 --param MinFeedbackCrops=0   # 1회 실행 (점검: replay만으로 돌려 본다)
```

`definition --dry-run`은 S3 업로드·역할 검증을 대역으로 바꾼다. SDK가 자격 증명 확인(STS `GetCallerIdentity`, IAM `SimulatePrincipalPolicy`)을
시도하지만 실패해도 정의는 만들어지고 리소스는 생기지 않는다 (`tests/test_sagemaker_pipeline.py`가 이를 검증한다).

파라미터(실행 때 변경): `ProcessingInstanceType`, `TrainingInstanceType`, `BaseModelUri`, `TestsetUri`, `ReplayUri`, `FeedbackUri`, `MinFeedbackCrops`, `Epochs`.
인스턴스 타입은 같은 프로세서 종류(Processing CPU / Training GPU) 안에서만 바꾼다 — 컨테이너 이미지는 등록 시점에 정해진다.
단계 스크립트를 SageMaker 없이 로컬에서 돌리는 방법은 `pipelines/README.md` "단계 스크립트 로컬 실행".

### 4.4 Prefect 주간 플로우 (`pipelines/prefect_flow.py`)

```bash
export MIN_NEW_FEEDBACK_FILES=50
python -m pipelines.prefect_flow check                 # 신규 파일 수·기준점·진행 중 실행 기록 (S3 읽기만, Prefect 없이). 기준 이상 0 / 미만 3
python -m pipelines.prefect_flow run                   # 로컬 1회 — 기본 dry-run: 읽기 + "execute였다면" 계획 로그, 쓰기·시작·대기 없음
python -m pipelines.prefect_flow run --execute [--force] [--param Epochs=2] [--no-wait]   # 실제 시작 (사람이 실행)
python -m pipelines.prefect_flow serve [--paused] [--cron "0 3 * * 1" --timezone Asia/Seoul]  # 스케줄 배포 + 실행 프로세스 (상시)
```

- 스케줄: `configs/prefect.yaml schedule` — 매주 월요일 03:00 KST. 바꾸면 `serve`를 다시 띄운다.
- 흐름: 기준점(`prefect/last_run.json`의 `watermark`) 이후 `feedback/` 신규 파일 수 → 최소 건수 미만이면 `skipped` → 파이프라인 start → 완료 대기(상한 16h) →
  단계 상태·Condition 결과·`evaluation.json`·`register.json` 로그 → `Succeeded`일 때만 기준점 저장.
- 중복 실행 방지: start 전에 `prefect/active_run.json`을 조건부 쓰기로 예약하고(`ClientRequestToken` = `run_id`), 살아 있는 기록이 있으면 새로 시작하지 않고
  그 실행을 이어받는다. 실패(`Failed`/`Stopped`)하면 기록을 해제하고 기준점은 그대로 → 다음 실행이 같은 데이터로 1회 재시도. 상세와 복구 규칙은 `pipelines/README.md`.
- 상시 운영: Prefect 서버/Cloud를 `PREFECT_API_URL`로 가리키고 `serve`를 systemd 등으로 띄운다. 워커에 필요한 권한은 `pipelines/README.md` 끝 부분.

## 5. 등록 조건과 산출물 검증

- **Evaluate**는 새 모델과 현재 모델(`BaseModelUri`)을 **같은 고정 테스트셋**에서 서빙 코드(`common/predictor.py`)로 다시 잰다. 지표는 `models/skin_v2/metrics.json`과 같다
  (순서형 QWK·정확도·±1등급·스피어맨, 회귀 피어슨·스피어맨·MAE).
- **Condition**: 축 = 현재 모델이 낸 (항목, 지표) 전부 (v2 기준 21축). 새 모델이 **모든 축**에서 현재 모델 이상(MAE는 이하, `condition.tolerance` 기본 0)일 때만 통과.
  선택 지표가 좋아져도 다른 지표가 나빠지면 불통과. 불통과는 실패가 아니라 "등록 없이 정상 종료"다.
- **Register**는 올리기 전에 `SHA256SUMS.txt`와 `reference.npz`(고정 seed 난수 입력의 출력)를 재현해 산출물 손상을 막고, `meta.json model.name`을 `skin vN`으로 바꾼다.
  S3는 `If-None-Match: *`로 올려 기존 버전을 절대 덮어쓰지 않는다.

## 6. 승인·배포 절차 (사람)

Register까지 끝나면 S3 `models/skin_vN/`과 Model Registry(그룹 `seoulection-skin`)에 **PendingManualApproval** 패키지가 생긴다. 여기서부터는 사람이 한다.

1. **검토**
   - Prefect 로그 또는 `s3://…/pipeline-artifacts/seoulection-skin-retrain/<실행 id>/evaluation/evaluation.json`: 점수, 축별 `candidate`/`baseline`/`delta`, `failed_axes == 0`.
     `<실행 id>/prepare/manifest/manifest.json`에서 어떤 feedback 소스가 몇 명·몇 크롭 들어갔는지 확인.
   - 새 폴더를 받아 무결성 확인: `aws s3 sync s3://…/models/skin_vN/ /tmp/skin_vN/ && cd /tmp/skin_vN && python check_onnx.py` (해시 5개 OK + 부위별 최대 차이 1e-3 미만).
   - 필요하면 로컬 고정 테스트셋으로 다시 평가: `python -m training.evaluate --model-dir /tmp/skin_vN` → `metrics.json`이 S3의 것과 같은지.
2. **승인/거절** — Model Registry에서 상태를 `Approved` 또는 `Rejected`로 바꾼다 (콘솔, 또는 `aws sagemaker update-model-package --model-package-arn <ARN> --model-approval-status Approved`).
   ARN은 `register.json`·Prefect 로그에 있다. 거절해도 S3 폴더는 지우지 않는다 (번호는 계속 증가하므로 충돌 없음).
3. **EC2 배포** — 서빙은 `MODEL_DIR`에 `meta.json`·`skin.onnx`·`skin.onnx.data`가 없을 때만 `MODEL_S3_URI`에서 받는다(`common/model_files.py`). 그래서 새 버전은 **새 폴더**에 받는다.
   - 호스트에 `models/skin_vN/` 빈 폴더를 만들고 컨테이너에 마운트, `.env`를 `MODEL_DIR=/models/skin_vN`, `MODEL_S3_URI=s3://seoulection-prod-skin-images/models/skin_vN`로 바꾼 뒤 컨테이너를 다시 띄운다.
   - 시작 로그에 `모델 로드: skin vN <날짜>`가 찍히고 `/health`의 모델 버전이 바뀌었는지 확인. 받은 파일은 `cd models/skin_vN && shasum -a 256 -c SHA256SUMS.txt`(`skin.onnx`·`skin.onnx.data`·`meta.json` 줄).
   - `python -m scripts.send_test_message`로 Kafka 왕복을 확인한다. 이후 completed 메시지의 `modelVersion`은 `"skin vN <날짜>"`가 된다.
   - **롤백**: `.env`를 이전 버전(`skin_v2` 폴더·주소)으로 되돌리고 컨테이너를 다시 띄운다. 이전 폴더는 지우지 않는다.
4. **다음 재학습의 기준 모델 갱신** — `configs/pipeline.yaml current_model: skin_vN`으로 바꾸고 `python -m pipelines.sagemaker_pipeline upsert`. 이때부터 Train은 `skin_vN/model.pt`에서 이어 학습하고 Evaluate는 `skin_vN`과 비교한다.
   (배포하지 않은 버전을 기준으로 삼지 않는다 — 운영 중인 모델이 항상 비교 기준이다.)

승인하지 않은 모델은 어디에도 배포되지 않는다. 파이프라인·Prefect에는 배포 단계가 없다.

## 7. 실제 AWS에서 처음 돌릴 때 확인할 것

로컬·대역으로 검증하지 못한 항목 (Phase 3·4 보고서의 목록):

- 컨테이너 pip: `mediapipe==0.10.35`·`opencv-contrib-python==5.0.0.93`·`numpy==2.4.6`의 py312 리눅스 휠 설치와 AWS 컨테이너 기존 패키지 충돌 여부. 시스템 라이브러리는 `pipelines/steps/bootstrap.py`가 apt로 설치.
- `create_model_package`를 `InferenceSpecification` 없이 받는지 (서빙이 SageMaker 엔드포인트가 아니라 추론 컨테이너를 지정하지 않는다).
- `models/skin_v2/deploy_model.pt`의 `tasks`·`stats` 구조가 `train.py --init-from`과 맞는지 (안 맞으면 Train이 항목 정의 불일치로 멈춘다).
- Condition 단계 `Metadata.Condition.Outcome` 값 형식, 산출물 경로의 실행 ID가 실행 ARN 마지막 조각과 같은지.
- S3 조건부 쓰기(`If-None-Match`, `If-Match`)가 대상 버킷에서 동작하는지, `ListPipelineExecutions` 요약에 표시 이름이 들어오는지, `ClientRequestToken` 멱등 창.
- 필요한 IAM 권한: 실행 역할(S3 `datasets/ feedback/ models/ pipeline-artifacts/`, SageMaker Processing·Training·Pipelines, `CreateModelPackage(Group)`, CloudWatch Logs, ECR pull),
  등록하는 사람(`CreatePipeline/UpdatePipeline/StartPipelineExecution`, `iam:PassRole`, 번들 업로드 `s3:PutObject`), Prefect 워커(`pipelines/README.md`).

## 8. 테스트

```bash
python -m pytest -q                                    # 전체 (venv: requirements.txt + train + pipeline + dev)
python -m pytest -q tests/test_train_serve_consistency.py tests/test_training_model.py tests/test_export_reference.py tests/test_sagemaker_pipeline.py
python -m pyflakes common serving scripts training pipelines tests
```

| 파일 | 검증하는 것 | 필요 패키지 |
|---|---|---|
| `tests/test_train_serve_consistency.py` | 학습 Dataset의 텐서 == 서빙 `SkinPredictor.model_input` (같은 PNG·face_h·부위, 비트 단위), 증강 0이면 동일, 타깃 정변환↔서빙 역변환 왕복, `tasks.yaml`·`TrainConfig`·`meta.json` 입력 계약 일치 | torch, PyYAML |
| `tests/test_training_model.py` | `SkinModel` 인자 없이 = v2(파라미터 50,637,427, 16열, 앞 2단계 고정), 인자 변경 반영(백본·헤드·활성화·dropout·freeze·경계값), CORAL 출력 배치, 기본값 < yaml < CLI | torch, timm |
| `tests/test_export_reference.py` | 작은 모델 export → 파일 구성·`SHA256SUMS` 순서 = `models/skin_v2/`, `reference.npz` 재현(ONNX·PyTorch) 허용 오차 1e-3, `check_onnx.py` 통과, 서빙 로더로 로드, 변조 감지, 운영 v2 폴더 검증(로컬에 가중치가 있을 때) | torch, timm, onnxscript, onnxruntime |
| `tests/test_sagemaker_pipeline.py` | 정의 JSON(파라미터·단계·이미지·배선·Spot·Condition·Register 인자), 설정이 `configs`에서만 오는지, 비밀값 없음, 코드 번들, CLI — **모든 AWS API 호출을 차단**한 채 | sagemaker |
| `tests/test_pipeline_steps.py`, `tests/test_prefect_flow.py` | Phase 3·4: 축 비교·tar 분리·feedback 소스 ID / Prefect 플로우(boto3 대역, Prefect 임시 서버) | scipy·scikit-learn / prefect |
| `tests/test_api.py`, `test_scoring.py`, `test_service.py`, `test_worker.py` | 서빙 (이 작업에서 수정하지 않았다) | requirements.txt |

CI(`.github/workflows/ci.yml`)는 서빙 의존성만 설치하므로 학습·파이프라인 테스트는 `pytest.importorskip`으로 **건너뛴다**(에러가 아니다). 전체는 로컬 venv에서 돌린다.
AWS를 호출하는 테스트는 없다 — S3·SageMaker는 대역(`FakeS3`, `FakeSageMaker`)이거나 API 호출 자체를 차단한다.

## 9. 작업 기록

Phase별 보고서와 Codex 검토는 `docs/agents/` (`mlops-plan.md`, `reports/phase-N.md`, `reviews/phase-N.md`).
