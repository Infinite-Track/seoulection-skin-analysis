# Phase 4 보고 — Prefect 플로우 (`pipelines/prefect_flow.py`)

> 작성: Claude, 2026-10-30. 브랜치 `feature/mlops-training`. **AWS 호출 없음** — 검증은 전부 boto3 클라이언트 대역 + Prefect 임시 서버로 했고, `run --execute`·`serve`를 실제 AWS에 대고 실행하지 않았다.

## 변경 파일

| 파일 | 내용 |
|---|---|
| `pipelines/prefect_flow.py` **(신규)** | Prefect 3.9 플로우 `seoulection-skin-weekly-retrain`. 흐름: 기준점(watermark) 읽기 → 신규 데이터 확인 → 최소 건수 미만이면 `skipped` 종료 → (`execute=False`면 `dry_run` 종료) → SageMaker 파이프라인 `start_pipeline_execution` → `describe_pipeline_execution` 폴링으로 완료 대기 → 단계 상태·Condition `Outcome`·`evaluation.json`(점수·실패 축)·`register.json`(새 버전·S3·Model Package ARN) 읽어 로그 → `Succeeded`일 때만 기준점 저장. 실행이 `Failed`/`Stopped`면 플로우도 실패(기준점 미갱신). 자동 배포 없음. 플로우는 boto3 `sagemaker`·`s3`만 쓴다(sagemaker SDK 불필요). CLI `check`(Prefect 없이 신규 데이터 수만) / `run`(로컬 1회, **기본 dry-run**, `--execute --force --since --param K=V --no-wait`) / `serve`(`flow.serve` 스케줄 배포, `--paused --cron --timezone`) |
| `pipelines/new_data.py` **(신규)** | 신규 데이터 확인 인터페이스 `NewDataSource.check(since) → NewDataReport(count, total, latest, by_prefix…)`. 구현 `S3FeedbackSource`(S3 `feedback/` 객체 중 `LastModified > since` 파일 수, 폴더 표시 객체 제외, 첫 하위 폴더별 집계). `RecommendServerSource`는 추천 서버 count·export API 연동 **TODO 주석 + NotImplementedError**. `make_source(name, …)` 팩토리 |
| `configs/prefect.yaml` **(신규)** | 플로우·배포 이름, `schedule.cron "0 3 * * 1"` + `timezone Asia/Seoul`(매주 월 03:00 KST, 설정으로 변경), `new_data.source s3_feedback`·`min_new_files 1`(환경변수 `MIN_NEW_FEEDBACK_FILES` 우선)·`marker_key prefect/last_run.json`, `wait.poll_seconds 60`·`timeout_seconds 57600`(16h = Training max_wait 12h + Processing 여유) |
| `tests/test_prefect_flow.py` **(신규)** | 12개: S3 소스 집계(기준점·페이지네이션·naive→UTC), 소스 팩토리, 설정(cron·시간대·환경변수 우선), 시간 파싱, 대기·시간 초과, 시작 파라미터·파이프라인 없음 안내, 요약(통과/불통과/실패 사유), 플로우 5개(기준 미달 skip, dry-run은 시작 안 함, 기준점·`--since`·`force`·`--no-wait`, 실행→대기→로그→기준점 저장→다음 주 skip, 실패 시 기준점 미갱신). AWS 없음(boto3 대역), Prefect는 `prefect_test_harness`(임시 SQLite 서버) |
| `requirements-pipeline.txt` | `prefect>=3.9,<4` 추가 (등록·플로우를 돌리는 쪽. 컨테이너에는 안 들어감) |
| `pipelines/README.md`, `configs/README.md` | Prefect 플로우 절·`prefect.yaml` 행 추가 |

금지 경로(`serving/`, 기존 `common/` 파일, `models/`, `Dockerfile`, `requirements.txt`, `.dockerignore`, `.env.example`, 기존 `tests/`) 변경 0줄. 하드코딩 없음: 파이프라인 이름·버킷·리전·feedback 주소는 `pipeline.yaml`·`paths.yaml`(Phase 3 `load_settings` 재사용), cron·최소 건수·기준점 경로·대기 상한은 `prefect.yaml`/환경변수.

