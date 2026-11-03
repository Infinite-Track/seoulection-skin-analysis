VERDICT: PASS
# Phase 3 검토

## 요약

수정 필요 — Major 3건. 기본 Condition의 비교 범위가 요구사항보다 좁고, feedback 병합과 모델 압축 해제에 이름 충돌이 있다. 금지 경로의 기존 파일 변경 및 비밀값 노출은 발견하지 않았다. AWS 호출과 코드 수정은 하지 않았다.

## 실행한 검증

- `git diff main --stat`, `git diff main`, `git diff main --name-status` 및 Phase 3 계획·보고서·구현 확인. 누적 diff와 별도로 `git diff fefdede HEAD`로 Phase 3 변경 20파일을 구분했다. `common/` 변경은 신규 2파일이며 기존 파일 변경은 없다. 다른 금지 경로 변경도 없다.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider`: **24 passed, 7.66초**.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pyflakes pipelines training`: 통과.
- `git diff fefdede HEAD --check`: 통과. 누적 `git diff main --check`에는 이전 Phase의 `training/templates/check_onnx.py` CRLF 관련 trailing whitespace가 나온다.
- 인라인 Python으로 실제 `compare()`에 합성 지표를 전달: QWK 0.70→0.71, acc 0.80→0.10, within1 0.95→0.20, spearman 0.70→0.10에서 기본 `select`는 **1축 통과**, `all`은 **4축 중 3축 실패**. `load_yaml('configs/pipeline.yaml')['condition']`은 `{'axes': 'select', 'tolerance': 0.0}`.
- `merge_source()`를 서로 다른 `/feedback/2026-10/batch1`, `/feedback/2026-11/batch1`에 호출하고 파일 복사만 mock 처리: 두 소스 모두 `feedback_batch1:001`, `/out/feedback_batch1/crops/img.png`로 합쳐짐. 동일 인물·부위·항목의 값 1과 5가 같은 키로 남음. 실제 파일 덮어쓰기는 실행하지 않았다.
- `unpack_model()`에 `/candidate/model.tar.gz`, `/baseline/model.tar.gz`를 같은 workdir로 전달하고 파일 I/O를 mock 처리: 두 반환 경로가 모두 `/work/model`, 두 `extractall` 목적지도 모두 `/work/model`임을 확인했다. 실제 모델 덮어쓰기는 실행하지 않았다.
- 실제 `verify_model_dir('models/skin_v2')`: 필수 파일·SHA256SUMS 일치, reference 최대 절대 차이 **1.430511474609375e-06 < 1e-3**, 통과. `common.model_files.ensure_model_files('models/skin_v2')`도 로컬 파일만으로 통과.
- 기존 로컬 스모크 입력에 `check_testset('/tmp/smoke/testset', 'aihub_kr')` 실행: **165명, 5,940행, ok 4,564크롭**, 고정 목록·완전성 검사 통과. replay의 `completeness_problems()`는 빈 목록, 테스트 인물과 교집합 **0명**.
- 기존 Prepare 산출물 `/tmp/smoke/out/train`을 `load_rows(..., part='train')`로 읽어 **2,685크롭·102명** 확인. 처음 `/tmp/smoke/out`을 지정한 시도는 `labels.csv` 없음으로 실패했으며, 실제 하위 `train/` 경로로 정정해 통과했다. 구현 결함으로 집계하지 않는다.
- 정의 소스에서 CPU Processing → GPU Training → Evaluation → Condition → Register 연결, 파라미터·설정 전달, 현재 모델 채널, 실행별 checkpoint 경로, `PendingManualApproval`, S3 `IfNoneMatch="*"` 사용 확인.

## 발견 사항

