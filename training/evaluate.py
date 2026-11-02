"""내보낸 모델(skin.onnx + meta.json)을 고정 테스트셋으로 평가한다 — 모든 모델 비교의 기준.

배포할 파일 그 자체를 서빙과 같은 코드(common/predictor.py)로 돌려서 잰다.
지표는 skin v2와 같다: 같은 (사람, 부위)의 여러 사진(각도·기기)을 평균한 뒤
  순서형: QWK (선택 지표), 정확도, ±1등급, 스피어맨(기대 등급)
  회귀:   피어슨, 스피어맨, MAE (원래 단위)
점수 = 항목별 선택 지표(configs/tasks.yaml의 select) 평균. 출력 metrics.json은 models/skin_v2/metrics.json과 같은 형식.

사용: python -m training.evaluate --model-dir DIR [--config configs/train.yaml] [--out DIR/metrics.json]
"""
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import pearsonr, spearmanr
from sklearn.metrics import cohen_kappa_score

from common.crop_files import load_region_png
from common.predictor import SkinPredictor
from common.training_targets import check_tasks_match, grade_to_class
from training.config import build_parser, config_from_args, load_tasks
from training.dataset import load_rows


def ordinal_metrics(probs, actual):
    """probs: (N, K-1) "등급 > k" 확률, actual: 클래스 번호."""
    pred = (probs > 0.5).sum(axis=1)
    return dict(qwk=float(cohen_kappa_score(actual, pred, weights="quadratic")), acc=float(np.mean(pred == actual)),
                within1=float(np.mean(np.abs(pred - actual) <= 1)),
                spearman=float(spearmanr(probs.sum(axis=1), actual).statistic), n=int(len(actual)))


def regression_metrics(pred, actual):
    return dict(pearson=float(pearsonr(pred, actual).statistic), spearman=float(spearmanr(pred, actual).statistic),
                mae=float(np.abs(pred - actual).mean()), n=int(len(actual)))


def evaluate_rows(model, rows, tasks, batch_size, min_groups):
    """테스트 행 → (항목별 지표, 점수)."""
    grouped, truth = defaultdict(list), {}  # (사람, 부위) → 크롭별 출력, 정답
    by_region = defaultdict(list)
    for row in rows:
        by_region[row["region"]].append(row)
    for region, region_rows in by_region.items():
        for start in range(0, len(region_rows), batch_size):
            batch = region_rows[start:start + batch_size]
            inputs = [model.model_input(*load_region_png(r["file"]), r["face_h"], region) for r in batch]
            for row, out in zip(batch, model.predict_regions(region, inputs, batch_size=batch_size)):
                key = (row["subject"], region)
                grouped[key].append(out)
                truth[key] = row["targets"]

    return score_grouped(grouped, truth, tasks, min_groups)


def score_grouped(grouped, truth, tasks, min_groups):
    """(사람, 부위)별 크롭 출력 목록과 정답 → (항목별 지표, 점수). 학습 중 검증(training/train.py)과 평가가 같은 계산을 쓴다.

    grouped: {(subject, region): [decode 출력 dict, ...]}, truth: {(subject, region): {항목: 원래 단위 정답}}
    사진을 먼저 평균(회귀 값·등급 확률)한 뒤 지표를 재고, (사람, 부위) 쌍이 min_groups보다 적은 항목은 뺀다.
    점수 = 항목별 선택 지표(select)의 평균 (nan은 0).
    """
    results = {}
    for task, spec in tasks.items():
        keys = [k for k in grouped if task in truth[k]]
        if len(keys) < min_groups:
            continue
        raw = np.array([truth[k][task] for k in keys])
        mean = np.array([np.mean([o[task] for o in grouped[k]], axis=0) for k in keys])
        if spec["kind"] == "ordinal":
            results[task] = ordinal_metrics(mean, np.array([grade_to_class(spec, g) for g in raw]))
        else:
            results[task] = regression_metrics(mean, raw)
    score = float(np.mean(np.nan_to_num([results[t][tasks[t]["select"]] for t in results]))) if results else 0.0
    return results, score


def evaluate_model(model_dir, cfg, tasks):
    """model_dir의 배포 파일을 cfg.datasets의 테스트 분할로 평가 → metrics.json 내용(dict)."""
    model = SkinPredictor(model_dir)
    # 모델이 내보낸 출력 규격(열·클래스 순서·등급 구간·log)과 평가에 쓰는 tasks가 다르면 정답 번호 체계가 어긋난다 → 거절
    check_tasks_match(model.meta["output"]["tasks"], tasks, what=f"{model_dir}/meta.json")
    report = {"model_dir": str(model_dir), "model": model.meta["model"], "datasets": {}}
    for ds in cfg.datasets:
        rows = load_rows([ds], tasks, cfg, "test")
        if not rows:
            continue
        results, score = evaluate_rows(model, rows, tasks, cfg.batch_size, cfg.eval_min_groups)
        report["datasets"][ds["name"]] = dict(score=score, people=len({r["subject"] for r in rows}), crops=len(rows),
                                              tasks=results)
    return report


def print_report(report, tasks):
    for name, d in report["datasets"].items():
        print(f"\n[{name}] 테스트 {d['people']}명, 크롭 {d['crops']:,}개 → 점수 {d['score']:.4f}")
        for task, r in d["tasks"].items():
            text = (f"QWK {r['qwk']:.3f} | 정확도 {r['acc']:.1%} | ±1등급 {r['within1']:.1%} | 스피어맨 {r['spearman']:.3f}"
                    if tasks[task]["kind"] == "ordinal" else
                    f"피어슨 {r['pearson']:.3f} | 스피어맨 {r['spearman']:.3f} | MAE {r['mae']:.3f}")
            print(f"  {task:14s} {text}  (n={r['n']})")


def main(argv=None):
    parser = build_parser("고정 테스트셋 평가")
    parser.add_argument("--model-dir", required=True, help="skin.onnx + meta.json 폴더")
    parser.add_argument("--out", help="기본: <model-dir>/metrics.json")
    args = parser.parse_args(argv)
    cfg = config_from_args(args)
    _, tasks = load_tasks(cfg.tasks_config)
    report = evaluate_model(args.model_dir, cfg, tasks)
    if not report["datasets"]:
        raise RuntimeError("테스트 행이 없다 (datasets[].test_split 확인)")
    print_report(report, tasks)
    out = Path(args.out or Path(args.model_dir) / "metrics.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장 {out}")


if __name__ == "__main__":
    main()
