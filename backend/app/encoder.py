"""HandBrake encode / preview pipelines + comparison frame extraction."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import (
    HANDBRAKE_BIN,
    MEDIA_BASE,
    OUTPUT_BASE,
    PRESET_JSON,
    PRESET_MAP,
    PREVIEW_PAD_FRAMES,
    PREVIEW_WARMUP,
    WATCH_BASE,
    human_size,
)
from app.db import SessionLocal
from app.frames import (
    compute_match_noise_graph,
    cut_source_clip,
    extract_comparison_frames,
    preview_window_times,
    probe_video,
)
from app.models import ComparisonFrame, EncodeJob
from app.paths import parse_job_id_from_ticket, unlink_watch_ticket
from app.ws import manager

log = logging.getLogger(__name__)

_encoding_lock = asyncio.Lock()
_current_encode_path: str | None = None
_current_job_id: int | None = None
_current_proc: asyncio.subprocess.Process | None = None
_cancel_requested: set[int] = set()


def get_current_encode_file() -> str | None:
    """Basename of current dest (legacy); prefer get_current_encode_path()."""
    if not _current_encode_path:
        return None
    return Path(_current_encode_path).name


def get_current_encode_path() -> str | None:
    return _current_encode_path


def get_current_job_id() -> int | None:
    return _current_job_id


def request_cancel(job_id: int) -> None:
    _cancel_requested.add(job_id)


def is_cancel_requested(job_id: int) -> bool:
    return job_id in _cancel_requested


def clear_cancel(job_id: int) -> None:
    _cancel_requested.discard(job_id)


async def signal_current_proc(job_id: int) -> bool:
    """SIGTERM then SIGKILL the live HandBrake process if it belongs to job_id."""
    global _current_proc
    if _current_job_id != job_id or _current_proc is None:
        return False
    proc = _current_proc
    try:
        proc.terminate()
    except ProcessLookupError:
        return True
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
    return True


def _touch(job: EncodeJob) -> None:
    job.updated_at = datetime.now(timezone.utc)


def _handbrake_import_args(preset_name: str) -> list[str]:
    if preset_name == "Super 8 Scan" and PRESET_JSON.exists():
        return ["--preset-import-file", str(PRESET_JSON)]
    return []


def _resolve_job_for_ticket(
    db,
    folder: str,
    infile_path: Path,
    job_id: int | None,
) -> EncodeJob | None:
    if job_id is not None:
        job = db.get(EncodeJob, job_id)
        if job is not None:
            return job
    parsed = parse_job_id_from_ticket(infile_path.name)
    if parsed is not None:
        job = db.get(EncodeJob, parsed)
        if job is not None and job.preset == folder and job.status in (
            "queued",
            "encoding",
        ):
            return job
    # Fallback: match by real source_path + preset
    real = str(infile_path.resolve()) if infile_path.exists() else str(infile_path)
    return (
        db.query(EncodeJob)
        .filter(
            EncodeJob.origin == "encode",
            EncodeJob.kind == "encode",
            EncodeJob.status.in_(("queued", "encoding")),
            EncodeJob.preset == folder,
            EncodeJob.source_path == real,
        )
        .order_by(EncodeJob.id.desc())
        .first()
    )


async def _read_handbrake_progress(
    proc: asyncio.subprocess.Process,
    job: EncodeJob,
    db,
    job_id: int,
    display_name: str,
) -> None:
    last_pct = -1.0
    buf = b""
    while True:
        if is_cancel_requested(job_id):
            break
        try:
            chunk = await proc.stdout.read(4096)  # type: ignore[union-attr]
        except Exception:
            break
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts[-1]
        for raw_line in parts[:-1]:
            try:
                line = raw_line.decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            if not line:
                continue
            lo = line.lower()
            if any(k in lo for k in ("error", "warning")) and "%" not in line:
                log.warning(line)

            m = re.search(r"([\d.]+)\s*%", line)
            if not m:
                continue
            pct = float(m.group(1))
            if abs(pct - last_pct) < 0.5:
                continue
            last_pct = pct
            fps: Optional[float] = None
            eta_seconds: Optional[int] = None

            fps_m = re.search(r"([\d.]+)\s*fps", line)
            if fps_m:
                fps = float(fps_m.group(1))

            eta_m = re.search(r"ETA\s+(\d+)h(\d+)m(\d+)s", line)
            if eta_m:
                eta_seconds = (
                    int(eta_m.group(1)) * 3600
                    + int(eta_m.group(2)) * 60
                    + int(eta_m.group(3))
                )

            job.progress = pct
            job.fps = fps
            job.eta_seconds = eta_seconds
            _touch(job)
            db.commit()

            await manager.broadcast(
                {
                    "type": "encode_progress",
                    "job_id": job_id,
                    "pct": pct,
                    "fps": fps,
                    "eta_seconds": eta_seconds,
                    "file": display_name,
                }
            )


async def run_encode(folder: str, infile_path: Path, job_id: int | None = None) -> None:
    """Run HandBrakeCLI on a watch ticket, then extract comparison frames.

    Unlinks only the watch ticket on success — never the library/source file.
    """
    global _current_encode_path, _current_job_id, _current_proc

    if folder not in PRESET_MAP:
        log.error("Unknown preset folder: %s", folder)
        return

    preset, fmt, extra_args = PRESET_MAP[folder]
    out_dir = OUTPUT_BASE / folder
    out_dir.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()
    job: EncodeJob | None = None
    try:
        job = _resolve_job_for_ticket(db, folder, infile_path, job_id)
        if job is None:
            # Orphan real drop-in: create job with job-id dest name after insert
            real_source = str(infile_path.resolve()) if infile_path.exists() else str(infile_path)
            stem = Path(infile_path.name).stem
            # Temporary dest; rewrite after we have id
            job = EncodeJob(
                filename=infile_path.name,
                preset=folder,
                status="encoding",
                origin="encode",
                kind="encode",
                source_path=real_source,
                dest_path="",
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            out_path = out_dir / f"{stem}.{job.id}.{fmt}"
            job.dest_path = str(out_path)
            db.commit()
        else:
            if job.status == "cancelled" or is_cancel_requested(job.id):
                clear_cancel(job.id)
                unlink_watch_ticket(infile_path, WATCH_BASE)
                return
            out_path = Path(job.dest_path) if job.dest_path else (
                out_dir / f"{Path(job.filename).stem}.{job.id}.{fmt}"
            )
            job.status = "encoding"
            job.dest_path = str(out_path)
            # Keep source_path as real target — do not overwrite with ticket
            if not job.source_path:
                job.source_path = (
                    str(infile_path.resolve()) if infile_path.exists() else str(infile_path)
                )
            job.error = None
            _touch(job)
            db.commit()

        job_id = job.id
        out_path = Path(job.dest_path)
        out_name = out_path.name
        display = job.filename
        _current_job_id = job_id
        _current_encode_path = str(out_path)

        log.info(
            "ENCODING [%s] job=%s %s -> %s (preset: %s)",
            folder,
            job_id,
            display,
            out_name,
            preset,
        )

        import_args = _handbrake_import_args(preset)
        cmd = [
            HANDBRAKE_BIN,
            *import_args,
            "-Z",
            preset,
            "-i",
            str(infile_path),
            "-o",
            str(out_path),
            *extra_args,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=4 * 1024 * 1024,
        )
        _current_proc = proc

        t0 = time.monotonic()
        await _read_handbrake_progress(proc, job, db, job_id, display)
        await proc.wait()
        encode_secs = time.monotonic() - t0
        _current_proc = None

        if is_cancel_requested(job_id):
            clear_cancel(job_id)
            job.status = "cancelled"
            job.error = "Cancelled"
            _touch(job)
            db.commit()
            if out_path.exists():
                out_path.unlink(missing_ok=True)
            unlink_watch_ticket(infile_path, WATCH_BASE)
            await manager.broadcast(
                {"type": "encode_cancelled", "job_id": job_id, "file": display}
            )
            return

        if proc.returncode != 0:
            log.error("FAILED: %s (exit %s)", display, proc.returncode)
            job.status = "failed"
            job.error = f"HandBrake exit {proc.returncode}"
            _touch(job)
            db.commit()
            await manager.broadcast(
                {
                    "type": "encode_failed",
                    "job_id": job_id,
                    "file": display,
                    "exit_code": proc.returncode,
                }
            )
            unlink_watch_ticket(infile_path, WATCH_BASE)
            return

        size = out_path.stat().st_size if out_path.exists() else 0
        job.progress = 100.0
        job.output_size = size
        job.encode_duration_seconds = round(encode_secs, 2)
        job.status = "extracting"
        _touch(job)
        db.commit()

        log.info("DONE encode: %s (%s)", out_name, human_size(size))
        await manager.broadcast(
            {
                "type": "encode_done",
                "job_id": job_id,
                "file": display,
                "size": size,
            }
        )

        source_for_frames = Path(job.source_path) if job.source_path else infile_path
        try:
            pairs = await asyncio.to_thread(
                extract_comparison_frames,
                job_id,
                source_for_frames,
                out_path,
            )
            for existing in list(job.frames):
                db.delete(existing)
            db.flush()
            for pos, src_png, dst_png in pairs:
                db.add(
                    ComparisonFrame(
                        job_id=job_id,
                        position=pos,
                        source_png=str(src_png),
                        dest_png=str(dst_png),
                    )
                )
            job.status = "done"
            _touch(job)
            db.commit()
            await manager.broadcast(
                {
                    "type": "compare_ready",
                    "job_id": job_id,
                    "file": display,
                }
            )
        except Exception as exc:
            log.exception("Frame extraction failed for job %s", job_id)
            job.status = "done"
            job.error = f"Encode ok; frame extract failed: {exc}"
            _touch(job)
            db.commit()

        # Unlink watch ticket only — never the library / parent source
        unlink_watch_ticket(infile_path, WATCH_BASE)
        log.info("Removed watch ticket: %s", infile_path.name)

    finally:
        _current_encode_path = None
        _current_job_id = None
        _current_proc = None
        db.close()


async def run_preview(job_id: int) -> None:
    """Encode a short HandBrake range around 25%, cut source pad, auto-align."""
    global _current_encode_path, _current_job_id, _current_proc

    db = SessionLocal()
    try:
        job = db.get(EncodeJob, job_id)
        if job is None:
            return
        if job.status == "cancelled" or is_cancel_requested(job_id):
            clear_cancel(job_id)
            job.status = "cancelled"
            _touch(job)
            db.commit()
            return

        folder = job.preset
        if folder not in PRESET_MAP:
            job.status = "failed"
            job.error = f"Unknown preset: {folder}"
            db.commit()
            return

        preset, fmt, extra_args = PRESET_MAP[folder]
        source = Path(job.source_path) if job.source_path else None
        if not source or not source.exists():
            job.status = "failed"
            job.error = "Source file missing"
            db.commit()
            return

        job.status = "previewing"
        job.progress = 0.0
        job.error = None
        _touch(job)
        db.commit()

        meta = await asyncio.to_thread(probe_video, source)
        duration = meta["duration"]
        fps = meta["fps"] or 30.0
        hb_start, hb_length, usable_start_abs = preview_window_times(duration)

        out_dir = MEDIA_BASE / "jobs" / str(job_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        dest_clip = out_dir / f"dest_clip.{fmt}"
        pad_sec = PREVIEW_PAD_FRAMES / fps
        src_clip_start = max(0.0, usable_start_abs - pad_sec)
        # Source covers usable dest length + pad on both sides
        usable_len = max(0.1, (hb_start + hb_length) - usable_start_abs)
        src_clip_len = usable_len + 2 * pad_sec
        source_clip = out_dir / f"source_clip{source.suffix or '.mkv'}"

        job.dest_path = str(dest_clip)
        job.dest_clip_path = str(dest_clip)
        _touch(job)
        db.commit()

        _current_job_id = job_id
        _current_encode_path = str(dest_clip)

        import_args = _handbrake_import_args(preset)
        cmd = [
            HANDBRAKE_BIN,
            *import_args,
            "-Z",
            preset,
            "-i",
            str(source),
            "-o",
            str(dest_clip),
            "--start-at",
            f"duration:{hb_start}",
            "--stop-at",
            f"duration:{hb_length}",
            *extra_args,
        ]

        log.info(
            "PREVIEW job=%s start=%.2f len=%.2f -> %s",
            job_id,
            hb_start,
            hb_length,
            dest_clip.name,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=4 * 1024 * 1024,
        )
        _current_proc = proc
        t0 = time.monotonic()
        await _read_handbrake_progress(proc, job, db, job_id, job.filename)
        await proc.wait()
        encode_secs = time.monotonic() - t0
        _current_proc = None

        if is_cancel_requested(job_id):
            clear_cancel(job_id)
            job.status = "cancelled"
            job.error = "Cancelled"
            _touch(job)
            db.commit()
            dest_clip.unlink(missing_ok=True)
            source_clip.unlink(missing_ok=True)
            await manager.broadcast(
                {"type": "encode_cancelled", "job_id": job_id, "file": job.filename}
            )
            return

        if proc.returncode != 0:
            job.status = "failed"
            job.error = f"HandBrake preview exit {proc.returncode}"
            _touch(job)
            db.commit()
            await manager.broadcast(
                {
                    "type": "encode_failed",
                    "job_id": job_id,
                    "file": job.filename,
                    "exit_code": proc.returncode,
                }
            )
            return

        # Cut padded source clip
        try:
            source_clip = await asyncio.to_thread(
                cut_source_clip,
                source,
                source_clip,
                src_clip_start,
                src_clip_len,
            )
            job.source_clip_path = str(source_clip)
            _touch(job)
            db.commit()
        except Exception as exc:
            log.exception("Source clip cut failed for job %s", job_id)
            job.status = "failed"
            job.error = f"Source clip failed: {exc}"
            db.commit()
            return

        # Auto-suggest offset from noise graph (source center vs dest frames)
        try:
            noise = await asyncio.to_thread(
                compute_match_noise_graph,
                source_clip,
                dest_clip,
            )
            job.frame_offset = int(noise["suggested_offset"])
            job.align_confidence = float(noise["best_mse"])
        except Exception as exc:
            log.exception("Align/noise failed for job %s", job_id)
            job.frame_offset = 0
            job.align_confidence = None
            job.error = f"Align failed: {exc}"

        job.progress = 100.0
        job.output_size = dest_clip.stat().st_size if dest_clip.exists() else 0
        job.encode_duration_seconds = round(encode_secs, 2)
        job.status = "preview_ready"
        _touch(job)
        db.commit()

        await manager.broadcast(
            {
                "type": "preview_ready",
                "job_id": job_id,
                "file": job.filename,
                "frame_offset": job.frame_offset,
            }
        )
    except Exception as exc:
        log.exception("Preview failed for job %s", job_id)
        job = db.get(EncodeJob, job_id)
        if job:
            job.status = "failed"
            job.error = str(exc)
            _touch(job)
            db.commit()
        await manager.broadcast(
            {"type": "encode_failed", "job_id": job_id, "file": "", "exit_code": 1}
        )
    finally:
        _current_encode_path = None
        _current_job_id = None
        _current_proc = None
        db.close()


async def extract_only(job_id: int, source_path: Path, dest_path: Path) -> None:
    """External compare: extract frames only (no HandBrake)."""
    db = SessionLocal()
    try:
        job = db.get(EncodeJob, job_id)
        if job is None:
            return
        job.status = "extracting"
        job.source_path = str(source_path)
        job.dest_path = str(dest_path)
        _touch(job)
        db.commit()

        pairs = await asyncio.to_thread(
            extract_comparison_frames,
            job_id,
            source_path,
            dest_path,
        )
        for existing in list(job.frames):
            db.delete(existing)
        db.flush()
        for pos, src_png, dst_png in pairs:
            db.add(
                ComparisonFrame(
                    job_id=job_id,
                    position=pos,
                    source_png=str(src_png),
                    dest_png=str(dst_png),
                )
            )
        job.status = "done"
        _touch(job)
        db.commit()
        await manager.broadcast({"type": "compare_ready", "job_id": job_id})
    except Exception as exc:
        log.exception("External extract failed for job %s", job_id)
        job = db.get(EncodeJob, job_id)
        if job:
            job.status = "failed"
            job.error = str(exc)
            _touch(job)
            db.commit()
        await manager.broadcast(
            {"type": "encode_failed", "job_id": job_id, "file": "", "exit_code": 1}
        )
    finally:
        db.close()
