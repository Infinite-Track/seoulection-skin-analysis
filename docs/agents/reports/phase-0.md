# Phase 0 — 탐색·보고

> 작성: Claude, 2026-10-29. 코드 변경 없음. 검증은 읽기 전용 명령만 실행했다.
> Codex 리뷰(`reviews/phase-0.md`) Minor 2건 반영: §2 회귀 표준화 통계 산출 방법·v2 산출값 표시, patience 4/6 구분, Q12 추가.

## 0. 참고 자료 위치 (이 Mac)

| 자료 | 경로 |
|---|---|
| 베이스 모델 노트북 (배포) | `~/Desktop/소마/face_analysis_AI/modeling/v2/deploy.ipynb` |
| 하이퍼파라미터 실험 노트북 | `~/Desktop/소마/face_analysis_AI/modeling/v2/train.ipynb` |
| 모델·데이터 모듈 (노트북이 import) | `modeling/v2/model_v2.py`, `modeling/v2/data_v2.py` |
| 전처리 (크롭) | `data_preprocessing/face_regions.py`, `extract_regions.ipynb`, `build_labels.ipynb` |
| AI-Hub 원본 (43 GB) | `face_analysis_AI/raw_data/028.한국인_피부상태_측정_데이터/3.개방데이터/1.데이터/{Training,Validation}` |
| 크롭 PNG (11 GB) + 목록 | `face_analysis_AI/data/01.원천데이터/{사람}/{기기}/{각도}_{부위}.png`, `data/index.csv` |
| 정답 | `face_analysis_AI/data/02.정답지데이터/labels_region.csv` (5,790행 = 965명 × 6부위) |
| 고정 테스트 165명 | `face_analysis_AI/ailabtools/test_set/test_subjects_165.json` (seed 42, Training 144 / Validation 21) |
| 배포 산출물 원본 | `modeling/v2/outputs/deploy/skin_v2/` — 레포 `models/skin_v2/`와 `meta.json`·`SHA256SUMS.txt` 동일 확인 |
| **학습 체크포인트 `deploy_model.pt`, `fold0~4.pt`(v2)** | **이 Mac에 없음** (학습한 Windows PC에만 있음). `modeling/outputs/fold0.pt`는 v1 것 |
| 이전 초안 (training/, configs/, splits) | 로컬 브랜치 `backup/full-draft` (89bd554) |

## 1. 노트북 전체 흐름

```
AI-Hub 원본 JPG + 라벨 JSON
  │ extract_regions.ipynb  (스마트패드·스마트폰만, 디지털카메라 제외 / 100만 px 미만 제외)
  │   face_regions.py: 랜드마크 → 6부위 다각형(이마 윗변 확장, 눈가는 검은 막대 경계까지) ∩ 얼굴 피부 마스크
  ▼
크롭 RGBA PNG (RGB=원본 해상도 크롭, 알파=피부 마스크) + index.csv (사진 1장 × 부위 6 = 6행, status/skin_px/face_h)
  │ build_labels.ipynb → labels_region.csv (사람·부위별 pore_count, pore_grade, wrinkle_grade, wrinkle_ra, pigment_grade, age)
  ▼
train.ipynb  load_rows: status=ok, view∈views, skin_px≥20000, 정답 있는 (사람,부위)만 / 테스트 165명 분리
  │   GroupKFold(5, 사람 단위) → train_fold (최대 15에폭, 검증 점수 기준 조기 종료. patience는 1차 튜닝 4 → 2차 튜닝·최종 5-fold 6) → 5-fold 앙상블로 테스트
  │   결과: fold 최고 에폭 14, 1, 6, 4, 8 → 중앙값 6 / 앙상블 테스트 점수 0.802
  ▼
deploy.ipynb  DEPLOY = CFG + views [F,L,R] + epochs 6 + schedule_epochs 15
  │   train_full: 800명 전원, 검증 없음, 6에폭 고정 (학습 21,334크롭, 약 20분/RTX) → deploy_model.pt
  │   테스트 165명(4,431크롭) 점수 0.792 → models/skin_v2/metrics.json 과 동일
  │   torch.onnx.export(opset 18, dynamo, 더미 입력 (1,3,576,512), 동적 batch/height/width, freeze_stages=0로 재생성)
  │   meta.json 작성 (입력 규격 + 출력 열 해석) → ONNX vs PyTorch 최대 차이 3e-6, meta.json만으로 해석 ≡ 노트북 해석 (불일치 0)
  ▼
skin.onnx (1.6 MB) + skin.onnx.data (203 MB) + meta.json
  (reference.npz · SHA256SUMS.txt · check_onnx.py는 노트북 셀에 없음 — 별도 스크립트로 만든 것으로 보임, §5 참고)
```

