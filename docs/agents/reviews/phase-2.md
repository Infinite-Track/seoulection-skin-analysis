# Phase 2 검토

## 요약

수정 필요 — Major 2건. 인물 단위 고정 분할, 공용 크롭 재사용, 기본 dry-run 요구사항은 구현됐고 정상 데이터 경로 검증도 통과했다. 다만 불완전한 테스트셋의 업로드와 중단 후 크롭 누락을 막는 검사가 부족하다.

## 실행한 검증

- `git status --short`, `git diff main --stat`, `git diff main`, `git diff main --check`: 추적 변경은 이전 Phase부터의 `.gitignore` 3줄. 신규 스크립트 5개·설정·분할 파일·문서를 직접 읽었다. 금지 경로의 기존 파일 및 기존 scripts 파일 변경 없음.
- `docs/agents/mlops-plan.md` Phase 2, `docs/agents/reports/phase-2.md`, `training/config.py`·`dataset.py`의 형식·import 변경을 대조했다.
- `.venv/bin/python -B -m pytest -q -p no:cacheprovider`: **24 passed**, 8.31초.
- `.venv/bin/python -B -m pyflakes common training scripts`: 통과.
- 실제 원본에서 `prepare_aihub.build_images/build_labels` 실행: 이미지 **12,545장·965명**, 라벨 **18,335행**. 노트북 `labels_region.csv`를 긴 형식으로 바꿔 비교한 결과 키와 값 **18,335개 전부 일치**.
- `configs/splits/aihub_kr_test.txt`: 기존 `test_subjects_165.json`과 **165명 ID 완전 일치**. replay는 **100명**, 테스트와 겹침 **0**. 실제 데이터셋에 대한 `make_splits.check`: 전체 965 / 테스트 165 / 학습 가능 800, 통과.
- 임시 분할 경로로 `make_splits.main`을 두 번 실행(seed 42, fraction 0.17): 테스트 164명 및 replay 100명 생성, 테스트 ID 재현됨. `--force` 없는 기존 테스트 목록 덮어쓰기 거절 확인. 기존 aihub_kr 고정 목록은 수정하지 않았다.
- 실제 표준 데이터셋과 노트북 index는 모두 **34,740행**. 가져온 크롭을 6부위별 하나씩 원본 PNG와 비교: 이미지·마스크 완전 일치.
- 원본 사진 `0002_03_F`를 `build_crops._init_worker/process_image`로 실제 처리: **6부위 status·이미지·마스크·face_h·skin_px 전부 기존 크롭과 일치**. 최초 실행은 샌드박스 내 MediaPipe 그래픽 초기화에서 중단되어, 승인된 제한 밖 로컬 실행으로 재검증해 통과했다. 병렬 전체 처리까지 반복한 것은 아니다.
- `upload_datasets.main`의 기본 옵션을 실제 데이터셋으로 실행하되 스테이징 경로만 `/tmp/phase2-review-8vk2wpel/outputs`로 지정: testset **165명·4,566파일·약 1,899MB**, replay **100명·2,774파일·약 1,170MB**. **boto3 미로드**, AWS 호출 없음. 스테이징 CSV·매니페스트 생성 확인.
- 불완전 데이터에 대한 아래 두 재현은 임시 디렉터리에서 수행했다. 크롭 재개 분기 확인에만 Pool 대역을 사용했으며 실제 크롭 연산 검증과 구분한다.
- 기존 `outputs/eval_v2_on_testset.json`의 datasets 블록은 `models/skin_v2/metrics.json`과 일치했다. 이는 저장된 결과 대조이며 이번 검토에서 165명 전체 추론을 재실행한 것은 아니다. 학습·export는 이번 Phase에서 변경된 기능이 아니므로 반복하지 않았다.

## 발견 사항

