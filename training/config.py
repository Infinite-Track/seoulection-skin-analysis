"""학습 설정. 우선순위: TrainConfig 기본값(= skin v2 배포 학습 설정) < configs/train.yaml < CLI 인자.

사용:
    parser = build_parser("설명")          # TrainConfig의 모든 스칼라 필드가 --lr-head 같은 옵션으로 생긴다
    parser.add_argument(...)               # 스크립트 고유 인자
    args = parser.parse_args()
    cfg = config_from_args(args)           # 기본값 → yaml → CLI(None이 아닌 것만) 순으로 덮어쓴 TrainConfig

yaml·CLI에서 datasets는 yaml로만 준다 (리스트 안 dict). 모르는 키는 오류로 멈춘다 (오타 방지).
"""
import argparse
import types
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import yaml

from common.config import resolve

DEFAULT_CONFIG = "configs/train.yaml"
DEFAULT_TASKS = "configs/tasks.yaml"
DEFAULT_PATHS = "configs/paths.yaml"
CLI_EXCLUDED = {"datasets"}  # 구조가 있어 CLI로 못 받는 필드
CROPS_FILE, LABELS_FILE = "crops.csv", "labels.csv"  # 표준 데이터셋 파일 이름 (training/dataset.py 맨 위)


@dataclass
class TrainConfig:
    """기본값은 전부 skin v2 배포 학습(face_analysis_AI/modeling/v2/deploy.ipynb의 DEPLOY)과 같다."""

    # ---- 데이터
    datasets: list = field(default_factory=list)  # [{name, dir, test_split?}] — yaml로만
    views: list = field(default_factory=lambda: ["F", "L", "R"])  # 사진 각도
    min_skin_px: int = 20_000       # 피부 픽셀이 이보다 적은 크롭은 제외 (서빙 MIN_SKIN_PX와 같다)
    mirror_right: bool = True       # r_ 부위를 좌우 반전해 l_과 방향을 맞춘다 (서빙도 meta.json으로 같은 값)

    # ---- 모델 (training/model.py SkinModel 인자)
    backbone: str = "convnext_small.fb_in22k_ft_in1k"
    freeze_stages: int = 2          # 백본 stem + 앞 N단계 고정 (0~4)
    head_hidden: int = 256          # 항목별 헤드 은닉층 (0이면 선형 한 층)
    head_act: str = "gelu"
    dropout: float = 0.4
    threshold_init: str = "prior"   # 순서형 경계값 시작: "prior"(학습 데이터 등급 비율의 로짓) | "zero". 처음부터 학습할 때만

    # ---- 학습
    epochs: int = 6                 # 검증 없이 고정 에폭 (5-fold 최고 에폭 중앙값)
    schedule_epochs: int = 15       # 코사인 스케줄 길이 (에폭). epochs보다 길면 중간에 멈추는 것 — v2와 같다
    batch_size: int = 8             # 배치는 같은 부위끼리만
    lr_backbone: float = 2e-5
    lr_head: float = 2e-4           # 헤드 + 순서형 경계값
    weight_decay: float = 0.05      # AdamW, 두 그룹 모두
    l0_batches: int = 50            # 항목별 시작 손실을 잴 배치 수 (손실 정규화 분모). 처음부터 학습할 때만
    aug_rotate: float = 20.0        # 회전 ±도
    aug_scale: float = 0.1          # 배율 ±비율
    aug_bright: float = 0.2         # 밝기 ±비율
    aug_bright_p: float = 0.5       # 밝기 증강 적용 확률
    seed: int = 42
    num_workers: int = 6            # v2 노트북 CFG와 같다 (워커 1개당 RAM 약 0.5GB)
    amp: bool = True                # CUDA에서만 fp16 autocast (다른 장치는 무시)
    device: str = "auto"            # auto(cuda → mps → cpu) | cuda | mps | cpu

    # ---- 시작점·재개
    init_from: str | None = None    # 체크포인트(model.pt) 경로 → 파인튜닝. None이면 ImageNet 사전학습에서 처음부터
    checkpoint_dir: str | None = None  # 에폭마다 last.pt 저장, 있으면 이어서 (Spot 중단 대비). SageMaker: /opt/ml/checkpoints

    # ---- 산출물·평가
    model_name: str = "skin"        # meta.json model.name (v2는 "skin v2"였다). 서빙 modelVersion = "<name> <version>"
    onnx_opset: int = 18
    reference_seed: int = 2026      # reference.npz 입력 난수 seed (v2와 같다)
    eval_min_groups: int = 10       # 평가에서 (사람, 부위) 쌍이 이보다 적은 항목은 지표를 내지 않는다
    tasks_config: str = DEFAULT_TASKS
    paths_config: str = DEFAULT_PATHS


