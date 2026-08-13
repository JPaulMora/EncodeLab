"""ffmpeg / ffprobe helpers for comparison frame extraction."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from app.config import COMPARE_POSITIONS, MEDIA_BASE

log = logging.getLogger(__name__)


def _require_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg not found on PATH")
    return ffmpeg


def _require_ffprobe() -> str:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ValueError("ffprobe not found on PATH")
    return ffprobe


def _parse_fps(rate: str | None) -> float:
    if not rate or rate in {"0/0", "N/A"}:
        return 30.0
    try:
        value = float(Fraction(rate))
        return value if value > 0 else 30.0
    except (ValueError, ZeroDivisionError):
        return 30.0


def probe_video(path: Path) -> dict:
    ffprobe = _require_ffprobe()
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,nb_frames,duration",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=120)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "ffprobe failed").strip()
        raise ValueError(err[:500])

    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise ValueError("No video stream found")

    stream = streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    fps = _parse_fps(stream.get("r_frame_rate"))

    duration = stream.get("duration")
    if duration in (None, "N/A"):
        duration = (data.get("format") or {}).get("duration")
    duration_f = float(duration) if duration not in (None, "N/A") else 0.0

    nb_frames = stream.get("nb_frames")
    frame_count = int(nb_frames) if nb_frames not in (None, "N/A") else None
    if frame_count is None and duration_f > 0 and fps > 0:
        frame_count = int(duration_f * fps)

    return {
        "width": width,
        "height": height,
        "fps": fps,
        "duration": duration_f,
        "frame_count": frame_count,
    }


def extract_frame_at_time(path: Path, time_seconds: float) -> bytes:
    ffmpeg = _require_ffmpeg()
    if time_seconds < 0:
        raise ValueError("time_seconds must be >= 0")

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(time_seconds),
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
    ]
    result = subprocess.run(cmd, capture_output=True, check=False, timeout=180)
    if result.returncode != 0 or not result.stdout:
        err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise ValueError(err[:500] or "ffmpeg failed to extract frame")
    return result.stdout


def extract_comparison_frames(
    job_id: int,
    source_path: Path,
    dest_path: Path,
) -> list[tuple[float, Path, Path]]:
    """Extract PNGs at COMPARE_POSITIONS from source and dest.

    Returns list of (position, source_png_path, dest_png_path).
    """
    source_meta = probe_video(source_path)
    dest_meta = probe_video(dest_path)
    duration = min(source_meta["duration"], dest_meta["duration"])
    if duration <= 0:
        raise ValueError("Could not determine video duration")

    out_dir = MEDIA_BASE / "jobs" / str(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[tuple[float, Path, Path]] = []
    for pos in COMPARE_POSITIONS:
        t = duration * pos
        # Avoid seeking past the last frame
        t = min(t, max(0.0, duration - 0.05))
        label = f"{int(pos * 100):02d}"
        src_png = out_dir / f"source_{label}.png"
        dst_png = out_dir / f"dest_{label}.png"
        src_png.write_bytes(extract_frame_at_time(source_path, t))
        dst_png.write_bytes(extract_frame_at_time(dest_path, t))
        results.append((pos, src_png, dst_png))
        log.info("Extracted comparison frames at %.0f%% for job %s", pos * 100, job_id)

    return results
