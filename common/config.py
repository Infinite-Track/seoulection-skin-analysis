"""configs/*.yaml 로더. 경로는 전부 여기를 통해 얻는다."""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "configs"


def load_yaml(name_or_path: str | Path) -> dict:
    """"serving" 또는 "serving.prod" → configs/serving(.prod).yaml. .yaml로 끝나는 경로는 그대로 (상대면 레포 루트 기준)."""
    p = Path(name_or_path)
    if p.suffix not in (".yaml", ".yml"):
        p = CONFIG_DIR / f"{p}.yaml"
    elif not p.is_absolute():
        p = resolve(p)
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve(path: str | Path) -> Path:
    """상대 경로는 레포 루트 기준으로 해석한다."""
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p

