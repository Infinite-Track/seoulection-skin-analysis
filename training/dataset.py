"""표준 데이터셋 폴더 → 학습/테스트 행, Dataset, 부위별 배치.

표준 데이터셋 형식 (모든 데이터 소스 공통. Phase 2 build_crops.py가 만든다):
  <dir>/crops.csv   사진 1장 × 부위 6 = 6행
                    image_id, person_id, device, view, region, status, file, width, height, skin_px, face_h
                    status: ok | hidden_view | low_res | no_face | empty | read_error (ok만 학습에 쓴다)
                    file: <dir> 기준 상대 경로 (RGBA PNG, common/crop_files.py), face_h: 얼굴 세로 길이(px)
  <dir>/labels.csv  person_id, region, task, value   (원래 단위. 등급은 원래 등급, 개수·Ra·나이는 측정값)
  <dir>/crops/...   크롭 PNG

SageMaker에서는 데이터셋 이름의 채널 SM_CHANNEL_<NAME 대문자>가 있으면 dir 대신 그 경로를 쓴다.

전처리는 전부 common에서 가져온다 (크롭 로드 common/crop_files, 반전·캔버스·정규화 common/transforms).
학습 때만 증강이 들어가는데, 증강은 반전 **앞**에 적용한다. skin v2 노트북은 반전 뒤에 적용했지만
회전각 분포가 대칭이고 배율·밝기는 반전과 무관해서 두 순서의 입력 분포는 같다 — 이렇게 하면 서빙과 같은
to_model_input을 그대로 호출할 수 있다.

증강 난수는 (seed, 에폭, 행 번호)로 정해진다 (노트북은 워커별 난수 스트림). 분포는 같고, 워커 수와 무관하게
같은 에폭·행이면 같은 증강이 나와서 중단 후 재개(train.py --checkpoint-dir)가 연속 학습과 같아진다.
그래서 RegionBatchSampler는 (에폭, 행 번호) 쌍을 낸다.
"""
import csv
import os
import random
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset, Sampler

from common.config import resolve
from common.crop_files import load_region_png
from common.training_targets import encode_target
from common.transforms import to_model_input
from training.config import CROPS_FILE, LABELS_FILE, TrainConfig, read_id_list  # noqa: F401 (re-export)


def dataset_dir(ds):
    """SageMaker 채널이 있으면 그 경로, 없으면 config의 dir."""
    return Path(os.environ.get(f"SM_CHANNEL_{ds['name'].upper()}") or resolve(ds["dir"]))


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_rows(datasets, tasks, cfg: TrainConfig, part):
    """크롭 목록 + 정답. part="train"이면 test_split 사람을 빼고, "test"면 test_split 사람만.

    행: subject("데이터셋/person_id"), person_id, region, view, device, file(절대 경로), face_h,
        targets{항목: 원래 단위 값} — 그 부위에 정의된 항목만
    """
    rows = []
    for ds in datasets:
        root = dataset_dir(ds)
        test = read_id_list(ds["test_split"]) if ds.get("test_split") else set()
        if part == "test" and not test:
            continue
        targets = defaultdict(dict)
        for r in _read_csv(root / LABELS_FILE):
            if r["task"] in tasks and r["region"] in tasks[r["task"]]["regions"] and r["value"] != "":
                targets[(r["person_id"], r["region"])][r["task"]] = float(r["value"])
        for r in _read_csv(root / CROPS_FILE):
            in_test = r["person_id"] in test
            if (part == "test") != in_test or r["status"] != "ok" or r["view"] not in cfg.views:
                continue
            if int(r["skin_px"]) < cfg.min_skin_px:
                continue
            t = targets.get((r["person_id"], r["region"]))
            if t:
                rows.append(dict(subject=f"{ds['name']}/{r['person_id']}", person_id=r["person_id"],
                                 region=r["region"], view=r["view"], device=r["device"], file=str(root / r["file"]),
                                 face_h=float(r["face_h"]), targets=t))
    return rows


def split_people(rows, fraction, seed):
    """학습 행을 인물 단위로 (학습, 검증, 검증 인물 목록)으로 나눈다. 같은 사람의 사진은 정답이 같아 사진 단위로 나누면 점수가 부풀려진다.

    fraction 0이면 전부 학습. 인물 목록을 정렬한 뒤 random.Random(seed)로 뽑으므로 같은 데이터·seed면 같은 분할이다.
    """
    if fraction <= 0:
        return rows, [], []
    people = sorted({r["subject"] for r in rows})
    n_val = max(1, round(len(people) * fraction))
    if n_val >= len(people):
        raise RuntimeError(f"검증 분할 불가: 인물 {len(people)}명에서 {n_val}명을 떼면 학습 인물이 없다 (--val-fraction 0 또는 데이터 추가)")
    val = set(random.Random(seed).sample(people, n_val))
    return [r for r in rows if r["subject"] not in val], [r for r in rows if r["subject"] in val], sorted(val)


