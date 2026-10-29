"""손실 (skin v2와 같다).

항목별: 회귀 SmoothL1 (표준화된 타깃) / 순서형 CORAL — "등급 > k" 질문별 BCE의 평균
결합:   항목마다 학습 전 시작 손실(l0)로 나눠 크기를 맞춘 뒤 평균 → 단위가 다른 6항목이 같은 비중으로 시작한다
"""
from collections import defaultdict

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from common.training_targets import grade_to_class

SMOOTH_L1 = nn.SmoothL1Loss()  # 이상치(모공 2000개 같은 경우)에 덜 휘둘린다


def task_losses(out, target, valid, tasks, layout):
    """항목별 손실 {항목: 값}. 정답이 있는 칸만 쓰고, 정답이 하나도 없는 항목은 빠진다."""
    losses = {}
    for t, (task, spec) in enumerate(tasks.items()):
        mask = valid[:, t] > 0
        if not mask.any():
            continue
        logits, y = out[mask][:, layout[task]].float(), target[mask, t]
        if spec["kind"] == "ordinal":
            # 정답 클래스 2 → [1, 1, 0, 0, ...] ("등급 > 0"? 예, "> 1"? 예, "> 2"? 아니오 ...)
            levels = (y[:, None] > torch.arange(logits.shape[1], device=logits.device)).float()
            losses[task] = F.binary_cross_entropy_with_logits(logits, levels)
        else:
            losses[task] = SMOOTH_L1(logits[:, 0], y)
    return losses


def combine(losses, l0):
    """항목마다 시작 손실로 나눠 크기를 맞춘 뒤 평균."""
    return torch.stack([loss / l0[task] for task, loss in losses.items()]).mean()


@torch.no_grad()
def measure_initial_losses(net, loader, batches, tasks, layout, device, use_amp):
    """학습 전 모델(eval 모드)의 항목별 평균 손실. 이 값으로 나눠 항목끼리 크기를 맞춘다."""
    net.eval()
    sums, counts = defaultdict(float), defaultdict(int)
    for b, (images, targets, valid, _) in enumerate(loader):
        if b >= batches:
            break
        with torch.autocast(device, dtype=torch.float16, enabled=use_amp):
            out = net(images.to(device))
        for task, loss in task_losses(out, targets.to(device), valid.to(device), tasks, layout).items():
            sums[task] += loss.item()
            counts[task] += 1
    missing = [t for t in tasks if not counts[t]]
    if missing:
        raise RuntimeError(f"{batches}배치 안에 정답이 없는 항목 {missing} → l0_batches를 늘릴 것")
    return {task: sums[task] / counts[task] for task in tasks}


def prior_thresholds(rows, tasks, eps=1e-3):
    """순서형 경계값 시작값 = 학습 데이터에서 "등급 > k"인 비율의 로짓 (등급 비율대로 예측하는 상태에서 출발)."""
    init = {}
    for task, spec in tasks.items():
        if spec["kind"] != "ordinal":
            continue
        classes = np.array([grade_to_class(spec, r["targets"][task]) for r in rows if task in r["targets"]])
        if not len(classes):
            continue
        above = np.clip([(classes > k).mean() for k in range(len(spec["classes"]) - 1)], eps, 1 - eps)
        init[task] = np.log(above / (1 - above)).tolist()
    return init
