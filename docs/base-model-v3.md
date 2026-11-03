# skin v3 베이스 모델 — SageMaker 노트북에서 학습하기

skin v3 = **ConvNeXt-Tiny + 강화 증강**, aihub_kr 800명(고정 테스트 165명 제외)을 인물 단위 8:2로 나눠 조기 종료.
설정은 `configs/train_v3.yaml` 한 파일이고, 코드는 주간 파이프라인과 같은 `training/`을 그대로 쓴다.
결과는 S3 `models/skin_v3/`에 넣고, 165명 점수가 v2(0.7921, `models/skin_v2/metrics.json`)와 비교할 만하면 서빙을 옮긴다.

데이터는 2026-11-03에 S3에 올려 두었다.

| S3 | 내용 |
|---|---|
| `datasets/full/aihub_kr/` | 표준 데이터셋 965명: `crops.csv`, `labels.csv`, `crops/<person>/…png` (11GB). 테스트·replay 인물 포함 — 분할은 `configs/splits/`가 정한다 |
| `datasets/ailabtools_165/` | 테스트 165명 목록 원본(`test_subjects_165.json`) + 상용 API 응답 165개 (비교용) |

## 0. 노트북 인스턴스

- **인스턴스**: `ml.g5.xlarge` (A10G 24GB, 쿼터 1대) — 또는 `ml.g4dn.xlarge` (T4 16GB, 쿼터 4대, 약 1.5배 느림)
- **볼륨**: **50GB 이상** (데이터 11GB + 패키지 + 산출물). 기본 5GB면 부족하다
- **실행 역할**: 2026-11-03 기준 계정에 노트북 인스턴스·SageMaker 역할이 없다. 콘솔에서 노트북 인스턴스를 만들 때
  "IAM 역할 → **새 역할 생성** → S3 버킷: **특정 S3 버킷 `seoulection-prod-skin-images`**"를 고르면 그 버킷 읽기·쓰기가 붙는다
  (데이터 받기, 모델 올리기에 필요). 기존 역할을 쓰려면 `s3:GetObject`·`s3:PutObject`·`s3:ListBucket`을 이 버킷에 한정해 추가
- **네트워크**: 기본(VPC 없음)으로 두면 인터넷으로 git·pip·S3에 바로 닿는다. prod VPC 프라이빗 서브넷에 넣으면 NAT·S3 엔드포인트가 있어야 한다
- **커널**: `conda_pytorch_*` 중 아무거나 — 어차피 아래에서 레포의 고정 버전을 설치한다

## 1. 준비 (터미널 셀, 처음 한 번)

```bash
cd ~/SageMaker
git clone https://github.com/Infinite-Track/seoulection-skin-analysis.git && cd seoulection-skin-analysis
git checkout feature/mlops-training            # 또는 v3 설정이 들어간 브랜치

sudo yum install -y mesa-libGL mesa-libEGL mesa-libGLES   # opencv·mediapipe 네이티브 라이브러리 (import에 필요)
conda create -y -n skin python=3.11 && source activate skin   # 노트북 인스턴스에는 conda가 있다. 시스템 python 대신 3.11 환경을 만든다
pip install -r requirements.txt -r requirements-train.txt   # 리눅스용 torch 2.14 휠은 CUDA 런타임을 포함한다 (별도 index 불필요)

aws s3 sync s3://seoulection-prod-skin-images/datasets/full/aihub_kr/ outputs/datasets/aihub_kr/ --only-show-errors   # 11GB, 같은 리전이라 수 분
tail -n +2 outputs/datasets/aihub_kr/crops.csv | wc -l      # 34,740 이어야 한다
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"   # True, A10G/T4
```

동작 확인(1~2분):
```bash
python -m training.train --config configs/train_v3.yaml --model-dir outputs/models/v3_smoke \
    --epochs 1 --max-batches 20 --l0-batches 30 --num-workers 0
```
`--l0-batches`는 항목별 시작 손실을 재는 배치 수다. 배치가 부위 단위라 너무 작으면 "정답이 없는 항목 pore_count"로 멈춘다 — 30이면 충분하고, 실제 학습은 기본값을 쓴다.