| # | 심각도 | 파일:줄 | 문제 | 근거 | 수정 제안 | 반영 |
|---|---|---|---|---|---|---|
| 1 | Major | `scripts/upload_datasets.py:43` | 고정 목록의 인물이 빠진 불완전 데이터셋도 경고만 남기고 업로드 대상으로 반환한다. 이후 `--execute` 경로는 이를 그대로 올리므로 고정 테스트셋이 축소된 상태로 게시될 수 있다. 현재 검사는 기존 파일의 존재만 확인하고 인물별 사진·부위 행의 완전성도 확인하지 않는다. | 인물 목록 `{'a','b'}`에 대해 a의 크롭 행 1개와 라벨만 제공하면 `stage_subset`은 경고 후 **people=1, crop_rows=1, files=3**으로 정상 반환했다. `main:106`은 이 반환값으로 곧바로 upload를 호출한다. 현재 정상 aihub_kr 데이터가 누락됐다는 뜻은 아니며, 미완료 build 또는 잘못된 데이터셋을 지정했을 때의 실제 허용 동작이다. | 업로드 전 요청 인물과 산출물 인물의 차집합을 검사해 누락 시 실패한다. images.csv 및 기기 필터 등을 기준으로 대상 사진마다 6부위의 명시적 결과 행이 있는지 검사한다. no_face/low_res/empty 등 정상 실패 status와 **아직 처리하지 않은 행**은 구분한다. testset·replay 양쪽의 검증을 끝낸 뒤 업로드를 시작하고 불완전 데이터 거절 사례를 검증한다. | ✅ 반영 — `scripts/crop_index.check_dataset`(인물 차집합·images.csv 기준 사진별 6부위 행·status·ok PNG 존재·대상 밖 행·라벨)로 스테이징 전 검사, 불일치 시 SystemExit. 둘 다 검사 후 업로드. 불완전 3종 거절·upload 호출 0회 확인 (reports/phase-2.md 리뷰 반영 표) |
| 2 | Major | `scripts/build_crops.py:74`, `scripts/build_crops.py:103` | 재개 시 사진의 행이 하나라도 있으면 완료로 간주한다. CSV 체크포인트를 기존 파일에 직접 덮어쓰므로 저장 도중 중단돼 일부 행만 남으면, 재실행이 그 사진의 누락 부위를 복구하지 않는다. 기존 ok PNG가 없어져도 완료 여부 검사에서 확인하지 않는다. | `write_csv`는 `open(path, 'w')`로 인덱스를 직접 교체한다. `done = {r['image_id'] for r in done_rows}`에는 부위 개수·중복·파일 존재 검사가 없다. images.csv에 사진 1개, crops.csv에 그 사진의 forehead 1행만 둔 재현에서 **완료 1, todo 0**, 재개 후 CSV도 **1행**으로 남았다. Pool은 대역으로 바꿔 실제 재개 분기의 todo만 확인했다. 저장 중단 자체를 강제 발생시킨 것은 아니며, 중단으로 생길 수 있는 부분 CSV 상태를 입력한 것이다. | 같은 디렉터리의 임시 파일에 완전히 쓴 뒤 원자적 replace로 CSV를 교체한다. 재개는 사진마다 예상 6부위가 중복 없이 있고 ok 행의 파일이 존재하는 경우만 완료 처리한다. 불완전 사진은 기존 부분 행을 제거하고 다시 처리해 중복 없이 복구한다. 부분 CSV 및 누락 PNG의 재개 사례를 검증한다. | ✅ 반영 — `write_csv_atomic`(tmp + os.replace), 재개는 `complete_image_ids`로 완전한 사진만 건너뛰고 부분 행 제거 후 재처리. 부분 CSV·누락 PNG 재개 사례: 2장만 재처리, 중복 0, PNG 복구 |

## Claude에게 전달할 수정 요청 (우선순위 순)

1. 불완전한 고정 테스트셋·replay를 업로드 대상으로 승인하지 않도록 스테이징 전 완전성 검사를 추가한다. — ✅ 반영 (2026-10-29)
2. 크롭 CSV 저장을 원자적으로 바꾸고, 부위 행 및 PNG가 완전한 사진만 재개 시 건너뛰도록 수정한다. — ✅ 반영 (2026-10-29)
3. 실제 S3 업로드는 이번 검토에서 실행하지 않았다. 위 수정과 로컬 검증 후 사람이 실행하는 기존 절차를 유지한다. — ✅ 반영 (절차 유지, 실행하지 않음)

신규 데이터 라벨 형식·replay 혼합 비율·SageMaker 채널 연결은 보고서에 미결정 또는 후속 Phase 사항으로 남아 있다. 이번 리뷰가 그 결정을 대신 확정하지 않는다. 이번 검토에서는 리뷰 파일만 수정했다.
