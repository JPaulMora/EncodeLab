"""Safe filesystem helpers for watch tickets and path containment."""
from __future__ import annotations

from pathlib import Path


def is_under(path: Path, base: Path) -> bool:
    """True if path is under base without resolving symlinks on path itself.

    Uses path's parent chain / absolute form relative to base.resolve(),
    but does not follow the final symlink target.
    """
    try:
        # Resolve only the base; for path, resolve parents but keep name
        # so a symlink under watch/ is still "under" watch.
        abs_path = path if path.is_absolute() else path.absolute()
        abs_base = base.resolve()
        # Walk parents: check absolute path string prefix carefully
        abs_path.relative_to(abs_base)
        return True
    except ValueError:
        return False


def unlink_watch_ticket(ticket: Path, watch_base: Path) -> bool:
    """Unlink a watch ticket without following the symlink target.

    Returns True if unlinked (or already missing), False if path escapes watch_base.
    """
    if not is_under(ticket, watch_base):
        return False
    if ticket.is_symlink() or ticket.exists():
        # Path.unlink follows nothing for the unlink syscall on the link itself
        ticket.unlink(missing_ok=True)
    return True


def ticket_name_for_job(job_id: int, source_suffix: str) -> str:
    return f"{job_id}{source_suffix}"


def parse_job_id_from_ticket(name: str) -> int | None:
    """Parse leading integer job id from watch ticket filename like '42.mov'."""
    stem = Path(name).stem
    # stem may be "42" or "42.something" — ticket is {job_id}{suffix} so stem is job_id
    # if suffix is .mp4, name is "42.mp4", stem is "42"
    digits = ""
    for ch in Path(name).name:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None
