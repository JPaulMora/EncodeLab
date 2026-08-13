"""Path and runtime configuration."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ENCODER_DATA_DIR", BASE_DIR / "data")).resolve()

WATCH_BASE = DATA_DIR / "watch"
OUTPUT_BASE = DATA_DIR / "output"
MEDIA_BASE = DATA_DIR / "media"
UPLOAD_TMP = DATA_DIR / "uploads"
LOG_FILE = DATA_DIR / "logs" / "encoder.log"
DB_PATH = DATA_DIR / "encoder.db"

PRESET_JSON = Path(
    os.environ.get("HANDBRAKE_PRESET_JSON", BASE_DIR.parent / "config" / "presets" / "Super8Scan.json")
)

# HandBrakeCLI binary (in-container or host PATH). Override if needed.
HANDBRAKE_BIN = os.environ.get("HANDBRAKE_BIN", "HandBrakeCLI")

PRESET_MAP: dict[str, tuple[str, str, list[str]]] = {
    # folder: (HandBrake preset name, output format, extra args)
    "fast-1080": ("General/Fast 1080p30", "mp4", []),
    "fast-720": ("General/Fast 720p30", "mp4", []),
    "whatsapp-720": ("General/Very Fast 720p30", "mp4", ["-q", "27"]),
    "whatsapp-480": ("General/Very Fast 480p30", "mp4", ["-q", "28"]),
    "super8": ("Super 8 Scan", "mkv", []),
    "hq-1080": ("General/HQ 1080p30", "mp4", []),
    "hevc-1080-mp4": ("H.265 MKV 1080p30", "mp4", []),
    "hevc-720-mp4": ("H.265 MKV 720p30", "mp4", []),
    "hevc-1080-mkv": ("H.265 MKV 1080p30", "mkv", []),
}

VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".wmv",
    ".flv",
    ".webm",
    ".ts",
    ".mts",
    ".m2ts",
}
SKIP_PREFIXES = ("._", ".DS_Store", "._.DS_Store")
SKIP_SUFFIXES = (".part",)

# Timeline sample points for comparison frames
COMPARE_POSITIONS = (0.25, 0.50, 0.75)


def ensure_dirs() -> None:
    for path in (WATCH_BASE, OUTPUT_BASE, MEDIA_BASE, UPLOAD_TMP, LOG_FILE.parent):
        path.mkdir(parents=True, exist_ok=True)
    for folder in PRESET_MAP:
        (WATCH_BASE / folder).mkdir(parents=True, exist_ok=True)
        (OUTPUT_BASE / folder).mkdir(parents=True, exist_ok=True)


def human_size(size: int | float) -> str:
    n = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"
