# Phase 1 — 학습 모듈 (`training/`) 보고

> 작성: Claude, 2026-10-29. 브랜치 `feature/mlops-training` (base `feat/private-ec2` = 운영 커밋 ec21418).
> Codex 리뷰(`reviews/phase-1.md`) Major 1·Minor 2건 반영 — 맨 아래 "리뷰 반영" 참고.

## 생성·수정한 파일

| 파일 | 내용 |
|---|---|
| `training/config.py` | `TrainConfig` dataclass (기본값 = skin v2 `DEPLOY`), 우선순위 기본값 < `configs/train.yaml` < CLI. 필드마다 `--lr-head`식 옵션 자동 생성. `load_tasks`, `load_paths` |
| `training/model.py` | `SkinModel` — 모든 하이퍼파라미터가 `__init__` 인자, 인자 없이 생성 = skin v2 구조 (파라미터 50,637,427, state_dict 키 `model_v2.py`와 동일) |
| `training/dataset.py` | 표준 데이터셋 형식(`crops.csv`, `labels.csv`, `crops/`) 정의·로더, `RegionDataset`(증강), `RegionBatchSampler`, `SM_CHANNEL_*` |
| `training/losses.py` | SmoothL1 / CORAL BCE, 시작 손실(l0) 정규화, 경계값 prior |
| `training/train.py` | 학습 → `model.pt` → 내보내기. `SM_MODEL_DIR`, `--init-from`(파인튜닝: 항목 정의 검사, stats·l0·경계값 이어받음), `--checkpoint-dir` 재개 |
| `training/export_onnx.py` | `model.pt` → `skin.onnx`, `skin.onnx.data`, `meta.json`, `reference.npz`, `check_onnx.py`, `SHA256SUMS.txt` (`models/skin_v2/`와 같은 구성·순서) |
| `training/evaluate.py` | 서빙 코드(`common/predictor.py`)로 고정 테스트셋 평가 → `metrics.json` (v2 형식) |
| `training/templates/check_onnx.py` | `models/skin_v2/check_onnx.py` 복사 (해시 동일 `11dd892c…`) — 내보낼 때 같이 넣는다 |
| `training/README.md`, `training/__init__.py` | |
| `common/training_targets.py` **(새 파일)** | 타깃 정변환 `encode_target`·`regression_stats`·`grade_to_class`, `output_layout`, `meta_tasks`, `task_signature`. numpy만. 서빙은 import하지 않음 |
| `common/crop_files.py` **(새 파일)** | RGBA PNG 크롭 `save_region_png` / `load_region_png` (v2 학습 데이터 형식). 서빙은 import하지 않음 |
| `configs/paths.yaml`, `configs/tasks.yaml`, `configs/train.yaml`, `configs/README.md`, `configs/splits/` | 경로·S3 접두사·항목 정의·학습 설정. `tasks.yaml`은 노트북 TASKS와 값 일치 |
| `requirements-train.txt` **(새 파일)** | torch 2.14.0, torchvision 0.29.1, timm 1.0.29, onnx 1.23.0, onnxscript 0.7.2, PyYAML, scipy, scikit-learn. 서빙 `requirements.txt` 미수정 |
| `.gitignore` | `outputs/` 추가 (3줄) |

## `git diff feat/private-ec2 --stat`

