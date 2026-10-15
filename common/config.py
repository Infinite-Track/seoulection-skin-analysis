"""레포 루트 기준 경로 해석. 설정값 자체는 환경변수에서 읽는다 (serving/settings.py)."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve(path: str | Path) -> Path:
    """상대 경로는 레포 루트 기준으로 해석한다."""
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p