데이터 규모: index.csv 34,740행 (ok 26,754 / hidden_view 7,644 / low_res 342), 965명 5,790장. 각도 F 11,466 / L 7,644 / R 7,644 (ok 기준).
학습 사용분(F+L+R, 800명): 21,334크롭. 항목별 정답 칸 수(정면만 기준): pore 3,170 / wrinkle_grade 5,939 / wrinkle_ra 2,821 / pigment 4,755 / age 9,109.

## 2. 하이퍼파라미터 전체 목록 (deploy.ipynb `DEPLOY`, 배포 모델 skin v2 기준)

| 구분 | 이름 | 값 | 비고 |
|---|---|---|---|
| 데이터 | `views` | `["F", "L", "R"]` | 실험(train.ipynb)은 `["F"]`만 |
| | `min_skin_px` | 20,000 | 피부 픽셀 수 미달 크롭 제외 (서빙 `MIN_SKIN_PX` 동일) |
| | `mirror_right` | True | `r_` 부위 좌우 반전 → `l_`과 방향 통일 (서빙 동일) |
| | 기기 | 스마트패드·스마트폰 | 디지털카메라 제외 (전처리 단계) |
| | 최소 해상도 | 1,000,000 px | 전처리 단계 (서빙 `MIN_PIXELS` 동일) |
| | 테스트셋 | 165명 고정 | `test_subjects_165.json`, 학습·선택에 미사용 |
| 모델 | `backbone` | `convnext_small.fb_in22k_ft_in1k` | timm, ImageNet 사전학습, `num_classes=0` (특징 768) |
| | `freeze_stages` | 2 | `stem` + `stages[:2]` 고정. 전체 50,637,427 / 학습 49,402,387 |
| | `head_hidden` | 256 | 항목별 헤드 `Linear(768,256) → GELU → Dropout → Linear(256,1)` |
| | `head_act` | `gelu` | |
| | `dropout` | 0.4 | `model_v2.py` 생성자 기본값은 0.2 — **CFG가 0.4로 덮어씀** |
| | `detached_tasks` | `()` | 미사용 (백본에 기울기 끊는 항목 없음) |
| | `threshold_init` | `"prior"` | 순서형 경계값 시작 = 학습 데이터 "등급 > k" 비율의 로짓 (clip 1e-3) |
| 입력 | 캔버스 (가로×세로) | forehead 608×288, glabella 192×160, eye 192×224, cheek 512×576 | `face_regions.CANVAS` |
| | `TARGET_FACE_HEIGHT` | 900 px | 얼굴 세로(랜드마크 10↔152)를 900으로 맞춘 배율 |
| | 픽셀 정규화 | mean (0.485, 0.456, 0.406) / std (0.229, 0.224, 0.225) | ImageNet, 정규화 후 마스크 밖 0 |
| 학습 | `epochs` | 6 | 5-fold 최고 에폭 중앙값, 검증 없이 고정 |
| | `schedule_epochs` | 15 | CosineAnnealingLR `T_max = 15 × (에폭당 배치 수)`, **배치마다 step**, 6에폭에서 중단 (lr 2.0e-5 → 1.3e-5) |
| | `batch_size` | 8 | 배치 = 같은 부위만 (`RegionBatchSampler`), 학습은 `drop_last=True` |
| | optimizer | AdamW | 2개 param group |
| | `lr_backbone` | 2e-5 | 백본(학습 가능 파라미터만) |
| | `lr_head` | 2e-4 | 헤드 + 순서형 경계값 |
| | `weight_decay` | 0.05 | 두 그룹 모두 |
| | AMP | fp16 autocast + GradScaler | CUDA일 때만 |
| | `l0_batches` | 50 | 학습 전(eval 모드) 50배치로 항목별 시작 손실 측정 → 손실 정규화 분모 |
| | `seed` | 42 | `torch.manual_seed(42)` 모델 생성 직전 / 샘플러 `seed+epoch` / Dataset 워커 `seed+worker_id` |
| | `num_workers` / `valid_workers` | 6 / 2 | |
| | `folds`, `patience` | 5, **4** | train.ipynb 전용. 기본 CFG는 `patience=4`이고 `DEPLOY`는 이 값을 그대로 상속하지만 `train_full`에는 조기 종료가 없어 **미사용**. `train.ipynb`의 2차 튜닝(`ROUND2 = dict(patience=6)`)과 최종 5-fold(`FINAL = {**CFG, views F/L/R, patience 6}`)만 6으로 덮어씀 |
| 손실 | 회귀 | `SmoothL1Loss()` (beta 1) | 표준화된 타깃 기준 |
| | 순서형 | CORAL: `BCEWithLogits(logits(K-1), [y>0, y>1, …])` 평균 | |
| | 결합 | `mean(loss_t / l0_t)` | 배치에 정답이 있는 항목만 |
| 타깃 변환 | 회귀 표준화 통계 (`target_stats`) | 학습 **크롭 행** 단위, 학습 rows만 | `train_full`이 `stats = target_stats(train_rows)`로 계산: 정답이 있는 크롭 행마다 값을 모아(log 항목은 `log1p` 후) `np.mean`, `np.std`(ddof=0), std가 0이면 1.0. 인물·부위별 중복 제거 없음(사진이 많은 사람이 더 반영됨), 테스트 165명 미포함. 체크포인트 `stats`와 `meta.json`의 `mean`/`std`로 저장 |
| | `pore_count` | `log1p` → 표준화. **v2 산출값** mean 6.6601, std 0.6619 (log 척도) | 유일한 log 항목. 복원 `expm1(z·std+mean)`. 숫자는 800명 F/L/R 학습 행에서 나온 결과이지 상수가 아님 |
| | `wrinkle_ra` | 표준화. **v2 산출값** mean 21.8764, std 5.5551 | |
| | `age` | 표준화. **v2 산출값** mean 41.8216, std 16.0429 | 실제 나이, 6부위 모두에 같은 값 |
| | `pore_grade` | 등급 0,1→"≤1" / 2 / 3 / 4,5→"≥4" (4클래스) | |
| | `wrinkle_grade` | 0~6 그대로 (7클래스) | |
| | `pigment_grade` | 0/1/2/3 / 4,5→"≥4" (5클래스) | |
| 증강 | `aug_rotate` | ±20° | `cv2.getRotationMatrix2D` 중심 회전+배율 → `warpAffine` (이미지 LINEAR, 마스크 NEAREST) |
| | `aug_scale` | ±10% | |
| | `aug_bright` | ±20%, 확률 0.5 | `clip(crop × factor)` |
| | 순서 | 반전 → **증강** → `normalize_crop` → 픽셀 정규화 → 마스크 | 증강은 원본 해상도 크롭에 적용 |
| 출력 | 열 배치 (16열) | pore_count [0,1) · pore_grade [1,4) · wrinkle_grade [4,10) · wrinkle_ra [10,11) · pigment_grade [11,15) · age [15,16) | `output_layout(TASKS)` 순서 = TASKS 정의 순서 |
| 평가 | 지표 | 순서형 QWK·acc·±1·spearman(기대등급) / 회귀 pearson·spearman·MAE | (사람,부위) 단위로 사진 평균 후 계산, n<10 항목 제외 |
| | 선택 점수 | mean(pore_count pearson, pore_grade qwk, wrinkle_grade qwk, wrinkle_ra spearman, pigment_grade qwk, age pearson) | skin v2 = 0.792 |
| 내보내기 | opset / 입력·출력 이름 | 18 / `crop` → `logits` | dynamo exporter (torch 2.14), 동적 축 batch·height·width |

