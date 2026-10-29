# Phase 0 검토

## 요약
수정 필요 — Phase 0의 여섯 보고 항목은 모두 포함되어 있고 주요 설명은 소스와 일치한다. 재현에 필요한 설명 두 곳을 보완해야 한다(Minor 2건). Phase 0에서 추가한 운영 코드 변경이나 Critical/Major 결함은 확인하지 못했다.

## 실행한 검증
- `git diff main --stat`, `git diff main`: 30파일, 1,092줄 추가·333줄 삭제. 금지 경로 변경이 보이지만 전부 기존 운영 브랜치에서 상속한 변경이다.
- `git diff feat/private-ec2 --stat`: 출력 없음. 현재 HEAD는 운영 브랜치와 같은 `ec21418`. `git status --short`의 미추적 항목은 `AGENTS.md`, `CLAUDE.md`, `docs/`였다(리뷰 작성 전). 따라서 위 30파일을 Phase 0의 금지 경로 침범으로 판정하지 않았다. 향후 PR의 main 대비 범위 문제는 보고서 Q2대로 별도 결정이 필요하다.
- `.venv/bin/python -B -m pytest -q -p no:cacheprovider`: **24 passed**, 9.73초.
- `.venv/bin/python -B models/skin_v2/check_onnx.py`: 해시 **5/5 OK**, onnxruntime 1.30.0 로드 성공. forehead 9.54e-07 / glabella 9.54e-07 / l_eye 1.19e-06 / r_eye 9.54e-07 / l_cheek 1.43e-06 / r_cheek 2.38e-07. 최대 **1.43e-06 < 1e-3**, 통과. 마지막 판정은 실제 출력값도 확인했다.
- `diff -u ../face_analysis_AI/data_preprocessing/face_regions.py common/face_regions.py`: 다각형·마스크·크롭·캔버스 정규화의 기존 로직은 같고, 경로 인자화와 입출력 함수 구성 차이를 확인했다.
- 외부 원본 `modeling/v2/{deploy,train}.ipynb`의 코드 셀, `model_v2.py`, `data_v2.py`를 직접 읽어 보고서의 CFG·DEPLOY, 학습·평가 순서와 비교했다. 레포 `common/{transforms,tasks,predictor,model_files}.py`, `models/skin_v2/meta.json` 및 검증 스크립트와도 대조했다.
- Phase 0은 탐색·보고 단계이므로 학습 스모크 및 새 export는 실행 대상이 아니다. 기존 v2 체크포인트에서 재export하는 검증은 수행하지 않았다. AWS 호출은 하지 않았다.
- 체크리스트 C~I의 아직 구현되지 않은 학습·파이프라인·Prefect 기능은 후속 Phase 검토 대상으로 두었다. 보고서의 과거 평가 재현·눈막대 실험 수치는 이번에 재실행하지 않았다.

## 발견 사항
| # | 심각도 | 파일:줄 | 문제 | 근거 | 수정 제안 | 반영 |
|---|---|---|---|---|---|---|
| 1 | Minor | `docs/agents/reports/phase-0.md:83` | 회귀 타깃의 mean/std가 고정 숫자로만 제시되어 있고, 그 값이 만들어지는 데이터 범위·가중 방식과 std=0 처리 규칙이 빠져 있다. 재학습 구현에서 이 숫자를 상수로 쓰거나 인물별 통계를 계산하면 원본 노트북과 다른 학습이 된다. | `deploy.ipynb`의 `target_stats(rows)`는 학습 크롭 행마다 유효 타깃을 수집하고 log 변환 후 `np.mean(v)`, `np.std(v)`(ddof=0)를 계산하며 std가 0이면 1.0을 쓴다. `train_full`은 `stats = target_stats(train_rows)`를 호출한다. 즉 인물·부위별 중복 제거 통계가 아니며 테스트 행도 포함하지 않는다. 체크포인트에는 이 stats가 저장된다. | 표의 숫자는 기존 v2 산출값이라고 표시하고, 학습 행만으로 크롭 빈도를 반영해 통계를 계산·저장·export하는 흐름을 추가한다. 기존 모델에서 이어 학습할 때 stats를 유지할지 재산출할지는 후속 설계 질문으로 분리한다. | ✅ 반영 — §2에 `target_stats` 행 추가(크롭 행 단위, 학습 rows만, log 후 mean/std ddof=0, std 0→1.0, 체크포인트·meta.json 저장), 세 숫자에 "v2 산출값" 표시, Q12(이어받기 vs 재산출) 추가 |
| 2 | Minor | `docs/agents/reports/phase-0.md:79` | `deploy.ipynb DEPLOY` 기준 표에서 patience를 6으로 적어 실제 설정값과 실험별 override를 구분하지 않았다. `train.ipynb`에도 기본 CFG는 4이므로 모든 실험을 6으로 설명하면 부정확하다. | 두 노트북 모두 초기 CFG는 `folds=5, epochs=15, patience=4`. `DEPLOY`는 views·epochs·schedule_epochs만 덮어쓴다. `train.ipynb`의 별도 셀 `ROUND2 = dict(patience=6)`에서 2차 실험에만 6을 적용한다. 배포 학습은 조기 종료를 사용하지 않는다. | 기본 CFG/DEPLOY에 남는 값은 4(배포에서는 미사용), 2차 튜닝 override는 6으로 나누어 적고 §1의 실험 흐름에도 적용 범위를 표시한다. | ✅ 반영 — §2 표를 `5, 4`(DEPLOY 상속·미사용)로 고치고 ROUND2·FINAL(최종 5-fold)만 6임을 명시, §1 흐름도 "1차 4 → 2차·최종 6"으로 수정 |

## Claude에게 전달할 수정 요청 (우선순위 순)
1. 회귀 타깃 통계 산출 범위와 방법을 추가하고 표의 숫자가 v2의 산출값임을 명시한다. — ✅ 반영 (2026-10-29)
2. patience 기본값 4와 ROUND2 override 6을 구분한다. — ✅ 반영 (2026-10-29)
3. 보고서에 이미 남긴 체크포인트 확보, 기준 브랜치, 고정 테스트셋 보존, 신규 데이터 라벨 출처 등은 미결정 사항으로 유지한다. 이번 리뷰가 해당 선택을 승인한 것은 아니다. — ✅ 반영 (질문은 §8에 미결로 유지, 결정 사항으로 바꾸지 않음)
