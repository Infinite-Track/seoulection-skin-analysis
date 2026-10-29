# MLOps 작업 계획

> 작성: Claude / 검토: Codex. 각자 규칙은 `CLAUDE.md`, `AGENTS.md` 참고.
> 지시 예시: "mlops-plan.md의 Phase 1 실행해" / "Phase 1 검토해" / "Phase 1 리뷰 반영해"

## 진행 현황

| Phase | 내용 | 작성 | 검토 | 비고 |
|---|---|---|---|---|
| 0 | 탐색·보고 | [x] | [x] | 보고서 `docs/agents/reports/phase-0.md` · 리뷰 Minor 2건 반영 (2026-10-29) |
| 1 | 학습 모듈 | [x] | [x] | 보고서 `docs/agents/reports/phase-1.md` · 리뷰 Major 1·Minor 2 반영, v2 체크포인트 검증은 보류 (2026-10-29) |
| 2 | 데이터 준비 스크립트 | [x] | [x] | 보고서 `docs/agents/reports/phase-2.md` · 리뷰 Major 2건 반영 (2026-10-29) |
| 3 | SageMaker 파이프라인 | [x] | [ ] | 보고서 `docs/agents/reports/phase-3.md` · AWS 미호출(dry-run 정의·로컬 단계 스모크) (2026-10-29) |
| 4 | Prefect 플로우 | [ ] | [ ] | |
| 5 | 테스트·문서 | [ ] | [ ] | |

---

## 프로젝트 맥락

Seoulection(해외 소비자 대상 K-beauty 개인화 추천)의 자체 피부분석 모델 레포.
`serving/`은 EC2에서 운영 중이고, 이번 작업은 **노트북으로 만든 베이스 모델의 스크립트화 + SageMaker 재학습 파이프라인 + Prefect 주 1회 실행**이다.

- 모델: timm ConvNeXt-Tiny 파인튜닝, AI-Hub 한국인 피부상태 측정 데이터(71645, 약 965명)
- 입력: 정면/좌/우 사진 → MediaPipe로 부위 크롭 → 부위별 지표
- 서빙 형식: ONNX (`models/skin_v2/` 구성 참고)
- 현재 운영 버전: `skin v2 2026-10-04`

## 참고 자료

- 베이스 모델 노트북: `~/Desktop/소마/face_analysis_AI/modeling/v2/deploy.ipynb` (배포 학습·ONNX), `train.ipynb` (5-fold 실험), 모듈 `model_v2.py`·`data_v2.py`, 전처리 `data_preprocessing/face_regions.py`
- `models/skin_v2/`: `meta.json`, `metrics.json`, `reference.npz`, `check_onnx.py`, `SHA256SUMS.txt`
- `common/` 전체 (특히 `model_files.py` — 서빙이 기대하는 모델 파일 형식)
- `serving/analyzer.py`, `serving/scoring.py` (모델 출력이 쓰이는 방식)

## S3 경로 (`configs/`에 정의, 하드코딩 금지)

| 용도 | 경로 |
|---|---|
| 모델 버전 | `s3://seoulection-prod-skin-images/models/skin_vN/` (현재 `skin_v2`) |
| 고정 테스트셋 | `s3://seoulection-prod-skin-images/datasets/testset/` |
| 망각 방지 데이터 | `s3://seoulection-prod-skin-images/datasets/replay/` |
| 신규 데이터 | `s3://seoulection-prod-skin-images/feedback/` |
| 파이프라인 산출물 | `s3://seoulection-prod-skin-images/pipeline-artifacts/` |

- 리전: `us-east-1`
- SageMaker 실행 역할: 환경변수 `SAGEMAKER_ROLE_ARN`

---

## Phase 0 — 탐색·보고 (코드 작성 금지)

다음을 정리해서 보고:
- 노트북 전체 흐름 (데이터 로딩 → 크롭 → 데이터셋 → 모델 → 학습 → 평가 → ONNX)
- 노트북의 하이퍼파라미터 전체 목록과 값 (백본, 입력 크기, freeze 범위, lr, weight decay, 배치, 에포크, loss, 스케줄러, 시드, 타깃 변환 등)
- 노트북 전처리가 `common/`과 같은지, 다르면 어디가 다른지
- 모델 출력 형식(`meta.json` 기준)과 서빙이 기대하는 파일 목록
- `reference.npz`의 용도와 검증 방법
- 불확실한 점은 질문으로 남김