학습 환경(Windows PC): torch 2.14.0, timm 1.0.29, mediapipe 0.10.35, opencv-contrib-python 5.0.0.93, numpy 2.5.3 (`face_analysis_AI/requirements.txt`). 이 레포 `.venv`도 torch 2.14.0 / timm 1.0.29 / mediapipe 0.10.35 / opencv 5.0.0.93 (numpy 2.4.6).

## 3. 노트북 전처리 vs `common/` 비교

**결론: 로직은 같다. 다른 것은 포장(모듈 경계)과 "증강 훅"뿐이다.**

| 항목 | 노트북 (`data_preprocessing/face_regions.py`, `data_v2.py`) | `common/` | 판정 |
|---|---|---|---|
| 랜드마크 다각형·이마 확장·눈막대 경계·피부 마스크·`crop_polygon`·`normalize_crop`·`CANVAS`·`TARGET_FACE_HEIGHT` | `face_regions.py` | `common/face_regions.py` | **동일** (`diff` 결과: 모델 경로를 인자로 받는 것, `read_image`/`extract_regions` 추가, `save_region_png` 제거만 다름) |
| 모델 입력 변환 순서 | 반전 → (증강) → `normalize_crop` → `/255` → `(x-mean)/std` → `×mask` → CHW (torch) | 반전 → `normalize_crop` → `/255` → `(x-mean)/std` → `×mask` → CHW (numpy float32) | **동일** (percentiles.ipynb도 서빙과 같은 numpy 식으로 재검증함) |
| 증강 | `RegionDataset.augment` (반전 뒤, 캔버스 전) | 없음 (`to_model_input`에 훅 없음) | 학습 전용. `backup/full-draft`에는 `augment=` 인자를 `common/transforms.py`에 추가했었는데 **현재 규칙상 기존 파일 수정 금지** → Phase 1에서 training 쪽 또는 새 common 파일로 |
| 크롭 입출력 | RGBA PNG 저장/로드 (`save_region_png`, `cv2.IMREAD_UNCHANGED`, 알파>0 → mask) | 없음 (서빙은 사진에서 바로 크롭) | 학습 전용. `backup/full-draft`의 `save/load_region_png`는 `common/face_regions.py`에 넣었었음 → 새 파일로 옮겨야 함 |
| 타깃 변환 (`grade_to_class`, `encode_target`, 표준화) | `data_v2.py` + 노트북 `target_stats` | `common/tasks.py`에는 **역변환(`decode`, `finalize`)만** 있음 | 정변환은 어디에도 없음 → 새 파일 필요. `decode`의 `expm1(z·std+mean)`은 노트북 `inverse`와 동일 |
| 항목 정의 (TASKS) | 노트북 dict 하드코딩 | 없음 (서빙은 `meta.json`의 `output.tasks`를 읽음) | `backup/full-draft`의 `configs/tasks.yaml`이 노트북 TASKS와 **값 일치** 확인 |
| 기기 필터·최소 해상도·`min_skin_px` | 스마트패드+스마트폰, 1,000,000 px, 20,000 | 서빙 `MIN_PIXELS`=1,000,000, `MIN_SKIN_PX`=20,000 | 동일 |
| 각도 처리 | `is_visible` (L → l_eye·l_cheek 숨김 등) | 동일 함수, `analyzer.VIEWS` front/left/right → F/L/R | 동일 |
| mediapipe / opencv | 0.10.35 / 5.0.0.93 | 0.10.35 / 5.0.0.93 (`requirements.txt`) | 동일 |

