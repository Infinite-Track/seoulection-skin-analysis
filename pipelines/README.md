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

## Prefect 주간 플로우 (`prefect_flow.py`)

매주 **월요일 03:00 KST**(`configs/prefect.yaml schedule`)에: 신규 데이터 확인 → 기준 미달이면 종료 → 위 파이프라인 `start` → 완료 대기 → 결과 로그(통과 여부·새 버전·지표) → 기준점 저장.
**자동 배포 없음** — Register가 `PendingManualApproval`로 올리는 데서 끝나고 승인·배포는 사람이 한다. 플로우는 boto3 `sagemaker`(start·describe·list steps)와 `s3`만 쓴다.

- **신규 데이터 확인**은 `new_data.py` 인터페이스(`NewDataSource.check(since) → NewDataReport`). 현재 `s3_feedback`: S3 `feedback/`에서 지난 실행 기준점 이후 올라온 파일 수. 최소 건수는 환경변수 `MIN_NEW_FEEDBACK_FILES`(없으면 `prefect.yaml new_data.min_new_files`). 추천 서버 count·export API 연동은 `RecommendServerSource`에 TODO로만 있다.
- **기준점(watermark)**: `s3://<bucket>/pipeline-artifacts/<name>/prefect/last_run.json`의 `watermark`(그때 센 파일 중 가장 최근 `LastModified`). 파이프라인이 `Succeeded`로 끝났을 때만 갱신한다 — 실패하면 다음 주에 같은 데이터를 다시 센다. Condition 불통과는 `Succeeded`(정상 종료)라 기준점이 갱신된다. 기존 watermark가 더 최근이면 뒤로 되돌리지 않는다.
- **중복 실행 방지** (`prefect/active_run.json`): 파이프라인을 시작하기 전에 이 기록을 **예약**한다 — 기록이 없으면 `If-None-Match: *`, 끝난 기록(`state: released` 묘비)이 있으면 읽은 ETag로 `If-Match` 덮어쓰기. 둘 다 원자적이라 하나만 성공한다. 기록에는 `state`(`reserved`→`started`→`released`), `run_id`(= SageMaker `ClientRequestToken`, 멱등 키), 실행 표시 이름, 실행 ARN, 그때의 기준점·신규 건수·파라미터가 들어간다. 플로우는 시작할 때 살아 있는 기록(`reserved`/`started`)이 있으면 **새로 시작하지 않고 그 실행을 이어받는다**: 진행 중이면 대기(`--no-wait`면 `in_progress`로 끝), 끝났으면 결과 로그·기준점 저장·기록 해제 뒤 신규 데이터를 다시 센다. 그래서 `--no-wait` 뒤 재호출, 대기 시간 초과 뒤 다음 주 실행, 프로세스 재시작, 같은 시각 중복 호출 모두 start는 1회다. `--force`는 최소 건수만 무시한다 — 진행 중 실행이 있으면 force여도 새로 시작하지 않는다.
- **시작 여부가 불확실한 예약(ARN 없음)**: 예약 뒤 ARN을 기록하기 전에 죽었거나 멈춘 경우. 먼저 표시 이름으로 실행 목록(`ListPipelineExecutions`)에서 찾아 이어받고, 그래도 없으면 예약이 `reserve_stale_seconds`(10분)보다 오래됐을 때 **그 예약의 `run_id`(`ClientRequestToken`)·표시 이름·파라미터 그대로 start를 다시 요청**해 실행을 확보한다. SageMaker 멱등성으로 원래 요청이 이미 닿았으면 같은 실행 ARN이, 아니면 새 실행 1개가 돌아온다. 새 `run_id`로 바꾸지 않으므로 멈춰 있던 원래 프로세스가 재개해 자기 토큰으로 start해도 같은 실행을 받고(응답이 늦게 도착한 start 요청도 같다), 기록의 ARN과 일치한다 — 실행은 1개만 생긴다. 10분이 안 된 예약은 다른 프로세스가 지금 시작하는 중일 수 있어 건드리지 않는다(`in_progress`). 새 `run_id`로 다시 가는 경우는 **확실한 종료**만: 기록된 실행 ARN이 더는 없거나(지워짐 → `abandoned`), SageMaker가 start를 확실히 거절했을 때(`ValidationException`·`ResourceNotFound`·`ResourceLimitExceeded`·`AccessDeniedException` 등 → ARN 없는 그 예약을 `rejected`로 해제하고 플로우는 실패). 스로틀링·5xx·네트워크 오류는 실행이 생겼는지 모르므로 예약을 그대로 두고 다음 실행이 같은 토큰으로 다시 시도한다. 토큰 멱등 창을 지난 아주 오래된 예약은 표시 이름 검색이 먼저 찾는다.
- **소유권·원자성**: 기록을 바꾸는 모든 쓰기(ARN 기록, 해제, 예약)는 "읽었을 때 `run_id`가 내 것이고 ETag가 그대로"일 때만 이루어진다(`If-Match`, 충돌(412/409)이면 다시 읽어 판단). 같은 실행을 여러 호출이 이어받아 마무리해도 먼저 끝낸 쪽만 해제하고, 그 뒤 새 실행이 예약됐으면 늦은 쪽은 `run_id`가 달라 손대지 않는다. 끝난 기록은 지우지 않고 `state: released` 묘비로 남긴다(일반 버킷에서 조건부 삭제 대신 조건부 덮어쓰기). 기준점 `last_run.json`도 같은 방식(읽은 ETag로 `If-Match`)이고 기존 watermark가 더 최근이면 쓰지 않는다 — 단조 증가.
- **dry-run은 읽기만**: `run`(기본, `--execute` 없음)은 기록·실행 상태·기준점·신규 데이터를 읽고 "execute였다면 무엇을 했을지"(`plan`)를 로그·결과로 남긴다. 완료 대기·ARN 복구 기록·기준점 갱신·해제·시작을 하지 않으므로 읽기 권한만으로 점검할 수 있다. 결과 `status`는 항상 `dry_run`.
- **실패 후 재시도 정책**: 실행이 `Failed`/`Stopped`로 끝나면 기록을 해제하고 기준점은 그대로 둔다 → 다음 실행(다음 주 스케줄 또는 수동 `run --execute`)이 같은 데이터로 다시 시도한다. 즉시 재시도는 사람이 `run --execute`로 한다. 플로우 자체는 실패로 끝난다.
- **결과 로그**: 단계별 상태, Condition `Outcome`, `evaluation.json`(점수·실패 축), `register.json`(새 버전 `skin_vN`, S3, Model Package ARN). 실행이 `Failed`/`Stopped`면 플로우도 실패로 끝난다. 대기 상한(`wait.timeout_seconds`, 16h)을 넘으면 플로우만 실패하고 실행·`active_run.json`은 그대로 둔다(다음 실행이 이어받는다).