| # | 심각도 | 파일:줄 | 문제 | 근거 | 수정 제안 | 반영 |
|---|---|---|---|---|---|---|
| 1 | Major | `configs/pipeline.yaml:40`, `pipelines/steps/evaluate.py:58` | 기본 등록 조건이 “모든 축에서 현재 모델 이상”을 보장하지 않는다. 선택 지표가 유지되면 정확도·±1등급·MAE 등 다른 평가 지표가 악화돼도 등록 단계로 진행할 수 있다. | 기본값이 `axes: select`이고 `compare()`는 이때 `tasks[task]['select']` 하나만 비교한다. 위 합성 입력에서 3개 지표가 크게 악화돼도 반환된 유일한 축은 `passed=True`. 파이프라인은 이 결과의 `failed_axes == 0`만 확인한다. | 등록에 사용하는 기본 조건을 `all`, tolerance 0으로 맞춘다. 선택 지표만 보는 모드는 분석용으로 구분하고 요구사항을 충족하는 등록 경로에 사용하지 않는다. QWK 상승/정확도 하락, 상관계수 상승/MAE 증가 사례가 각각 등록을 막는지 검증한다. | ✅ 반영 — `pipeline.yaml`에서 `condition.axes` 항목 제거(tolerance만 남김), 파이프라인 정의는 상수 `REGISTER_AXES = "all"`을 Evaluate에 고정 전달(정의 JSON `--axes all` 확인), `evaluate.py` 기본값 `all`·`select`는 분석용으로 문서화. 검증: QWK↑/acc↓ → acc·within1·spearman 3축 실패, spearman↑/MAE↑ → mae 1축 실패, 같은 모델 → 전부 통과 (`tests/test_pipeline_steps.py`) |
| 2 | Major | `pipelines/steps/prepare.py:186`, `pipelines/steps/prepare.py:123` | feedback 소스 식별자를 마지막 폴더 이름만으로 만들어, 지원하는 깊이 2의 서로 다른 데이터셋이 충돌한다. PNG가 덮어써지고 라벨이 다른 인물/배치에 적용될 수 있다. | `find_datasets()`는 `2026-10/batch1`, `2026-11/batch1`을 모두 찾지만 소스 이름은 둘 다 `feedback_batch1`. 재현 결과 person_id와 파일 목적지가 동일하다. `link_or_copy()`는 목적지 존재로 `os.link`가 실패하면 `shutil.copyfile`로 덮어쓴다. `training/dataset.py:64`는 같은 라벨 키를 마지막 값으로 덮어쓰며 `sources[source]` 집계도 사라진다. | feedback 루트 기준 상대 경로 전체 또는 안정적인 해시로 고유 소스 ID를 만든다. 병합 전에 소스 ID·출력 파일 경로 중복을 검사해 거절하고, 이름이 같은 두 하위 폴더의 이미지·라벨이 독립적으로 유지되는지 검증한다. | ✅ 반영 — 소스 ID = `feedback/<루트 기준 상대 경로>`(`feedback_source`), 복사 전 소스 ID 중복 검사, `link_or_copy`는 목적지가 있으면 덮어쓰지 않고 `SystemExit`, 합친 뒤 출력 파일·(image_id, region)·라벨 키 중복 검사(`merge_conflicts`). 검증: `2026-10/batch1`·`2026-11/batch1` 동일 인물 001 값 1·5 → person_id `feedback/2026-10/batch1:001`·`feedback/2026-11/batch1:001`, PNG 12개·라벨 2행 독립 보존; 옛 방식 ID 재사용 시 복사 거절·기존 파일 불변; 실데이터 스모크(replay 100명 + 동명 배치 2개) 끝까지 통과, `load_rows` 2,685크롭 |
| 3 | Major | `pipelines/steps/util.py:19`, `pipelines/steps/evaluate.py:85` | 후보와 기준 모델이 모두 같은 이름의 tar이면 같은 디렉터리에 풀려, 후보가 기준 모델로 덮어써진 뒤 두 번 평가된다. 이 입력에서는 실제 개선 여부와 무관하게 같은 모델 비교가 된다. | `unpack_model()`의 목적지는 입력 부모 경로를 무시한 `workdir / tar.stem.replace('.tar', '')`. Evaluate는 같은 tmp에서 양쪽을 먼저 풀고 나중에 평가한다. mock I/O 재현에서 두 `model.tar.gz` 모두 `/work/model` 반환. 기본 S3 baseline이 풀린 폴더인 경로에는 해당하지 않지만, 함수와 Evaluate가 수용하는 양쪽 tar 입력에서 발생한다. | candidate/baseline별 독립 하위 workdir 또는 호출마다 고유 추출 디렉터리를 사용한다. 서로 다른 가중치를 담은 동명 tar 두 개를 받아 후보·기준 경로와 출력이 서로 유지되는지 검증한다. | ✅ 반영 — `unpack_model`은 호출마다 `tempfile.mkdtemp(dir=workdir)` 새 폴더에 풀고, Evaluate는 `tmp/candidate`·`tmp/baseline` 하위 workdir로 분리 + 두 경로가 같으면 중단. 검증: 서로 다른 내용의 동명 tar 두 개 → 다른 폴더·내용 유지(테스트), v2를 tar 두 개로 묶어 실제 evaluate 4명 실행 → 경로 분리·11축 통과 |

