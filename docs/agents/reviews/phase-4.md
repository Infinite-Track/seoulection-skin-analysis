VERDICT: FAIL
# Phase 4 검토

## 요약
수정 필요 — Critical 0건, Major 1건. 주간 스케줄·신규 데이터 인터페이스·완료 대기·결과 로그는 구현됐으나, 같은 데이터에 대한 중복 실행 방지가 없다.

## 실행한 검증
- `git diff main --stat`, `git diff main`, `git diff main --numstat`으로 전체 변경 범위를 확인했다. 전체 diff 출력은 도구 한도로 잘려 Phase 4 파일은 별도로 읽었다.
- `git diff 0922b05 --stat`: 직전 Phase 3 리뷰 이후 Phase 4 변경은 9파일, +869/-1. `pipelines/prefect_flow.py`, `pipelines/new_data.py`, 설정·의존성·README·테스트·보고서를 검토했다.
- `git diff main --name-status -- serving common models Dockerfile requirements.txt .dockerignore .env.example tests`: 신규 common 파일 2개와 신규 테스트 2개만 A이며, 금지된 기존 파일 수정·삭제는 없다.
- `PYTHONDONTWRITEBYTECODE=1 PREFECT_HOME=/tmp/phase4-review-prefect PREFECT_SERVER_ANALYTICS_ENABLED=false AWS_EC2_METADATA_DISABLED=true .venv/bin/python -m pytest -q -p no:cacheprovider`: **39 passed, 5 errors (14.09초)**. 오류 5개는 모두 `prefect_test_harness`의 `sock.bind(("127.0.0.1", 0))`에서 `PermissionError: [Errno 1] Operation not permitted`로 발생했다. 샌드박스 제한이며 제품 코드 결함으로 집계하지 않는다. 기존 테스트에서 실패는 관찰되지 않았다.
- 위 5개 플로우 테스트 함수를 별도로 호출하고, flow/task는 `.fn`, 로거·AWS 클라이언트는 대역으로 교체했다. 기준 미달 skip, dry-run, marker/since/force/no-wait, 성공 요약·marker 저장·다음 실행 skip, 실패 시 marker 미갱신의 **5개 모두 통과**. 이는 Prefect 서버·태스크 엔진 통합 검증을 대체하지 않는다.
- 중복 실행 재현: 기존 테스트의 `FakeS3`, `FakeSageMaker`, `feedback_objects(5, T0)`, `SETTINGS`를 사용하고 설정·로거·read/check/start 태스크를 대역으로 교체한 뒤 `weekly_retrain.fn(execute=True, wait=False)`를 연속 2회 실행했다. 결과: `{'statuses': ['started', 'started'], 'start_requests': 2, 'marker_writes': 0, 'explicit_tokens': [False, False]}`. 실제 AWS 호출은 없었다.
- 설정에서 `cron: "0 3 * * 1"`, `timezone: Asia/Seoul`과 `MIN_NEW_FEEDBACK_FILES` 우선 적용을 확인했다. 최소 건수 미달 시 start 태스크 전 반환, 실패 시 예외 및 기준점 미갱신, Condition·버전·평가 점수 로그, 추천 서버 연동 TODO, 로컬 수동 CLI를 확인했다.
- Phase 4는 학습·export 코드를 바꾸지 않아 스모크 학습·reference 출력 비교는 재실행하지 않았다. `docs/mlops.md`는 아직 없으며 계획상 Phase 5 산출물이다. Phase 4 사용법은 `pipelines/README.md`에 있다.

