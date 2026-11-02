VERDICT: FAIL
# Phase 1 검토

## 요약
수정 필요 — Major 1건, Minor 2건. 기본 학습·export·서빙 로드·평가 경로는 통과했지만, 순서형 클래스 순서 변경을 호환성 검사가 놓친다.

Phase 1 요구 파일은 모두 존재한다. 금지 경로의 기존 파일 변경은 없고, 전처리는 `common/`을 재사용한다. 기존 v2 체크포인트 재export 검증은 체크포인트 미확보로 남아 있다.

## 실행한 검증
- `git diff main --stat`, `git diff main`, `git diff feat/private-ec2 --stat`: 추적 파일 변경은 `.gitignore` 3줄뿐. `git status --short`로 미추적 `training/`, `configs/`, 신규 `common/` 2파일, `requirements-train.txt`, 문서까지 직접 읽었다. 기존 `serving/`, `common/`, `models/`, 테스트 및 금지 설정 파일 변경 없음. 시작 시 존재한 `:memory:.ses`는 이번 구현의 코드로 취급하지 않았다.
- `docs/agents/mlops-plan.md` Phase 1, Phase 0 리뷰와 반영된 보고서, 원본 노트북 모듈·설정 및 Phase 1 구현을 대조했다.
- `.venv/bin/python -B -m pytest -q -p no:cacheprovider`: **24 passed**, 7.99초.
- `.venv/bin/python -B -m pyflakes common training`: 출력 없이 종료 코드 0.
- `SkinModel(pretrained=False)`: 전체 **50,637,427**, 학습 가능 **49,402,387** 파라미터로 v2와 일치. 원본 모듈의 백본·헤드·경계값 구조 및 키 구성과 대조했다.
- 설정 CLI `--lr-head 0.001 --no-amp --views F`: 각각 `0.001`, `False`, `['F']`로 반영. 아래 스모크에서도 YAML의 데이터셋·모델 설정 위에 CLI 옵션이 적용됐다.
- 6부위 합성 RGBA PNG를 `common.crop_files`로 저장·로드하고 `RegionDataset(train=False)`와 서빙 `to_model_input`을 비교: **최대 차이 0**(오른쪽 부위 반전 포함). 타깃 변환은 log1p/표준화와 서빙 expm1/역표준화의 짝 및 출력 열·등급 매핑을 소스로 확인했다.
- 임시 디렉터리 `/tmp/phase1-review-ct3j6pw_`에 표준 CSV·합성 크롭·랜덤 초기 체크포인트를 만들었다. 외부 가중치 다운로드 없이 `convnext_tiny`, head_hidden=8, batch_size=1, num_workers=0, `--init-from`, `--epochs 1 --max-batches 2 --checkpoint-dir ...`로 실제 `training.train.main`을 실행: CPU 학습·체크포인트 저장·ONNX export 성공. 학습 인물 1명 6크롭, 테스트 인물 3명 18크롭으로 분리됐다. 이는 코드 경로 검증이며 성능 평가가 아니다.
- 위 export가 6부위 크기·배치 2에서 측정한 **ONNX vs PyTorch 최대 차이 6.6e-07 < 1e-3**. `common.model_files.REQUIRED`의 파일 생성 확인 및 `SkinPredictor` 로드 성공.
- `.venv/bin/python -B /tmp/phase1-review-ct3j6pw_/out/check_onnx.py`: **해시 5/5 일치**, 6부위 reference 최대 차이 **0**. 새 reference는 해당 ONNX에서 생성되므로 이 값은 export 정합성 검증과 구분한다.
- `training.evaluate --config /tmp/phase1-review-ct3j6pw_/config.yaml --model-dir /tmp/phase1-review-ct3j6pw_/out --eval-min-groups 3`: 6개 항목 지표 및 `metrics.json` 생성 성공. 기본 min_groups=10일 때는 표본 미달 항목을 생략하는 것도 확인했다.
- 클래스 순서 변경 가드 및 재개 샘플러는 아래와 같이 별도 재현했다. `last.pt` 실제 키도 열어 확인했다.
- 실제 v2 `deploy_model.pt`에서 재export해 기존 `models/skin_v2/reference.npz`와 비교하는 검증, CUDA/AMP, 실제 SageMaker 환경·Spot 중단은 미실행. `SM_CHANNEL_*`·`SM_MODEL_DIR` 환경변수 분기는 소스 확인에 한정했다. AWS 호출은 하지 않았다.