```bash
pip install -r requirements.txt -r requirements-pipeline.txt          # prefect>=3.9 포함. 워커 PC에는 sagemaker SDK가 없어도 플로우는 돈다
export MIN_NEW_FEEDBACK_FILES=50                                       # 선택. 없으면 prefect.yaml 값(1)
python -m pipelines.prefect_flow check [--since 2026-10-01T00:00:00Z]  # 신규 데이터 수·기준점·진행 중 실행 기록(active)·마지막 해제 기록(last_released) (S3 읽기, Prefect 없이). 기준 이상이면 0, 미만이면 3
python -m pipelines.prefect_flow run                                   # 로컬 1회 — 기본 dry-run: S3 읽기 + 계획 로그만, 쓰기·대기·시작 없음
python -m pipelines.prefect_flow run --execute [--force] [--param Epochs=2 --param MinFeedbackCrops=0] [--no-wait]   # 실제 시작 (사람이 실행)
python -m pipelines.prefect_flow serve [--paused] [--cron "0 3 * * 1" --timezone Asia/Seoul]   # 스케줄 배포 등록 + 실행 프로세스 (계속 떠 있음)
```

`serve`는 Prefect 3의 `flow.serve`(워크 풀 없이 이 프로세스가 스케줄을 실행)로 배포 `seoulection-skin-weekly-retrain/weekly`를 만들고 `execute=True`, `global_limit=1`(같은 배포의 플로우 실행 동시 1개 — 보조 장치, 본체는 위 `active_run.json`)로 돈다.
`PREFECT_API_URL`이 없으면 로컬 임시 서버를 쓴다 — 상시 운영은 Prefect 서버/Cloud를 가리키고 `serve`를 systemd 등으로 띄운다. cron을 바꾸면 `prefect.yaml`을 고치고 `serve`를 다시 띄운다.
플로우를 돌리는 자격증명에 필요한 권한: `sagemaker:StartPipelineExecution/DescribePipelineExecution/ListPipelineExecutions/ListPipelineExecutionSteps`, S3 `feedback/` 목록, `pipeline-artifacts/` 읽기 + `prefect/last_run.json`·`prefect/active_run.json` 쓰기(`s3:PutObject`, 조건부 쓰기 `If-Match`/`If-None-Match` 사용 — 일반 버킷은 2024-11 이후 지원; 삭제 권한은 필요 없다). dry-run(`run` 기본값)·`check`는 읽기 권한만으로 된다.

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
