# pipelines — SageMaker 재학습 파이프라인

`sagemaker_pipeline.py`가 파이프라인을 정의·등록·실행하고, `steps/`의 스크립트가 각 단계 안에서 돈다.
AWS 제공 PyTorch 컨테이너(커스텀 이미지 없음) + SageMaker Python SDK v3. 설정은 전부 `configs/pipeline.yaml`·`configs/paths.yaml`, 실행 역할은 환경변수 `SAGEMAKER_ROLE_ARN`.

| 단계 | 종류 | 스크립트 | 하는 일 |
|---|---|---|---|
| Prepare | Processing (CPU) | `steps/prepare.py` | 고정 테스트셋 검증(인물 목록 = `configs/splits/<name>_test.txt`, PNG 존재) → replay + feedback(신규)을 학습 데이터셋 하나로 합침. 테스트 인물은 제외(누수 방지), 신규 크롭이 `MinFeedbackCrops` 미만이면 멈춤 |
| Train | Training (GPU, Spot) | `steps/train.py` = `training/train.py` | 현재 운영 모델(`BaseModelUri`의 `model.pt`/`deploy_model.pt`)에서 이어 학습 → `model.tar.gz` (model.pt + 서빙 파일). Spot 중단 시 `/opt/ml/checkpoints/last.pt`에서 재개 |
| Evaluate | Processing (CPU) | `steps/evaluate.py` | 새 모델·현재 모델을 **같은 고정 테스트셋**으로 `training/evaluate.py`(서빙 코드)로 평가 → `evaluation.json` (metrics.json 형식 + 축별 비교) |
| Condition | — | — | `failed_axes == 0`: **모든 축**(항목별 모든 지표 — 선택 지표 + 정확도·±1등급·스피어맨·MAE, 현재 v2 기준 21축)에서 새 모델 ≥ 현재 모델 (MAE는 ≤, `condition.tolerance` 기본 0). 선택 지표만 좋아지고 다른 지표가 나빠지면 불통과. 불통과면 등록 없이 정상 종료 |
| Register | Processing (CPU) | `steps/register.py` | S3 `models/skin_vN/`에 서빙 형식 그대로 저장(N = 기존 최댓값 + 1, `If-None-Match`로 덮어쓰기 불가) + Model Registry `PendingManualApproval`. 배포는 사람 |

파라미터(실행 때 변경): `ProcessingInstanceType`, `TrainingInstanceType`, `BaseModelUri`, `TestsetUri`, `ReplayUri`, `FeedbackUri`, `MinFeedbackCrops`, `Epochs`.
산출물: `s3://<bucket>/pipeline-artifacts/<name>/<실행 id>/{prepare,train,evaluation,register}/`.

## 명령

```bash
pip install -r requirements.txt -r requirements-pipeline.txt     # 등록하는 쪽 (컨테이너는 pipelines/requirements-container.txt)
export SAGEMAKER_ROLE_ARN=arn:aws:iam::<계정>:role/<SageMaker 실행 역할>

python -m pipelines.sagemaker_pipeline definition --dry-run --out /tmp/def.json   # AWS 호출 없이 정의 JSON (검토용)
python -m pipelines.sagemaker_pipeline upsert [--no-spot]                          # 생성·갱신 — 코드 번들을 S3에 올린다 (사람이 실행)
python -m pipelines.sagemaker_pipeline start --param Epochs=2 --param MinFeedbackCrops=0   # 1회 실행 (파이프라인 점검: replay만으로)
```

`upsert`는 `common/ training/ configs/ scripts/ pipelines/` + 단계 스크립트 복사본 + `requirements.txt`(= `requirements-container.txt`)를 묶어 S3에 올린다.
코드를 바꾸면 다시 `upsert`해야 컨테이너에 반영된다.

## 단계 스크립트 로컬 실행 (SageMaker 없이)

입력은 전부 표준 데이터셋 형식(`crops.csv`, `labels.csv`, 크롭 PNG — `training/dataset.py` 맨 위). `scripts/upload_datasets.py`가 올리는 것과 같은 구성이다.

```bash
python -m pipelines.steps.prepare --testset DIR --replay DIR --feedback DIR|s3://… --out outputs/datasets/pipeline_train --manifest outputs/manifest.json
python -m training.train --config configs/train_pipeline.yaml --init-from <model.pt> --epochs 3     # Prepare 출력으로 학습
python -m pipelines.steps.evaluate --candidate <model.tar.gz|폴더> --baseline models/skin_v2 --testset DIR --out outputs/eval [--limit-people 4] [--axes select]  # select는 분석용(선택 지표 6축만)
python -m pipelines.steps.register --candidate <model.tar.gz|폴더> --evaluation outputs/eval --bucket B --models-prefix models --dry-run
```

## 전제·주의

- S3에 `datasets/testset/<name>/`, `datasets/replay/<name>/`(`scripts/upload_datasets.py --execute`)와 `models/skin_v2/`(서빙 파일 + `deploy_model.pt`)가 있어야 한다. `feedback/`은 비어 있어도 된다.
- `feedback/` 안의 신규 데이터는 **표준 데이터셋 형식 폴더**(`crops.csv` + `labels.csv` + 크롭, 깊이 2까지)만 학습에 쓴다. 다른 파일은 세기만 한다. 소스 ID는 `feedback/` 기준 상대 경로(`feedback/2026-10/batch1`)라 이름이 같은 하위 폴더(`2026-10/batch1`, `2026-11/batch1`)도 섞이지 않는다.
- 인스턴스 타입 파라미터는 같은 프로세서 종류(Processing은 CPU, Training은 GPU) 안에서만 바꾼다 — 컨테이너 이미지는 등록 시점에 정해진다.
- 컨테이너에는 mediapipe가 요구하는 시스템 라이브러리가 없을 수 있어 `steps/bootstrap.py`가 시작 시 apt로 설치한다(Dockerfile과 같은 목록).
- 실행 역할에 필요한 권한: S3 버킷 읽기·쓰기(`pipeline-artifacts/`, `models/`, `datasets/`, `feedback/`), SageMaker Processing·Training·Pipelines, `sagemaker:CreateModelPackage(Group)`, CloudWatch Logs, ECR 공개 이미지 pull. 등록하는 사람에게는 `sagemaker:CreatePipeline/UpdatePipeline/StartPipelineExecution`, `iam:PassRole`, 코드 번들 업로드용 `s3:PutObject`.
