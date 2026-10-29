# Phase 3 보고 — SageMaker 파이프라인 (`pipelines/`)

> 작성: Claude, 2026-10-29. 브랜치 `feature/mlops-training`. **AWS 호출 없음** — 정의는 `--dry-run`으로만 만들었고 `upsert`·`start`는 실행하지 않았다.

## 변경 파일

| 파일 | 내용 |
|---|---|
| `pipelines/sagemaker_pipeline.py` **(신규)** | 파이프라인 정의·등록·실행. SageMaker Python SDK **v3**(3.27.0, `sagemaker.train.ModelTrainer` + `sagemaker.mlops.workflow`), AWS 제공 PyTorch 컨테이너(`pytorch-training:2.9-{cpu,gpu}-py312`, 커스텀 이미지 없음). 단계 Prepare(Processing CPU) → Train(Training GPU, Spot) → Evaluate(Processing CPU) → Condition(`failed_axes == 0`) → Register(Processing CPU). 파라미터 8개(`ProcessingInstanceType`, `TrainingInstanceType`, `BaseModelUri`, `TestsetUri`, `ReplayUri`, `FeedbackUri`, `MinFeedbackCrops`, `Epochs`). CLI `definition [--dry-run]` / `upsert [--no-spot]` / `start --param K=V`. 코드 번들(`common/ training/ configs/ scripts/ pipelines/` + 루트 복사본 + `requirements.txt`)을 임시 폴더에 만들어 SDK가 S3에 올린다. `--dry-run`은 S3 업로드·역할 검증을 대역으로 바꿔 AWS 없이 정의 JSON을 만든다 |
| `pipelines/steps/prepare.py` **(신규)** | 고정 테스트셋 검증(인물 집합 = `configs/splits/<name>_test.txt`, 6부위 행·PNG 존재·라벨 — `scripts/crop_index` 재사용) → replay + feedback을 학습 데이터셋 하나로 합침(`person_id`·`image_id`·`file`에 소스 접두사, 하드링크/복사). 테스트 인물: replay에 있으면 **멈춤**, feedback에 있으면 제외. feedback은 S3 주소를 받아 boto3로 내려받고(비어 있어도 됨) 표준 형식 폴더만 데이터셋으로 인식. 신규 ok 크롭 < `--min-feedback-crops`면 멈춤. `manifest.json` 출력 |
| `pipelines/steps/train.py` **(신규)** | Training 진입점 = `training/train.py` 그대로 (`SM_CHANNEL_TRAIN`, `SM_CHANNEL_BASE_MODEL`, `SM_MODEL_DIR`, `--checkpoint-dir /opt/ml/checkpoints`) |
| `pipelines/steps/evaluate.py` **(신규)** | 새 모델(model.tar.gz)과 현재 모델을 **같은 고정 테스트셋**에서 `training/evaluate.py`(서빙 코드)로 다시 평가 → `evaluation.json`(두 모델 metrics + 축별 비교 + `failed_axes`), `metrics.json`(새 모델, v2 형식), `metrics_baseline.json`. 축 = 항목별 선택 지표(`select`) 또는 모든 지표(`all`, MAE는 낮을수록 좋음). NaN은 실패 처리, JSON에는 null |
| `pipelines/steps/register.py` **(신규)** | 통과 시: 산출물 검증(SHA256SUMS + reference.npz 재현, check_onnx.py와 같은 규칙) → N = S3 `models/skin_v<k>/` 최댓값 + 1 → `meta.json model.name`을 `"skin vN"`으로 갱신 + SHA256SUMS 재작성 → **If-None-Match: \*** 조건부 업로드(meta.json 선점, 412면 N+1 재시도, 부분적으로 찬 폴더는 중단) → Model Registry `PendingManualApproval`(그룹 없으면 생성, 추론 컨테이너 미지정, `ModelMetrics`=evaluation.json, 메타데이터에 점수·베이스·실행 ARN). `--dry-run`은 검증·계획만 |
| `pipelines/steps/bootstrap.py`, `util.py`, `__init__.py` **(신규)** | 컨테이너에 mediapipe가 요구하는 `libgl1 libglib2.0-0 libegl1 libgles2`가 없으면 apt 설치(Dockerfile과 같은 목록, root일 때만) / model.tar.gz 풀기, S3 접두사 내려받기, NaN 없는 JSON |
| `pipelines/requirements-container.txt` **(신규)** | 컨테이너에 pip 설치되는 목록: 서빙과 같은 numpy·opencv·mediapipe·onnxruntime 버전 + timm·onnx·onnxscript·PyYAML·scipy·scikit-learn·boto3. **torch는 컨테이너 것**(2.9) |
| `requirements-pipeline.txt` **(신규)** | 등록하는 쪽: `sagemaker>=3.27,<4` |
| `configs/pipeline.yaml` **(신규)** | 이름·Model Registry 그룹·현재 운영 모델(`skin_v2`)·컨테이너 버전·인스턴스(`ml.m5.2xlarge` / `ml.g5.xlarge`)·Spot(6h/12h)·체크포인트 경로·에폭 3·`num-workers 4`·테스트셋/replay 이름·`min_feedback_crops 1`·비교 축 `select`·tolerance 0·버전 접두사 `skin_v`·이름 형식 `"{name} v{n}"` |
| `configs/train_pipeline.yaml` **(신규)** | Training 단계 학습 설정: 데이터셋 `train` 하나(= Prepare 출력, `SM_CHANNEL_TRAIN`), `init_from: null`(= `SM_CHANNEL_BASE_MODEL`) |
| `training/train.py` | `resolve_init_from`: 폴더를 주면 `model.pt` 다음으로 v2 노트북 이름 `deploy_model.pt`를 찾는다 (S3 `models/skin_v2/`에 올라올 파일 이름) |
| `training/export_onnx.py` | `write_checksums(out_dir)` 분리 (Register가 meta.json 갱신 후 재사용). 동작 동일 |
| `pipelines/README.md`, `configs/README.md`, `training/README.md` | 문서 |