def regression_values(rows, tasks):
    """회귀 항목별 원래 단위 값 목록 (크롭 행 단위) → common.training_targets.regression_stats 입력."""
    values = defaultdict(list)
    for row in rows:
        for task, value in row["targets"].items():
            if tasks[task]["kind"] == "regression":
                values[task].append(value)
    return values


class RegionDataset(Dataset):
    """크롭 → 이미지 텐서 (3,H,W), 정답 (항목 수,), 정답 유무 (항목 수,), 행 번호"""

    def __init__(self, rows, stats, cfg: TrainConfig, tasks, train=False):
        self.rows, self.stats, self.cfg, self.tasks, self.train = rows, stats, cfg, tasks, train

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, item):
        """item: 행 번호 i 또는 (에폭, i). 에폭은 증강 난수에만 쓴다."""
        epoch, i = item if isinstance(item, tuple) else (0, int(item))
        row = self.rows[i]
        crop, mask = load_region_png(row["file"])
        if self.train:
            crop, mask = self.augment(crop, mask, np.random.default_rng([self.cfg.seed, epoch, i]))
        image = to_model_input(crop, mask, row["face_h"], row["region"], mirror_right=self.cfg.mirror_right)
        targets, valid = torch.zeros(len(self.tasks)), torch.zeros(len(self.tasks))
        for t, (task, spec) in enumerate(self.tasks.items()):
            if task in row["targets"]:
                targets[t] = encode_target(spec, row["targets"][task], self.stats.get(task))
                valid[t] = 1
        return torch.from_numpy(image), targets, valid, i

    def augment(self, crop, mask, rng):
        """원본 해상도 크롭에 회전·배율·밝기 (skin v2와 같은 값·방법). rng: numpy Generator."""
        cfg = self.cfg
        h, w = crop.shape[:2]
        angle = rng.uniform(-cfg.aug_rotate, cfg.aug_rotate)
        scale = rng.uniform(1 - cfg.aug_scale, 1 + cfg.aug_scale)
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, scale)
        crop = cv2.warpAffine(crop, matrix, (w, h), flags=cv2.INTER_LINEAR)
        mask = cv2.warpAffine(mask, matrix, (w, h), flags=cv2.INTER_NEAREST)
        if cfg.aug_bright and rng.random() < cfg.aug_bright_p:
            factor = rng.uniform(1 - cfg.aug_bright, 1 + cfg.aug_bright)
            crop = np.clip(crop * factor, 0, 255).astype(np.uint8)
        return crop, mask


class RegionBatchSampler(Sampler):
    """한 배치를 같은 부위끼리만 묶는다 (부위마다 캔버스 크기가 달라 섞으면 텐서 하나로 못 담는다).
    배치 순서는 섞어서 한쪽 부위에 쏠리지 않게 한다. 섞는 순서는 seed + epoch로 정해지며, 에폭은 set_epoch로
    밖에서 지정한다 (재개 시 같은 에폭이면 같은 순서). 배치 원소는 (에폭, 행 번호)."""

    def __init__(self, rows, batch_size, shuffle, drop_last, seed):
        self.by_region = defaultdict(list)
        for i, row in enumerate(rows):
            self.by_region[row["region"]].append(i)
        self.batch_size, self.shuffle, self.drop_last, self.seed = batch_size, shuffle, drop_last, seed
        self.epoch = 0

    def set_epoch(self, epoch):
        self.epoch = epoch

    def __iter__(self):
        rng = np.random.default_rng(self.seed + self.epoch)
        batches = []
        for indices in self.by_region.values():
            indices = list(indices)
            if self.shuffle:
                rng.shuffle(indices)
            for start in range(0, len(indices), self.batch_size):
                batch = indices[start:start + self.batch_size]
                if len(batch) == self.batch_size or not self.drop_last:
                    batches.append([(self.epoch, i) for i in batch])
        if self.shuffle:
            rng.shuffle(batches)
        return iter(batches)

    def __len__(self):
        return sum(len(v) // self.batch_size if self.drop_last else int(np.ceil(len(v) / self.batch_size))
                   for v in self.by_region.values())


def make_loader(rows, stats, cfg: TrainConfig, tasks, train, device="cpu"):
    """DataLoader. 워커 base_seed 추출에 쓰는 generator를 따로 준다 — 없으면 iterator를 만들 때마다 전역 torch RNG를 소비해서
    (persistent 워커를 유지하는 연속 학습 vs 새 프로세스 재개) dropout 난수가 어긋난다. 증강·배치 순서는 이 generator와 무관하다."""
    dataset = RegionDataset(rows, stats, cfg, tasks, train=train)
    sampler = RegionBatchSampler(rows, cfg.batch_size, shuffle=train, drop_last=train, seed=cfg.seed)
    generator = torch.Generator().manual_seed(cfg.seed)
    return DataLoader(dataset, batch_sampler=sampler, num_workers=cfg.num_workers, generator=generator,
                      persistent_workers=cfg.num_workers > 0, pin_memory=device == "cuda")