## Claude에게 전달할 수정 요청 (우선순위 순)

1. 등록 Condition이 전체 평가 지표의 비악화를 검사하도록 기본 설정과 문서를 맞추고, 일부 지표만 좋아지는 경우를 거절하는 검증을 추가한다. — ✅ 반영 (2026-10-29)
2. feedback 소스 ID를 전체 상대 경로 기준으로 고유하게 만들고, 병합 시 파일·라벨 충돌을 막는다. — ✅ 반영 (2026-10-29)
3. 후보·기준 tar 추출 경로를 분리하고 동명 tar 비교를 검증한다. — ✅ 반영 (2026-10-29)

## 검증 범위와 남은 한계

- 이번에는 읽기 위주의 검증과 I/O mock을 사용했다. 새 학습·export·파이프라인 정의 번들 생성은 실행하지 않았다. v2 reference 실측은 기존 ONNX 산출물 검증이며, 새 학습 결과의 PyTorch↔ONNX 동등성 검증을 대체하지 않는다.
- 실제 v2 `deploy_model.pt` 호환성·재export는 Phase 1부터 보류된 항목이다. 파일명 fallback 추가만으로 state_dict 호환성이 실증됐다고 보지 않는다.
- Spot 재개 경로는 구현돼 있다. 다중 persistent worker의 재개 재현성 잔여 Minor는 기존 `phase-1.md` 재검토에 기록돼 있으며 Phase 3에서 수정되지 않았다. 실제 CUDA/AMP·Spot 중단/재개, 컨테이너 설치, AWS API 수용 여부는 검증하지 않았다.
- Prefect와 `docs/mlops.md`·신규 테스트 전체는 각각 Phase 4·5 범위로 판단했다. Phase 3 누락으로 집계하지 않았다.
- 리뷰 파일 외 소스·설정·기존 리뷰·진행 현황은 수정하지 않았다.


## 재검토 (1회차)

### 요약

통과 — 기존 Major 3건 해결을 코드와 회귀 테스트로 확인했다. 이번 재검토에서 추가 Critical·Major는 발견하지 않았다. 아래 검증은 2026-10-29, HEAD `6e9eabb` 기준이다. 위 최초 검토 내용은 이력이며, 최신 판정은 첫 줄과 이 섹션을 따른다.

### 실행한 검증

- `git diff main --stat`, `git diff main`, `git diff main --name-status`로 누적 변경 확인. `git diff fefdede HEAD --stat`로 Phase 3 범위(23파일)를 구분했다. 금지 경로의 기존 파일 변경 없음. `common/` 2파일과 `tests/test_pipeline_steps.py`는 신규 파일이다.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider`: **32 passed, 8.83초**. 기존 24개와 신규 회귀 테스트 8개 통과.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pyflakes pipelines training tests/test_pipeline_steps.py`: 통과.
- `git diff fefdede HEAD --check`: 통과.
- 인라인 Python에서 `botocore.client.BaseClient._make_api_call`을 호출 즉시 예외를 던지도록 차단하고 더미 자격증명·메타데이터 비활성화를 설정한 뒤 `main(['definition', '--dry-run'])` 실행: 정상 종료. 파라미터 8개, Prepare → Train → Evaluate → Condition 정의 생성 확인. Evaluate 인자는 `--axes all --tolerance 0.0`, Condition은 `EvaluationReport.failed_axes == 0`이다. AWS API는 호출하지 않았다.
- 실제 `verify_model_dir('models/skin_v2')`: 필수 파일 및 SHA256SUMS 검증 통과, reference 최대 절대 차이 **1.430511474609375e-06 < 1e-3**. `ensure_model_files('models/skin_v2')`도 기존 로컬 파일로 통과.
- 기존 `/tmp/smoke/testset`에 `check_testset(..., 'aihub_kr')`: 고정 목록과 일치하는 **165명, 5,940행, ok 4,564크롭**. 기존 replay 완전성 문제 0건, 테스트 인물 교집합 **0명**.
- 기존 `/tmp/smoke/out/train`을 실제 `load_rows(..., part='train')`로 로드: **2,685크롭, 102명**. 이번에는 새 스모크 학습·export를 실행하지 않았다.