## 발견 사항
| # | 심각도 | 파일:줄 | 문제 | 근거 | 수정 제안 | 반영 |
|---|---|---|---|---|---|---|
| 1 | Major | `pipelines/prefect_flow.py:266` (`98–105`, `249–278`, `304–307` 관련) | 실행 중 같은 데이터로 다시 호출하면 별도 SageMaker 실행을 시작한다. 같은 주 중복 실행 안전성(G)을 충족하지 않는다. | marker는 성공 후 278행에서만 저장된다. 시작 전 실행 중 ARN 확인·원자적 실행 예약이 없고, start 요청에 안정적인 `ClientRequestToken`도 없다. `serve`에도 동시 실행 제한이 없다. 대역 재현에서 동일 데이터로 `wait=False` 2회 호출 시 start 요청 2회, marker 쓰기 0회를 확인했다. 기본 wait 경로 역시 266행에서 시작한 뒤 기다리므로 완료 전 겹친 호출을 막지 못한다. 타임아웃 시 원격 실행을 유지하는 112–125행 경로도 재호출을 차단하지 않는다. | 데이터 기준점·실행 파라미터에 대응하는 영속 실행 ID/토큰과 원자적 예약을 도입하고, 기존 실행이 진행 중이면 ARN을 재사용하거나 종료한다. 프로세스 재시작·`--no-wait`·타임아웃 이후에도 진행 중 실행을 복구하도록 한다. 성공 marker는 동시 실행으로 과거 값으로 돌아가지 않게 보호한다. Prefect 동시 실행 제한만으로 수동 CLI·재시작까지 해결된다고 간주하지 않는다. | ✅ 반영 (2026-10-30) — S3 `prefect/active_run.json` 예약 기록을 start **전에** `If-None-Match: *`로 원자적 생성(이미 있으면 시작 안 함, `in_progress` 반환). 기록 = `run_id`(uuid4 hex 32자 = SageMaker `ClientRequestToken` 멱등 키)·표시 이름·ARN·기준점·신규 건수·파라미터. 플로우 시작 시 기록이 있으면 ARN을 `describe`해 진행 중이면 이어받아 대기(`--no-wait`면 `in_progress`), 끝났으면 요약 로그·기준점 저장·기록 삭제 후 신규 데이터를 다시 센다. ARN 기록 전에 죽은 경우 표시 이름으로 `list_pipeline_executions`에서 복구, 없으면 `reserve_stale_seconds`(10분) 지난 예약만 삭제. 기준점은 `merge_marker`로 기존 watermark가 더 최근이면 유지. `--force`는 최소 건수만 무시(진행 중 실행 있으면 새로 시작 안 함), 실패 종료 시 기록 삭제·기준점 유지 → 다음 실행이 같은 데이터로 1회 재시도. `serve`에 `global_limit=1` 보조 추가. 대역 재현 재실행: 동일 데이터 `wait=False` 2회 → start 1회, 두 번째는 `in_progress` |

## Claude에게 전달할 수정 요청 (우선순위 순)
1. 진행 중 실행을 영속적으로 식별하고 같은 데이터·파라미터에 대해 중복 start가 발생하지 않도록 구현한다. 실패 후 의도적 재시도 정책과 `--force` 동작도 명시한다. — ✅ 반영 (2026-10-30). 정책은 `pipelines/prefect_flow.py` 모듈 docstring·`pipelines/README.md` "중복 실행 방지"·"실패 후 재시도 정책" 절에 명시
2. 신규 테스트 파일에 완료 전 중복 호출, `--no-wait` 후 재호출, 대기 타임아웃 후 재호출, 프로세스 재시작 상황을 추가한다. 같은 실행을 재사용/skip하고 start는 1회만 발생하는지, 성공 기준점이 후퇴하지 않는지를 검증한다. Prefect 서버가 가능한 환경에서 전체 pytest를 다시 실행한다. — ✅ 반영 (2026-10-30). `tests/test_prefect_flow.py` "중복 실행 방지" 절 8개 + 순수 함수 4개 추가(총 25개). 완료 전 중복 호출·force·`--no-wait` 후 재호출, 대기 시간 초과 후 재호출, 재시작(ARN 있음 / ARN 없음→표시 이름 복구 / 예약만 있음·실행 삭제됨), 이전 실패 실행 정리 후 1회 재시도, 읽기–예약 사이 선점(412), 기준점 후퇴 방지. Prefect 임시 서버로 전체 pytest **57 passed**; 서버를 띄울 수 없는 샌드박스에서는 fixture가 자동으로 태스크를 `.fn`으로 바꿔 같은 플로우 함수를 돌린다(같은 25개 통과 확인)


## 재검토 (1회차)

### 요약
수정 필요 — Critical 0건, Major 2건. 예약 생성과 순차 재호출·재시작 복구는 개선됐지만, 동시 마무리의 예약 소유권 보호가 없고 dry-run도 운영 상태를 변경한다. 기존 #1은 부분 해결이며 아래 #R1이 남아 있다.

