"""파이프라인 단계 스크립트의 순수 함수 검증 (AWS·모델 없음). Codex Phase 3 리뷰 #1~#3 회귀 방지."""
import csv
import io
import json
import tarfile

import pytest

from pipelines.steps import evaluate, prepare, util

TASKS = {"pore_grade": {"select": "qwk"}, "wrinkle_ra": {"select": "spearman"}}
BASE = {"tasks": {
    "pore_grade": {"n": 100, "qwk": 0.70, "acc": 0.80, "within1": 0.95, "spearman": 0.70, "mae": 0.30},
    "wrinkle_ra": {"n": 100, "pearson": 0.60, "spearman": 0.70, "mae": 1.0},
}}


def _with(task, **changes):
    cand = {"tasks": {t: dict(m) for t, m in BASE["tasks"].items()}}
    cand["tasks"][task].update(changes)
    return cand


def _failed(rows):
    return sorted(f"{r['task']}.{r['metric']}" for r in rows if not r["passed"])


# ---- #1 등록 조건: 선택 지표가 유지돼도 다른 지표가 나빠지면 불통과 ----

def test_compare_all_rejects_select_up_but_others_down():
    cand = _with("pore_grade", qwk=0.71, acc=0.10, within1=0.20, spearman=0.10)
    assert _failed(evaluate.compare(cand, BASE, TASKS, "all", 0.0)) == ["pore_grade.acc", "pore_grade.spearman", "pore_grade.within1"]
    assert _failed(evaluate.compare(cand, BASE, TASKS, "select", 0.0)) == []  # select는 분석용 — 등록 경로에 쓰지 않는다


def test_compare_all_rejects_correlation_up_but_mae_up():
    cand = _with("wrinkle_ra", spearman=0.75, mae=1.3)
    assert _failed(evaluate.compare(cand, BASE, TASKS, "all", 0.0)) == ["wrinkle_ra.mae"]


def test_compare_same_model_passes_and_counts_all_metrics():
    rows = evaluate.compare(BASE, BASE, TASKS, "all", 0.0)
    assert _failed(rows) == [] and len(rows) == 8  # n 제외 5 + 3
    assert _failed(evaluate.compare(_with("pore_grade", acc=None), BASE, TASKS, "all", 0.0)) == ["pore_grade.acc"]  # 값 없음 = 실패


def test_evaluate_default_axes_is_all_and_pipeline_config_has_no_axes_knob():
    from training.config import load_yaml

    assert evaluate.DEFAULT_AXES == "all"
    assert "axes" not in load_yaml("configs/pipeline.yaml")["condition"]
    with pytest.raises(ValueError):
        evaluate.compare(BASE, BASE, TASKS, "selected", 0.0)


# ---- #3 동명 tar 두 개를 같은 workdir에 풀어도 서로 덮어쓰지 않는다 ----

def _write_tar(path, who):
    with tarfile.open(path, "w:gz") as tf:
        for name, content in (("meta.json", json.dumps({"who": who})), ("skin.onnx", who.upper())):
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_unpack_model_keeps_same_named_tars_apart(tmp_path):
    for who in ("candidate", "baseline"):
        (tmp_path / who).mkdir()
        _write_tar(tmp_path / who / util.MODEL_TAR, who)
    work = tmp_path / "work"
    cand = util.unpack_model(tmp_path / "candidate" / util.MODEL_TAR, work)
    base = util.unpack_model(tmp_path / "baseline", work)  # tar가 든 폴더 입력
    assert cand != base and cand.is_relative_to(work) and base.is_relative_to(work)
    assert json.loads((cand / "meta.json").read_text())["who"] == "candidate"
    assert json.loads((base / "meta.json").read_text())["who"] == "baseline"
    assert (cand / "skin.onnx").read_text() == "CANDIDATE" and (base / "skin.onnx").read_text() == "BASELINE"
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "meta.json").write_text("{}")
    assert util.unpack_model(extracted, work) == extracted  # 이미 풀린 폴더는 그대로


# ---- #2 feedback 소스 ID = feedback 루트 기준 상대 경로, 충돌은 거절 ----