## 2. 학습 → 내보내기 → 평가

```bash
python -m training.train --config configs/train_v3.yaml --model-dir outputs/models/skin_v3 \
    --checkpoint-dir outputs/checkpoints/skin_v3 2>&1 | tee outputs/models/skin_v3_train.log
```
- 800명 → 학습 640 / 검증 160 (인물 단위, seed 42). 에폭마다 검증 점수를 찍고 `patience 6`만큼 안 오르면 멈춘다. 최대 15에폭.
- 예상 시간: v2(Small, 800명, 6에폭)가 RTX 5070에서 20분 → Tiny는 에폭당 약 2분(g5.xlarge). 15에폭 상한이면 길어야 40분.
- `--checkpoint-dir`를 주면 에폭마다 `last.pt`가 남아 커널이 죽어도 같은 명령으로 **이어서** 돌릴 수 있다 (`training/train.py` 맨 위).
- 끝나면 `outputs/models/skin_v3/`에 `model.pt`, `train_history.json`, 서빙 파일 6개(`meta.json skin.onnx skin.onnx.data reference.npz check_onnx.py SHA256SUMS.txt`)가 생긴다 (내보내기가 자동).

```bash
python -m training.evaluate --model-dir outputs/models/skin_v3        # 고정 테스트 165명 → outputs/models/skin_v3/metrics.json
python - <<'EOF'
import json
v2 = json.load(open("models/skin_v2/metrics.json"))["datasets"]["aihub_kr"]
v3 = json.load(open("outputs/models/skin_v3/metrics.json"))["datasets"]["aihub_kr"]
print(f"score  v2 {v2['score']:.4f}  v3 {v3['score']:.4f}")
for t in v3["tasks"]:
    k = "qwk" if "qwk" in v3["tasks"][t] else "pearson"
    print(f"  {t:14s} {k:8s} v2 {v2['tasks'][t][k]:.3f}  v3 {v3['tasks'][t][k]:.3f}")
EOF
```
평가는 학습에 쓴 적 없는 165명이고, v2와 같은 사진·같은 지표라 바로 비교된다.

## 3. S3에 올리기

```bash
aws s3 sync outputs/models/skin_v3/ s3://seoulection-prod-skin-images/models/skin_v3/ --only-show-errors
aws s3 ls s3://seoulection-prod-skin-images/models/skin_v3/        # model.pt, train_history.json, metrics.json, 서빙 파일 6개
```
`models/skin_v3/`는 새 폴더다. `skin_v2/`는 그대로 둔다 (롤백·비교 기준).

## 4. 서빙 전환 (점수가 납득될 때)

`docs/mlops.md` §6 3번과 같다: EC2 호스트에 `models/skin_v3/` 빈 폴더를 만들어 마운트하고 `.env`를
`MODEL_DIR=/models/skin_v3`, `MODEL_S3_URI=s3://seoulection-prod-skin-images/models/skin_v3`로 바꾼 뒤 컨테이너를 다시 띄운다.
`/health`의 `model.version`이 `skin v3 <날짜>`로 바뀌면 끝. 이후 주간 파이프라인은 `configs/pipeline.yaml current_model: skin_v3`로 바꾸고 `upsert`.

## 메모

- 증강 값(`aug_*`)은 제안값이다 — `configs/train_v3.yaml` 주석 참고. 바꾸면 `--checkpoint-dir`의 `last.pt`는 다른 실행이 되므로 지우고 시작한다.
- 노트북 인스턴스는 **쓰고 나면 Stop** (g5.xlarge 시간당 약 1.4달러). 볼륨은 Stop 상태에서 보존된다.
- `outputs/datasets/aihub_kr`은 노트북 볼륨에 두는 것이고 git에는 올라가지 않는다 (`.gitignore`).