### 발견 사항 및 기존 요청 해결 확인

| # | 심각도 | 파일:줄 | 문제 / 확인 결과 | 근거 | 수정 제안 |
|---|---|---|---|---|---|
| 기존 1 | 해결 | `pipelines/sagemaker_pipeline.py:207`, `pipelines/steps/evaluate.py:60` | 등록 경로가 모든 지표를 비교한다. | 생성된 정의의 `--axes all`, 기본 tolerance 0. 선택 지표 상승/다른 지표 하락 및 MAE 증가를 거절하는 테스트 통과. | 추가 요청 없음. |
| 기존 2 | 해결 | `pipelines/steps/prepare.py:114`, `pipelines/steps/prepare.py:220` | 동명 feedback 하위 폴더가 분리되고 중복을 거절한다. | 상대 경로 전체를 소스 ID로 사용. 회귀 테스트에서 두 batch1의 PNG 12개·라벨 2행 보존, 중복 소스·덮어쓰기 거절 확인. | 추가 요청 없음. |
| 기존 3 | 해결 | `pipelines/steps/util.py:25`, `pipelines/steps/evaluate.py:99` | 동명 모델 tar를 별도 디렉터리에 추출한다. | 호출별 `mkdtemp` 및 candidate/baseline 작업 폴더 분리. 실제 tar 두 개의 경로·내용 보존 테스트 통과. | 추가 요청 없음. |
| R1 | Minor (기존 잔여) | `training/dataset.py:166`, `training/train.py:165` | Phase 1에 기록한 CPU 다중 persistent worker 재개 시 dropout 난수 재현성 한계가 남아 있다. | DataLoader에 전용 generator가 없고 재개 직후 전역 torch RNG를 복원한다. `phase-1.md` 재검토에서 실측한 가중치 차이 4.53e-04의 관련 구현은 그대로다. 이번에 해당 학습을 재실행하지는 않았다. 체크포인트 재개 자체는 구현돼 있다. | Phase 1 잔여 요청대로 모델 RNG와 DataLoader generator를 분리하고 workers=2→2 연속/재개를 비교한다. |

### Claude에게 전달할 수정 요청 (우선순위 순)

1. Phase 3의 기존 Major 3건에 대한 추가 수정 요청 없음.
2. 기존 Minor R1은 Phase 1 리뷰의 수정 제안을 참고한다.

### 검증 범위와 남은 한계

- 현재 모델 채널을 통한 파인튜닝, replay 병합, 실행별 Spot 체크포인트 경로, S3 기존 버전 탐색 및 `IfNoneMatch="*"`, Model Registry의 `PendingManualApproval`을 코드에서 확인했다. 자동 배포 코드는 없다.
- 실제 AWS 실행, 컨테이너 의존성 설치, GPU/AMP 및 Spot 중단·재개는 검증하지 않았다. v2 `deploy_model.pt` 로드·재export 호환성은 기존 보류 사항이다. 위 reference 실측은 기존 v2 ONNX 검증이며 새 학습 산출물의 동등성 실측은 아니다.
- Prefect와 `docs/mlops.md`는 각각 Phase 4·5 범위로 보아 이번 Phase 3 누락으로 집계하지 않았다.
- 소스·설정은 수정하지 않았으며 이 리뷰 파일만 갱신했다.
