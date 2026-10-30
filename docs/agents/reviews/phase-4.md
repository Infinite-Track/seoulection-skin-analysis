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