REGIONS = ["forehead", "glabella", "l_eye", "r_eye", "l_cheek", "r_cheek"]


def _dataset(root, person, value):
    rows = []
    for region in REGIONS:
        f = f"crops/{person}/img_{region}.png"
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_bytes(b"png")
        rows.append(dict(image_id="img", person_id=person, device="phone", view="front", region=region, status="ok",
                         file=f, width=1, height=1, skin_px=1, face_h=1))
    rows.append(dict(rows[0], image_id="img2", region="forehead", status="no_face", file=""))
    with open(root / "crops.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with open(root / "labels.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["person_id", "region", "task", "value"])
        w.writeheader()
        w.writerow(dict(person_id=person, region="l_cheek", task="pore_grade", value=value))


def test_feedback_sources_with_same_leaf_name_stay_separate(tmp_path):
    fb = tmp_path / "feedback"
    _dataset(fb / "2026-10" / "batch1", "001", 1)
    _dataset(fb / "2026-11" / "batch1", "001", 5)
    datasets, _ = prepare.find_datasets(fb)
    ids = [prepare.feedback_source(fb, d) for d in datasets]
    assert ids == ["feedback/2026-10/batch1", "feedback/2026-11/batch1"]
    assert prepare.feedback_source(fb / "2026-10" / "batch1", fb / "2026-10" / "batch1") == "feedback"

    out, rows, labels = tmp_path / "out", [], []
    for source, d in zip(ids, datasets):
        crops, lab = prepare.load_dataset(d)
        r, l, _ = prepare.merge_source(source, d, crops, lab, out, set())
        rows += r
        labels += l
    assert prepare.merge_conflicts(ids, rows, labels) == []
    assert sorted({r["person_id"] for r in rows}) == ["feedback/2026-10/batch1:001", "feedback/2026-11/batch1:001"]
    assert sorted(r["value"] for r in labels) == ["1", "5"]  # 라벨 1과 5가 각각 남는다
    ok_files = {r["file"] for r in rows if r["status"] == "ok"}
    assert len(ok_files) == 12 and all((out / f).is_file() for f in ok_files)


def test_merge_refuses_duplicate_source_id_and_overwrite(tmp_path):
    fb = tmp_path / "feedback"
    _dataset(fb / "a" / "batch1", "001", 1)
    _dataset(fb / "b" / "batch1", "001", 5)
    datasets, _ = prepare.find_datasets(fb)
    assert prepare.merge_conflicts(["replay", "feedback_batch1", "feedback_batch1"], [], []) == ["소스 ID 중복: ['feedback_batch1']"]
    out = tmp_path / "out"
    crops, lab = prepare.load_dataset(datasets[0])
    prepare.merge_source("feedback_batch1", datasets[0], crops, lab, out, set())
    before = (out / "feedback_batch1/crops/001/img_forehead.png").read_bytes()
    crops, lab = prepare.load_dataset(datasets[1])
    with pytest.raises(SystemExit, match="출력 파일 충돌"):
        prepare.merge_source("feedback_batch1", datasets[1], crops, lab, out, set())
    assert (out / "feedback_batch1/crops/001/img_forehead.png").read_bytes() == before  # 덮어쓰지 않았다


def test_merge_conflicts_detects_duplicate_labels_and_crops():
    label = dict(person_id="replay:1", region="l_cheek", task="pore_grade", value="1")
    crop = dict(image_id="replay:img", region="l_eye", status="ok", file="replay/crops/1/x.png")
    assert prepare.merge_conflicts(["replay"], [], [label, label]) == ["라벨 키 (person_id, region, task) 중복 1건: [\"('replay:1', 'l_cheek', 'pore_grade')\"]"]
    problems = prepare.merge_conflicts(["replay"], [crop, dict(crop, file="replay/crops/1/y.png")], [])
    assert problems == ["크롭 (image_id, region) 중복 1건: [\"('replay:img', 'l_eye')\"]"]
    assert prepare.merge_conflicts(["replay"], [crop, dict(crop, region="r_eye", file="")], []) == []  # 실패 행의 빈 file은 충돌 아님