## git diff main --stat

```
 .gitignore                           |   3 +
 common/crop_files.py                 |  28 +++
 common/training_targets.py           | 103 +++++++++++
 configs/README.md                    |  15 ++
 configs/data.yaml                    |  17 ++
 configs/paths.yaml                   |  17 ++
 configs/pipeline.yaml                |  45 +++++
 configs/prefect.yaml                 |  19 ++
 configs/splits/aihub_kr_replay.txt   | 101 +++++++++++
 configs/splits/aihub_kr_test.txt     | 167 +++++++++++++++++
 configs/tasks.yaml                   |  41 +++++
 configs/train.yaml                   |  19 ++
 configs/train_pipeline.yaml          |  12 ++
 docs/agents/mlops-plan.md            | 112 ++++++++++++
 docs/agents/reports/phase-0.md       | 180 ++++++++++++++++++
 docs/agents/reports/phase-1.md       |  84 +++++++++
 docs/agents/reports/phase-2.md       |  70 +++++++
 docs/agents/reports/phase-3.md       | 136 ++++++++++++++
 docs/agents/reviews/phase-0.md       |  25 +++
 docs/agents/reviews/phase-1.md       |  71 ++++++++
 docs/agents/reviews/phase-2.md       |  66 +++++++
 docs/agents/reviews/phase-3.md       |  81 +++++++++
 pipelines/README.md                  |  70 +++++++
 pipelines/__init__.py                |   0
 pipelines/new_data.py                | 113 ++++++++++++
 pipelines/prefect_flow.py            | 341 +++++++++++++++++++++++++++++++++++
 pipelines/requirements-container.txt |  16 ++
 pipelines/sagemaker_pipeline.py      | 330 +++++++++++++++++++++++++++++++++
 pipelines/steps/__init__.py          |   0
 pipelines/steps/bootstrap.py         |  34 ++++
 pipelines/steps/evaluate.py          | 135 ++++++++++++++
 pipelines/steps/prepare.py           | 252 ++++++++++++++++++++++++++
 pipelines/steps/register.py          | 232 ++++++++++++++++++++++++
 pipelines/steps/train.py             |  14 ++
 pipelines/steps/util.py              |  78 ++++++++
 requirements-pipeline.txt            |   6 +
 requirements-train.txt               |  15 ++
 scripts/README.md                    |  30 +++
 scripts/build_crops.py               | 117 ++++++++++++
 scripts/crop_index.py                |  87 +++++++++
 scripts/import_notebook_crops.py     |  61 +++++++
 scripts/make_splits.py               | 129 +++++++++++++
 scripts/prepare_aihub.py             | 121 +++++++++++++
 scripts/upload_datasets.py           | 119 ++++++++++++
 tests/test_pipeline_steps.py         | 152 ++++++++++++++++
 tests/test_prefect_flow.py           | 251 ++++++++++++++++++++++++++
 training/README.md                   |  48 +++++
 training/__init__.py                 |   0
 training/config.py                   | 163 +++++++++++++++++
 training/dataset.py                  | 167 +++++++++++++++++
 training/evaluate.py                 | 112 ++++++++++++
 training/export_onnx.py              | 140 ++++++++++++++
 training/losses.py                   |  70 +++++++
 training/model.py                    |  99 ++++++++++
 training/templates/check_onnx.py     |  41 +++++
 training/train.py                    | 207 +++++++++++++++++++++
 56 files changed, 5162 insertions(+)
```
(Phase 0~3 파일 포함, 전부 신규. 금지 경로 검사 `git diff main --name-status -- serving common models Dockerfile requirements.txt .dockerignore .env.example tests` → 신규 `A` 4개(`common/crop_files.py`, `common/training_targets.py`, `tests/test_pipeline_steps.py`, `tests/test_prefect_flow.py`)뿐, 기존 파일 `M`·`D` 없음. Phase 4 자체 변경은 `git diff 6e9eabb --stat`: 8파일 +789/-1 — 위 신규 5개 + README 2개 + `docs/agents/reviews/phase-3.md`(Codex 재검토 PASS 기록, 별도 커밋).)

## 검증

