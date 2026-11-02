"""학습. 끝나면 model_dir에 model.pt + 배포용 skin.onnx·skin.onnx.data·meta.json·reference.npz·SHA256SUMS.txt·check_onnx.py.

데이터: 학습 인물을 인물 단위로 (1 - val_fraction) : val_fraction 으로 나눈다 (기본 80:20, seed 고정). 같은 사람의 사진은 정답이 같아
      사진 단위로 나누면 점수가 부풀려진다. 표준화 통계·경계값 시작값·시작 손실은 학습 쪽 인물로만 잰다 (검증 정보가 새지 않게).
조기 종료: 에폭마다 검증 인물로 점수(평가와 같은 계산 — training/evaluate.score_grouped, 항목별 선택 지표 평균)를 재고,
      patience만큼 연속으로 좋아지지 않으면 멈춘다. 최고 점수 에폭의 가중치를 저장·내보낸다. epochs는 상한이다.
      val_fraction 0이면 검증 없이 epochs만큼 고정 학습 (= skin v2 배포 학습: --val-fraction 0 --epochs 6).
처음부터 (init_from 없음): ImageNet 사전학습 백본, 회귀 표준화 통계·순서형 경계값 시작값·시작 손실(l0)을 학습 데이터에서 계산
파인튜닝 (--init-from model.pt): 항목 정의(순서·클래스 순서 포함)가 체크포인트와 같아야 하고, stats·l0·경계값을 체크포인트에서 이어받는다
    (stats를 새 데이터로 다시 재면 같은 로짓의 의미가 바뀌어 기존 헤드가 틀어진다)
최종 비교는 training/evaluate.py로 고정 테스트셋에서 한다 (검증 인물은 학습 데이터 안의 분할이고 테스트셋은 아니다).

사용:
    python -m training.train [--config configs/train.yaml] [--init-from PATH] [--model-dir DIR]
                             [--epochs N] [--val-fraction 0.2] [--patience 6] [--lr-head 1e-4 ...] [--max-batches N] [--no-export]
SageMaker: SM_MODEL_DIR(출력), SM_CHANNEL_<데이터셋 이름>(입력), SM_CHANNEL_BASE_MODEL(베이스 model.pt 폴더)을 자동으로 쓴다.
Spot:      --checkpoint-dir DIR 에 에폭마다 last.pt(모델·optimizer·scheduler·난수 상태·최고 가중치·조기 종료 상태·실행 정보)를 쓰고,
           다시 시작하면 거기서 이어간다. 재개 결과는 연속 학습과 같다 (배치 순서·증강은 에폭으로 정해지고, dropout 난수 상태를 복원한다).
           실행 정보(데이터·분할·stats·설정)가 다르면 다른 실행의 체크포인트로 보고 멈춘다. epochs·patience·num_workers·checkpoint_dir만 바꿀 수 있다.
"""
import json
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import torch

from common.tasks import decode
from common.training_targets import meta_tasks, output_layout, regression_stats, task_signature
from training.config import build_parser, config_from_args, config_to_dict, load_tasks, output_dir_for
from training.dataset import load_rows, make_loader, regression_values, split_people
from training.evaluate import score_grouped
from training.export_onnx import export
from training.losses import combine, measure_initial_losses, prior_thresholds, task_losses
from training.model import SkinModel

CHECKPOINT_NAME, LAST_NAME = "model.pt", "last.pt"
LEGACY_CHECKPOINT_NAMES = ("deploy_model.pt",)  # skin v2 노트북이 저장한 이름 (S3 models/skin_v2/). 폴더를 주면 model.pt 다음으로 찾는다
RESUME_MUTABLE = {"epochs", "patience", "num_workers", "checkpoint_dir"}  # 재개할 때 바꿔도 되는 설정


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
    for key in ("signature", "datasets", "crops", "people", "val_people", "steps", "parent", "stats"):
        if saved_run.get(key) != run[key]:
            raise RuntimeError(f"last.pt는 다른 실행의 체크포인트다 ({key}가 다름: {saved_run.get(key)!r} ≠ {run[key]!r})")
    changed = {k for k, v in run["cfg"].items() if saved_run["cfg"].get(k) != v} - RESUME_MUTABLE
    if changed:
        raise RuntimeError(f"last.pt는 다른 설정의 체크포인트다 (바뀐 설정: {sorted(changed)}; 바꿀 수 있는 것: {sorted(RESUME_MUTABLE)})")


def cpu_state_dict(net):
    return {k: v.detach().cpu().clone() for k, v in net.state_dict().items()}


@torch.no_grad()
def validate(net, loader, rows, tasks, meta_list, device, use_amp, min_groups):
    """검증 인물 → (항목별 지표, 점수). 출력 해석(common.tasks.decode)과 지표(evaluate.score_grouped)는 서빙·평가와 같다."""
    net.eval()
    grouped, truth = defaultdict(list), {}
    for images, _, _, indices in loader:
        with torch.autocast(device, dtype=torch.float16, enabled=use_amp):
            out = net(images.to(device))
        for logits, i in zip(out.float().cpu().numpy(), indices.tolist()):
            row = rows[i]
            key = (row["subject"], row["region"])
            grouped[key].append(decode(logits, meta_list, row["region"]))
            truth[key] = row["targets"]
    return score_grouped(grouped, truth, tasks, min_groups)


