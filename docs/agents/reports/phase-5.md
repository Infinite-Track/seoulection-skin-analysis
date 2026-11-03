# Phase 5 보고 — 테스트·문서

> 작성: Claude, 2026-11-01. 브랜치 `feature/mlops-training`. **AWS 호출 없음** — 파이프라인 테스트는 botocore의 모든 API 호출을 가로채 실패시킨 채 돌렸고, 그래도 정의가 만들어져야 통과한다.

## 변경 파일

| 파일 | 내용 |
|---|---|
| `tests/test_train_serve_consistency.py` **(신규, 13개)** | 학습 전처리 == 서빙 전처리. 부위 6개 합성 크롭(확대·축소·캔버스보다 큰/작은 쪽·`r_` 반전이 섞이게)을 학습 형식 RGBA PNG로 저장 → `RegionDataset`(증강 없음) 텐서와 `SkinPredictor.model_input`(운영 `models/skin_v2/meta.json`의 입력 계약, ONNX 세션 없이) 출력이 **비트 단위로 같음** (`np.array_equal`). 증강 값 0이면 학습 입력 == 서빙 입력(회전 0·배율 1 warpAffine는 항등), 기본 증강은 바꾸되 `(seed, 에폭, 행)` 재현. 학습이 `common.transforms.to_model_input`·`common.crop_files.load_region_png` **그 함수 자체**를 쓰는지(identity) + 서빙이 같은 함수를 meta의 `mirror_right`로 부르는지(monkeypatch). 타깃: `encode_target`(학습) → `common.tasks.decode`(서빙) 왕복 — pore_count(log1p↔expm1)·wrinkle_ra·age 오차 < 1e-6, 순서형 등급→클래스 번호→CORAL 로짓→`finalize` 등급 이름 일치, 부위에 정의된 항목만 나옴. `tasks.yaml` == v2 `meta.json output.tasks`(열·클래스 순서·등급 구간·log, `check_tasks_match`), v2 `meta.json input` == `common` 상수 == `TrainConfig` 기본값, `min_skin_px`·`min_pixels`가 서빙 `settings` 기본값과 같음, PNG 왕복 보존, Dataset 정답 텐서 = `encode_target`·valid 마스크 |
| `tests/test_training_model.py` **(신규, 5개)** | `SkinModel()` = skin v2: 백본 이름·**파라미터 50,637,427 = meta.json params**·출력 16열·state_dict 키 체계(`backbone/heads/thresholds`)·경계값 K-1개 0 초기화·stem+stages[:2] 고정, `TrainConfig()` 기본값과 동일 구조. 인자 변경 반영(convnext_atto): `head_hidden=0`→`nn.Linear`, `head_hidden=32/head_act=relu/dropout=0.1`→Sequential 구성, silu, 잘못된 활성화→`ValueError`, `freeze_stages` 0/1/4(4는 stem+모든 단계 고정, 최종 norm `backbone.head.norm`만 남음 — 노트북 규칙), `threshold_init` 반영, tasks 축소 시 헤드·열 축소. forward: 두 입력 크기에서 (B,16), 각 항목 열 = 헤드 점수(+경계값), CORAL 로짓 단조. `param_groups` lr 분리·고정 파라미터 제외. 설정 우선순위 기본값 < yaml < CLI가 `from_config` 모델 구조까지 이어짐, 모르는 키·없는 파일 거절, `datasets`는 CLI에 없음 |
| `tests/test_export_reference.py` **(신규, 8개)** | 작은 체크포인트(convnext_atto, `train.py` 저장 형식) → `export` (약 4초). 파일 구성 == `models/skin_v2/`(6개), `SHA256SUMS.txt` 순서 == `HASHED_FILES` == v2 파일 순서, 해시 일치, `check_onnx.py`가 v2 것과 같은 파일(해시). `meta.json input` **== v2 input 완전 동일**, output 규격 == `tasks.yaml`, mean/std = 체크포인트 stats, float32 표기 문제 없음. **reference.npz 재현**(check_onnx.py 규칙) 최대 차이 **0.00e+00** < 1e-3, `register.verify_model_dir` 같은 값; **PyTorch 체크포인트 vs reference.npz 4.92e-07** < 1e-3; 내보낸 `check_onnx.py`를 `runpy`로 실행 → 해시 5/5 OK·"통과". 서빙 로더 `ensure_model_files`·`SkinPredictor` 로드, `predict_regions`·`aggregate`(개수 ≥ 0). 변조 감지: `rename_model`("skin v7")은 meta.json 줄만 바꾸고 검증 통과 / 가중치 1바이트 변조 → "해시 불일치" / 해시는 맞지만 reference가 다른 값 → "reference.npz와 출력이 다르다". **운영 v2 폴더**(가중치는 로컬에만, 없으면 skip): `verify_model_dir(models/skin_v2)` → 최대 차이 **1.43e-06** |
| `tests/test_sagemaker_pipeline.py` **(신규, 13개)** | 모듈 fixture가 가짜 자격 증명(`~/.aws`·SSO 안 봄) + `botocore.client.BaseClient._make_api_call`을 가로채 **모든 AWS 호출을 기록·실패**시킨다. 그 상태에서 `build_pipeline(...).definition()`(Spot/온디맨드) + CLI `definition --dry-run`이 성공해야 하고, 시도된 호출은 자격 증명·역할 확인(`GetCallerIdentity`, `SimulatePrincipalPolicy`)뿐이어야 한다 — 생성·업로드·시작 호출 0. 정의 검사: 파라미터 8개 이름·타입·기본값 = `configs`, 단계 순서 Prepare→Train→Evaluate→ModelBetterOrEqual, AWS 제공 `pytorch-training:2.9-{cpu,gpu}-py312`, 인스턴스는 파라미터 참조, Prepare 입력·feedback 인자·실행별 출력 경로, Train 채널(`train`=Prepare 출력, `base_model`=파라미터)·하이퍼파라미터(config·epochs·checkpoint-dir·num-workers)·체크포인트 S3·GZIP, Evaluate 입력(candidate=Train 산출물)·`--axes all --tolerance 0.0`, Condition `failed_axes == 0`→`[Register]`/else `[]`, Register 인자(버킷·접두사·`skin_v`·그룹·리전·`{name} v{n}`·실행 ARN·베이스 모델), Spot(`EnableManagedSpotTraining`·`MaxWaitTimeInSeconds`) vs `--no-spot`. 비밀값 없음(AKIA·실제 계정 ID 없음, 역할은 자리 표시 또는 `SAGEMAKER_ROLE_ARN`), S3 주소는 설정 버킷만. `load_settings` 주소가 `paths.yaml`·`pipeline.yaml`에서만 조립, Spot `max_wait < max_run` 거절, `role_arn` 분기, `parse_params`, 코드 번들(디렉터리 5개 + 루트 단계 스크립트 + `requirements.txt`=컨테이너 목록, `__pycache__`·`.sh` 제외, serving/models/outputs 없음), CLI `start`는 SDK `Pipeline.start` 대역으로 파라미터·표시 이름 전달 확인 |
| `tests/test_pipeline_steps.py`, `tests/test_prefect_flow.py` (Phase 3·4 신규 파일, 각 3~5줄) | `pytest.importorskip("scipy"/"sklearn")`, `importorskip("prefect")` 추가 — 서빙 CI(`requirements.txt`만)에서 collection 에러 대신 skip. 테스트 내용 변경 없음 |
| `docs/mlops.md` **(신규)** | 전체 그림, 설치(의존성 4묶음), 환경변수 표, S3 구조(버전 폴더 내용물·산출물 경로·Prefect 기록), 실행 방법(학습/데이터/파이프라인/Prefect), 등록 조건·산출물 검증, **승인·배포 절차**(검토 → Model Registry 승인/거절 → EC2 배포: 새 `MODEL_DIR` 폴더 + `MODEL_S3_URI`, 로그·해시·Kafka 확인, 롤백 → `pipeline.yaml current_model` 갱신·재 upsert), 실제 AWS에서 확인할 것, 테스트 표·CI 동작 |
| `.env.training.example` **(신규)** | `SAGEMAKER_ROLE_ARN`, `AWS_PROFILE`, `SAGEMAKER_VALIDATE_ROLE`, `MIN_NEW_FEEDBACK_FILES`, `PREFECT_API_URL/KEY`, `AIHUB_RAW_ROOT`, `MEDIAPIPE_MODELS_DIR` + SageMaker 자동 변수 설명. 비밀값 없음 (`.env.example`은 수정하지 않음) |
| `docs/agents/reviews/phase-4.md` | Codex 재검토 3회차 PASS 기록을 별도 커밋(`phase 4: Codex 재검토 3회차 PASS 기록`)으로 보존 (Phase 3 때와 같은 방식) |
| `docs/agents/mlops-plan.md` | 진행 현황: Phase 4 검토 [x], Phase 5 작성 [x] |

