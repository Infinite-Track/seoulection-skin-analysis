"""학습. 끝나면 model_dir에 model.pt + 배포용 skin.onnx·skin.onnx.data·meta.json·reference.npz·SHA256SUMS.txt·check_onnx.py.

처음부터 (init_from 없음): ImageNet 사전학습 백본, 회귀 표준화 통계·순서형 경계값 시작값·시작 손실(l0)을 학습 데이터에서 계산 (= skin v2 재현)
파인튜닝 (--init-from model.pt): 항목 정의(순서·클래스 순서 포함)가 체크포인트와 같아야 하고, stats·l0·경계값을 체크포인트에서 이어받는다
    (stats를 새 데이터로 다시 재면 같은 로짓의 의미가 바뀌어 기존 헤드가 틀어진다)

검증 데이터 없이 고정 에폭만 돈다 (skin v2와 같다). 모델 비교는 training/evaluate.py로 고정 테스트셋에서 한다.

사용:
    python -m training.train [--config configs/train.yaml] [--init-from PATH] [--model-dir DIR]
                             [--epochs N] [--lr-head 1e-4 ...] [--max-batches N] [--no-export]
SageMaker: SM_MODEL_DIR(출력), SM_CHANNEL_<데이터셋 이름>(입력), SM_CHANNEL_BASE_MODEL(베이스 model.pt 폴더)을 자동으로 쓴다.
Spot:      --checkpoint-dir DIR 에 에폭마다 last.pt(모델·optimizer·scheduler·난수 상태·실행 정보)를 쓰고, 다시 시작하면 거기서 이어간다.
           재개 결과는 연속 학습과 같다 (배치 순서·증강은 에폭으로 정해지고, dropout 난수 상태를 복원한다).
           실행 정보(데이터·stats·설정)가 다르면 다른 실행의 체크포인트로 보고 멈춘다. epochs·num_workers·checkpoint_dir만 바꿀 수 있다.
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

import torch

from common.training_targets import output_layout, regression_stats, task_signature
from training.config import build_parser, config_from_args, config_to_dict, load_tasks, output_dir_for
from training.dataset import load_rows, make_loader, regression_values
from training.export_onnx import export
from training.losses import combine, measure_initial_losses, prior_thresholds, task_losses
from training.model import SkinModel

CHECKPOINT_NAME, LAST_NAME = "model.pt", "last.pt"
LEGACY_CHECKPOINT_NAMES = ("deploy_model.pt",)  # skin v2 노트북이 저장한 이름 (S3 models/skin_v2/). 폴더를 주면 model.pt 다음으로 찾는다
RESUME_MUTABLE = {"epochs", "num_workers", "checkpoint_dir"}  # 재개할 때 바꿔도 되는 설정


def pick_device(choice="auto"):
    if choice != "auto":
        return choice
    if torch.cuda.is_available():
        return "cuda"
    return "mps" if torch.backends.mps.is_available() else "cpu"


def resolve_init_from(cfg):
    """--init-from / yaml init_from / SM_CHANNEL_BASE_MODEL. 폴더면 안의 model.pt."""
    source = cfg.init_from or os.environ.get("SM_CHANNEL_BASE_MODEL")
    if not source:
        return None
    p = Path(source)
    if p.is_dir():
        names = (CHECKPOINT_NAME, *LEGACY_CHECKPOINT_NAMES)
        found = [p / n for n in names if (p / n).exists()]
        if not found:
            raise FileNotFoundError(f"베이스 체크포인트 없음: {p}에 {list(names)} 중 하나가 있어야 한다")
        p = found[0]
    if not p.exists():
        raise FileNotFoundError(f"베이스 체크포인트 없음: {p}")
    return p


def load_checkpoint(path):
    return torch.load(path, map_location="cpu", weights_only=False)


def rng_states():
    """dropout 등 torch 난수 상태 (장치별). 재개 때 복원한다."""
    states = {"cpu": torch.get_rng_state()}
    if torch.cuda.is_available():
        states["cuda"] = torch.cuda.get_rng_state_all()
    if torch.backends.mps.is_available():
        states["mps"] = torch.mps.get_rng_state()
    return states


def restore_rng(states):
    torch.set_rng_state(states["cpu"])
    if "cuda" in states and torch.cuda.is_available():
        torch.cuda.set_rng_state_all(states["cuda"])
    if "mps" in states and torch.backends.mps.is_available():
        torch.mps.set_rng_state(states["mps"])


def check_resumable(saved_run, run):
    """last.pt의 실행 정보가 지금 실행과 같은지. 다르면 다른 실행의 체크포인트 → 멈춘다."""
    for key in ("signature", "datasets", "crops", "people", "steps", "parent", "stats"):
        if saved_run[key] != run[key]:
            raise RuntimeError(f"last.pt는 다른 실행의 체크포인트다 ({key}가 다름: {saved_run[key]!r} ≠ {run[key]!r})")
    changed = {k for k, v in run["cfg"].items() if saved_run["cfg"].get(k) != v} - RESUME_MUTABLE
    if changed:
        raise RuntimeError(f"last.pt는 다른 설정의 체크포인트다 (바뀐 설정: {sorted(changed)}; 바꿀 수 있는 것: {sorted(RESUME_MUTABLE)})")


def main(argv=None):
    parser = build_parser("skin 모델 학습 (기본값 = skin v2)")
    parser.add_argument("--model-dir", help="산출물 폴더. 기본: SM_MODEL_DIR 또는 <output_root>/models/<시각>")
    parser.add_argument("--max-batches", type=int, help="에폭당 배치 수 상한 (동작 확인용)")
    parser.add_argument("--no-export", action="store_true", help="ONNX 내보내기 생략")
    args = parser.parse_args(argv)
    cfg = config_from_args(args)
    if not cfg.datasets:
        parser.error("datasets가 비어 있다 (configs/train.yaml)")

    _, tasks = load_tasks(cfg.tasks_config)
    layout = output_layout(tasks)
    device = pick_device(cfg.device)
    use_amp = cfg.amp and device == "cuda"
    model_dir = Path(args.model_dir or os.environ.get("SM_MODEL_DIR")
                     or output_dir_for(cfg, "models", datetime.now().strftime("%Y%m%d-%H%M%S")))
    model_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = Path(cfg.checkpoint_dir) if cfg.checkpoint_dir else None
    last = load_checkpoint(checkpoint_dir / LAST_NAME) if checkpoint_dir and (checkpoint_dir / LAST_NAME).exists() else None

    rows = load_rows(cfg.datasets, tasks, cfg, "train")
    if not rows:
        raise RuntimeError("학습 크롭이 없다 (datasets·views·min_skin_px·test_split 확인)")
    people = len({r["subject"] for r in rows})
    init_from = resolve_init_from(cfg)
    print(f"장치 {device} (AMP {'사용' if use_amp else '미사용'}) | 학습 크롭 {len(rows):,}개, 사람 {people:,}명 | "
          f"시작점 {init_from or 'imagenet'} → {model_dir}", flush=True)

    if init_from is None:
        stats = regression_stats(tasks, regression_values(rows, tasks))
        thresholds = prior_thresholds(rows, tasks) if cfg.threshold_init == "prior" else None
        torch.manual_seed(cfg.seed)
        net = SkinModel.from_config(cfg, tasks, pretrained=last is None, threshold_init=thresholds)
        l0, parent = None, None
    else:
        saved = load_checkpoint(init_from)
        if task_signature(saved["tasks"]) != task_signature(tasks):
            raise RuntimeError(f"{cfg.tasks_config}가 베이스 체크포인트의 항목 정의와 다르다 → 파인튜닝 불가 (처음부터 학습할 것)")
        stats, l0, parent = saved["stats"], saved["l0"], str(init_from)
        torch.manual_seed(cfg.seed)
        net = SkinModel.from_config(cfg, tasks, pretrained=False)
        net.load_state_dict(saved["model"])
    net.to(device)
    print("  " + net.summary(), flush=True)

    loader = make_loader(rows, stats, cfg, tasks, train=True, device=device)
    sampler = loader.batch_sampler
    steps = min(len(loader), args.max_batches or len(loader))
    if steps == 0:
        raise RuntimeError(f"배치가 없다: 크롭 {len(rows)}개 < batch_size {cfg.batch_size} (부위별로 묶는다)")

    run = dict(signature=task_signature(tasks), datasets=[d["name"] for d in cfg.datasets], crops=len(rows),
               people=people, steps=steps, parent=parent, stats=stats, cfg=config_to_dict(cfg))
    if last is not None:
        check_resumable(last["run"], run)
        l0 = last["run"]["l0"]
    elif l0 is None:
        sampler.set_epoch(0)
        l0 = measure_initial_losses(net, loader, cfg.l0_batches, tasks, layout, device, use_amp)
        print("  시작 손실", {t: round(v, 3) for t, v in l0.items()}, flush=True)
    run["l0"] = l0

    optimizer = torch.optim.AdamW(net.param_groups(cfg.lr_backbone, cfg.lr_head), weight_decay=cfg.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=cfg.schedule_epochs * steps)
    scaler = torch.amp.GradScaler(device, enabled=use_amp)

    start_epoch, history = 1, []
    if last is not None:
        net.load_state_dict(last["model"])
        optimizer.load_state_dict(last["optimizer"])
        scheduler.load_state_dict(last["scheduler"])
        scaler.load_state_dict(last["scaler"])
        restore_rng(last["rng"])
        start_epoch, history = last["epoch"] + 1, last["history"]
        print(f"  {checkpoint_dir / LAST_NAME}에서 재개: {start_epoch}에폭부터", flush=True)

    for epoch in range(start_epoch, cfg.epochs + 1):
        net.train()
        sampler.set_epoch(epoch)  # 배치 순서·증강 난수가 에폭으로 정해진다 → 재개해도 같은 순서
        started, total = time.time(), 0.0
        for b, (images, targets, valid, _) in enumerate(loader):
            if b >= steps:
                break
            optimizer.zero_grad()
            with torch.autocast(device, dtype=torch.float16, enabled=use_amp):
                out = net(images.to(device))
                loss = combine(task_losses(out, targets.to(device), valid.to(device), tasks, layout), l0)
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            scheduler.step()
            total += loss.item()
        history.append(dict(epoch=epoch, train_loss=total / steps, lr_backbone=scheduler.get_last_lr()[0]))
        print(f"  epoch {epoch:2d} loss {total / steps:.3f} | lr {scheduler.get_last_lr()[0]:.1e} "
              f"({time.time() - started:.0f}초)", flush=True)
        if checkpoint_dir:
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            torch.save({"model": net.state_dict(), "optimizer": optimizer.state_dict(),
                        "scheduler": scheduler.state_dict(), "scaler": scaler.state_dict(), "rng": rng_states(),
                        "epoch": epoch, "history": history, "run": run}, checkpoint_dir / LAST_NAME)

    checkpoint = model_dir / CHECKPOINT_NAME
    torch.save({"model": net.state_dict(), "stats": stats, "l0": l0, "cfg": config_to_dict(cfg), "tasks": tasks,
                "people": people, "crops": len(rows), "datasets": [d["name"] for d in cfg.datasets],
                "parent": parent, "history": history, "created": datetime.now().isoformat(timespec="seconds")},
               checkpoint)
    (model_dir / "train_history.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 {checkpoint}", flush=True)
    if not args.no_export:
        export(checkpoint, model_dir, name=cfg.model_name, opset=cfg.onnx_opset, reference_seed=cfg.reference_seed)


if __name__ == "__main__":
    main()
