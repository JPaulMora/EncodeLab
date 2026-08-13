"""ffmpeg / ffprobe helpers for comparison frame extraction and preview clips."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from collections import Counter
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np

from app.config import (
    COMPARE_POSITIONS,
    MEDIA_BASE,
    PREVIEW_LEAD_IN,
    PREVIEW_PAD_FRAMES,
    PREVIEW_WARMUP,
    PREVIEW_WINDOW,
    SCENE_CHANGE_MIN_MSE,
    SCENE_CHANGE_RATIO,
)

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


def extract_frame_at_index(path: Path, frame_index: int, fps: float | None = None) -> bytes:
    """Extract a single frame by approximate frame index (via timestamp)."""
    if frame_index < 0:
        raise ValueError("frame_index must be >= 0")
    meta = probe_video(path) if fps is None else None
    rate = fps if fps is not None else (meta["fps"] if meta else 30.0)
    if rate <= 0:
        rate = 30.0
    t = frame_index / rate
    return extract_frame_at_time(path, t)


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


def preview_window_times(duration: float) -> tuple[float, float, float]:
    """Return (hb_start, hb_length, usable_start_abs) for preview around 25%.

    hb_start/length are HandBrake --start-at / --stop-at duration args.
    usable_start_abs is absolute time where usable dest content begins
    (after warmup).
    """
    if duration <= 0:
        raise ValueError("Could not determine video duration")
    t = 0.25 * duration
    lead = min(PREVIEW_LEAD_IN, t)
    hb_start = max(0.0, t - lead)
    remaining = max(0.1, duration - hb_start)
    hb_length = min(PREVIEW_WINDOW, remaining)
    usable_start_abs = hb_start + min(PREVIEW_WARMUP, hb_length * 0.25)
    return hb_start, hb_length, usable_start_abs


def cut_source_clip(
    source_path: Path,
    out_path: Path,
    start: float,
    length: float,
) -> Path:
    """Cut a source clip with ffmpeg (copy when possible, else re-encode lightly)."""
    ffmpeg = _require_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(start),
        "-i",
        str(source_path),
        "-t",
        str(length),
        "-c",
        "copy",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, check=False, timeout=300)
    if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        # Fallback re-encode
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(start),
            "-i",
            str(source_path),
            "-t",
            str(length),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            str(out_path.with_suffix(".mp4")),
        ]
        out_path = out_path.with_suffix(".mp4")
        result = subprocess.run(cmd, capture_output=True, check=False, timeout=600)
        if result.returncode != 0 or not out_path.exists():
            err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            raise ValueError(err[:500] or "ffmpeg failed to cut source clip")
    return out_path


def _to_gray_small(png_bytes: bytes, width: int = 320) -> np.ndarray:
    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode frame for alignment")
    h, w = img.shape[:2]
    if w > width:
        nh = max(1, int(h * (width / w)))
        img = cv2.resize(img, (width, nh), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def compute_frame_offset(
    source_clip: Path,
    dest_clip: Path,
    *,
    pad_frames: int = PREVIEW_PAD_FRAMES,
    warmup_seconds: float = PREVIEW_WARMUP,
) -> tuple[int, float | None]:
    """Vote for a constant frame offset (source = dest + offset) using MSE.

    Returns (offset, confidence) where confidence is margin vs second-best
    (higher is better). Returns (0, None) if unaligned.
    """
    src_meta = probe_video(source_clip)
    dst_meta = probe_video(dest_clip)
    src_fps = src_meta["fps"] or 30.0
    dst_fps = dst_meta["fps"] or 30.0
    dst_dur = dst_meta["duration"]
    src_dur = src_meta["duration"]

    usable_start = min(warmup_seconds, max(0.0, dst_dur * 0.2))
    usable_dur = max(0.1, dst_dur - usable_start)
    probe_fracs = (0.25, 0.50, 0.75)

    votes: list[int] = []
    margins: list[float] = []

    for frac in probe_fracs:
        dest_t = usable_start + usable_dur * frac
        dest_idx = int(dest_t * dst_fps)
        try:
            dest_png = extract_frame_at_time(dest_clip, dest_t)
            dest_g = _to_gray_small(dest_png)
        except Exception as exc:
            log.warning("Align probe dest failed: %s", exc)
            continue

        # Map dest time into source clip: source clip includes pad before usable
        # Source clip starts pad_frames before usable_start_abs; dest usable starts
        # at warmup into dest clip. Approximate: source index ≈ dest_idx + pad
        # when offset=0, then search offset in [-pad, +pad].
        best_off = 0
        best_mse = float("inf")
        second_mse = float("inf")
        for off in range(-pad_frames, pad_frames + 1):
            src_idx = dest_idx + pad_frames + off
            src_t = src_idx / src_fps
            if src_t < 0 or src_t >= src_dur:
                continue
            try:
                src_png = extract_frame_at_time(source_clip, src_t)
                src_g = _to_gray_small(src_png)
            except Exception:
                continue
            # Resize to match
            if src_g.shape != dest_g.shape:
                src_g = cv2.resize(
                    src_g, (dest_g.shape[1], dest_g.shape[0]), interpolation=cv2.INTER_AREA
                )
            mse = float(np.mean((src_g.astype(np.float64) - dest_g.astype(np.float64)) ** 2))
            if mse < best_mse:
                second_mse = best_mse
                best_mse = mse
                best_off = off
            elif mse < second_mse:
                second_mse = mse

        if best_mse < float("inf"):
            votes.append(best_off)
            margins.append(second_mse - best_mse if second_mse < float("inf") else best_mse)

    if not votes:
        return 0, None

    counts = Counter(votes)
    # Prefer offset with most votes; tie-break by closest to 0
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], abs(kv[0])))
    winner, win_count = ranked[0]
    if win_count < 2 and len(votes) >= 3:
        # probes disagree
        return 0, None

    conf = float(np.mean(margins)) if margins else None
    return winner, conf


def preview_meta(source_clip: Path, dest_clip: Path) -> dict:
    src = probe_video(source_clip)
    dst = probe_video(dest_clip)
    dst_fps = dst["fps"] or 30.0
    usable_start = min(PREVIEW_WARMUP, max(0.0, dst["duration"] * 0.2))
    usable_frames = max(1, int((dst["duration"] - usable_start) * dst_fps))
    return {
        "source_fps": src["fps"],
        "dest_fps": dst_fps,
        "source_duration": src["duration"],
        "dest_duration": dst["duration"],
        "usable_start_seconds": usable_start,
        "usable_frame_count": usable_frames,
        "pad_frames": PREVIEW_PAD_FRAMES,
    }


def _gray_small_bgr(img: np.ndarray, width: int = 160) -> np.ndarray:
    h, w = img.shape[:2]
    if w > width:
        nh = max(1, int(h * (width / w)))
        img = cv2.resize(img, (width, nh), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _detect_scene_cuts(
    consecutive_mse: list[float],
    *,
    ratio: float = SCENE_CHANGE_RATIO,
    min_mse: float = SCENE_CHANGE_MIN_MSE,
) -> list[int]:
    """Return dest frame indices where a new scene begins (cut before this index)."""
    finite = [v for v in consecutive_mse if v == v]  # not NaN
    if len(finite) < 3:
        return []
    median = float(np.median(finite))
    threshold = max(min_mse, median * ratio)
    cuts: list[int] = []
    # consecutive_mse[i] is diff between dest frame i and i-1 → cut at i
    for i, v in enumerate(consecutive_mse):
        if i == 0:
            continue
        if v == v and v >= threshold:
            cuts.append(i)
    return cuts


def _selection_window(
    n: int, best_index: int, scene_cuts: list[int]
) -> tuple[int, int]:
    """Inclusive [start, end] segment containing best_index, bounded by scene cuts."""
    bounds = [0, *scene_cuts, n]
    for a, b in zip(bounds, bounds[1:]):
        # segment is [a, b)
        if a <= best_index < b:
            return a, b - 1
    return 0, max(0, n - 1)


def compute_match_noise_graph(
    source_clip: Path,
    dest_clip: Path,
    *,
    pad_frames: int = PREVIEW_PAD_FRAMES,
    warmup_seconds: float = PREVIEW_WARMUP,
) -> dict:
    """Compare one source-center frame to every usable dest frame.

    Also detects scene cuts on the dest timeline (big consecutive-frame jumps).
    Best match / suggested offset are chosen only inside the scene segment that
    contains the lowest-noise frame, so a later scene change does not dominate
    the chart or the lock.

    Pairing used by preview-frame:
        source_clip_index = pad + dest_index + offset
    with offset = source_center_index - pad - best_index
    (source clip starts `pad` frames before dest usable content).
    """
    t0 = time.perf_counter()
    log.info("match-noise: probe %s / %s", source_clip.name, dest_clip.name)
    src_meta = probe_video(source_clip)
    dst_meta = probe_video(dest_clip)
    src_fps = src_meta["fps"] or 30.0
    dst_fps = dst_meta["fps"] or 30.0
    src_dur = src_meta["duration"]
    dst_dur = dst_meta["duration"]

    usable_start = min(warmup_seconds, max(0.0, dst_dur * 0.2))
    usable_frames = max(1, int((dst_dur - usable_start) * dst_fps))

    src_frame_count = src_meta["frame_count"] or int(src_dur * src_fps) or 1
    source_center_idx = max(0, (src_frame_count - 1) // 2)
    source_center_t = source_center_idx / src_fps

    log.info(
        "match-noise: usable_frames=%s start=%.2fs center_idx=%s (probe %.1fs)",
        usable_frames,
        usable_start,
        source_center_idx,
        time.perf_counter() - t0,
    )

    t_center = time.perf_counter()
    src_png = extract_frame_at_time(source_clip, source_center_t)
    arr = np.frombuffer(src_png, dtype=np.uint8)
    src_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if src_bgr is None:
        raise ValueError("Could not decode source center frame")
    src_g = _gray_small_bgr(src_bgr)
    log.info("match-noise: source center extracted (%.1fs)", time.perf_counter() - t_center)

    values: list[float] = []
    consecutive: list[float] = []
    prev_g: np.ndarray | None = None

    cap = cv2.VideoCapture(str(dest_clip))
    if not cap.isOpened():
        raise ValueError("Could not open dest clip for noise graph")

    t_loop = time.perf_counter()
    progress_every = max(1, usable_frames // 10)
    try:
        start_frame = int(usable_start * dst_fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        for i in range(usable_frames):
            ok, frame = cap.read()
            if not ok or frame is None:
                dest_t = usable_start + (i / dst_fps)
                try:
                    png = extract_frame_at_time(dest_clip, dest_t)
                    arr = np.frombuffer(png, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                except Exception:
                    values.append(float("nan"))
                    consecutive.append(0.0 if i == 0 else float("nan"))
                    prev_g = None
                    continue
                if frame is None:
                    values.append(float("nan"))
                    consecutive.append(0.0 if i == 0 else float("nan"))
                    prev_g = None
                    continue

            dest_g = _gray_small_bgr(frame)
            if i == 0 or prev_g is None:
                consecutive.append(0.0)
            else:
                pg = prev_g
                if pg.shape != dest_g.shape:
                    pg = cv2.resize(
                        pg, (dest_g.shape[1], dest_g.shape[0]), interpolation=cv2.INTER_AREA
                    )
                consecutive.append(
                    float(np.mean((pg.astype(np.float64) - dest_g.astype(np.float64)) ** 2))
                )

            g = dest_g
            if g.shape != src_g.shape:
                g = cv2.resize(
                    g, (src_g.shape[1], src_g.shape[0]), interpolation=cv2.INTER_AREA
                )
            mse = float(np.mean((src_g.astype(np.float64) - g.astype(np.float64)) ** 2))
            values.append(mse)
            prev_g = dest_g

            if (i + 1) % progress_every == 0 or i + 1 == usable_frames:
                elapsed = time.perf_counter() - t_loop
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                remaining = usable_frames - (i + 1)
                eta = remaining / rate if rate > 0 else 0
                log.info(
                    "match-noise: %s/%s frames (%.0f fps, ~%.0fs left)",
                    i + 1,
                    usable_frames,
                    rate,
                    eta,
                )
    finally:
        cap.release()

    if not values:
        raise ValueError("Could not compute noise graph")

    scene_cuts = _detect_scene_cuts(consecutive)

    # Global min first, then re-pick best inside its scene segment
    finite_all = [(i, v) for i, v in enumerate(values) if v == v]
    if not finite_all:
        raise ValueError("Could not compute noise graph")
    seed_best = min(finite_all, key=lambda iv: iv[1])[0]
    sel_start, sel_end = _selection_window(len(values), seed_best, scene_cuts)

    finite_sel = [
        (i, v) for i, v in enumerate(values) if sel_start <= i <= sel_end and v == v
    ]
    best_index, best_mse = min(finite_sel, key=lambda iv: iv[1])

    # source_clip_index = pad + dest_index + offset
    suggested_offset = int(source_center_idx - pad_frames - best_index)

    sel_vals = [v for _, v in finite_sel]
    scale_max = float(max(sel_vals)) if sel_vals else float(best_mse)

    log.info(
        "match-noise: done best=%s mse=%.1f offset=%s cuts=%s window=[%s,%s] (%.1fs total)",
        best_index,
        best_mse,
        suggested_offset,
        scene_cuts,
        sel_start,
        sel_end,
        time.perf_counter() - t0,
    )

    return {
        "values": values,
        "frame_count": len(values),
        "best_index": best_index,
        "best_mse": best_mse,
        "source_center_index": source_center_idx,
        "suggested_offset": suggested_offset,
        "pad_frames": pad_frames,
        "scene_cuts": scene_cuts,
        "selection_start": sel_start,
        "selection_end": sel_end,
        "scale_max": scale_max,
        "source_frame_count": src_frame_count,
    }


def resolve_preview_pair(
    *,
    source_clip: Path,
    dest_clip: Path,
    index: int,
    offset: int,
    full_source: Path | None = None,
    source_clip_start: float | None = None,
) -> dict:
    """Extract dest[index] paired with source[pad + index + offset].

    The source clip is cut with `pad` frames before dest usable content, so
    offset≈0 means temporally aligned. Large offsets are residual drift
    (missing frames at start/end).

    Prefer the short source clip whenever the index is in range — stream-copy
    cuts often start on an earlier keyframe, so mapping via clip_start on the
    full file would pick the wrong frame. Only seek the original when OOB.
    """
    meta = preview_meta(source_clip, dest_clip)
    dst_fps = meta["dest_fps"] or 30.0
    src_fps = meta["source_fps"] or 30.0
    usable_start = meta["usable_start_seconds"]
    pad = int(meta["pad_frames"])
    n = int(meta["usable_frame_count"])
    index = max(0, min(index, max(0, n - 1)))

    dest_t = usable_start + (index / dst_fps)
    dest_png = extract_frame_at_time(dest_clip, dest_t)

    src_meta = probe_video(source_clip)
    src_frames = src_meta["frame_count"] or int((src_meta["duration"] or 0) * src_fps) or 1
    raw_src_idx = pad + index + offset
    source_oob = False
    source_shortfall = 0
    src_idx = raw_src_idx

    if raw_src_idx < 0:
        source_oob = True
        source_shortfall = raw_src_idx  # negative = missing at start
        src_idx = 0
    elif raw_src_idx >= src_frames:
        source_oob = True
        source_shortfall = raw_src_idx - (src_frames - 1)  # positive = past end
        src_idx = src_frames - 1

    source_png: bytes
    used_full = False
    if (
        source_oob
        and full_source
        and full_source.exists()
        and source_clip_start is not None
    ):
        # Map padded clip index onto the original timeline
        abs_t = source_clip_start + (raw_src_idx / src_fps)
        full_meta = probe_video(full_source)
        full_dur = full_meta["duration"] or 0.0
        full_fps = full_meta["fps"] or src_fps
        if 0 <= abs_t < full_dur:
            source_png = extract_frame_at_time(full_source, abs_t)
            used_full = True
            source_oob = False
            source_shortfall = 0
            src_idx = raw_src_idx
        else:
            clamped_t = min(max(0.0, abs_t), max(0.0, full_dur - 0.05))
            source_png = extract_frame_at_time(full_source, clamped_t)
            used_full = True
            source_oob = True
            if abs_t < 0:
                source_shortfall = int(abs_t * full_fps)
            else:
                source_shortfall = int((abs_t - full_dur) * full_fps)
    else:
        source_png = extract_frame_at_time(source_clip, src_idx / src_fps)

    return {
        "index": index,
        "offset": offset,
        "source_index": src_idx,
        "raw_source_index": raw_src_idx,
        "usable_frame_count": n,
        "source_frame_count": src_frames,
        "pad_frames": pad,
        "source_oob": source_oob,
        "source_shortfall": source_shortfall,
        "used_full_source": used_full,
        "source": source_png,
        "dest": dest_png,
    }


def compute_preview_noise_score(
    *,
    source_clip: Path,
    dest_clip: Path,
    offset: int,
    full_source: Path | None = None,
    source_clip_start: float | None = None,
    max_frames: int | None = None,
) -> dict:
    """SSIM / PSNR / MSE mean±std over usable dest frames at a locked offset.

    Skips pairs where source is OOB (clamped). Optionally caps frame count
    via max_frames (evenly subsampled) for very long clips.
    """
    from app.diff import compare_png_bytes

    t0 = time.perf_counter()
    meta = preview_meta(source_clip, dest_clip)
    n = int(meta["usable_frame_count"])
    if n <= 0:
        raise ValueError("No usable frames")

    indices = list(range(n))
    if max_frames is not None and max_frames > 0 and n > max_frames:
        # Evenly spaced sample including first and last
        step = (n - 1) / (max_frames - 1)
        indices = sorted({int(round(i * step)) for i in range(max_frames)})

    log.info(
        "noise-score: scoring %s/%s frames offset=%s src=%s dest=%s",
        len(indices),
        n,
        offset,
        source_clip.name,
        dest_clip.name,
    )

    ssims: list[float] = []
    psnrs: list[float] = []
    mses: list[float] = []
    skipped = 0
    progress_every = max(1, len(indices) // 10)
    t_loop = time.perf_counter()

    for i, idx in enumerate(indices):
        pair = resolve_preview_pair(
            source_clip=source_clip,
            dest_clip=dest_clip,
            index=idx,
            offset=offset,
            full_source=full_source,
            source_clip_start=source_clip_start,
        )
        if pair["source_oob"]:
            skipped += 1
            continue
        try:
            m = compare_png_bytes(pair["source"], pair["dest"])
        except Exception as exc:
            skipped += 1
            log.debug("noise-score: frame %s compare failed: %s", idx, exc)
            continue
        ssims.append(float(m["ssim"]))
        mses.append(float(m["mse"]))
        if m["psnr"] is not None:
            psnrs.append(float(m["psnr"]))

        done = i + 1
        if done % progress_every == 0 or done == len(indices):
            elapsed = time.perf_counter() - t_loop
            rate = done / elapsed if elapsed > 0 else 0
            remaining = len(indices) - done
            eta = remaining / rate if rate > 0 else 0
            log.info(
                "noise-score: %s/%s sampled (ok=%s skip=%s, %.1f/s, ~%.0fs left)",
                done,
                len(indices),
                len(ssims),
                skipped,
                rate,
                eta,
            )

    if not ssims:
        raise ValueError("No valid frame pairs to score (all OOB or failed)")

    def _mean_std(vals: list[float]) -> tuple[float, float]:
        arr = np.asarray(vals, dtype=np.float64)
        mean = float(np.mean(arr))
        std = float(np.std(arr)) if len(arr) > 1 else 0.0
        return mean, std

    ssim_mean, ssim_std = _mean_std(ssims)
    mse_mean, mse_std = _mean_std(mses)
    if psnrs:
        psnr_mean, psnr_std = _mean_std(psnrs)
    else:
        psnr_mean, psnr_std = None, None

    log.info(
        "noise-score: done ssim=%.4f±%.4f psnr=%s mse=%.2f n=%s skip=%s (%.1fs)",
        ssim_mean,
        ssim_std,
        f"{psnr_mean:.2f}" if psnr_mean is not None else "—",
        mse_mean,
        len(ssims),
        skipped,
        time.perf_counter() - t0,
    )

    return {
        "frame_count": len(ssims),
        "sampled": len(indices),
        "skipped": skipped,
        "offset": offset,
        "ssim_mean": ssim_mean,
        "ssim_std": ssim_std,
        "psnr_mean": psnr_mean,
        "psnr_std": psnr_std,
        "mse_mean": mse_mean,
        "mse_std": mse_std,
        # Single list-friendly score: mean SSIM (higher = cleaner match)
        "noise_score": ssim_mean,
    }