## 발견 사항
| # | 심각도 | 파일:줄 | 문제 | 근거 | 수정 제안 | 반영 |
|---|---|---|---|---|---|---|
| 1 | Major | `common/training_targets.py:80`, `training/evaluate.py:71` | 순서형 클래스의 **순서** 변경을 파인튜닝 호환성 가드가 감지하지 못한다. 평가도 모델 metadata와 외부 tasks의 매핑 일치를 검사하지 않아 서로 다른 클래스 번호 체계로 지표를 계산할 수 있다. | signature 안의 classes가 dict라 동등성 비교에서 순서를 무시한다. `pore_grade.classes`를 역순으로 바꾸면 `task_signature(original) == task_signature(changed)`는 **True**인데 `grade_to_class(..., 0)`은 **0 → 3**으로 변한다. `train.py:91`은 이 signature만 검사하므로 기존 경계값에 다른 정답 번호를 학습시킨다. 같은 변경 tasks를 합성 모델의 `evaluate_model`에 전달해도 거절하지 않고 지표를 반환했다. 이 합성 예제의 QWK는 우연히 양쪽 모두 0이었으며 지표 변화 자체를 입증한 것은 아니다. | classes를 순서를 보존하는 `(이름, 등급 목록)`의 리스트/튜플로 signature에 넣는다. 평가 시작 시에도 모델 `meta.output.tasks`와 평가 tasks의 항목·종류·부위·클래스 순서·등급 매핑·log를 비교해 불일치를 거절한다. 역순 변경 사례를 회귀 검증에 포함한다. | ✅ 반영 — `task_signature` classes를 (이름, 등급) 리스트로, `meta_signature`·`check_tasks_match` 추가, `evaluate_model` 시작 시 검사. 역순 tasks로 signature False·evaluate ValueError·init-from RuntimeError 확인 (reports/phase-1.md 리뷰 반영 표) |
| 2 | Minor | `training/train.py:113`, `training/train.py:142` | 재개는 가중치·optimizer·scheduler를 복원하지만 데이터 순서·증강·dropout 난수 상태를 복원하지 않는다. 같은 시드로 중단 전후를 비교해도 연속 학습과 달라진다. `stats`·`l0`·tasks·cfg도 last.pt에 없어 재개 전에 현재 입력/베이스로 다시 준비하며, 기존 실행과의 일치 검사도 없다. | 실제 last.pt 키는 `epoch, history, model, optimizer, scaler, scheduler`뿐이었다. 샘플러를 12행·batch 2·seed 42로 실행하면 연속 2에폭의 첫 배치는 `[2,7]`이지만 새 프로세스에서 복원 없이 시작하면 1에폭과 같은 `[6,9]`다. `RegionBatchSampler.epoch`은 매번 0으로 초기화되고 train의 재개 경로는 이를 설정하지 않는다. torch 및 Dataset의 NumPy RNG도 저장하지 않는다. | 에폭 기준 sampler 상태와 torch CPU/CUDA·증강 RNG 상태를 저장·복원한다(다중 worker 처리 포함). 재개용 파일에 stats/l0/tasks/cfg 및 데이터 식별 정보를 포함하고 변경된 실행의 체크포인트를 잘못 재사용하지 않게 검사한다. `epochs` 연장처럼 허용할 변경은 명시한다. 연속 2에폭과 1+재개 1에폭을 비교해 검증한다. | ✅ 반영 — 증강 RNG를 (seed, 에폭, 행)로 결정해 워커 무관, 샘플러 `set_epoch`, torch RNG(cpu/cuda/mps) 저장·복원, `run` 정보 저장 + `check_resumable`(허용: epochs, num_workers, checkpoint_dir). 연속 2에폭 vs 1+재개: 가중치 차이 0 (workers 0→2) |
| 3 | Minor | `training/config.py:57` | 기본값이 전부 v2 DEPLOY와 같다는 설명과 달리 `num_workers`가 4다. | 원본 `deploy.ipynb` CFG는 `num_workers=6`; DEPLOY는 이 값을 덮어쓰지 않는다. 현재 `TrainConfig`는 4이며 기본 YAML에도 override가 없다. Dataset의 증강 RNG가 worker ID를 사용하므로 처리량뿐 아니라 같은 seed에서의 증강 배정도 달라진다. | 원본 재현 기본값은 6으로 맞춘다. 의도적으로 4를 쓸 필요가 있으면 YAML override로 분리하고 보고서에 차이를 명시한다. | ✅ 반영 — 기본값 6 |