환경: 프로젝트 venv에 `prefect` 3.9.0 설치(`pip check` 충돌 없음; `websockets` 17.2 → 16.1.1로 내려감, 서빙 `requirements.txt`에는 없는 패키지). 플로우가 쓰는 boto3 클라이언트는 `prefect_flow.make_clients`를 대역으로 바꿔 **AWS를 호출하지 않았다**.

| 검증 | 결과 |
|---|---|
| `pyflakes pipelines training scripts common tests/test_prefect_flow.py tests/test_pipeline_steps.py` | 통과 |
| `pytest -q` 전체 | **44 passed** (기존 32 + 신규 12), 15초 |
| 설정 로드 (`load_flow_settings`, 실제 `configs/`) | 플로우 `seoulection-skin-weekly-retrain/weekly`, cron `0 3 * * 1` `Asia/Seoul`, 파이프라인 `seoulection-skin-retrain`, 리전 `us-east-1`, feedback `s3://seoulection-prod-skin-images/feedback/`, 기준점 `pipeline-artifacts/seoulection-skin-retrain/prefect/last_run.json`, 최소 1(`MIN_NEW_FEEDBACK_FILES=25` → 25) |
| `S3FeedbackSource.check` (가짜 S3 6파일 + 폴더 표시 객체 2개 + 다른 접두사 1개, 2페이지) | 기준점 없음 → 6/6, 기준점 지정 → 3/6(`LastModified > since`만), 폴더 객체·다른 접두사 제외, `latest`=가장 최근 시각, naive 시각은 UTC로 |
| CLI `check` (대역) | JSON(신규 수·전체·`latest`·`marker`·`enough`) 출력, 기준 이상 종료 0 / `--since 2030-…` → 0개, 종료 3 |
| CLI `run` (기본, 대역 + Prefect 임시 서버) | 신규 4개 ≥ 1이지만 `execute=False` → `dry_run`, `start_pipeline_execution` 호출 없음 |
| CLI `run --execute --param Epochs=2 --param MinFeedbackCrops=0` (대역: Executing→Succeeded, 단계 5개, Condition `Outcome=True`, `evaluation.json`·`register.json`) | 시작 요청에 `PipelineParameters [{Epochs,2},{MinFeedbackCrops,0}]`, 상태 변화 로그, 요약 로그(점수 0.8100 vs 0.8000, 축 21개 중 실패 0, 조건 통과, 새 버전 `skin_v3` → `s3://…/models/skin_v3/`, Model Package ARN, "승인·배포는 사람"), 기준점 저장(`watermark`=센 파일 중 최근 시각, `version skin_v3`, `new_files 4`) |
| CLI `run --execute` 다시 | 기준점 이후 신규 0 → `skipped`, 시작 호출 없음 |
| 플로우 테스트 | 기준 미달 skip(시작·저장 없음) / dry-run 시작 없음 / 기준점을 `since`로 사용 + `--since`가 기준점보다 우선 + `force`로 기준 미달 시작 + `wait=False`면 `started`·기준점 없음 / 실행 `Failed` → `RuntimeError`, 기준점 미갱신 |
| `wait_pipeline` | 상태 변화만 콜백(`Executing`, `Succeeded`), 시간 초과 → `TimeoutError`("실행은 그대로 둔다") |
| `start_pipeline` | 파이프라인 없음(`ResourceNotFound`) → "먼저 upsert" 안내 |
| `summarize_execution` | 통과/불통과(`Outcome=False`, register 없음, 실패 축 `pore_grade.mae`)/Prepare 실패(`FailureReason`, 판정 없음) 세 경우 요약·로그 문구 확인 |
| CLI `serve` (호출 가로채기) | `flow.serve(name="weekly", schedule=Cron("0 3 * * 1", timezone="Asia/Seoul"), parameters={"execute": True}, paused=…)`, `--cron "30 4 * * 2" --timezone UTC` 덮어쓰기 반영 |

## 설계 결정·가정 (답이 다르면 `configs/prefect.yaml` 또는 `new_data.py`만 바꾸면 된다)