금지 경로(`serving/`, 기존 `common/` 파일, `models/`, `Dockerfile`, `requirements.txt`, `.dockerignore`, `.env.example`, 기존 `tests/`) 변경 0줄. 하드코딩 없음: 버킷·접두사·리전은 `paths.yaml`, 인스턴스·컨테이너·하이퍼파라미터는 `pipeline.yaml`, 역할은 `SAGEMAKER_ROLE_ARN`.

## git diff main --stat

```
 .gitignore                           |   3 +
 common/crop_files.py                 |  28 +++
 common/training_targets.py           | 103 +++++++++++
 configs/README.md                    |  14 ++
 configs/data.yaml                    |  17 ++
 configs/paths.yaml                   |  17 ++
 configs/pipeline.yaml                |  45 +++++
 configs/splits/aihub_kr_replay.txt   | 101 +++++++++++
 configs/splits/aihub_kr_test.txt     | 167 ++++++++++++++++++
 configs/tasks.yaml                   |  41 +++++
 configs/train.yaml                   |  19 ++
 configs/train_pipeline.yaml          |  12 ++
 docs/agents/mlops-plan.md            | 112 ++++++++++++
 docs/agents/reports/phase-0.md       | 180 +++++++++++++++++++
 docs/agents/reports/phase-1.md       |  84 +++++++++
 docs/agents/reports/phase-2.md       |  70 ++++++++
 docs/agents/reviews/phase-0.md       |  25 +++
 docs/agents/reviews/phase-1.md       |  71 ++++++++
 docs/agents/reviews/phase-2.md       |  66 +++++++
 pipelines/README.md                  |  48 +++++
 pipelines/__init__.py                |   0
 pipelines/requirements-container.txt |  16 ++
 pipelines/sagemaker_pipeline.py      | 329 +++++++++++++++++++++++++++++++++++
 pipelines/steps/__init__.py          |   0
 pipelines/steps/bootstrap.py         |  34 ++++
 pipelines/steps/evaluate.py          | 125 +++++++++++++
 pipelines/steps/prepare.py           | 219 +++++++++++++++++++++++
 pipelines/steps/register.py          | 232 ++++++++++++++++++++++++
 pipelines/steps/train.py             |  14 ++
 pipelines/steps/util.py              |  74 ++++++++
 requirements-pipeline.txt            |   4 +
 requirements-train.txt               |  15 ++
 scripts/README.md                    |  30 ++++
 scripts/build_crops.py               | 117 +++++++++++++
 scripts/crop_index.py                |  87 +++++++++
 scripts/import_notebook_crops.py     |  61 +++++++
 scripts/make_splits.py               | 129 ++++++++++++++
 scripts/prepare_aihub.py             | 121 +++++++++++++
 scripts/upload_datasets.py           | 119 +++++++++++++
 training/README.md                   |  48 +++++
 training/__init__.py                 |   0
 training/config.py                   | 163 +++++++++++++++++
 training/dataset.py                  | 167 ++++++++++++++++++
 training/evaluate.py                 | 112 ++++++++++++
 training/export_onnx.py              | 140 +++++++++++++++
 training/losses.py                   |  70 ++++++++
 training/model.py                    |  99 +++++++++++
 training/templates/check_onnx.py     |  41 +++++
 training/train.py                    | 207 ++++++++++++++++++++++
 49 files changed, 3996 insertions(+)
```
(Phase 0~2 파일 포함, 전부 신규. 금지 경로 0줄. 보고서·plan 갱신은 이 stat 뒤에 추가됨.)