금지 경로 변경 0줄 — `git diff main --name-status -- serving common models Dockerfile requirements.txt .dockerignore .env.example tests`:
신규 `A` 8개(`common/crop_files.py`, `common/training_targets.py`, `tests/test_pipeline_steps.py`, `tests/test_prefect_flow.py`, `tests/test_export_reference.py`, `tests/test_sagemaker_pipeline.py`, `tests/test_train_serve_consistency.py`, `tests/test_training_model.py`)뿐. 기존 파일 `M`·`D` 없음. CI 설정(`.github/workflows/ci.yml`)도 손대지 않았다.

## git diff main --stat

```
 .env.training.example                 |  29 ++
 .gitignore                            |   3 +
 common/crop_files.py                  |  28 ++
 common/training_targets.py            | 103 ++++
 configs/README.md                     |  15 +
 configs/data.yaml                     |  17 +
 configs/paths.yaml                    |  17 +
 configs/pipeline.yaml                 |  45 ++
 configs/prefect.yaml                  |  27 ++
 configs/splits/aihub_kr_replay.txt    | 101 ++++
 configs/splits/aihub_kr_test.txt      | 167 +++++++
 configs/tasks.yaml                    |  41 ++
 configs/train.yaml                    |  19 +
 configs/train_pipeline.yaml           |  12 +
 docs/agents/mlops-plan.md             | 112 +++++
 docs/agents/reports/phase-0.md        | 180 +++++++
 docs/agents/reports/phase-1.md        |  84 ++++
 docs/agents/reports/phase-2.md        |  70 +++
 docs/agents/reports/phase-3.md        | 136 ++++++
 docs/agents/reports/phase-4.md        | 219 +++++++++
 docs/agents/reviews/phase-0.md        |  25 +
 docs/agents/reviews/phase-1.md        |  71 +++
 docs/agents/reviews/phase-2.md        |  66 +++
 docs/agents/reviews/phase-3.md        |  81 ++++
 docs/agents/reviews/phase-4.md        | 117 +++++
 docs/mlops.md                         | 203 ++++++++
 pipelines/README.md                   |  75 +++
 pipelines/__init__.py                 |   0
 pipelines/new_data.py                 | 113 +++++
 pipelines/prefect_flow.py             | 682 +++++++++++++++++++++++++++
 pipelines/requirements-container.txt  |  16 +
 pipelines/sagemaker_pipeline.py       | 330 +++++++++++++
 pipelines/steps/__init__.py           |   0
 pipelines/steps/bootstrap.py          |  34 ++
 pipelines/steps/evaluate.py           | 135 ++++++
 pipelines/steps/prepare.py            | 252 ++++++++++
 pipelines/steps/register.py           | 232 +++++++++
 pipelines/steps/train.py              |  14 +
 pipelines/steps/util.py               |  78 +++
 requirements-pipeline.txt             |   6 +
 requirements-train.txt                |  15 +
 scripts/README.md                     |  30 ++
 scripts/build_crops.py                | 117 +++++
 scripts/crop_index.py                 |  87 ++++
 scripts/import_notebook_crops.py      |  61 +++
 scripts/make_splits.py                | 129 +++++
 scripts/prepare_aihub.py              | 121 +++++
 scripts/upload_datasets.py            | 119 +++++
 tests/test_export_reference.py        | 189 ++++++++
 tests/test_pipeline_steps.py          | 155 ++++++
 tests/test_prefect_flow.py            | 859 ++++++++++++++++++++++++++++++++++
 tests/test_sagemaker_pipeline.py      | 280 +++++++++++
 tests/test_train_serve_consistency.py | 217 +++++++++
 tests/test_training_model.py          | 139 ++++++
 training/README.md                    |  48 ++
 training/__init__.py                  |   0
 training/config.py                    | 163 +++++++
 training/dataset.py                   | 167 +++++++
 training/evaluate.py                  | 112 +++++
 training/export_onnx.py               | 140 ++++++
 training/losses.py                    |  70 +++
 training/model.py                     |  99 ++++
 training/templates/check_onnx.py      |  41 ++
 training/train.py                     | 207 ++++++++
 64 files changed, 7520 insertions(+)
```
(Phase 0~4 포함, 전부 신규. Phase 5 자체 변경 `git diff 1b57550 --stat`: 9파일 +1105/-3 — 보고서·계획서 제외.)