### 실행한 검증
- `git diff main --stat`, `git diff main`, `git diff main --name-status` 실행. 전체 diff는 출력 한도로 잘려 Phase 4 구현·설정·테스트·README는 별도로 읽었다. Phase 4 요구사항은 `docs/agents/mlops-plan.md`와 대조했다.
- `git diff main --name-status -- serving common models Dockerfile requirements.txt .dockerignore .env.example tests`: 신규 common 파일 2개·신규 테스트 파일 2개만 A. 금지된 기존 파일 변경 없음.
- `PYTHONDONTWRITEBYTECODE=1 PREFECT_HOME=/tmp/phase4-review2-prefect PREFECT_SERVER_ANALYTICS_ENABLED=false AWS_EC2_METADATA_DISABLED=true .venv/bin/python -m pytest -q -p no:cacheprovider`: **57 passed in 11.88s**. 테스트 fixture는 서버 소켓을 열 수 없으면 태스크 `.fn`으로 대체하므로 이 결과만으로 Prefect 엔진 통합 검증까지 통과했다고 판단하지 않는다.
- 추가 재현은 `runpy.run_path('tests/test_prefect_flow.py')`로 기존 FakeS3/FakeSageMaker/SETTINGS/feedback_objects/artifacts를 재사용했다. `make_clients`, 설정, 로거와 모든 태스크를 대역/`.fn`으로 교체하고 `weekly_retrain.fn`·`_finish`를 직접 호출했다. 실제 AWS 호출 없음. 코드·테스트 파일 수정 없음.
- 동시 마무리의 호출 순서를 직렬로 재현: A·B가 같은 완료 실행을 읽었다고 두고 B의 `_finish(..., Succeeded, T0, 1)` → B의 `weekly_retrain.fn(execute=True, wait=False)` → 지연된 A의 같은 `_finish` → C의 `weekly_retrain.fn(execute=True, wait=False)`. 남은 신규 파일은 4개이며 최소값 3개다. 결과: `{'B': 'started', 'active_before_old_finisher': '.../execution/exec123', 'active_after_old_finisher': None, 'C': 'started', 'start_requests': 2}`. B 실행 상태를 Executing으로 유지했는데 C도 시작됐다. 대역의 첫 ARN은 기존 완료 실행의 ARN과 같은 상수지만, 결함은 ARN 값과 무관하게 active 객체를 무조건 삭제하는 데 있다.
- dry-run 재현: 완료된 active 기록을 넣고 `weekly_retrain.fn(execute=False, wait=False)` 호출. 결과 `status='skipped'`, S3 쓰기 `[.../prefect/last_run.json]`, 삭제 `[.../prefect/active_run.json]`. 실행 플래그 없이 기준점을 쓰고 예약을 삭제했다.
- 월 03:00 KST 설정·cron 변경·환경변수 최소 건수·신규 데이터 인터페이스·추천 서버 TODO·완료 대기·평가/버전 로그·로컬 CLI·자동 배포 없음은 확인했다. `docs/mlops.md`는 Phase 5 대상이다. 이번 변경은 학습·export 수정이 아니므로 스모크 학습·reference 비교는 반복하지 않았다.

