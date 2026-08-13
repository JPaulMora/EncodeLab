"""Safe filesystem helpers for path containment."""
from __future__ import annotations

from pathlib import Path


def is_under(path: Path, base: Path) -> bool:
    """True if path is under base without resolving symlinks on path itself."""
    try:
        abs_path = path if path.is_absolute() else path.absolute()
        abs_base = base.resolve()
        abs_path.relative_to(abs_base)
        return True
    except ValueError:
        return False