## Phase 1 — 학습 모듈 (`training/`)

- `training/model.py`
  - 모델 클래스는 **모든 하이퍼파라미터를 `__init__` 인자**로 받고, 기본값은 skin v2와 동일
  - 인자 없이 생성하면 skin v2와 같은 구조
- `training/config.py`
  - 학습 하이퍼파라미터를 `TrainConfig` dataclass로 정의, 기본값은 skin v2와 동일
  - 우선순위: 기본값 < `configs/train.yaml` < CLI 인자 (`--lr 1e-4`처럼 즉시 변경)
- `training/dataset.py`, `losses.py`, `train.py`, `evaluate.py`, `export_onnx.py`
  - 전처리는 `common/` import
  - `train.py`는 로컬·SageMaker 양쪽 동작: `SM_CHANNEL_*`, `SM_MODEL_DIR`이 있으면 사용, 없으면 configs 경로
  - 파인튜닝용 `--init-from <모델 경로>` 지원
  - `export_onnx.py` 출력은 `models/skin_v2/`와 같은 파일 구성
- `requirements-train.txt` 신규 (서빙 `requirements.txt` 수정 금지)
- 검증: 작은 데이터로 1 에포크 스모크 테스트

## Phase 2 — 데이터 준비 스크립트 (`scripts/`)

- `make_splits.py`: 인물 단위 분할, held-out 테스트셋 person_id를 `configs/splits/`에 저장 (시드 고정)
- `build_crops.py`: AI-Hub 원본 → `common/`으로 크롭 (원본 경로는 환경변수)
- `upload_datasets.py`: 테스트셋·망각 방지 데이터 S3 업로드 (기본 `--dry-run`)

## Phase 3 — SageMaker 파이프라인 (`pipelines/sagemaker_pipeline.py`)

- SageMaker Python SDK, AWS 제공 PyTorch 컨테이너 (커스텀 이미지 없음)
- 단계: Processing(CPU) → Training(GPU, Spot 옵션) → Evaluation(CPU) → Condition → Register
- Training은 현재 운영 모델에서 이어 학습, replay 데이터 혼합
- Evaluation: 고정 테스트셋으로 새 모델 vs 현재 모델 비교, `evaluation.json` 출력 (`metrics.json`과 같은 지표)
- Condition: **모든 축에서 현재 모델 이상**일 때만 통과
- Register: 통과 시 `s3://.../models/skin_vN/`에 서빙 형식 그대로 저장 + Model Registry에 `PendingManualApproval` 등록. N은 S3 기존 버전을 보고 자동 결정 (덮어쓰기 금지)
- 인스턴스 타입·버킷·역할은 파이프라인 파라미터/환경변수
- 등록 CLI: `python -m pipelines.sagemaker_pipeline upsert`

## Phase 4 — Prefect 플로우 (`pipelines/prefect_flow.py`)

- **주 1회** 실행: 매주 월요일 03:00 KST (cron은 설정으로 변경 가능)
- 흐름: 신규 데이터 확인 → 기준 미달이면 종료 → SageMaker 파이프라인 실행 → 완료까지 대기 → 결과 로그 (통과 여부, 새 버전, 지표)
- 신규 데이터 확인은 인터페이스로 분리
  - 현재 구현: S3 `feedback/`의 지난 실행 이후 신규 파일 수, 최소 건수는 환경변수
  - 추천 서버 count·export API 연동은 TODO 주석으로만
- 자동 배포 없음 (승인·배포는 사람)
- 로컬 1회 수동 실행 명령도 제공

## Phase 5 — 테스트·문서

- `tests/`에 **새 파일**로 추가
  - 학습 전처리와 서빙 전처리가 같은 입력에 같은 결과를 내는지
  - 모델 클래스 하이퍼파라미터 인자 변경이 반영되는지
  - export 결과가 `reference.npz`와 허용 오차 내 일치하는지
  - 파이프라인 정의 생성 (AWS 호출은 mock)
- `docs/mlops.md`: 실행 방법, 환경변수, S3 구조, 승인·배포 절차
- `.env.training.example` 신규