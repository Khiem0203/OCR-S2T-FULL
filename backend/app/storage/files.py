"""File tạm."""
import shutil
from pathlib import Path


def remove_tree(path: str | Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