def load_yaml(path):
    with open(resolve(path), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_paths(path=DEFAULT_PATHS):
    return load_yaml(path)


def read_id_list(path):
    """한 줄에 id 하나인 파일 (configs/splits/*.txt). '#' 줄은 주석."""
    p = resolve(path)
    if not p.exists():
        raise FileNotFoundError(f"분할 파일 없음: {p} (scripts/make_splits.py로 만든다)")
    return {line.strip() for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")}


def load_tasks(path=DEFAULT_TASKS):
    """configs/tasks.yaml → (부위 목록, {항목: spec}). 순서형 classes의 등급 목록은 tuple로."""
    cfg = load_yaml(path)
    tasks = {}
    for task, spec in cfg["tasks"].items():
        spec = dict(spec)
        if spec["kind"] == "ordinal":
            spec["classes"] = {str(k): tuple(int(g) for g in v) for k, v in spec["classes"].items()}
        elif spec["kind"] != "regression":
            raise ValueError(f"{task}: kind는 regression|ordinal ({spec['kind']})")
        tasks[task] = spec
    return list(cfg["regions"]), tasks


def _cli_type(annotation):
    """dataclass 필드 타입 → argparse type. str | None 은 str, list 는 원소 타입."""
    if isinstance(annotation, types.UnionType):
        annotation = next(a for a in annotation.__args__ if a is not type(None))
    return annotation


def build_parser(description, config_default=DEFAULT_CONFIG):
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--config", default=config_default, help="학습 yaml (TrainConfig 기본값 위에 덮어쓴다)")
    group = parser.add_argument_group("TrainConfig 덮어쓰기 (주지 않으면 yaml·기본값)")
    for f in fields(TrainConfig):
        if f.name in CLI_EXCLUDED:
            continue
        option, typ = "--" + f.name.replace("_", "-"), _cli_type(f.type)
        if typ is bool:
            group.add_argument(option, action=argparse.BooleanOptionalAction, default=None)
        elif typ is list:
            group.add_argument(option, nargs="+", default=None)
        else:
            group.add_argument(option, type=typ, default=None)
    return parser


def config_from_args(args):
    """기본값 → yaml → CLI 순으로 덮어쓴 TrainConfig."""
    cfg = asdict(TrainConfig())
    known = set(cfg)
    path = resolve(args.config)
    if path.exists():
        overrides = load_yaml(path)
        unknown = set(overrides) - known
        if unknown:
            raise ValueError(f"{path}: TrainConfig에 없는 키 {sorted(unknown)}")
        cfg.update(overrides)
    elif args.config != DEFAULT_CONFIG:
        raise FileNotFoundError(f"설정 파일 없음: {path}")
    for name in known - CLI_EXCLUDED:
        value = getattr(args, name, None)
        if value is not None:
            cfg[name] = value
    return TrainConfig(**cfg)


def config_to_dict(cfg: TrainConfig) -> dict:
    return asdict(cfg)


def config_from_dict(d: dict) -> TrainConfig:
    """체크포인트에 저장된 cfg → TrainConfig. 모르는 키(옛 버전)는 버린다."""
    known = {f.name for f in fields(TrainConfig)}
    return TrainConfig(**{k: v for k, v in d.items() if k in known})


def output_dir_for(cfg: TrainConfig, *parts) -> Path:
    """<output_root>/<parts...> (configs/paths.yaml)."""
    return resolve(load_paths(cfg.paths_config)["output_root"]).joinpath(*parts)