```
 .gitignore | 3 +++
```
그 외는 전부 **새 파일**(untracked). 금지 경로(`serving/`, 기존 `common/` 파일, `models/`, `Dockerfile`, `requirements.txt`, `.dockerignore`, `.env.example`, 기존 `tests/`) 변경 0줄.
(2026-10-29 확인) `origin/main`은 `feat/private-ec2`를 이미 포함(PR #2). 로컬 `main`을 전진시킨 뒤 `git diff main --stat`도 같은 결과다.

## 실행한 검증

| 검증 | 결과 |
|---|---|
| `pyflakes common training` | 통과 |
| 기존 테스트 `pytest -q` | 24 passed |
| `SkinModel(pretrained=False)` 파라미터 수 | **50,637,427** = v2 `meta.json` `params`. 학습 49,402,387 / 헤드 1,182,726 = 노트북 `summary()`와 동일 |
| 인자 변경 반영 | `backbone=convnext_tiny, head_hidden=0, freeze_stages=0, dropout=0.1` → 27,824,755 파라미터, 헤드 `Linear`, 전 파라미터 학습 가능 |
| 설정 우선순위 | 기본값(lr_head 2e-4, epochs 6) → `--lr-head 1e-4 --epochs 3 --no-amp --views F` 반영 확인. yaml의 모르는 키는 오류 |
| 정변환↔역변환 | `encode_target` → `common.tasks.decode`(서빙) 왕복: pore_count 777.0, age 33.3 복원 (오차 < 1e-6) |
| 스모크 학습 (처음부터) | 로컬 크롭 10명을 표준 형식으로 만든 데이터셋(scratchpad, 레포 밖). 학습 6명 74크롭, `--epochs 1 --max-batches 20 --l0-batches 5`, MPS. 시작 손실 측정 → 1에폭 5초 → `model.pt` → 내보내기 (ONNX vs PyTorch 최대 차이 4.1e-06) |
| 산출물 구성 | `skin.onnx` 1.69 MB, `skin.onnx.data` 203 MB, `meta.json`, `reference.npz`, `check_onnx.py`, `SHA256SUMS.txt`, `model.pt`, `train_history.json` |
| 내보낸 `check_onnx.py` 실행 | 해시 5/5 OK, 부위별 최대 차이 0.00e+00, 통과 |
| `meta.json` vs v2 | `input` 블록 **완전 동일** (pixel_mean/std float32 표기 문제 1건 수정 후), `output.tasks`는 mean/std 제외 동일, 추가 키 `model.parent`만 |
| 서빙 로더 | `SkinPredictor(내보낸 폴더)` 로드·입력 계약 assert 통과 |
| 평가 | `evaluate --eval-min-groups 3` → 테스트 4명 46크롭, 항목 6개 지표 + 점수, `metrics.json` 저장 (수치는 데이터가 작아 의미 없음) |
| 파인튜닝 | `--init-from run1/model.pt --lr-head 1e-4`: stats·l0 이어받음 확인, parent 기록, 텐서 307/369 변경(앞 2단계 고정) |
| 재개 | `--checkpoint-dir`로 1에폭 후 `last.pt` 저장 → 같은 폴더로 `--epochs 2` 재실행 시 "2에폭부터 재개", history [1, 2] |
| 항목 정의 불일치 가드 | 등급 구간을 바꾼 `tasks.yaml`로 `--init-from` → `RuntimeError: … 파인튜닝 불가` |

## 가정 (Phase 0 질문에 대한 임시 결정 — 답이 다르면 수정)

- **Q3 공용 코드 위치**: 타깃 정변환·크롭 파일 IO만 `common/` 새 파일 2개로, 나머지(증강·배치·손실)는 `training/`.
- **Q4 모델 이름**: `meta.json` `model.name` = `model_name`(기본 `"skin"`), `version` = 내보낸 날짜. 서빙 `modelVersion`은 `"skin 2026-11-01"` 형태가 된다. 버전 번호 부여는 Phase 3 Register.
- **Q5 reference.npz**: 내보낼 때마다 seed 2026 규칙으로 새로 만든다 (모델 전용 파일). `check_onnx.py`도 함께 복사.
- **Q10 TrainConfig 범위**: 배포 학습 경로만. `folds`·`patience`·`valid_workers`·`detached_tasks` 제외.
- **Q12 파인튜닝 stats**: 베이스 체크포인트의 `stats`·`l0`·경계값을 이어받는다. 재산출 옵션은 두지 않았다.
- **증강 순서**: 반전 앞에 적용 (노트북은 뒤). 회전각 분포가 대칭이라 입력 분포는 같고, 서빙과 같은 `to_model_input`을 그대로 쓸 수 있다.
- **데이터셋 형식**: parquet 대신 CSV (`crops.csv`, `labels.csv`) — pandas 의존 없이 `csv` 모듈로 읽는다. Phase 2 `build_crops.py`가 이 형식을 만든다.

## 결정이 필요한 질문

1. (Phase 0 Q1) `deploy_model.pt` — 사용자가 Windows PC에서 S3 `models/skin_v2/deploy_model.pt`로 직접 올리기로 함 (2026-10-29). 도착하면 v2 체크포인트 로드·재export·reference 비교를 검증한다.
2. ~~(Phase 0 Q2) 리뷰·PR의 비교 기준~~ → **해결 (2026-10-29)**: `feat/private-ec2`는 이미 PR #2로 `origin/main`에 병합돼 있었다(e3abf75). 로컬 `main`만 뒤처져 있어 `origin/main`으로 전진시켰다. 이제 `git diff main --stat` = `.gitignore` 3줄만.
3. CI(`.github/workflows/ci.yml`)의 pyflakes 대상에 `training`을 넣을지. CI 파일은 금지 목록에 없지만 이번 Phase 범위 밖이라 건드리지 않았다. 학습 의존성(torch)이 커서 CI에서 `training` 테스트까지 돌릴지는 Phase 5에서.
4. `model_name` 기본값 `"skin"`이 맞는지, 아니면 v2처럼 `"skin v2"`를 유지하고 Register에서 바꿀지 (Phase 0 Q4).

## 다음 Phase에 넘기는 것

- Phase 2 `build_crops.py`는 `training/dataset.py` 맨 위의 표준 형식(`crops.csv`, `labels.csv`, `crops/`)을 만들어야 한다. 스모크 데이터셋 변환 로직(노트북 `index.csv`·`labels_region.csv` → 표준 형식)은 scratchpad에만 있고 레포에 넣지 않았다.
- `configs/train.yaml`의 `datasets[0].test_split`(`configs/splits/aihub_kr_test.txt`)은 Phase 2가 만든다. 없으면 `load_rows`가 안내 메시지와 함께 멈춘다.

## 리뷰 반영 (2026-10-29, `reviews/phase-1.md`)

| # | 심각도 | 수정 | 검증 |
|---|---|---|---|
| 1 | Major | `common/training_targets.py`: `task_signature`의 classes를 dict → **(이름, 등급 목록) 리스트**로 바꿔 순서를 비교에 포함. `meta_signature`(meta.json → 같은 형식)와 `check_tasks_match` 추가. `training/evaluate.py`: 평가 시작 시 모델 `meta.output.tasks`와 tasks를 비교해 다르면 `ValueError` | `pore_grade` 클래스를 역순으로 바꾼 tasks.yaml: `grade_to_class(0)` 0→3, signature 비교 **False**. `evaluate` → `ValueError`(다른 곳: pore_grade), `train --init-from` → `RuntimeError`. 원본 tasks로는 평가 정상 |
| 2 | Minor | `training/dataset.py`: 증강 난수를 `(seed, 에폭, 행 번호)`로 결정, 샘플러가 `(에폭, 행 번호)`를 내고 `set_epoch`로 에폭 지정. `training/train.py`: `last.pt`에 torch 난수 상태(cpu/cuda/mps)와 실행 정보(`signature, datasets, crops, people, steps, parent, stats, l0, cfg`) 저장, 재개 시 `check_resumable`로 비교(바꿀 수 있는 설정 `epochs, num_workers, checkpoint_dir`), 재개 때 l0는 last.pt에서 | CPU·convnext_tiny·batch 2·3배치: **연속 2에폭 vs 1에폭+재개 1에폭 가중치 최대 차이 0.000e+00**, train_loss 동일(0.972565, 1.135914). 재개 때 `num_workers` 0→2로 바꿔도 동일. `--lr-head` 변경 재개 → 거절, `--max-batches` 변경 → 거절(steps 3≠2), `--epochs 3` 연장 → 3에폭만 추가 실행 |
| 3 | Minor | `training/config.py`: `num_workers` 기본값 4 → **6**(v2 CFG). `device` 필드 추가(`auto|cuda|mps|cpu`, 검증용) | `TrainConfig().num_workers == 6` |
| 4 | — | v2 `deploy_model.pt` 로드·재export·reference 비교 | ⏭ 보류: 파일 미도착 (사용자가 S3 `models/skin_v2/`에 업로드 예정) |

반영 후 재검증: pyflakes 통과, 기존 테스트 24 passed, 전체 스모크(MPS·convnext_small·workers 2) 재실행 → export(ONNX vs PyTorch 3.6e-06)·`check_onnx.py` 통과·`evaluate` 정상.
