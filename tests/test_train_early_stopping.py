"""training/train.py의 인물 단위 분할·조기 종료·재개 상태 전이 (Phase 1 재검토 2회차 ES1·ES2 회귀).

실제 main()을 돌리되 무거운 부분만 바꾼다: 백본 대신 작은 모델(가중치 다운로드 없음), 합성 RGBA 크롭(캔버스 크기 그대로),
validate는 점수 목록을 순서대로 돌려주는 대역. 손실·optimizer·체크포인트 저장·재개 경로는 실제 코드다.
"""
import csv
import json
from pathlib import Path

import numpy as np
import pytest
import torch
import torch.nn as nn

pytest.importorskip("timm")  # 서빙 전용 CI에서는 건너뛴다 (training 의존성 없음)

from common import face_regions  # noqa: E402
from common.crop_files import save_region_png  # noqa: E402
from common.training_targets import output_width  # noqa: E402
from training import train as train_mod  # noqa: E402
from training.config import load_tasks  # noqa: E402
from training.dataset import split_people  # noqa: E402

REGIONS = ["forehead", "l_cheek", "l_eye"]


class TinyModel(nn.Module):
    """SkinModel 대역: 전역 평균 → 선형. param_groups/summary/num_params 인터페이스만 맞춘다. dropout으로 전역 RNG를 소비한다."""

    def __init__(self, width):
        super().__init__()
        self.backbone = nn.Sequential(nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Linear(3, 8), nn.GELU(), nn.Dropout(0.3))
        self.heads = nn.Linear(8, width)
        self.thresholds = nn.ParameterDict()

    def forward(self, x):
        return self.heads(self.backbone(x))

    def param_groups(self, lr_backbone, lr_head):
        return [{"params": list(self.backbone.parameters()), "lr": lr_backbone}, {"params": list(self.heads.parameters()), "lr": lr_head}]

    def num_params(self):
        return sum(p.numel() for p in self.parameters())

    def summary(self):
        return f"tiny {self.num_params()}"