- **기준점 = S3 마커 파일**(`pipeline-artifacts/<name>/prefect/last_run.json`), 값은 그때 센 feedback 파일 중 가장 최근 `LastModified`. Prefect 상태 저장소 대신 S3를 쓴 이유: Prefect 서버를 바꿔도 유지되고, `check` 명령이 Prefect 없이도 같은 기준점을 본다. 파이프라인이 `Succeeded`일 때만 갱신 → Prepare가 "신규 크롭 부족"으로 실패하면 다음 주 같은 데이터를 다시 센다. **Condition 불통과는 `Succeeded`(정상 종료)라 기준점이 갱신된다** — 그 데이터는 이미 학습에 쓰였으므로 다음 주에 다시 세지 않는다.
- **신규 파일 수 vs 라벨 있는 크롭 수**: 플로우는 feedback/ 파일 수만 세고(형식 검사 없음), 실제 학습 가능한 신규 크롭 수는 Prepare의 `MinFeedbackCrops`가 거른다 (두 단계 기준). 플로우 쪽 최소값 기본 1은 "뭐라도 올라왔으면 돌린다"이고, 운영 기준은 `MIN_NEW_FEEDBACK_FILES`로 정한다.
- **`run` 기본 dry-run**: 규칙 5(AWS 리소스 생성 명령은 기본 dry-run)에 맞춰 `--execute` 없이는 파이프라인을 시작하지 않는다. 스케줄 배포(`serve`)는 `execute=True`로 등록된다.
- **boto3 직접 사용**: start·describe·list steps는 boto3 `sagemaker` 클라이언트로 한다(sagemaker SDK v3는 `upsert` 쪽에만 필요). 워커 환경이 가벼워지고 대역 테스트가 쉽다.
- **`flow.serve`**: 워크 풀·워커 없이 `serve` 프로세스 하나가 스케줄을 실행한다(Prefect 3 권장 단순 구성). `PREFECT_API_URL` 없으면 로컬 임시 서버. 상시 운영에서 Prefect 서버/Cloud + systemd로 `serve`를 띄우면 된다. 워크 풀 기반 `prefect deploy`가 필요하면 `weekly_retrain.to_deployment(...)`로 바꿀 수 있다.
- **대기 상한 16h**: Spot 대기 포함 Training 12h + Processing 3단계. 넘으면 플로우만 실패하고 실행은 멈추지 않는다(다음 주 플로우는 독립 실행 — 동시 실행 방지는 하지 않았다; 필요하면 `serve(global_limit=1)`).
- **실행 ID → 산출물 경로**: `evaluation.json`·`register.json`은 Phase 3 정의의 `run_uri`(`<artifacts>/<PIPELINE_EXECUTION_ID>/…`)와 같은 규칙으로 읽는다. SageMaker의 `PipelineExecutionId`는 실행 ARN의 마지막 경로 조각이다.

## 실제 AWS에서만 확인할 수 있는 것 (첫 `run --execute` 때 볼 것)

1. `list_pipeline_execution_steps`의 Condition 단계 `Metadata.Condition.Outcome` 값 형식("True"/"False" 문자열로 가정, 소문자 비교).
2. 산출물 경로의 실행 ID가 ARN 마지막 조각과 같은지 (`pipeline-artifacts/<name>/<id>/evaluation/evaluation.json`).
3. 플로우 자격증명 권한: `sagemaker:StartPipelineExecution/DescribePipelineExecution/ListPipelineExecutionSteps`, S3 `feedback/` 목록·`pipeline-artifacts/` 읽기·`prefect/last_run.json` 쓰기.

## Codex 리뷰 반영 (1회차, 2026-10-30)

리뷰 `docs/agents/reviews/phase-4.md`: Major 1건 — 진행 중 실행이 있어도 같은 데이터로 다시 부르면 별도 SageMaker 실행을 시작한다(`--no-wait`·대기 시간 초과·재시작 뒤 복구 없음, 기준점 후퇴 보호 없음). **AWS 호출 없음** — 전부 boto3 대역.

### 변경 파일 (`git diff f609140 --stat`: 6파일 +507/-86)