**알려진 학습·서빙 차이 (코드가 아니라 데이터 차이):** AI-Hub 사진에는 눈 가림 검은 막대가 있어 학습 눈가 크롭은 막대 경계까지 넓다. 서비스 사진엔 막대가 없어 `eye_bar_box`가 None → 랜드마크만으로 좁게 잘린다 (피부 면적 약 -33%). 165명 측정(2026-10-08): 눈가 주름 등급 QWK 0.897→0.900(변화 없음), 주름 Ra spearman 0.777→0.744. 재학습 때 눈가 다각형을 막대와 무관하게 만들지 결정 필요 (질문 Q6).

## 4. 모델 출력 형식과 서빙이 기대하는 파일

**파일** (`common/model_files.py` `REQUIRED`): `meta.json`, `skin.onnx`, `skin.onnx.data` — 셋 다 같은 폴더(`MODEL_DIR`), 없으면 `MODEL_S3_URI`에서 받음. 운영 S3: `s3://seoulection-prod-skin-images/models/skin_v2/` (여기엔 위 3개 + `SHA256SUMS.txt`).
레포 `models/skin_v2/`에는 추가로 `metrics.json`(테스트 점수), `reference.npz`, `check_onnx.py`, `SHA256SUMS.txt`가 있다 (서빙은 읽지 않음).

