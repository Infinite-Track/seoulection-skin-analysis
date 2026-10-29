# Phase 2 보고

> 작성: Claude, 2026-10-29. 브랜치 `feature/mlops-training`. AWS 호출 없음 (업로드는 dry-run만).
> Codex 리뷰(`reviews/phase-2.md`) Major 2건 반영 — "리뷰 반영" 절 참고.

## 변경 파일

| 파일 | 내용 |
|---|---|
| `scripts/prepare_aihub.py` | AI-Hub 원본(환경변수 `AIHUB_RAW_ROOT`) → `images.csv`, `labels.csv`. 라벨 JSON만 읽는다(스마트폰 정면 6부위 JSON, skin v2와 같음). 데이터 소스별 어댑터 |
| `scripts/build_crops.py` | `images.csv` → `crops.csv` + `crops/` (RGBA PNG). 크롭은 `common/face_regions.extract_regions`, 저장은 `common/crop_files`. 기기 필터·최소 해상도는 `configs/data.yaml`. 멀티프로세스. 재개는 완전한 사진만 건너뛰고 부분 행은 재처리, CSV는 원자적 교체 |
| `scripts/import_notebook_crops.py` | 노트북이 이미 만든 크롭(`face_analysis_AI/data`)을 하드링크로 가져와 같은 `crops.csv` + `crops/` 구성. aihub_kr은 이걸 썼다(사용자 지시) |
| `scripts/make_splits.py` | 인물 단위 분할 → `configs/splits/<name>_test.txt`, `<name>_replay.txt`. `--import`(기존 목록 채택) / `--fraction`(seed 분할) / `--replay N` / `--check`. 기존 테스트 목록은 `--force` 없이 덮어쓰지 않음 |
| `scripts/upload_datasets.py` | 테스트셋·replay를 S3 `datasets/{testset,replay}/<name>/`에 올림. **기본 `--dry-run`**(boto3도 import하지 않음), `--execute`로만 실제 업로드. 올리기 전 완전성 검사 — 불완전하면 아무것도 올리지 않음. testset·replay 둘 다 검사 후 업로드 |
| `scripts/crop_index.py` | `crops.csv` 공통: 원자적 저장(`write_csv_atomic`), 완전성 검사(`complete_image_ids`, `check_dataset`: 인물·사진별 6부위 행·status·ok PNG 존재) |
| `scripts/README.md` | 스크립트 표 + aihub_kr 준비 순서 |
| `configs/data.yaml` | 기기 필터(스마트패드·스마트폰), `min_pixels` 1,000,000, 분할 seed 42·비율, replay seed 42·인원 100 |
| `configs/splits/aihub_kr_test.txt` | **고정 테스트셋 165명** — skin v2가 쓴 `test_subjects_165.json`을 `--import`로 채택. 백업 브랜치 목록과 id 동일 |
| `configs/splits/aihub_kr_replay.txt` | replay 100명 (테스트 제외 800명 중 seed 42) |
| `training/config.py`, `training/dataset.py` | `read_id_list`·`CROPS_FILE`·`LABELS_FILE`을 `config.py`(torch 없음)로 옮겨 scripts가 torch 없이 import. `dataset.py`는 re-export |
| `docs/agents/reports/` | 보고서 위치를 CLAUDE.md 새 형식에 맞춰 `docs/agents/phase-N.md` → `docs/agents/reports/phase-N.md`로 이동 (Phase 0·1 포함), 참조 경로 갱신 |

표준 데이터셋 형식은 Phase 1 `training/dataset.py` 맨 위 그대로: `images.csv`(어댑터), `crops.csv`, `labels.csv`, `crops/{person}/{image_id}_{region}.png`.
`scripts/`의 기존 파일(`fetch_mediapipe_models.py`, `send_test_message.py`)과 사용자 파일 `agent_loop.sh`는 건드리지 않았다.

## git diff main --stat

```
 .gitignore | 3 +++
 1 file changed, 3 insertions(+)
```
(커밋 전 기준. 그 외는 전부 새 파일 — 금지 경로 `serving/`, 기존 `common/`, `models/`, `Dockerfile`, `requirements.txt`, `.dockerignore`, `.env.example`, 기존 `tests/` 변경 0줄.)

## 검증