| 파일 | 내용 |
|---|---|
| `pipelines/prefect_flow.py` | **예약 기록** `s3://<bucket>/<artifacts>/<name>/prefect/active_run.json`(설정 `new_data.active_key`): 파이프라인 start **전에** `put_object(IfNoneMatch="*")`로 원자적 생성 → 이미 있으면 시작하지 않고 `in_progress` 반환. 내용 = `run_id`(uuid4 hex 32자 = SageMaker `ClientRequestToken`, 멱등 키)·`display_name`(`prefect-<UTC시각>-<run_id 8자>`)·`execution_arn`·`reserved/started/owner`·그때의 `since/watermark/new_files/parameters`. start 직후 ARN을 기록. **이어받기**: 플로우 시작 시 기록이 있으면 `describe_pipeline_execution` → 진행 중이면 대기(`wait=False`면 `in_progress`), 끝났으면 요약 로그 → `Succeeded`면 기준점 저장 → 기록 삭제 → 그 다음 신규 데이터를 **새 기준점으로 다시 센다**(결과 `previous`에 이전 실행 요약). ARN 기록 전에 죽은 경우 `list_pipeline_executions`에서 표시 이름으로 ARN 복구(`recovered` 표시), 실행이 없으면 예약이 `reserve_stale_seconds`(10분)보다 오래됐을 때만 삭제(더 새 예약은 다른 프로세스가 시작 중일 수 있어 `in_progress`). 실행이 삭제된 ARN(`ResourceNotFound`)도 같은 처리. **기준점 보호** `merge_marker`: 기존 watermark가 더 최근이면 유지(`watermark_kept: true`). **정책**: `--force`는 최소 건수만 무시(진행 중 실행 있으면 새로 시작 안 함); 실패·중단 종료 시 기록 삭제·기준점 유지 → 다음 실행(스케줄·수동)이 같은 데이터로 1회 재시도, 플로우는 실패로 끝남; 대기 시간 초과 시 기록 유지 + 경고 로그 → 다음 실행이 이어받음. `serve`에 `global_limit=1`(Prefect 쪽 보조). `check` 출력에 `active` 추가 |
| `configs/prefect.yaml` | `new_data.active_key: prefect/active_run.json`, `new_data.reserve_stale_seconds: 600` 추가, 주석 보강 |
| `tests/test_prefect_flow.py` | 12 → **25개**. 대역 확장: `FakeS3.put_object(IfNoneMatch)` 412·`delete_object`, `FakeSageMaker.list_pipeline_executions`(2페이지)·삭제된 실행. 순수 함수 4개(토큰·표시 이름 규격, 원자적 예약·해제, 표시 이름 검색·삭제된 실행, `merge_marker`). 플로우 "중복 실행 방지" 8개: 완료 전 중복 호출(+`force`) → `in_progress`·start 1회 → `--no-wait` 후 재호출이 이어받아 기준점 저장·해제·`skipped`; 대기 시간 초과 → `TimeoutError`, 기록 유지 → 다음 호출이 마무리(start 1회); 재시작(기록에 ARN 있음) → start 0회; 재시작(ARN 없음) → 표시 이름으로 복구·기록 갱신; 예약만 있음(새 예약 → 시작 안 함 / 오래된 예약 → 삭제 후 1회 시작 / 실행 삭제됨 → 삭제 후 계속); 이전 실행 `Failed` → 정리 후 같은 5개로 1회 재시도; 읽기–예약 사이 선점(412) → 시작 안 함; 더 최근 기준점이 있으면 옛 실행 마무리가 되돌리지 않음. `check` CLI 테스트 1개. **fixture가 Prefect 임시 서버를 띄울 수 없으면(샌드박스 소켓 bind 금지) 태스크를 `.fn`으로 바꿔 같은 플로우 함수를 돈다** — 리뷰 환경에서 5개 error 나던 문제 대응 |
| `pipelines/README.md`, `configs/README.md` | "중복 실행 방지"·"실패 후 재시도 정책" 절, 필요한 권한(`ListPipelineExecutions`, `active_run.json` 쓰기·삭제) 추가 |
| `docs/agents/mlops-plan.md`, `docs/agents/reviews/phase-4.md` | 진행 현황 비고, 리뷰 항목 반영 표시 |