**`meta.json`** — 서빙(`common/predictor.py`)이 읽는 키:
- `version` ("2026-10-04"), `model.name` ("skin v2") → `modelVersion = "skin v2 2026-10-04"` (Kafka completed 메시지에 실림). `model.backbone`, `params`, `trained_on`은 정보용.
- `input`: `name`("crop"), `canvas`(부위→[가로,세로]), `target_face_height`(900), `mirror_right`(true), `pixel_mean`, `pixel_std`, `mask_fill`(0). **`SkinPredictor._check_input_contract`가 `canvas`·`target_face_height`·`pixel_mean`·`pixel_std`를 `common/` 상수와 assert로 비교** → 값이 다르면 서빙이 기동 시 멈춤.
- `output.tasks[]`: `task`, `kind`(regression|ordinal), `columns`([start,end)), `regions`, 회귀는 `mean`·`std`·`log`, 순서형은 `classes`(표시 이름 순서)·`grades`(표시 이름→원래 등급 목록). 서빙은 그 부위 `regions`에 있는 항목만 해석하고, 회귀 `z·std+mean`(log면 `expm1`), 순서형 `sigmoid` → 사진 평균 → `grade = classes[(p>0.5).sum()]`, `expected = p.sum()`.
- 열 순서·클래스 수가 바뀌면 다른 모델 버전이다(파인튜닝 불가, 처음부터 학습).

**ONNX 계약**: 입력 `crop` (N,3,H,W) float32, H·W 동적(부위별 캔버스), 출력 `logits` (N,16) float32. `analyzer`는 부위별로 배치 8로 돌림.

**`scoring.py` 의존값**: `RANGE = {pore_count: (94, 2113), wrinkle_ra: (12.3, 37.8)}`는 skin v2 **학습 라벨**의 1~99 백분위(모델 산출물 아님). 새 데이터로 재학습해도 라벨 분포가 같으면 그대로.

## 5. `reference.npz` 용도와 검증 방법

- 내용: `seed`=2026(int64), `regions`=6부위 이름, 부위별 `(1,16)` float32 로짓.
- 생성 규칙(`check_onnx.py`에서 역추적): `rng = np.random.default_rng(2026)`; 부위 순서대로 `rng.standard_normal((1,3,세로,가로), float32)`를 **같은 rng에서 연속으로** 뽑아 ONNX에 넣은 출력. 생성 코드는 노트북 셀에 없다(별도 스크립트, PC에 있을 것으로 추정 — Q5).
- 용도: (1) `SHA256SUMS.txt`로 전송 손상 확인, (2) 다른 머신·onnxruntime에서 같은 모델이 같은 값을 내는지 확인(기준 최대 차이 < 1e-3). 즉 **"이 ONNX 파일이 PC에서 만든 그 모델인가"**를 보는 것이고, 모델이 바뀌면 새로 만들어야 하는 모델 전용 파일이다.
- 이 Mac에서 실행한 결과 (`python models/skin_v2/check_onnx.py`, 읽기 전용): 해시 5개 모두 OK, 로드 OK(onnxruntime 1.30.0), 부위별 최대 차이 2.4e-07 ~ 1.43e-06 → **통과**.
- Phase 5 요구 "export 결과가 reference.npz와 허용 오차 내 일치"는 **`deploy_model.pt`에서 다시 export한 결과를 v2 reference와 비교**해야 의미가 있다. 체크포인트가 이 Mac에 없으므로 현재는 불가능 (Q1). 새 모델은 자기 reference.npz를 새로 만들어 함께 저장하는 규칙이 맞다.

## 6. `backup/full-draft`(이전 초안)와 현재 규칙의 충돌