## 검증

환경: 프로젝트 venv(`.venv`, torch 2.14 / timm 1.0.29 / onnxruntime 1.30 / sagemaker 3.27 / prefect 3.9). 사전학습 가중치 다운로드 없음(`pretrained=False`), 운영 v2 가중치는 로컬 `models/skin_v2/skin.onnx.data`(git 밖).

| 검증 | 결과 |
|---|---|
| `pyflakes tests training pipelines scripts common` | 0 warnings |
| `pytest -q` 전체 (venv, Prefect 임시 서버) | **105 passed**, 69초 (서빙 24 + Phase 3 8 + Phase 4 34 + **Phase 5 39**: 13 + 5 + 8 + 13). 종료 코드 0 (`.venv/bin/python -m pytest` 직접 실행도 0) |
| 서빙 CI 흉내 (`torch timm onnxscript onnx scipy sklearn sagemaker prefect yaml`을 import 불가로 만들고 `pytest -q`) | **24 passed, 6 skipped**, 종료 코드 0 — 학습·파이프라인 테스트 6파일은 `importorskip`으로 skip. (수정 전에는 `tests/test_pipeline_steps.py`·`test_prefect_flow.py`가 collection 에러를 냈을 것 — scipy·prefect가 서빙 `requirements.txt`에 없다) |
| 학습 == 서빙 전처리 | 부위 6개 전부 `np.array_equal` 통과 (float32 (3,H,W) 비트 동일). `r_eye`·`r_cheek`는 반전 없이 만든 입력과 다름, `l_`·이마·미간은 같음 |
| 타깃 왕복 | pore_count 777.0 / wrinkle_ra 21.9 / age 33.3 복원 오차 < 1e-6, 순서형 3항목 모든 클래스 이름 일치 |
| 모델 기본값 | 파라미터 **50,637,427** = v2 `meta.json params`, 출력 16열 |
| export → reference.npz | ONNX vs PyTorch(export 내부) 4.2e-07, **reference 재현 0.00e+00**, PyTorch vs reference 4.92e-07, 내보낸 `check_onnx.py` "통과: 최대 차이 0.00e+00" |
| 운영 v2 reference.npz (실측) | `verify_model_dir(models/skin_v2)` **1.43e-06** < 1e-3 (Phase 3 보고와 같은 값) |
| 파이프라인 정의 (API 전부 차단) | Spot·온디맨드 정의 생성 성공. 시도된 호출: `GetCallerIdentity`·`SimulatePrincipalPolicy`만 (전부 차단돼 실패했는데도 정의 생성) — `CreatePipeline`·`PutObject`·`StartPipelineExecution` 시도 0 |