def main(argv=None):
    parser = build_parser("skin 모델 학습 (모델·손실·학습률 기본값 = skin v2, 인물 단위 80:20 검증 + 조기 종료)")
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

    all_rows = load_rows(cfg.datasets, tasks, cfg, "train")
    if not all_rows:
        raise RuntimeError("학습 크롭이 없다 (datasets·views·min_skin_px·test_split 확인)")
    rows, val_rows, val_people = split_people(all_rows, cfg.val_fraction, cfg.seed)  # 인물 단위 분할, seed 고정
    people = len({r["subject"] for r in rows})
    init_from = resolve_init_from(cfg)
    print(f"장치 {device} (AMP {'사용' if use_amp else '미사용'}) | 학습 {len(rows):,}크롭 {people:,}명"
          + (f" / 검증 {len(val_rows):,}크롭 {len(val_people):,}명 (val_fraction {cfg.val_fraction}, patience {cfg.patience}, 최대 {cfg.epochs}에폭)"
             if val_rows else f" / 검증 없음 (고정 {cfg.epochs}에폭)")
          + f" | 시작점 {init_from or 'imagenet'} → {model_dir}", flush=True)

    if init_from is None:
        stats = regression_stats(tasks, regression_values(rows, tasks))  # 학습 인물만 (검증 정보 누수 방지)
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
    val_loader = make_loader(val_rows, stats, cfg, tasks, train=False, device=device) if val_rows else None
    meta_list = meta_tasks(tasks, stats)

    run = dict(signature=task_signature(tasks), datasets=[d["name"] for d in cfg.datasets], crops=len(rows),
               people=people, val_people=val_people, steps=steps, parent=parent, stats=stats, cfg=config_to_dict(cfg))
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
    best = dict(score=float("-inf"), epoch=0, state=None, results={})  # 검증 최고점 (val_rows 없으면 쓰지 않음)
    bad_epochs = 0
    if last is not None:
        net.load_state_dict(last["model"])
        optimizer.load_state_dict(last["optimizer"])
        scheduler.load_state_dict(last["scheduler"])
        scaler.load_state_dict(last["scaler"])
        restore_rng(last["rng"])
        start_epoch, history = last["epoch"] + 1, last["history"]
        best, bad_epochs = last.get("best", best), last.get("bad_epochs", 0)
        print(f"  {checkpoint_dir / LAST_NAME}에서 재개: {start_epoch}에폭부터"
              + (f" (최고 {best['epoch']}에폭 {best['score']:.4f}, 정체 {bad_epochs}에폭)" if val_rows else ""), flush=True)

    stopped_early = False
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
        entry = dict(epoch=epoch, train_loss=total / steps, lr_backbone=scheduler.get_last_lr()[0])
        line = f"  epoch {epoch:2d} loss {total / steps:.3f} | lr {scheduler.get_last_lr()[0]:.1e}"
        if val_loader is not None:
            results, score = validate(net, val_loader, val_rows, tasks, meta_list, device, use_amp, cfg.eval_min_groups)
            entry.update(val_score=score, **{f"val_{t}_{tasks[t]['select']}": r[tasks[t]["select"]] for t, r in results.items()})
            improved = score > best["score"]
            if improved:
                best, bad_epochs = dict(score=score, epoch=epoch, state=cpu_state_dict(net), results=results), 0
            else:
                bad_epochs += 1
            summary = "  ".join(f"{t[:9]} {r[tasks[t]['select']]:.3f}" for t, r in results.items())
            line += f" | 검증 {summary} | 점수 {score:.4f}{' ★' if improved else f' (정체 {bad_epochs}/{cfg.patience})'}"
        history.append(entry)
        print(line + f" ({time.time() - started:.0f}초)", flush=True)
        if val_loader is not None and bad_epochs >= cfg.patience:
            stopped_early = True
        if checkpoint_dir:
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            torch.save({"model": net.state_dict(), "optimizer": optimizer.state_dict(),
                        "scheduler": scheduler.state_dict(), "scaler": scaler.state_dict(), "rng": rng_states(),
                        "epoch": epoch, "history": history, "run": run, "best": best, "bad_epochs": bad_epochs},
                       checkpoint_dir / LAST_NAME)
        if stopped_early:
            print(f"  {cfg.patience}에폭 연속 개선 없음 → 조기 종료. 최고 {best['epoch']}에폭 (점수 {best['score']:.4f}) 가중치를 쓴다", flush=True)
            break

    epochs_trained = history[-1]["epoch"] if history else 0
    if val_loader is not None and best["state"] is not None:
        net.load_state_dict(best["state"])
        epochs_trained = best["epoch"]
        if not stopped_early:
            print(f"  최대 에폭 도달. 최고 {best['epoch']}에폭 (점수 {best['score']:.4f}) 가중치를 쓴다", flush=True)

    checkpoint = model_dir / CHECKPOINT_NAME
    torch.save({"model": net.state_dict(), "stats": stats, "l0": l0, "cfg": config_to_dict(cfg), "tasks": tasks,
                "people": people, "crops": len(rows), "datasets": [d["name"] for d in cfg.datasets],
                "val_people": val_people, "val_crops": len(val_rows), "best_epoch": best["epoch"], "best_score": best["score"],
                "val_results": best["results"], "epochs_trained": epochs_trained, "stopped_early": stopped_early,
                "parent": parent, "history": history, "created": datetime.now().isoformat(timespec="seconds")},
               checkpoint)
    (model_dir / "train_history.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 {checkpoint} ({epochs_trained}에폭 가중치)", flush=True)
    if not args.no_export:
        export(checkpoint, model_dir, name=cfg.model_name, opset=cfg.onnx_opset, reference_seed=cfg.reference_seed)


if __name__ == "__main__":
    main()
