# training — 학습·내보내기·평가 (skin v2 노트북의 스크립트화)

설치: `pip install -r requirements.txt -r requirements-train.txt` (서빙 의존성 위에 학습용을 더한다).
설정·경로는 전부 `configs/`에서 읽고, 전처리는 `common/`을 import한다 (크롭·캔버스·정규화·타깃 변환 재구현 없음).

| 파일 | 역할 |
|---|---|
| `config.py` | `TrainConfig`(모델·손실·학습률 기본값 = skin v2, 검증 분할 `val_fraction` 0.2·`patience` 6·`epochs` 15 상한) · 우선순위 기본값 < `configs/train.yaml` < CLI · `load_tasks`, `load_paths` |
| `model.py` | `SkinModel` — 모든 하이퍼파라미터가 생성자 인자. 인자 없이 만들면 skin v2 구조 (v2 체크포인트와 state_dict 키 호환) |
| `dataset.py` | 표준 데이터셋(`crops.csv`, `labels.csv`, `crops/`) → 행·Dataset·부위별 배치. 형식은 파일 맨 위 |
| `losses.py` | 회귀 SmoothL1 / 순서형 CORAL BCE, 시작 손실(l0) 정규화, 경계값 prior |
| `train.py` | 학습 인물을 **인물 단위 80:20**으로 나눠 에폭마다 검증 점수를 재고 **조기 종료**(최고 에폭 가중치) → `model.pt` → 내보내기. 로컬·SageMaker(`SM_MODEL_DIR`, `SM_CHANNEL_*`) 양쪽, `--init-from` 파인튜닝(항목 정의·클래스 순서 검사), `--checkpoint-dir` 재개(연속 학습과 동일 결과) |
| `export_onnx.py` | `model.pt` → `skin.onnx`, `skin.onnx.data`, `meta.json`, `reference.npz`, `check_onnx.py`, `SHA256SUMS.txt` (= `models/skin_v2/` 구성) |
| `evaluate.py` | 내보낸 모델을 서빙 코드(`common/predictor.py`)로 고정 테스트셋에 평가 → `metrics.json`. 지표 계산 `score_grouped`는 학습 중 검증도 같이 쓴다 |

## 명령

```bash
# 처음부터: ImageNet 사전학습 → 인물 80:20 분할, 최대 15에폭, 검증 점수 6에폭 정체 시 조기 종료
python -m training.train
# skin v2 배포 학습 그대로 재현 (검증 없이 800명 전원, 6에폭 고정)
python -m training.train --val-fraction 0 --epochs 6
# 파인튜닝: 베이스 체크포인트에서 이어 학습 (항목 정의·stats·l0·경계값 이어받음)
python -m training.train --init-from outputs/models/<run>/model.pt --epochs 3 --lr-head 1e-4
# 설정 파일 바꿔서
python -m training.train --config configs/train.yaml --batch-size 16
# 내보내기만 / 평가만
python -m training.export_onnx --checkpoint outputs/models/<run>/model.pt
python -m training.evaluate --model-dir outputs/models/<run>
python outputs/models/<run>/check_onnx.py        # 다른 머신에서 같은 모델인지
```

CLI 옵션 이름은 `TrainConfig` 필드와 같다 (`lr_head` → `--lr-head`, bool은 `--amp/--no-amp`). `datasets`는 yaml로만.
`python -m training.train --help`로 전체 목록.

## 동작 확인 (스모크)

작은 데이터셋(사람 몇 명)을 표준 형식으로 만들고 `--epochs 1 --max-batches 20 --l0-batches 5 --num-workers 0`로 돌린다.
끝나면 `check_onnx.py`가 통과하고 `evaluate`가 `metrics.json`을 만들어야 한다. 데이터 준비 스크립트는 Phase 2(`scripts/`).

## 메모

- **검증·조기 종료**: 학습 인물을 정렬해 `random.Random(seed)`로 `val_fraction`만큼 뽑는다(인물 단위, 같은 데이터·seed면 같은 분할, 목록은 체크포인트 `val_people`). 에폭마다 검증 인물을 서빙과 같은 해석(`common.tasks.decode`)·평가와 같은 지표(`evaluate.score_grouped`: (사람, 부위) 사진 평균 → 항목별 선택 지표 평균)로 점수 내고, `patience` 에폭 연속 개선이 없으면 멈춘 뒤 **최고 점수 에폭의 가중치**를 저장·내보낸다(`best_epoch`, `epochs_trained`, `meta.json` `trained_on`에 기록). 검증 인물은 학습 데이터 안의 분할이며 고정 테스트셋(`configs/splits/`)과는 별개다 — 최종 비교는 `evaluate.py`.
- 회귀 표준화 통계(`stats`)·경계값 시작값·시작 손실은 **학습 쪽 인물로만** 계산해 체크포인트·`meta.json`에 저장한다 (검증 정보 누수 방지). 파인튜닝은 베이스의 것을 이어받는다.
- 증강은 반전 **앞**에 적용한다 (노트북은 뒤). 회전각 분포가 대칭이라 입력 분포는 같고, 덕분에 서빙과 같은 `to_model_input`을 그대로 쓴다.
- 증강 난수는 `(seed, 에폭, 행 번호)`로 정해진다 (노트북은 워커별 스트림). 분포는 같고, 워커 수와 무관하게 재현된다.
- **재개(`--checkpoint-dir`)**: `last.pt`에 모델·optimizer·scheduler·GradScaler·torch 난수 상태·실행 정보(데이터셋·크롭 수·stats·l0·항목 정의·설정)를 저장한다. 배치 순서(`set_epoch`)와 증강이 에폭으로 정해지고 난수 상태를 복원하므로 "연속 2에폭"과 "1에폭 + 재개 1에폭"의 가중치가 같다(CPU에서 차이 0 확인). 실행 정보가 다르면 다른 실행의 체크포인트로 보고 멈춘다. `last.pt`에는 최고 가중치·조기 종료 상태도 들어 있어 재개해도 같은 에폭에서 같은 결정을 한다(연속 3에폭 vs 1+재개 2에폭: 가중치 차이 0, 최고 에폭·점수 동일). 바꿔도 되는 설정은 `epochs`(연장), `patience`, `num_workers`, `checkpoint_dir`.
- 출력 규격 검사: `--init-from`은 베이스 체크포인트와, `evaluate`는 모델 `meta.json`과 `tasks.yaml`을 비교한다(항목 순서·종류·부위·**클래스 순서**·등급 구간·log). 다르면 멈춘다.
- `meta.json`의 `model.name`은 `model_name`(기본 `"skin"`), `version`은 날짜. 서빙 `modelVersion`은 이 둘을 붙인 값이다. 버전 번호(`skin_vN`)는 파이프라인 Register 단계가 S3 기존 버전을 보고 정하고, 그때 `model.name`을 `"skin vN"`으로 바꾼다 (`configs/pipeline.yaml register.model_name_format`).
- `--init-from`(또는 `SM_CHANNEL_BASE_MODEL`)에 폴더를 주면 `model.pt`, 없으면 skin v2 노트북 이름 `deploy_model.pt`를 찾는다 (S3 `models/skin_v2/`).
- 사전학습 가중치는 timm이 Hugging Face Hub에서 받는다 (SageMaker 컨테이너에 인터넷 또는 캐시 필요).
