"""Evaluate 단계 (Processing, CPU): 새 모델과 현재 운영 모델을 **같은 고정 테스트셋**으로 평가하고 축별로 비교한다.

둘 다 training/evaluate.py(= 서빙 코드 common/predictor.py로 추론)로 재서 metrics.json 형식 결과를 낸다. 현재 모델의 저장된
metrics.json을 믿지 않고 다시 재는 이유: 테스트셋·평가 코드가 같을 때만 비교가 공정하기 때문이다.

축(axis) = (항목, 지표). --axes select면 항목별 선택 지표(configs/tasks.yaml select: QWK·피어슨·스피어맨)만, all이면 항목별 모든 지표
(정확도·±1등급·MAE 포함). MAE는 낮을수록 좋다. 축 하나라도 새 모델이 (현재 모델 - tolerance)보다 나쁘거나 값이 없으면 실패.

출력 (--out 폴더):
  evaluation.json     두 모델 요약 + axes[] + failed_axes/total_axes/passed  ← Condition 단계가 failed_axes == 0 을 본다
  metrics.json        새 모델의 metrics.json (models/skin_v2/metrics.json과 같은 형식) — Register가 모델 폴더에 같이 올린다
  metrics_baseline.json  현재 모델의 metrics.json

사용: python -m pipelines.steps.evaluate --candidate <model.tar.gz|폴더> --baseline <폴더> --testset <폴더> --out <폴더>
                                        [--axes select|all] [--tolerance 0] [--testset-name aihub_kr] [--limit-people N]
"""
from pipelines.steps.bootstrap import system_libs  # noqa: I001 (common을 import하기 전에)

system_libs()

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from common.config import resolve  # noqa: E402
from pipelines.steps.util import SM_INPUT, SM_OUTPUT, dump_json, unpack_model  # noqa: E402
from scripts.crop_index import read_csv  # noqa: E402
from training.config import CROPS_FILE, build_parser, config_from_args, load_tasks, load_yaml, read_id_list  # noqa: E402
from training.evaluate import evaluate_model, print_report  # noqa: E402

PIPELINE_CONFIG = "configs/pipeline.yaml"
TRAIN_CONFIG = "configs/train_pipeline.yaml"
SPLITS_DIR = "configs/splits"
LOWER_IS_BETTER = {"mae"}
NOT_METRICS = {"n"}


def testset_dataset(root, name, workdir, limit_people=None):
    """테스트셋 폴더 → training.evaluate용 datasets 항목. 폴더 인물 전원이 테스트 분할이다 (목록 파일을 workdir에 쓴다).

    configs/splits/<name>_test.txt가 있으면 폴더 인물과 같아야 한다 (다르면 고정 테스트셋이 아니다 → 멈춤). --limit-people은 동작 확인용.
    """
    root = Path(root)
    people = sorted({r["person_id"] for r in read_csv(root / CROPS_FILE)})
    split_file = resolve(SPLITS_DIR) / f"{name}_test.txt"
    if split_file.exists() and set(people) != read_id_list(split_file):
        raise SystemExit(f"{root}의 인물({len(people)}명)이 고정 테스트셋 {split_file}({len(read_id_list(split_file))}명)과 다르다")
    if limit_people:
        people = people[:limit_people]
    listing = Path(workdir) / f"{name}_test_people.txt"
    listing.write_text("\n".join(people) + "\n", encoding="utf-8")
    return dict(name=name, dir=str(root), test_split=str(listing)), len(people)


def compare(candidate, baseline, tasks, axes, tolerance):
    """두 metrics 결과(datasets[name] 블록) → 축 목록. 기준은 현재 모델이 지표를 낸 항목 전부."""
    rows = []
    for task, base in baseline["tasks"].items():
        metrics = [tasks[task]["select"]] if axes == "select" else [m for m in base if m not in NOT_METRICS]
        cand = candidate["tasks"].get(task, {})
        for metric in metrics:
            b, c = base[metric], cand.get(metric)
            lower = metric in LOWER_IS_BETTER
            passed = c is not None and (c <= b + tolerance if lower else c >= b - tolerance)  # NaN은 비교가 False → 실패
            rows.append(dict(task=task, metric=metric, candidate=c, baseline=b, higher_is_better=not lower,
                             delta=(c - b) if c is not None else None, passed=bool(passed)))
    return rows


