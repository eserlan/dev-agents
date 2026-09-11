"""Retention helpers for bounded workflow artifacts."""

from __future__ import annotations

import shutil
import time
from pathlib import Path


def remove_older_than(directory: Path, pattern: str, age_seconds: float, *, directories: bool = False) -> int:
    """Remove matching files/directories older than the age threshold."""
    if not directory.is_dir():
        return 0
    cutoff = time.time() - age_seconds
    removed = 0
    for path in directory.glob(pattern):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            if directories and path.is_dir():
                shutil.rmtree(path)
            elif not directories and path.is_file():
                path.unlink()
            else:
                continue
            removed += 1
        except FileNotFoundError:
            continue
    return removed