## Claude에게 전달할 수정 요청 (우선순위 순)
1. 순서형 클래스 순서를 포함한 파인튜닝 호환성 검사를 구현하고 평가에도 metadata 일치 검사를 추가한다. — ✅ 반영 (2026-10-29)
2. 재개 상태 및 실행 정합성 검사를 보완하고 연속 학습과 재개 학습을 비교한다. — ✅ 반영 (2026-10-29, 가중치 차이 0)
3. num_workers 기본값을 원본과 맞추거나 의도적 차이로 분리해 문서화한다. — ✅ 반영 (6)
4. 실제 v2 체크포인트가 확보되면 해당 파일의 로드·재export 및 기존 reference 비교 결과를 추가한다. 랜덤 초기 모델의 이번 스모크 결과로 이 검증을 대체하지 않는다. — ⏭ 보류: `deploy_model.pt` 미도착 (사용자가 S3 `models/skin_v2/`에 업로드 예정, 도착 후 수행)

파이프라인 Condition/Register·Prefect·데이터 준비 스크립트·신규 테스트 및 docs/mlops.md는 후속 Phase 범위로 판단했다. 임시로 선택한 모델 이름과 증강 순서 변경은 보고서에 공개되어 있으며 이번 리뷰에서 사용자 결정으로 확정하지 않았다. 코드 수정 및 진행 현황 파일 변경은 하지 않았다.

## 재검토 (2026-10-29)

### 요약

수정 필요 — 기존 Major #1과 Minor #3은 해결됐다. Minor #2는 대부분 반영됐으나 **다중 워커를 유지하는 재개에서 난수 재현성 문제가 남아 있다**. 남은 사항은 Minor 1건이며, 기존 Major를 다시 발견한 것은 아니다.

### 실행한 검증

- `git status --short`, `git diff main --stat`, `git diff main`, `git diff main --check`: 추적 변경은 여전히 `.gitignore` 3줄. 금지 경로의 기존 파일 변경 없음. 미추적 구현 파일은 직접 읽어 확인했다.
- `.venv/bin/python -B -m pytest -q -p no:cacheprovider`: **24 passed**, 8.32초. `.venv/bin/python -B -m pyflakes common training`: 통과.
- 순서형 `pore_grade.classes`를 역순으로 변경: `task_signature` 비교에서 불일치 검출. 원본 tasks와 v2 meta의 `check_tasks_match`는 통과하고 역순 tasks는 `ValueError`로 거절됐다.
- 실제 `evaluate_model`에 역순 tasks 전달: `ValueError`로 거절. 역순 tasks YAML로 실제 `training.train.main --init-from ...` 실행: 모델 생성·학습 전 `RuntimeError`로 거절. 기존 Major #1 해결 확인.
- `TrainConfig().num_workers == 6`: 기존 Minor #3 해결 확인.
- 실제 새 last.pt에서 `rng` 및 `run` 추가 확인. run에는 cfg·signature·stats·l0·steps 등 저장. `check_resumable`은 epochs 연장·num_workers 변경을 허용하고 lr_head 변경을 거절했다. 증강은 `(seed, epoch, row index)`로 결정되고 sampler에 `set_epoch`가 추가됐다.
- 이전 검토의 합성 데이터·랜덤 초기 베이스 체크포인트를 재사용해 별도 임시 폴더 `/tmp/phase1-rereview-ns2o38ix`에서 **CPU, convnext_tiny, head_hidden=8, batch_size=1, num_workers=2, max_batches=2, no-export**로 실제 학습을 비교했다. 모든 실행은 같은 seed·데이터·베이스를 사용했다.
  - 연속 2에폭: train_loss **0.4917176514863968 → 0.4549177587032318**.
  - 1에폭 + 새 프로세스에서 2에폭으로 연장·재개: **0.4917176514863968 → 0.4733984023332596**.
  - 최종 state_dict 전체 비교: 최대 절대 차이 **0.00045301392674446106**. 두 실행의 1에폭은 동일했지만 재개한 2에폭부터 달라졌다.