def main(argv=None):
    defaults = load_yaml(PIPELINE_CONFIG)
    parser = build_parser("새 모델 vs 현재 모델 고정 테스트셋 평가", config_default=TRAIN_CONFIG)
    parser.add_argument("--candidate", default=f"{SM_INPUT}/candidate", help="새 모델: model.tar.gz, 그 폴더, 또는 풀린 폴더")
    parser.add_argument("--baseline", default=f"{SM_INPUT}/baseline", help="현재 운영 모델 폴더 (meta.json, skin.onnx, skin.onnx.data)")
    parser.add_argument("--testset", default=f"{SM_INPUT}/testset", help="고정 테스트셋 폴더")
    parser.add_argument("--out", default=f"{SM_OUTPUT}/evaluation")
    parser.add_argument("--testset-name", default=defaults["prepare"]["testset_name"])
    parser.add_argument("--axes", choices=["select", "all"], default=defaults["condition"]["axes"])
    parser.add_argument("--tolerance", type=float, default=defaults["condition"]["tolerance"])
    parser.add_argument("--limit-people", type=int, help="동작 확인용: 테스트 인물 앞 N명만")
    args = parser.parse_args(argv)
    cfg = config_from_args(args)
    _, tasks = load_tasks(cfg.tasks_config)

    with tempfile.TemporaryDirectory(prefix="evaluate-") as tmp:
        candidate_dir = unpack_model(args.candidate, tmp)
        baseline_dir = unpack_model(args.baseline, tmp)
        ds, people = testset_dataset(args.testset, args.testset_name, tmp, args.limit_people)
        cfg.datasets = [ds]
        print(f"테스트셋 {ds['dir']}: {people}명 | 새 모델 {candidate_dir} | 현재 모델 {baseline_dir}", flush=True)

        reports = {}
        for label, model_dir in (("candidate", candidate_dir), ("baseline", baseline_dir)):
            report = evaluate_model(model_dir, cfg, tasks)
            if ds["name"] not in report["datasets"]:
                raise SystemExit(f"{label}: 테스트 행이 없다 ({ds['dir']})")
            print(f"\n[{label}] {report['model']}", flush=True)
            print_report(report, tasks)
            reports[label] = report

    cand, base = reports["candidate"]["datasets"][ds["name"]], reports["baseline"]["datasets"][ds["name"]]
    axes = compare(cand, base, tasks, args.axes, args.tolerance)
    failed = [a for a in axes if not a["passed"]]
    evaluation = dict(
        testset=dict(name=ds["name"], dir=ds["dir"], people=cand["people"], crops=cand["crops"], limited=bool(args.limit_people)),
        candidate=dict(model_dir=str(candidate_dir), model=reports["candidate"]["model"], score=cand["score"], tasks=cand["tasks"]),
        baseline=dict(model_dir=str(baseline_dir), model=reports["baseline"]["model"], score=base["score"], tasks=base["tasks"]),
        axes_mode=args.axes, tolerance=args.tolerance, axes=axes,
        failed_axes=len(failed), total_axes=len(axes), passed=not failed and bool(axes),
    )
    if not axes:
        evaluation["failed_axes"] = 1  # 비교할 축이 없으면 통과시키지 않는다
    out = Path(args.out)
    dump_json(out / "evaluation.json", evaluation)
    dump_json(out / "metrics.json", reports["candidate"])
    dump_json(out / "metrics_baseline.json", reports["baseline"])

    print(f"\n점수 새 모델 {cand['score']:.4f} vs 현재 {base['score']:.4f} | 축 {len(axes)}개 중 실패 {len(failed)}개 → "
          f"{'통과' if evaluation['passed'] else '불통과'}", flush=True)
    for a in failed:
        print(f"  ✗ {a['task']}.{a['metric']}: {a['candidate']} vs {a['baseline']}", flush=True)
    print(f"저장 {out}", flush=True)


if __name__ == "__main__":
    main()