### 발견 사항
| # | 심각도 | 파일:줄 | 문제 | 근거 | 수정 제안 | 반영 |
|---|---|---|---|---|---|---|
| R1 | Major | `pipelines/prefect_flow.py:372` (312–315, 389–407, 354–359 관련) | 같은 실행을 이어받은 여러 호출이 마무리하면, 늦은 호출이 다음 실행의 예약을 삭제한다. 이후 같은 데이터로 중복 학습이 시작될 수 있다. | 진행 중 실행을 여러 호출이 대기할 수 있다(405행). `_finish`는 받은 실행의 소유권을 확인하지 않고 `release_active_task(settings)`를 호출하며, 삭제는 조건 없는 `delete_object`다. 위 재현에서 B가 만든 새 예약을 A가 삭제하고 C도 start했다. `global_limit=1`은 수동 CLI/다른 배포를 보호하지 않는다. marker의 read→merge→write 역시 원자적이지 않아 동시 쓰기에서 후퇴 방지가 보장되지 않는다. | 마무리·예약 갱신·해제 전 과정에서 읽은 run_id와 객체 버전을 보호하는 원자적 조건부 연산 또는 영속 트랜잭션/소유권 프로토콜을 사용한다. 단순 read 후 소유권 비교만으로는 경쟁을 막지 못한다. marker도 원자적으로 단조 증가시킨다. 두 호출의 완료 처리 사이에 다음 실행이 예약·시작되는 교차 실행 테스트를 추가한다. | ✅ 반영 (2026-10-30) — `update_json_s3`: 읽기(ETag) → 결정 → `If-Match: <ETag>` 조건부 쓰기, 412/409면 다시 읽어 반복(최대 8회). 해제·ARN 기록은 "현재 기록이 살아 있고 `run_id`가 내 것"일 때만 쓰고, 아니면 손대지 않는다(`owns`). 해제는 삭제 대신 `state: released` 묘비로 조건부 덮어쓰기(일반 버킷의 조건부 삭제 지원이 불확실해 PutObject `If-Match` 사용). 예약은 기록이 없으면 `If-None-Match:*`, 묘비면 그 ETag로 `If-Match` → 둘이 같은 묘비를 읽어도 하나만 성공. 기준점도 같은 CAS, `merge_marker`가 기존 watermark가 더 최근이면 None(쓰지 않음). 테스트: 리뷰 재현과 같은 교차 실행(A·B가 끝난 X를 이어받음, B 마무리·해제·Y 예약·시작 → A 늦은 마무리 → C) = Y 예약 유지·start 1회·C `in_progress`; 해제·기록은 남의 run_id/묘비/지워진 객체에 쓰지 않음; 묘비 위 예약 경합 1승; 기준점 CAS 충돌 재시도 후 더 최근 값 유지 / 더 옛 값은 덮어씀 |
| R2 | Major | `pipelines/prefect_flow.py:389` (305, 398, 405–407, 421 관련) | 기본 dry-run이 신규 데이터 확인에 그치지 않고 실행을 기다리거나 S3 상태를 변경한다. 읽기 권한으로 점검하는 명령이 실패하거나 운영 예약을 삭제할 수 있다. | `execute` 검사는 421행인데 active 복구/정리는 그 전에 실행된다. 완료된 active가 있으면 `execute=False, wait=False`여도 `_finish`가 marker를 쓰고 active를 삭제하는 것을 실측했다. 진행 중이면 기본 wait=True에 따라 최대 설정 대기 시간 동안 기다린다. README의 “기본 dry-run: 확인까지만” 및 379행 설명과 다르다. | execute=False에서는 active·marker·신규 데이터 조회와 계획 로그만 수행하고, 복구 쓰기·완료 대기·기준점 갱신·예약 삭제를 수행하지 않는다. 진행 중/완료/오래된 ARN 없는 예약 각각에서 쓰기·삭제·대기가 없다는 테스트를 추가한다. | ✅ 반영 (2026-10-30) — 기록 처리를 `locate_active_task`(읽기만: ARN·상태·표시 이름 검색)와 execute 전용 쓰기(`record_active_task`·`release_active_task`·대기·`_finish`)로 분리. `execute=False`면 기록·실행 상태·기준점·신규 데이터를 읽고 `plan`(execute였다면 할 일)을 로그·결과에 남기며 상태는 항상 `dry_run`(기준 미달도 `dry_run` + "시작하지 않음" 계획). 테스트 `test_dry_run_is_read_only_for_every_active_state`: 진행 중(기본 wait=True, `wait_pipeline` 호출 시 fail)·끝남·ARN 없는 오래된 예약·ARN 없지만 표시 이름으로 찾은 경우·묘비+기준 미달 각각 `puts == [] and deletes == []`, 시작 0, 기록 원본 그대로. CLI 스모크(대역)에서도 dry-run 3회 모두 S3 쓰기 0 |

### Claude에게 전달할 수정 요청 (우선순위 순)
1. R1: 완료 처리와 예약 해제를 실행 소유권에 결합해 이전 호출이 새 예약을 삭제하지 못하도록 한다. marker 갱신도 동시성 아래 단조 증가를 보장하고 교차 실행으로 검증한다. — ✅ 반영 (2026-10-30). 위 R1 반영 칸 참고. 전체 pytest(Prefect 임시 서버) **62 passed**, 서버 없는 모드 `tests/test_prefect_flow.py` **30 passed**
2. R2: dry-run의 읽기 전용 경로를 active 복구·정리보다 앞에서 분리하고, active 상태별 부작용 없음 테스트를 추가한다. — ✅ 반영 (2026-10-30). 위 R2 반영 칸 참고. `pipelines/README.md` "dry-run은 읽기만" 절·권한 문구(삭제 권한 불필요, dry-run·check는 읽기 권한만) 갱신