초안에는 Phase 1·2에 해당하는 `training/{model,data,train,export,evaluate,preprocess,prepare_aihub}.py`, `configs/{train,tasks,paths,preprocess,faceparts}.yaml`, `configs/splits/aihub_kr_test.txt`(165명), `scripts/check_eye_bar_skew.py`가 있다. 하이퍼파라미터·구조는 노트북과 일치하고, `evaluate.py`는 v2 ONNX로 테스트 점수 0.792를 재현했다(2026-10-08).
단, 초안은 **기존 `common/` 파일을 수정**해 두었다: `common/config.py`(`load_yaml`, `read_id_list`), `common/tasks.py`(`load_tasks`, `output_layout`, `grade_to_class`, `to_train_scale`, `encode_target`, `meta_tasks`), `common/face_regions.py`(`save/load_region_png`), `common/transforms.py`(`augment=` 인자). 현재 규칙(기존 `common/` 수정 금지)에서는 이들을 **새 파일**(예: `common/training_targets.py`, `common/crop_io.py`)이나 `training/`으로 옮겨야 한다. `load_yaml`은 PyYAML이 필요하므로 서빙 `requirements.txt`에 없는 의존성 — 새 파일이 training에서만 import되면 문제없음.
초안의 `export.py`는 `model.name`을 `"skin"`으로 쓴다(v2는 `"skin v2"`) → 새 버전 이름 규칙 결정 필요 (Q4).

## 7. 브랜치 상태

- `main` = `origin/main` = ff49511. 운영 배포 커밋 ec21418은 `feat/private-ec2`(origin에 push됨, main 미병합)에 있고 `main`과 30파일 차이(serving/, tests/ 등).
- Phase 0에서 `feature/mlops-training`을 **`feat/private-ec2`(ec21418)에서** 만들었다. 운영 중인 `common/`·`serving/`과 같은 코드에서 시작해야 학습·서빙 불일치를 막을 수 있기 때문이다.
- 따라서 지금 `git diff main --stat`을 돌리면 서빙 변경 30파일이 함께 보인다. 보고 형식의 "금지 경로 0줄" 확인은 `git diff feat/private-ec2 --stat`으로 해야 맞다 (Q2).

## 8. 결정이 필요한 질문