금지 경로 변경 0줄(`git diff main --name-status -- serving common models Dockerfile requirements.txt .dockerignore .env.example tests` → 신규 `A` 4개만). `git diff main --stat` 누적: 57 files changed, 5702 insertions(+) (`pipelines/prefect_flow.py` 512줄, `tests/test_prefect_flow.py` 494줄, `configs/prefect.yaml` 24줄, `pipelines/README.md` 72줄).

### 검증

| 검증 | 결과 |
|---|---|
| `pyflakes pipelines training scripts common tests/test_prefect_flow.py tests/test_pipeline_steps.py` | 통과 |
| `pytest -q` 전체 (Prefect 임시 서버) | **57 passed** (기존 32 + Phase 4 25), 32초 |
| `tests/test_prefect_flow.py` 서버 없는 모드 (`prefect_test_harness.__enter__`가 `PermissionError`를 내도록 바꿔 샌드박스 재현) | **25 passed**, 0.09초 — fixture가 태스크를 `.fn`으로 바꿔 Prefect 엔진 없이 같은 플로우 함수 실행 |
| 리뷰의 중복 실행 재현 (동일 데이터 `execute=True, wait=False` 연속 2회) | start 요청 **1회**, 두 번째는 `in_progress`(같은 ARN). `force=True`로 세 번째 호출도 `in_progress`. 이어서 `wait=True` 호출 → 이어받아 `Succeeded` 요약·기준점 저장(watermark = 그때 센 파일 최근 시각, `new_files 5`)·기록 삭제 → 신규 0 → `skipped` |
| CLI 종단 (대역 + Prefect 임시 서버): `check` → `run --execute --no-wait --param Epochs=2` → `run --execute --no-wait` → `run --execute` → `check` | start 요청에 `ClientRequestToken`(기록 `run_id`와 같음)·`PipelineExecutionDisplayName prefect-20261030-…`; 두 번째 run은 `in_progress` + `active` 기록 출력; 세 번째는 "이전 실행 이어받음 … 상태 Executing" 로그 → 요약 → 기준점 저장 → 해제 → `skipped`; S3 쓰기 순서 `active_run.json`(예약) → `active_run.json`(ARN) → `last_run.json`, 삭제 `active_run.json` 1회, 끝나면 `active` 없음 |
| CLI `serve` (호출 가로채기) | `flow.serve(name="weekly", schedule=Cron("0 3 * * 1", timezone="Asia/Seoul"), parameters={"execute": True}, paused=True, global_limit=1, …)`; Prefect 3.9.0 `Flow.serve`에 `global_limit` 인자 있음 확인 |
| SageMaker API 규격 (botocore 모델) | `StartPipelineExecution.ClientRequestToken` 32~128자 idempotencyToken, `PipelineExecutionDisplayName` ≤82자 `[a-zA-Z0-9](-*[a-zA-Z0-9])*`; `ListPipelineExecutions` 요약에 `PipelineExecutionDisplayName`·`PipelineExecutionArn` 포함; S3 `PutObject.IfNoneMatch` 지원 |

### 설계 메모

- 예약은 **파이프라인 단위 단일 실행**이다(데이터가 달라도 진행 중 실행이 있으면 시작 안 함) — 같은 기준 모델에서 두 학습이 동시에 돌면 Register 버전 번호 경합과 낭비가 생기기 때문. 새 데이터는 진행 중 실행이 끝난 뒤 같은 플로우 실행이 바로 다시 센다.
- `ClientRequestToken`은 데이터에서 유도하지 않고 예약마다 새로 만든다(uuid4). 데이터 기반 토큰이면 실패 후 재시도가 SageMaker 멱등성 때문에 **실패한 옛 실행을 돌려받을** 위험이 있다. 예약 기록이 원자적이므로 토큰의 영속성은 기록이 담당한다.
- 실제 AWS에서만 확인할 것(기존 3개에 추가): S3 버킷이 `If-None-Match` 조건부 쓰기를 지원하는지(2024-08 이후 모든 리전 일반 버킷 지원), `list_pipeline_executions` 요약의 `PipelineExecutionDisplayName`이 start 때 준 값으로 들어오는지.

## 질문

없음
