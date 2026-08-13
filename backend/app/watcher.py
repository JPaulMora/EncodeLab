"""Filesystem watcher for watch/<preset>/ drop folders."""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from app.config import (
    PRESET_MAP,
    SKIP_PREFIXES,
    SKIP_SUFFIXES,
    VIDEO_EXTS,
    WATCH_BASE,
    ensure_dirs,
    human_size,
)
from app.encoder import _encoding_lock, run_encode
from app.ws import manager

log = logging.getLogger(__name__)


def list_watch_files() -> list[dict]:
    files: list[dict] = []
    if not WATCH_BASE.exists():
        return files
    for folder in PRESET_MAP:
        d = WATCH_BASE / folder
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if not f.is_file():
                continue
            if any(f.name.startswith(p) for p in SKIP_PREFIXES):
                continue
            if f.suffix.lower() not in VIDEO_EXTS:
                continue
            size = f.stat().st_size
            files.append(
                {
                    "folder": folder,
                    "name": f.name,
                    "size": size,
                    "size_human": human_size(size),
                }
            )
    return files


async def broadcast_watch_update() -> None:
    await manager.broadcast({"type": "watch_update", "files": list_watch_files()})


def _is_video_candidate(path: Path) -> bool:
    if not path.is_file():
        return False
    if any(path.name.startswith(p) for p in SKIP_PREFIXES):
        return False
    if path.suffix.lower().endswith(SKIP_SUFFIXES):
        return False
    if path.suffix.lower() not in VIDEO_EXTS:
        return False
    return True


async def watch_folders() -> None:
    try:
        from watchfiles import Change, awatch
    except ImportError:
        log.error("watchfiles not installed")
        return

    ensure_dirs()
    log.info("=== encoder-watch started. Watching: %s ===", list(PRESET_MAP.keys()))
    await broadcast_watch_update()

    # Startup scan
    for folder in PRESET_MAP:
        d = WATCH_BASE / folder
        for f in sorted(d.iterdir()):
            if not _is_video_candidate(f):
                continue
            log.info("STARTUP SCAN: %s in [%s]", f.name, folder)
            await broadcast_watch_update()
            async with _encoding_lock:
                if f.exists():
                    await run_encode(folder, f)
            await broadcast_watch_update()

    async for changes in awatch(str(WATCH_BASE)):
        for change_type, path_str in changes:
            if change_type not in (Change.added, Change.modified):
                continue
            path = Path(path_str)
            if not _is_video_candidate(path):
                continue
            try:
                rel = path.relative_to(WATCH_BASE)
            except ValueError:
                continue
            if len(rel.parts) != 2:
                continue
            folder = rel.parts[0]
            if folder not in PRESET_MAP:
                continue

            log.info("DETECTED: %s in [%s]", path.name, folder)
            await broadcast_watch_update()

            async with _encoding_lock:
                if path.exists():
                    orphan = (
                        subprocess.run(
                            ["pgrep", "-x", "HandBrakeCLI"], capture_output=True
                        ).returncode
                        == 0
                    )
                    if orphan:
                        log.warning(
                            "HandBrakeCLI already running — queuing %s", path.name
                        )
                    else:
                        try:
                            await run_encode(folder, path)
                        except Exception as enc_err:
                            log.error("Encode crashed for %s: %s", path.name, enc_err)

            await broadcast_watch_update()


async def broadcast_system_loop() -> None:
    """Push CPU + encode status every 3 seconds."""
    import time

    from app.encoder import get_current_encode_file
    from app.config import OUTPUT_BASE

    def cgroup_usage_usec() -> int:
        try:
            for line in open("/sys/fs/cgroup/cpu.stat"):
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
        except Exception:
            pass
        return 0

    def ncpus() -> float:
        try:
            parts = open("/sys/fs/cgroup/cpu.max").read().split()
            quota, period = int(parts[0]), int(parts[1])
            return quota / period if quota > 0 else 1.0
        except Exception:
            return 1.0

    cpus = ncpus()
    u_prev = cgroup_usage_usec()
    t_prev = time.monotonic()

    while True:
        await asyncio.sleep(3)
        cpu = None
        try:
            u_cur = cgroup_usage_usec()
            t_cur = time.monotonic()
            elapsed_us = (t_cur - t_prev) * 1_000_000
            delta_us = u_cur - u_prev
            if elapsed_us > 0 and u_prev > 0:
                cpu = round(100 * delta_us / (elapsed_us * cpus))
                cpu = max(0, min(100, cpu))
            u_prev, t_prev = u_cur, t_cur
        except Exception:
            cpu = None

        msg: dict = {"type": "system", "cpu_pct": cpu}
        enc_file = get_current_encode_file()
        if enc_file:
            size = 0
            for f in OUTPUT_BASE.rglob(enc_file):
                if f.is_file():
                    size = f.stat().st_size
                    break
            msg.update(
                {
                    "encoding": True,
                    "encoding_file": enc_file,
                    "encoding_size": size,
                    "encoding_size_human": human_size(size),
                }
            )
        await manager.broadcast(msg)