| 검증 | 결과 |
|---|---|
| `pyflakes common training scripts` | 통과. scripts import 시 torch 미로드 확인 |
| 기존 테스트 `pytest -q` | 24 passed |
| `prepare_aihub` (실제 원본, 라벨 JSON만, 1초) | `images.csv` 12,545장·965명 (디지털카메라 6,755 / 패드 2,895 / 폰 2,895; 각도 F·L·R + 카메라 전용 Fb·Ft·L15·L30·R15·R30), `labels.csv` 18,335행 |
| 라벨 vs 노트북 `labels_region.csv` | (사람, 부위, 항목) 18,335개 전부 일치, 값 불일치 **0** |
| `build_crops` 3명 18장 (워커 3, 2.4초) | status ok 84 / hidden_view 24. 노트북 크롭과 **84개 전부 픽셀·마스크·face_h·skin_px 동일**, status 불일치 0 |
| `build_crops` 전체 5,790장 | 사용자 지시로 중단(기존 크롭 재사용). 855명분까지 정상 진행 중이었음 |
| `import_notebook_crops` (7초, 하드링크 11GB) | `crops.csv` 34,740행·965명, status ok 26,754 / hidden_view 7,644 / low_res 342 = 노트북 `index.csv`와 동일 |
| `make_splits --import test_subjects_165.json` | 165명 저장, 백업 브랜치 목록과 id 동일. 재실행 시 "이미 있다, --force 필요"로 거절. replay 100명 생성 |
| `make_splits --check` | 데이터셋 965 / 테스트 165 / 학습 가능 800 / replay 100, 겹침 없음 → 통과 |
| `upload_datasets` (dry-run) | testset: 165/165명, 크롭 행 5,940(ok 4,564), 파일 4,566개, 1,899 MB → `s3://…/datasets/testset/aihub_kr/`. replay: 100/100명, ok 2,772, 2,774개, 1,170 MB → `…/datasets/replay/aihub_kr/`. 둘 다 완전성 검사 통과. boto3 미로드, 매니페스트 `outputs/upload/aihub_kr_manifest.json` |
| 기본 `configs/train.yaml`로 `training.train` (1에폭 10배치) | 학습 크롭 **21,334개 / 800명** = 노트북 deploy.ipynb와 동일. 시작 손실 측정·학습·저장 정상 (`l0_batches 5`로 줄였을 때는 wrinkle_ra 정답 없음 가드로 멈춤 — 의도된 동작) |
| 운영 v2 ONNX(`models/skin_v2`)를 새 테스트셋으로 `training.evaluate` (CPU 약 10분) | 165명·4,431크롭 → **점수 0.792064 = 노트북 `metrics.json`과 차이 0.00e+00**, 6항목 모든 지표(QWK·acc·within1·spearman·pearson·MAE·n) 차이 0. 데이터셋·분할·평가 코드가 노트북과 동일함을 뜻한다. 결과 `outputs/eval_v2_on_testset.json` |

### 리뷰 반영 (`reviews/phase-2.md`)

| # | 심각도 | 수정 | 검증 |
|---|---|---|---|
| 1 | Major | `scripts/upload_datasets.py`: 스테이징 전 `crop_index.check_dataset`로 완전성 검사 — 목록 인물이 `images.csv`에 전부 있는지, 대상 사진(기기 필터 `configs/data.yaml`)마다 6부위 행이 중복 없이 있는지, status 어휘, ok 행 PNG 존재, 대상 밖 행 없음, 라벨 존재. 하나라도 어긋나면 `SystemExit`(경고 아님). testset·replay **둘 다 검사·스테이징을 끝낸 뒤** 업로드 시작 | 실제 테스트 인물 2명으로 만든 불완전 데이터셋 3종 — 인물 누락 / 사진 하나의 부위 행 삭제 / ok PNG 삭제 — 전부 거절, `upload` 호출 0회. 같은 2명 완전한 데이터는 통과. aihub_kr 전체 dry-run: testset 165/165, replay 100/100 완전성 통과 |
| 2 | Major | `scripts/build_crops.py`: `crops.csv`를 임시 파일에 쓰고 `os.replace`로 교체(`crop_index.write_csv_atomic`). 재개는 **완전한 사진**(6부위 행·status·ok PNG 존재)만 건너뛰고, 불완전한 사진은 기존 행을 버리고 다시 처리. 처리할 게 없어도 정리된 목록을 다시 쓴다 | 3명 18장 처리 후 CSV를 조작(사진 A는 forehead 행만 남김, 사진 B는 ok PNG 삭제) → 재실행이 "불완전한 사진 2장" 보고, 2장만 재처리, 결과 108행·18장 전부 6행·중복 0·PNG 복구·`.tmp` 잔재 없음. 재실행(처리 0장)도 정상 |

반영 후 재검증: pyflakes 통과, 기존 테스트 24 passed, aihub_kr dry-run 통과.

### 가정 (Phase 0 질문 Q7·Q8·Q9에 대한 임시 결정)

- **테스트셋은 기존 165명 보존**(Q7). `make_splits.py`는 새 데이터셋에만 seed 분할을 쓰고, aihub_kr은 `--import`. 바꾸면 v2 0.792와 비교 불가라 `--force` 없이는 덮어쓰지 않는다.
- **replay = AI-Hub 크롭 중 100명**(Q8·Q9). 인원수는 `configs/data.yaml`의 임시값. Processing은 S3의 크롭 PNG(표준 형식)를 그대로 채널로 받는 방향 — 원본 43GB를 올리지 않는다.
- `images.csv`에는 디지털카메라도 들어가고(어댑터는 원본을 그대로 목록화), `build_crops`·`upload_datasets`가 `configs/data.yaml` `devices`로 거른다.

## 질문

1. **replay 인원 100명**이 적절한가 (학습 800명의 1/8, 크롭 2,772개 1.2GB). Phase 3 Training이 신규 데이터와 섞을 비율과 함께 정해야 한다. 답이 없으면 Phase 3은 100명·설정값으로 진행한다.
2. **S3 업로드 실행** — `python -m scripts.upload_datasets --dataset outputs/datasets/aihub_kr --execute`는 사람이 실행한다(총 약 3.1GB, 7,340파일). 올린 뒤 Phase 3에서 채널로 쓴다.
3. `feedback/` 신규 데이터의 형식(Phase 0 Q8)은 여전히 미정. 서비스 사진에는 라벨이 없어 `prepare_*.py` 어댑터를 쓸 수 없다. Phase 4의 "신규 데이터 확인"은 파일 수만 세는 인터페이스로 만들 수 있지만, Phase 3 Training이 그 데이터를 학습에 쓰려면 라벨 출처가 필요하다.
