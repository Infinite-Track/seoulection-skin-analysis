# scripts — 운영 보조·데이터 준비

| 파일 | 역할 | 비고 |
|---|---|---|
| `fetch_mediapipe_models.py` | mediapipe 모델 파일 다운로드 (해시 고정) | 서빙·Docker 빌드에서 사용 |
| `send_test_message.py` | Kafka 연동 확인 메시지 | 서빙 |
| `prepare_aihub.py` | AI-Hub 원본 → `images.csv`, `labels.csv` (데이터 소스별 어댑터) | 원본 경로는 환경변수 `AIHUB_RAW_ROOT` |
| `build_crops.py` | `images.csv` → `crops.csv` + `crops/` (부위 크롭). 전처리는 `common/` | 기기·해상도 기준 `configs/data.yaml`. 새 데이터·새 환경에서 처음부터 만들 때 |
| `import_notebook_crops.py` | skin v2 노트북이 만든 크롭(`face_analysis_AI/data`)을 하드링크로 가져와 `crops.csv` + `crops/` | aihub_kr은 이미 크롭이 있어 이걸 썼다 (build_crops와 결과 동일 — 3명 84크롭 픽셀 비교) |
| `make_splits.py` | 인물 단위 분할 → `configs/splits/<name>_test.txt`, `<name>_replay.txt` | aihub_kr은 skin v2의 165명을 `--import` |
| `upload_datasets.py` | 테스트셋·replay를 S3 `datasets/{testset,replay}/<name>/`에 업로드 | **기본 `--dry-run`**, 실제 업로드는 `--execute`. 올리기 전 완전성 검사 — 불완전하면 아무것도 올리지 않음 |
| `crop_index.py` | `crops.csv` 공통: 원자적 저장, "완전한 사진" 검사(6부위 행·status·ok PNG 존재) | `build_crops` 재개와 `upload_datasets` 검사가 같이 쓴다 |

## 데이터 준비 순서 (aihub_kr)

```bash
export AIHUB_RAW_ROOT="/path/to/028.한국인_피부상태_측정_데이터/3.개방데이터/1.데이터"
python -m scripts.prepare_aihub                                   # → outputs/datasets/aihub_kr/{images,labels}.csv
python -m scripts.make_splits --dataset outputs/datasets/aihub_kr \
    --import /path/to/ailabtools/test_set/test_subjects_165.json  # → configs/splits/aihub_kr_test.txt, aihub_kr_replay.txt
# 크롭: (a) 노트북 크롭이 있으면 가져오기 — 수 초
python -m scripts.import_notebook_crops --src /path/to/face_analysis_AI/data --dataset outputs/datasets/aihub_kr
#      (b) 처음부터 — 약 5,800장, 6워커 수 분, 재실행 시 이어서
python -m scripts.build_crops --dataset outputs/datasets/aihub_kr --workers 6
python -m scripts.make_splits --dataset outputs/datasets/aihub_kr --check
python -m scripts.upload_datasets --dataset outputs/datasets/aihub_kr           # dry-run: 올릴 목록·크기만
python -m scripts.upload_datasets --dataset outputs/datasets/aihub_kr --execute # 실제 업로드 (사람이 실행)
```

표준 데이터셋 형식(`crops.csv`, `labels.csv`, `crops/`)은 `training/dataset.py` 맨 위에 있다. 새 데이터 소스는 `prepare_*.py` 어댑터만 추가하면 된다.