def make_dataset(root, people=10):
    """표준 데이터셋: 사람마다 정면 사진 1장, 부위 3개(이마·왼볼·왼눈가 — 6항목 전부 정답이 있게). 정답은 사람마다 다르게."""
    (root / "crops").mkdir(parents=True)
    rng = np.random.default_rng(0)
    crops, labels = [], []
    for n in range(people):
        pid = f"{n:04d}"
        for region in REGIONS:
            w, h = face_regions.CANVAS[region]
            crop = rng.integers(60, 200, (h // 4, w // 4, 3), dtype=np.uint8)
            mask = np.ones(crop.shape[:2], np.uint8)
            rel = Path("crops") / pid / f"{pid}_03_F_{region}.png"
            (root / rel).parent.mkdir(exist_ok=True)
            save_region_png(root / rel, crop, mask)
            crops.append(dict(image_id=f"{pid}_03_F", person_id=pid, device="smartphone", view="F", region=region, status="ok",
                              file=str(rel), width=crop.shape[1], height=crop.shape[0], skin_px=int(mask.sum()), face_h=900.0))
        age = 20 + n * 3
        labels += [dict(person_id=pid, region=r, task="age", value=age) for r in REGIONS]
        labels += [dict(person_id=pid, region="forehead", task="wrinkle_grade", value=n % 7),
                   dict(person_id=pid, region="forehead", task="pigment_grade", value=n % 5),
                   dict(person_id=pid, region="l_cheek", task="pore_count", value=100 + 50 * n),
                   dict(person_id=pid, region="l_cheek", task="pore_grade", value=n % 5),
                   dict(person_id=pid, region="l_cheek", task="pigment_grade", value=(n + 1) % 5),
                   dict(person_id=pid, region="l_eye", task="wrinkle_grade", value=(n + 2) % 7),
                   dict(person_id=pid, region="l_eye", task="wrinkle_ra", value=15.0 + n)]
    for name, rows in (("crops.csv", crops), ("labels.csv", labels)):
        with open(root / name, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return root


@pytest.fixture
def setup(tmp_path, monkeypatch):
    ds = make_dataset(tmp_path / "ds")
    cfg_path = tmp_path / "train.yaml"
    cfg_path.write_text(f"datasets:\n  - name: syn\n    dir: {ds}\nnum_workers: 0\ndevice: cpu\nbatch_size: 2\nl0_batches: 4\n"
                        f"min_skin_px: 1\neval_min_groups: 2\nval_fraction: 0.2\n", encoding="utf-8")
    _, tasks = load_tasks()
    width = output_width(tasks)
    monkeypatch.setattr(train_mod.SkinModel, "from_config", staticmethod(lambda cfg, tasks, pretrained, threshold_init=None, freeze_stages=None: TinyModel(width)))
    monkeypatch.setattr(train_mod, "export", lambda *a, **k: None)
    return dict(tmp=tmp_path, cfg=str(cfg_path), tasks=tasks)


def scripted_validate(monkeypatch, scores):
    """validate 대역: 호출 순서대로 점수를 돌려준다 (지표 dict는 age 피어슨 하나)."""
    it = iter(scores)

    def fake(net, loader, rows, tasks, meta_list, device, use_amp, min_groups):
        s = next(it)
        return {"age": {"pearson": s, "spearman": s, "mae": 1.0, "n": 2}}, s
    monkeypatch.setattr(train_mod, "validate", fake)


def run(setup, model_dir, extra):
    train_mod.main(["--config", setup["cfg"], "--model-dir", str(setup["tmp"] / model_dir), "--no-export", *extra])
    return torch.load(setup["tmp"] / model_dir / "model.pt", map_location="cpu", weights_only=False)


def test_split_people_is_person_level_and_deterministic():
    rows = [dict(subject=f"d/{n:04d}", region=r) for n in range(1000) for r in REGIONS]
    train, val, val_people = split_people(rows, 0.2, 42)
    assert len(val_people) == 200 and len({r["subject"] for r in train}) == 800
    assert not ({r["subject"] for r in train} & {r["subject"] for r in val})
    assert split_people(rows[::-1], 0.2, 42)[2] == val_people  # 행 순서와 무관
    assert split_people(rows, 0, 42)[1:] == ([], [])
    with pytest.raises(RuntimeError):
        split_people(rows[:2], 0.9, 42)  # 인물 1명 → 학습 인물이 없다


def test_early_stopping_keeps_best_epoch(setup, monkeypatch):
    scripted_validate(monkeypatch, [0.5, 0.7, 0.6, 0.65])  # 최고 2에폭, patience 2 → 4에폭 끝에 종료
    ck = run(setup, "a", ["--epochs", "10", "--patience", "2", "--checkpoint-dir", str(setup["tmp"] / "ckpt_a")])
    assert ck["best_epoch"] == 2 and ck["epochs_trained"] == 2 and ck["stopped_early"] is True
    assert [h["epoch"] for h in ck["history"]] == [1, 2, 3, 4]
    assert len(ck["val_people"]) == 2 and ck["people"] == 8
    last = torch.load(setup["tmp"] / "ckpt_a" / "last.pt", map_location="cpu", weights_only=False)
    assert last["bad_epochs"] == 2 and last["best"]["epoch"] == 2
    for k, v in last["best"]["state"].items():  # 저장된 model.pt = 최고 에폭 가중치
        assert torch.equal(ck["model"][k], v)


def test_resume_after_early_stop_does_not_train_further(setup, monkeypatch):
    """ES1: patience에 도달한 last.pt(export 전에 끊긴 상황)를 재개하면 추가 에폭 없이 같은 최고 모델을 저장한다."""
    scripted_validate(monkeypatch, [0.9, 0.8])  # patience 1 → 2에폭에서 종료
    ck1 = run(setup, "b1", ["--epochs", "5", "--patience", "1", "--checkpoint-dir", str(setup["tmp"] / "ckpt_b")])
    assert ck1["stopped_early"] and ck1["best_epoch"] == 1 and len(ck1["history"]) == 2
    scripted_validate(monkeypatch, [1.0, 0.9, 0.95])  # 재개가 학습을 이어가면 이 점수로 최고 에폭이 바뀐다 → 바뀌면 안 된다
    ck2 = run(setup, "b2", ["--epochs", "5", "--patience", "1", "--checkpoint-dir", str(setup["tmp"] / "ckpt_b")])
    assert len(ck2["history"]) == 2 and ck2["best_epoch"] == 1 and ck2["stopped_early"] is True
    assert all(torch.equal(ck1["model"][k], ck2["model"][k]) for k in ck1["model"])


def test_resume_with_larger_patience_continues(setup, monkeypatch):
    scripted_validate(monkeypatch, [0.9, 0.8])
    run(setup, "c1", ["--epochs", "5", "--patience", "1", "--checkpoint-dir", str(setup["tmp"] / "ckpt_c")])
    scripted_validate(monkeypatch, [0.95])  # patience 3으로 늘리면 3에폭을 이어서 학습하고 최고가 갱신된다
    ck = run(setup, "c2", ["--epochs", "3", "--patience", "3", "--checkpoint-dir", str(setup["tmp"] / "ckpt_c")])
    assert [h["epoch"] for h in ck["history"]] == [1, 2, 3] and ck["best_epoch"] == 3 and ck["stopped_early"] is False


def test_resume_matches_continuous_training(setup, monkeypatch):
    """연속 3에폭 == 1에폭 + 재개 2에폭 (가중치·검증 이력·최고 에폭). dropout이 있는 모델이라 난수 상태 복원까지 확인된다."""
    scripted_validate(monkeypatch, [0.1, 0.2, 0.3])
    a = run(setup, "d_cont", ["--epochs", "3", "--patience", "9"])
    scripted_validate(monkeypatch, [0.1])
    run(setup, "d1", ["--epochs", "1", "--patience", "9", "--checkpoint-dir", str(setup["tmp"] / "ckpt_d")])
    scripted_validate(monkeypatch, [0.2, 0.3])
    b = run(setup, "d2", ["--epochs", "3", "--patience", "9", "--checkpoint-dir", str(setup["tmp"] / "ckpt_d")])
    assert all(torch.equal(a["model"][k], b["model"][k]) for k in a["model"])
    assert [h["train_loss"] for h in a["history"]] == [h["train_loss"] for h in b["history"]]
    assert a["best_epoch"] == b["best_epoch"] == 3


def test_no_validation_runs_fixed_epochs(setup, monkeypatch):
    def must_not_run(*a, **k):
        raise AssertionError("val_fraction 0이면 validate를 부르면 안 된다")
    monkeypatch.setattr(train_mod, "validate", must_not_run)
    ck = run(setup, "e", ["--val-fraction", "0", "--epochs", "2"])
    assert ck["val_people"] == [] and ck["epochs_trained"] == 2 and ck["stopped_early"] is False and ck["people"] == 10


def test_missing_regression_labels_fail_clearly(setup, tmp_path):
    """처음부터 학습하는데 회귀 항목 정답이 아예 없으면 KeyError 대신 설명과 함께 멈춘다."""
    ds = Path(setup["tmp"]) / "ds"
    rows = [r for r in csv.DictReader(open(ds / "labels.csv", encoding="utf-8")) if r["task"] != "wrinkle_ra"]
    with open(ds / "labels.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(RuntimeError, match="wrinkle_ra"):
        run(setup, "h", ["--epochs", "1", "--val-fraction", "0"])


def test_validation_without_valid_metric_fails(setup):
    """ES2: 유효한 검증 지표가 없으면 점수 0으로 모델을 고르지 않고 멈춘다 (eval_min_groups가 검증 인물 수보다 큼)."""
    with pytest.raises(RuntimeError, match="검증 점수를 낼 수 있는 항목이 없다"):
        run(setup, "f", ["--epochs", "1", "--eval-min-groups", "50"])


def test_check_validation_ignores_nan_and_rejects_all_nan():
    _, tasks = load_tasks()
    ok = {"age": {"pearson": 0.5}, "pore_grade": {"qwk": float("nan")}}
    assert train_mod.check_validation(ok, tasks, 2) == ["age"]
    with pytest.raises(RuntimeError):
        train_mod.check_validation({"pore_grade": {"qwk": float("nan")}}, tasks, 2)
    with pytest.raises(RuntimeError):
        train_mod.check_validation({}, tasks, 2)


def test_history_json_written(setup, monkeypatch):
    scripted_validate(monkeypatch, [0.4])
    run(setup, "g", ["--epochs", "1"])
    history = json.loads((setup["tmp"] / "g" / "train_history.json").read_text(encoding="utf-8"))
    assert history[0]["val_score"] == 0.4 and "val_age_pearson" in history[0]