### 발견한 것 (코드 변경 없음, 문서에 반영)

- `definition --dry-run`이 "AWS 호출 없음"은 정확히는 "**리소스를 만드는 호출 없음**"이다. SageMaker SDK는 정의를 만들면서 STS `GetCallerIdentity`(여러 번)와 IAM `SimulatePrincipalPolicy`(`SAGEMAKER_VALIDATE_ROLE=false`인데도 2회)를 시도하고, 실패하면 삼킨다. 자격 증명이 없는 환경에서도 dry-run은 되고, 있는 환경에서는 읽기 전용 호출이 나간다. `docs/mlops.md` §4.3과 테스트 docstring에 적었다.
- ConvNeXt의 최종 norm(`backbone.head.norm`)은 `freeze_stages=4`여도 학습된다(stem + stages만 고정). 노트북 `model_v2.py`와 같은 규칙이라 그대로 두고 테스트에 명시했다.

## 설계 결정

- **서빙 쪽 전처리 기준**: 서빙 `SkinAnalyzer`는 사진→mediapipe→크롭을 거치지만, 학습 데이터도 같은 `common.face_regions.extract_regions`로 만든다(Phase 2에서 노트북 크롭과 픽셀 동일 확인). 그래서 Phase 5 테스트는 **크롭 이후**(PNG 저장·로드 → `to_model_input`)를 비교 대상으로 잡았다. 얼굴 사진 없이 돌아가고, 학습·서빙이 갈라질 수 있는 유일한 지점(학습은 PNG 왕복 + Dataset, 서빙은 메모리 크롭 + `model_input`)을 정확히 덮는다.
- **reference.npz 비교 대상**: v2 체크포인트(`deploy_model.pt`)가 이 Mac에 없어 "v2 재export → v2 reference.npz"는 여전히 할 수 없다(Phase 1 보류 항목). 대신 ① 같은 규칙으로 만든 작은 모델의 reference 재현(ONNX·PyTorch 양쪽), ② Register 단계 검증 함수와 내보낸 `check_onnx.py`가 같은 결과, ③ 운영 v2 ONNX의 reference 재현 실측(1.43e-06)으로 "export 결과가 reference.npz와 허용 오차 내"를 검증했다. 체크포인트가 S3에 올라오면 `training.export_onnx --checkpoint deploy_model.pt` → `verify_model_dir`로 같은 테스트를 그대로 쓸 수 있다.
- **CI**: `.github/workflows/ci.yml`은 수정하지 않았다(금지 목록은 아니지만 서빙 배포 검사용이고, torch 2.14+sagemaker 설치는 CI에 무겁다). 대신 테스트 쪽에서 `importorskip`으로 건너뛰게 해 PR CI가 깨지지 않게 했다. 전체 테스트는 로컬 venv에서 돌린다(`docs/mlops.md` §8).
- **테스트 속도**: 큰 백본은 `test_default_model_is_skin_v2` 한 곳(가중치 생성만, 0.3초)에서만 만들고, 나머지는 convnext_atto(3.4M). export 테스트는 모듈 fixture 한 번(약 4초).

## 질문

없음