1. **`deploy_model.pt` 확보** — ✅ 결정(2026-10-29): 사용자가 S3 `models/skin_v2/deploy_model.pt`로 직접 업로드. (원문) 파인튜닝 `--init-from`과 Phase 5의 reference 재현 테스트 모두 이 파일이 필요하다. Windows PC → S3 `models/skin_v2/deploy_model.pt`(또는 `pipeline-artifacts/`)로 올려 둘 수 있나? (업로드는 사람이. 구조는 `{"model": state_dict, "stats", "l0", "cfg", "tasks", "people"}`.) 없으면 Phase 1 스모크 테스트는 `init=imagenet`으로만 가능하다.
2. **비교 기준 브랜치** — ✅ 해결(2026-10-29): `feat/private-ec2`는 이미 PR #2로 `origin/main`에 병합돼 있었고 로컬 `main`만 뒤처져 있었다. 로컬 `main`을 전진시켰다. (원문) `feat/private-ec2`를 `main`에 먼저 병합할지, 아니면 리뷰에서 `git diff feat/private-ec2`로 볼지. (추천: 병합. 운영 코드가 main이 되는 게 자연스럽다.)
3. **`common/` 새 파일 이름·범위** — 타깃 정변환(`encode_target` 등)·yaml 로더·크롭 PNG 입출력·증강 훅을 (a) `common/` 새 파일 2~3개로, (b) 전부 `training/`에 둘지. 서빙이 쓸 일이 없는 코드라 (b)가 규칙 3("serving 동작에 영향 없음")에 더 안전하지만, 역변환 `decode`와 정변환이 다른 폴더에 갈라진다. 추천: (a) `common/training_targets.py` 1개(정변환+`meta_tasks`, numpy만)와 나머지는 `training/`.
4. **새 모델 버전의 `meta.json` 이름** — `model.name`을 `"skin v3"`처럼 올릴지, `"skin"` + `version`(날짜)만 쓸지. `modelVersion` 문자열이 Kafka 메시지로 나가므로 받는 쪽(추천 서버)과 합의가 필요하다. S3 폴더는 `skin_vN`으로 자동 증가(Phase 3).
5. **`reference.npz` 생성 규칙** — seed 2026, 부위 순서, (1,3,H,W) 연속 추출 규칙을 그대로 코드화해 `export_onnx.py`가 새 모델마다 `reference.npz`·`SHA256SUMS.txt`·`check_onnx.py`까지 만들게 할지(`models/skin_v2/`와 같은 구성이라 plan 그대로면 "예").
6. **눈가 크롭의 막대 의존** — 재학습 크롭을 만들 때 `eye_bar_box`를 끄고(서비스와 같은 좁은 눈가) 학습할지. 서빙 쪽 `common/face_regions.py`는 수정 금지라 서빙은 그대로이고, 학습만 서비스 조건에 맞추는 선택이다. Ra 지표가 테스트셋 기준으로는 내려갈 수 있다(막대 기준 측정과 비교 불가해짐).
7. **고정 테스트셋** — Phase 2 `make_splits.py`가 "시드 고정 분할을 새로 만들기"인지, 기존 165명(`test_subjects_165.json` → `configs/splits/aihub_kr_test.txt`)을 **그대로 보존**하는 것인지. 새로 나누면 v2의 0.792와 비교할 수 없다. 추천: 기존 165명을 원본으로 저장하고 스크립트는 검증(인물 겹침 없음)만.
8. **`feedback/`·`replay/` 데이터 형식** — S3 `feedback/`에 쌓이는 신규 데이터의 구조(사진만? 라벨은 누가·어떤 형식으로?)가 정해져 있지 않다. 서비스 사진에는 전문가·장비 라벨이 없으므로 학습 라벨 출처가 필요하다. `replay/`는 AI-Hub 크롭의 일부를 올리는 것으로 이해해도 되나(크롭 PNG 11 GB vs 원본 43 GB — 어느 쪽을 올릴지도 포함)?
9. **SageMaker Processing 입력** — 원본 JPG에서 매번 크롭(43 GB, CPU 약 0.2초/장 × 5,790장 ≈ 20분)할지, 크롭 PNG를 올려 두고 Processing은 목록·분할만 할지.
10. **`TrainConfig` 범위** — `folds`·`patience`·`valid_workers`(검증용)와 `detached_tasks`(미사용)를 기본값에 포함할지, 배포 학습(`train_full`) 경로만 스크립트화할지. 추천: 배포 경로만, `folds`/`patience`는 제외.
11. **노트북 환경 재현** — 학습 PC는 numpy 2.5.3(py 3.14), 레포는 numpy 2.4.6(py 3.11). `requirements-train.txt`를 어느 파이썬·CUDA 기준(SageMaker PyTorch 컨테이너 버전)으로 맞출지.
12. **파인튜닝 때 회귀 표준화 통계(`stats`)** — 베이스 체크포인트의 `stats`를 그대로 이어받을지(출력 열 의미 유지, 초안 `train.py`의 방식), 새 데이터로 재산출할지(같은 로짓의 의미가 바뀌어 기존 헤드가 틀어짐). `meta.json`의 `mean`/`std`는 어느 쪽이든 학습에 쓴 값을 그대로 내보내야 서빙 복원이 맞다. 추천: 이어받기.

## 9. 실행한 검증 (모두 읽기 전용)

| 명령 | 결과 |
|---|---|
| `diff face_analysis_AI/data_preprocessing/face_regions.py common/face_regions.py` | 로직 차이 없음 (경로 인자화·입출력 함수만) |
| `git diff backup/full-draft feat/private-ec2 -- common/` | 초안이 기존 `common/` 4개 파일을 수정했음을 확인 (§6) |
| `python models/skin_v2/check_onnx.py` | 해시 5/5 OK, 부위별 최대 차이 ≤ 1.43e-06, 통과 |
| `diff models/skin_v2/{meta.json,SHA256SUMS.txt}` vs 노트북 `outputs/deploy/skin_v2/` | 동일 |
| `labels_region.csv` / `index.csv` 집계 | 965명·5,790행 / 34,740행 (ok 26,754) |
| `find … deploy_model.pt fold*.pt` | v2 체크포인트 없음 (v1 `fold0.pt`만) |
| `git log main..feat/private-ec2`, `git diff main feat/private-ec2 --stat` | 3커밋, 30파일 차이 (§7) |