## 검증

로컬 데이터: `outputs/upload/{testset,replay}/aihub_kr`의 CSV + `outputs/datasets/aihub_kr/crops` 링크로 S3 채널과 같은 폴더를 `/tmp/smoke/`에 만들었다. feedback은 replay 인물 2명(42크롭)을 `feedback/batch1/`에 표준 형식으로 복사한 가짜 신규 데이터 + 형식 밖 파일 1개.

| 검증 | 결과 |
|---|---|
| `pyflakes common training scripts pipelines` | 통과 |
| 기존 테스트 `pytest -q` | 24 passed |
| `sagemaker_pipeline definition --dry-run --out /tmp/pipeline-definition.json` (AWS 호출 없음, 자리 표시 역할) | 정의 JSON 생성. 파라미터 8개 기본값 확인. Prepare: 입력 testset/replay 채널 + `--feedback {FeedbackUri}` `--min-feedback-crops {MinFeedbackCrops}` 인자, 출력 `pipeline-artifacts/<name>/<실행 id>/prepare/{train,manifest}`. Train: 이미지 `pytorch-training:2.9-gpu-py312`, 채널 `train`(=Prepare 출력)·`base_model`(={BaseModelUri}), 하이퍼파라미터 `--config configs/train_pipeline.yaml --epochs {Epochs} --checkpoint-dir /opt/ml/checkpoints --num-workers 4`, `EnableManagedSpotTraining true`, `MaxRuntime 21600 / MaxWait 43200`, 체크포인트 S3 = 실행별 경로. Evaluate: 입력 candidate(=`Steps.Train.ModelArtifacts.S3ModelArtifacts`)·baseline·testset, PropertyFile `evaluation.json`. Condition: `JsonGet(failed_axes) == 0` → if `Register`, else 없음. Register 인자에 버킷·`models`·`skin_v`·그룹·리전·실행 ARN·`{name} v{n}` |
| 코드 번들 | 53파일 224KB: `common/ training/ configs/ scripts/ pipelines/` + 루트 `prepare.py train.py evaluate.py register.py requirements.txt` (`.sh`·`__pycache__` 제외) |
| `steps.prepare` (testset 165명 / replay 100명 / feedback 2명) | 1.2초. 테스트셋 165명·ok 4,564 검증 통과, replay 100명 ok 2,772 + feedback 42 → 학습 데이터셋 102명·ok 2,814·라벨 1,938행, 형식 밖 파일 1개 집계, `manifest.json` |
| prepare 거절 사례 | ① replay에 테스트 인물 1명 → **거절**(누수) ② 테스트셋 인물 1명 누락 → **거절**(고정 목록과 다름) ③ `--min-feedback-crops 100`(42개) → **거절** ④ feedback에 테스트 인물 → 그 인물만 **제외**하고 진행, 합친 데이터에 없음 |
| `training.train --config configs/train_pipeline.yaml --init-from outputs/models/smoke_real/model.pt --epochs 1 --max-batches 3` (Prepare 출력) | 학습 크롭 2,685개·102명 로드, 파인튜닝 1에폭 5초(MPS), `model.pt` 저장 |
| `steps.evaluate` 새 모델(스모크 체크포인트 export → model.tar.gz 188MB) vs `models/skin_v2`, 테스트 4명 `--limit-people 4` | 25초. 두 모델 다 110크롭 평가, 축 6개 중 5개 실패 → **불통과**, `evaluation.json`에 축별 값·delta·passed, NaN 없음 |
| `steps.evaluate` v2 vs v2 (`select` / `all`) | 축 6개 / 21개 전부 통과 (같은 모델은 tolerance 0에서도 통과해야 한다) |
| `steps.register --dry-run` (v2, 통과 평가) | 산출물 검증 통과(reference 최대 차이 1.4e-06), 계획 출력, `register.json`(8개 파일 목록) |
| `steps.register --dry-run` (불통과 평가) | `failed_axes=5 → 등록하지 않는다`로 중단 |
| register 가짜 S3 (`/tmp/test_register_fake.py`) | 기존 `skin_v2, skin_v3, skin_v10`(+`mediapipe/`, `skin_vX` 무시) → 다음 11. 11이 경쟁으로 선점된 상황 → **12로 올라가** 업로드, 이름 콜백 [11, 12], 기존 객체 미변경. 부분적으로 찬 폴더 → 중단 |
| register 이름 변경·손상 감지 (`/tmp/test_register_rename.py`) | tar 풀기 → 검증 → `"skin v7"`로 바꾸면 SHA256SUMS의 meta.json 줄만 바뀌고 재검증 통과, `"skin v7"`→v8 재변경 정상. `skin.onnx.data` 1바이트 변조 → 해시 불일치로 거절 |
| `resolve_init_from` | 빈 폴더 → 안내 오류, `deploy_model.pt`만 → 선택, 둘 다 → `model.pt` |

