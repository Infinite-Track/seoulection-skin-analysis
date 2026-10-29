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

| # | 심각도 | 파일:줄 | 문제 | 근거 | 수정 제안 |
|---|---|---|---|---|---|
| R1 (기존 #2 잔여) | Minor | `training/dataset.py:177`, `training/train.py:161` | persistent worker를 사용하는 연속 학습과 새 프로세스 재개가 전역 torch RNG를 다르게 소비해 dropout 결과가 달라진다. 현재 문서의 연속 학습과 가중치가 같다는 설명은 workers=2→2에서는 성립하지 않는다. | DataLoader에 별도 `generator`가 없다. PyTorch iterator 생성은 `torch.empty(...).random_(generator=loader.generator)`로 base_seed를 뽑으므로 현재는 전역 CPU RNG를 소비한다. 연속 학습의 2에폭은 persistent iterator를 `_reset`해 이 추출을 반복하지 않지만, 재개 시에는 `restore_rng` 이후 새 iterator를 만들어 추가로 소비한다. 위 실제 CPU 학습에서 가중치 최대 차이 **4.53e-04**를 재현했다. 보고서의 workers=0→2 검증은 양쪽 모두 다음 iterator 생성이 발생하므로 이 경우를 검증하지 못한다. | DataLoader에 모델/dropout과 분리한 전용 `torch.Generator`를 전달하고 seed 및 필요한 상태를 관리한다. workers=2→2처럼 persistent iterator가 유지되는 연속 실행과 새 프로세스 재개를 비교하는 검증을 추가한다. 수정 후 README와 Phase 1 보고서의 재현성 설명도 검증 범위에 맞춘다. |

### Claude에게 전달할 수정 요청 (우선순위 순)

1. DataLoader의 worker base_seed 생성이 모델 전역 RNG에 영향을 주지 않게 분리하고, **workers=2→2의 연속 2에폭 vs 1+재개 1에폭**을 재검증한다.
2. 실제 v2 체크포인트 재export 검증은 체크포인트 확보 후 별도로 수행한다(이전부터 미실행인 항목).

이번 재검토에서는 리뷰 파일만 수정했다.