- 다중 워커 검증은 처음 샌드박스의 `torch_shm_manager: Operation not permitted`로 실패해, 승인된 제한 밖 실행으로 같은 명령을 재실행했다. 위 수치는 정상 종료한 재실행 결과다.
- 설치된 PyTorch의 `_BaseDataLoaderIter.__init__` 및 `DataLoader.__iter__` 소스를 직접 확인해 아래 원인을 대조했다.
- exporter 변경은 없고 이번 검증은 수정된 호환성·재개 경로에 집중했다. 새 ONNX export를 반복하지 않았다. 실제 v2 체크포인트 재export·기존 reference 비교, CUDA/AMP·실제 Spot 검증은 여전히 미실행이다. AWS 호출 없음.

### 남은 발견 사항

| # | 심각도 | 파일:줄 | 문제 | 근거 | 수정 제안 | 반영 |
|---|---|---|---|---|---|---|
| R1 (기존 #2 잔여) | Minor | `training/dataset.py:177`, `training/train.py:161` | persistent worker를 사용하는 연속 학습과 새 프로세스 재개가 전역 torch RNG를 다르게 소비해 dropout 결과가 달라진다. 현재 문서의 연속 학습과 가중치가 같다는 설명은 workers=2→2에서는 성립하지 않는다. | DataLoader에 별도 `generator`가 없다. PyTorch iterator 생성은 `torch.empty(...).random_(generator=loader.generator)`로 base_seed를 뽑으므로 현재는 전역 CPU RNG를 소비한다. 연속 학습의 2에폭은 persistent iterator를 `_reset`해 이 추출을 반복하지 않지만, 재개 시에는 `restore_rng` 이후 새 iterator를 만들어 추가로 소비한다. 위 실제 CPU 학습에서 가중치 최대 차이 **4.53e-04**를 재현했다. 보고서의 workers=0→2 검증은 양쪽 모두 다음 iterator 생성이 발생하므로 이 경우를 검증하지 못한다. | DataLoader에 모델/dropout과 분리한 전용 `torch.Generator`를 전달하고 seed 및 필요한 상태를 관리한다. workers=2→2처럼 persistent iterator가 유지되는 연속 실행과 새 프로세스 재개를 비교하는 검증을 추가한다. 수정 후 README와 Phase 1 보고서의 재현성 설명도 검증 범위에 맞춘다. | ✅ 반영 (2026-11-02) — `make_loader`가 `torch.Generator().manual_seed(seed)`를 DataLoader `generator`로 전달. tiny 데이터셋·CPU·convnext_tiny·`--num-workers 2` 연속 2에폭 vs 1+재개 1에폭: 가중치 최대 차이 **0.000e+00**, train_loss 동일. README 재현성 설명 갱신 |

### Claude에게 전달할 수정 요청 (우선순위 순)

1. DataLoader의 worker base_seed 생성이 모델 전역 RNG에 영향을 주지 않게 분리하고, **workers=2→2의 연속 2에폭 vs 1+재개 1에폭**을 재검증한다. — ✅ 반영 (2026-11-02, 차이 0)
2. 실제 v2 체크포인트 재export 검증은 체크포인트 확보 후 별도로 수행한다(이전부터 미실행인 항목). — ⏭ 보류: `deploy_model.pt` 로컬 미확보 (사용자 S3 업로드 후 수행)

이번 재검토에서는 리뷰 파일만 수정했다.

## 재검토 (2회차)

2026-11-02 — 인물 단위 80:20 분할·early stopping 변경(`b427517`) 추가 검수.

### 요약

**수정 필요 — Major 1건, Minor 1건.** 인물 단위 분할·검증 점수 기반 조기 종료·최고 에폭 가중치 저장은 구현됐다. 다만 조기 종료가 이미 확정된 last.pt를 재개할 때 종료 결정을 지키지 않는다.

사용자 표현의 train/test와 구현의 train/validation을 구분해야 한다. 현재 구현은 **기존 고정 테스트셋을 먼저 제외하고 남은 인물을 80:20으로 학습/검증 분할**한다. 실제 aihub_kr은 **640명 학습 + 160명 검증 + 165명 최종 테스트**다. 순수 입력 1,000명에 split_people만 적용하면 800/200이지만, 전체 1,000명 중 고정 테스트 200명을 먼저 빼는 설정이라면 640/160/200이 된다. 최종 테스트를 early stopping에 쓰지 않는 점은 적절하며, 이것을 데이터 누수 결함으로 집계하지 않았다. 이번 변경이 전체 인원을 train/test 두 집합으로만 나누는 구현은 아니라는 점을 사용자에게 명시한다.

### 실행한 검증

- `git status --short`, `git diff main --stat`, `git diff main --name-status`, `git show b427517`으로 변경 범위를 확인했다. 지정한 11개 파일은 모두 해당 커밋의 변경 파일과 일치한다(+147/-42). main 대비 누적 변경은 65파일·7,748줄 추가이며 기존 Phase들을 포함한다. 금지 경로의 기존 파일 변경은 없고 common·tests에는 신규 파일만 추가돼 있다.
- `training/train.py`, `config.py`, `dataset.py`, `evaluate.py`, `export_onnx.py` 및 지정한 YAML·README·docs/mlops.md를 직접 대조했다. 이번 사용자 지시의 변경을 기존 고정 6에폭 계획보다 우선해 검토했다.
- `PYTHONDONTWRITEBYTECODE=1 PREFECT_HOME=/tmp/earlystop-review-prefect PREFECT_SERVER_ANALYTICS_ENABLED=false AWS_EC2_METADATA_DISABLED=true .venv/bin/python -m pytest -q -p no:cacheprovider`: **105 passed, 3 warnings, 21.97초**. 경고는 ONNX dynamic_axes 및 SageMaker PipelineSession 안내였다. 기존 export/reference·서빙 호환성 테스트도 포함된다.
- `.venv/bin/python -B -m pyflakes training`: 오류 출력 없음.
- 합성 1,000명(인물별 2행)의 `split_people(.2, 42)`: 학습 800명 / 검증 200명 / 겹침 0. 입력 행 순서를 뒤집어도 검증 인물 목록이 같았다.
- 실제 기본 설정으로 `load_rows` 및 `split_people` 실행: 학습 640명 / 검증 160명 / 고정 테스트 165명, 테스트와 학습·검증의 교집합 0.
- 표준화 통계·prior threshold·l0는 새 학습 rows로 계산하고 검증은 증강 없이 전체 loader로 계산하는 경로를 확인했다. `--init-from`에서는 기존 stats/l0를 상속하는 정책을 유지한다. 상속 모델 자체가 이미 해당 인물로 학습했는지까지 검증하는 것은 아니다.
- 임시 경로 `/tmp/earlystop-review-1faak6z9`에서 실제 main·분할·Dataset·loader·loss·optimizer·checkpoint 저장을 실행했다. 외부 가중치 다운로드를 피하기 위해 모델 생성만 작은 Linear 모델로 대체하고, load_rows는 10명·6부위 합성 행을 제공했다. 조기 종료 결정 검증에는 validate의 점수를 의도적으로 제어했다.
  - 최대 5에폭, patience=1, 검증 점수 0.9→0.8: **2에폭에서 종료**, best_epoch=1. 저장 model.pt의 가중치가 1에폭 스냅샷과 텐서별로 완전히 일치했다.
  - 동일 설정·같은 checkpoint-dir로 다시 실행하고 후속 점수를 1.0→0.9로 제공: **3·4에폭을 추가 실행**, history 길이 2→4, best_epoch 1→3. 아래 ES1 재현이다.
- 별도 age-only 작은 모델과 **실제 validate/score_grouped**로 10명→8/2 분할: 기본 eval_min_groups=10 때문에 검증 지표는 `{}`인데 score=0으로 처리돼 1에폭을 최고점으로 채택하고 2에폭에 조기 종료했다(patience=1). 아래 ES2 재현이다.
- 같은 경로에서 `--val-fraction 0 --epochs 1`: 검증 없이 정확히 1에폭, val_people=[], stopped_early=False로 정상 완료.
- pytest에는 새 split_people/조기 종료의 종료 후 재개 시나리오가 추가되지 않았다. 따라서 105 passed만으로 이번 변경의 모든 상태 전이를 검증했다고 볼 수 없다. 이번 검토의 추가 재현과 구분한다.
- AWS 호출 없음. 실제 대규모 재학습·CUDA/Spot·운영 v2 체크포인트 재export는 반복하지 않았다. exporter가 최종 model.pt를 로드하는 경로와 best 가중치 저장은 확인했지만, 이번 합성 조기 종료 모델로 새 ONNX를 생성한 것은 아니다.

### 발견 사항

| # | 심각도 | 파일:줄 | 문제 | 근거 | 수정 제안 | 반영 |
|---|---|---|---|---|---|---|
| ES1 | Major | `training/train.py:202`, `training/train.py:206` | 조기 종료된 last.pt를 재개해도 학습을 다시 진행한다. export·최종 저장 전 중단으로 재시작되는 경우, 원래의 종료 결정과 최고 모델이 달라질 수 있다. | bad_epochs와 best는 복원하지만 stopped_early를 무조건 False로 초기화하고 다음 에폭 루프에 들어간다. patience 확인은 추가 학습 후 237행에서만 한다. 위 재현에서 동일 설정인데도 history **2→4**, 최고 에폭 **1→3**으로 바뀌었다. 점수는 분기 검증을 위해 대역으로 제어했으며 모델 학습·저장은 실제 실행했다. | 복원한 bad_epochs가 현재 patience 이상이면 학습 루프를 건너뛰고 저장된 best를 복원해 최종 저장/export만 수행한다. patience를 의도적으로 늘려 계속하는 정책은 별도로 처리한다. 중단 직전/조기 종료 직후/최종 export 실패 후 재개 및 patience 변경을 회귀 검증한다. | ✅ 반영 — 재개 시 복원한 `bad_epochs ≥ patience`면 학습 루프를 건너뛰고(`stopped_early=True`) 저장된 best를 저장·export. patience를 늘리면 이어서 학습. `tests/test_train_early_stopping.py`: 조기 종료 후 재개 → history·best_epoch·가중치 불변, patience 증가 재개 → 계속, 연속 vs 재개 동일 |
| ES2 | Minor | `training/evaluate.py:75`, `training/train.py:228` | 유효한 검증 지표가 하나도 없어도 score 0을 정상 점수로 간주해 첫 에폭을 최고 모델로 저장하고 조기 종료한다. 작은 데이터·희소 라벨에서는 성능 검증 없이 학습이 중단된다. | 실제 validate 경로의 2명 age-only 검증에서 min_groups=10 미달 → results={}·score=0.0 → best_epoch=1·stopped_early=True를 확인했다. score_grouped의 기존 평가용 fallback이 이번에 학습 종료 결정에도 사용되면서 드러나는 동작이다. | 학습 검증에서는 results가 비거나 선택 지표가 모두 비유효하면 설명과 함께 실패시키거나, 명시한 검증 loss 등의 대체 기준을 사용한다. min_groups를 낮추거나 val_fraction=0으로 실행할 수 있다는 안내도 제공한다. | ✅ 반영 — `check_validation`: 유효(nan 아닌) 선택 지표가 없으면 `RuntimeError`(안내: --val-fraction 늘리기 / --eval-min-groups 낮추기 / --val-fraction 0). 검증 점수는 유효 지표만의 평균(nan→0 대체는 평가 전용). 테스트: `eval_min_groups 50` → 거절, all-nan → 거절, 일부 nan → 제외 |

### Claude에게 전달할 수정 요청 (우선순위 순)

1. ES1: 이미 patience에 도달한 체크포인트 재개가 추가 학습 없이 같은 최고 모델을 저장·내보내는지 수정하고 검증한다. — ✅ 반영 (2026-11-02)
2. ES2: 유효한 검증 지표가 없을 때 점수 0으로 자동 모델 선택하지 않도록 가드와 안내를 추가한다. — ✅ 반영 (2026-11-02)
3. 사용자에게 현재 비율이 **학습 후보 인물 내 train/validation 80:20 + 별도 고정 test**임을 유지해 설명한다. 고정 테스트셋을 early stopping에 재사용하지 않는다. — ✅ 반영 (README·docs/mlops.md에 명시, 사용자 보고에 포함)

이번 작업에서는 이 리뷰 파일만 수정했다.