## 설계 결정·가정 (답이 다르면 `configs/pipeline.yaml`만 바꾸면 된다)

- **SDK v3**: `pip install sagemaker`가 3.27.0을 주고(v2 API `sagemaker.estimator`는 없음) v3가 현재 SDK라 v3로 작성. `TrainingStep`은 `ModelTrainer.train()` step_args만 받는다. v2(`2.257.x`)로 바꾸려면 `build_pipeline`만 다시 쓰면 된다.
- **컨테이너 2.9 / py312**: export의 opset 18(dynamo)은 torch 2.9+ 필요(Phase 1 메모). 2.10은 py313이라 mediapipe 0.10.35 휠 유무가 불확실해 2.9를 골랐다.
- **Processing 입력**: replay·테스트셋은 S3 크롭 PNG 채널(원본 43GB를 올리지 않음 — Phase 0 Q9). Prepare가 replay+feedback을 **복사해** 하나로 합친다(실행당 약 1.2GB+ 산출물). 두 채널을 그대로 학습에 주는 방법보다 누수 검사·소스 접두사·형식 검증을 한곳에서 하려고 선택.
- **feedback 형식**: Phase 2 Q3(라벨 출처) 미정이라 "표준 데이터셋 형식 폴더만 학습에 사용, 나머지는 집계"로 두었다. 라벨 출처가 정해지면 `scripts/prepare_*.py` 어댑터로 변환해 올리면 Prepare는 그대로 쓴다. 비어 있어도 되도록 채널 대신 boto3 다운로드.
- **replay 혼합**: 100명 전부 + feedback 전부를 그냥 이어 붙인다(비율 조정 없음, Phase 2 Q1).
- **파인튜닝 하이퍼파라미터**: `Epochs 3`(파라미터), 나머지는 v2 기본값(lr 2e-5/2e-4, 코사인 15에폭 스케줄). stats·l0·경계값은 베이스에서 이어받음(Phase 1 Q12).
- **조건 축**: 기본 `select`(항목별 선택 지표 6축). `all`(21축)도 구현. tolerance 0 = "모든 축에서 현재 모델 이상". 불통과는 FailStep이 아니라 등록 없이 정상 종료 — Prefect(Phase 4)가 ConditionStep 결과를 읽는다.
- **버전·이름**: S3 `models/skin_vN/`(N = 최댓값+1, 조건부 PUT으로 덮어쓰기 불가) + `meta.json model.name = "skin vN"`(v2와 같은 꼴, Phase 0 Q4 가정; `model_name_format: null`이면 그대로). 다음 파인튜닝을 위해 `model.pt`도 같은 폴더에 둔다.
- **Model Registry**: 서빙이 EC2(ONNX)라 추론 컨테이너 없이 `create_model_package`(그룹·설명·메타데이터·ModelMetrics). 승인·배포는 사람.

## 실제 AWS에서만 확인할 수 있는 것 (첫 `upsert`·`start` 때 볼 것)

1. 컨테이너 pip: `mediapipe==0.10.35`·`opencv-contrib-python==5.0.0.93`·`numpy==2.4.6` py312 리눅스 휠 설치와 DLC 기존 패키지 충돌 여부. 시스템 라이브러리는 `bootstrap.py`가 apt로 설치(DLC에 이미 있으면 건너뜀).
2. `create_model_package`를 `InferenceSpecification` 없이 받는지 (boto3 스키마상 필수 아님).
3. SDK v3 `upsert`는 `iam:SimulatePrincipalPolicy`로 역할을 검사한다 — 권한이 없으면 경고 후 진행(`SAGEMAKER_VALIDATE_ROLE=false`로 생략 가능).
4. v2 `deploy_model.pt`(사용자가 S3에 올릴 예정, Phase 1 보류 항목)의 `tasks`·`stats` 구조가 `train.py --init-from`과 맞는지 — 맞지 않으면 Train 단계가 `task_signature` 불일치로 멈춘다.

## 질문

없음
