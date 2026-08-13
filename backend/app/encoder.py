"""HandBrake encode pipeline + comparison frame extraction."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import (
    HANDBRAKE_BIN,
    OUTPUT_BASE,
    PRESET_JSON,
    PRESET_MAP,
    human_size,
)
from app.db import SessionLocal
from app.frames import extract_comparison_frames
from app.models import ComparisonFrame, EncodeJob
from app.ws import manager

log = logging.getLogger(__name__)

_encoding_lock = asyncio.Lock()
_current_encode_file: str | None = None


def get_current_encode_file() -> str | None:
    return _current_encode_file


def _touch(job: EncodeJob) -> None:
    job.updated_at = datetime.now(timezone.utc)


async def run_encode(folder: str, infile_path: Path, job_id: int | None = None) -> None:
    """Run HandBrakeCLI, then extract comparison frames. Deletes source on success."""
    global _current_encode_file

    if folder not in PRESET_MAP:
        log.error("Unknown preset folder: %s", folder)
        return

    preset, fmt, extra_args = PRESET_MAP[folder]
    out_name = infile_path.stem + "." + fmt
    out_dir = OUTPUT_BASE / folder
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / out_name

    log.info("ENCODING [%s] %s -> %s (preset: %s)", folder, infile_path.name, out_name, preset)

    db = SessionLocal()
    job: EncodeJob | None = None
    try:
        if job_id is not None:
            job = db.get(EncodeJob, job_id)
        if job is None:
            # Reuse queued job created at upload time (match source path or filename+preset)
            job = (
                db.query(EncodeJob)
                .filter(
                    EncodeJob.origin == "encode",
                    EncodeJob.status.in_(("queued", "encoding")),
                    EncodeJob.filename == infile_path.name,
                    EncodeJob.preset == folder,
                )
                .order_by(EncodeJob.id.desc())
                .first()
            )
        if job is None:
            job = EncodeJob(
                filename=infile_path.name,
                preset=folder,
                status="encoding",
                origin="encode",
                source_path=str(infile_path),
                dest_path=str(out_path),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
        else:
            job.status = "encoding"
            job.source_path = str(infile_path)
            job.dest_path = str(out_path)
            job.error = None
            _touch(job)
            db.commit()

        job_id = job.id
        _current_encode_file = out_name

        import_args: list[str] = []
        if preset == "Super 8 Scan" and PRESET_JSON.exists():
            import_args = ["--preset-import-file", str(PRESET_JSON)]

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

        last_pct = -1.0
        buf = b""

        while True:
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
                        "file": out_name,
                    }
                )

        await proc.wait()

        if proc.returncode != 0:
            log.error("FAILED: %s (exit %s)", infile_path.name, proc.returncode)
            job.status = "failed"
            job.error = f"HandBrake exit {proc.returncode}"
            _touch(job)
            db.commit()
            await manager.broadcast(
                {
                    "type": "encode_failed",
                    "job_id": job_id,
                    "file": infile_path.name,
                    "exit_code": proc.returncode,
                }
            )
            return

        size = out_path.stat().st_size if out_path.exists() else 0
        job.progress = 100.0
        job.output_size = size
        job.status = "extracting"
        _touch(job)
        db.commit()

        log.info("DONE encode: %s (%s)", out_name, human_size(size))
        await manager.broadcast(
            {
                "type": "encode_done",
                "job_id": job_id,
                "file": out_name,
                "size": size,
            }
        )

        # Extract comparison frames while source still exists
        try:
            pairs = await asyncio.to_thread(
                extract_comparison_frames,
                job_id,
                infile_path,
                out_path,
            )
            # Replace any existing frames
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
                    "file": out_name,
                }
            )
        except Exception as exc:
            log.exception("Frame extraction failed for job %s", job_id)
            job.status = "done"
            job.error = f"Encode ok; frame extract failed: {exc}"
            _touch(job)
            db.commit()

        try:
            infile_path.unlink(missing_ok=True)
            log.info("Deleted source: %s", infile_path.name)
        except Exception as e:
            log.warning("Could not delete source: %s", e)

    finally:
        _current_encode_file = None
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
