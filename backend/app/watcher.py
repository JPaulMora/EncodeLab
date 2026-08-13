"""DB-backed encode/preview worker and system status broadcast."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.config import human_size
from app.db import SessionLocal
from app.encoder import (
    _encoding_lock,
    get_current_encode_path,
    run_encode,
    run_preview,
    wait_for_job,
)
from app.models import EncodeJob
from app.ws import manager

log = logging.getLogger(__name__)


def _recover_stale_jobs() -> None:
    """Re-queue jobs left encoding/previewing after an API restart."""
    db = SessionLocal()
    try:
        stale = (
            db.query(EncodeJob)
            .filter(EncodeJob.status.in_(("encoding", "previewing", "extracting")))
            .all()
        )
        for job in stale:
            log.warning("Recovering stale job %s (%s → queued)", job.id, job.status)
            job.status = "queued"
        if stale:
            db.commit()
    finally:
        db.close()


def _next_queued_job() -> tuple[int, str] | None:
    db = SessionLocal()
    try:
        job = (
            db.query(EncodeJob)
            .filter(
                EncodeJob.status == "queued",
                EncodeJob.kind.in_(("encode", "preview")),
            )
            .order_by(EncodeJob.id.asc())
            .first()
        )
        if job is None:
            return None
        return job.id, job.kind
    finally:
        db.close()


async def job_worker() -> None:
    """Pick queued encode/preview jobs from the DB and run them one at a time."""
    _recover_stale_jobs()
    log.info("=== job worker started (DB queue) ===")
    while True:
        nxt = _next_queued_job()
        if nxt is None:
            await wait_for_job(2.0)
            continue
        job_id, kind = nxt
        log.info("WORKER pickup job=%s kind=%s", job_id, kind)
        async with _encoding_lock:
            try:
                if kind == "preview":
                    await run_preview(job_id)
                else:
                    await run_encode(job_id)
            except Exception:
                log.exception("Job %s crashed", job_id)
                db = SessionLocal()
                try:
                    job = db.get(EncodeJob, job_id)
                    if job and job.status in ("queued", "encoding", "previewing"):
                        job.status = "failed"
                        job.error = "Worker crashed"
                        db.commit()
                finally:
                    db.close()


async def broadcast_system_loop() -> None:
    """Push CPU + encode status every 3 seconds."""

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
        enc_path = get_current_encode_path()
        if enc_path:
            p = Path(enc_path)
            size = p.stat().st_size if p.exists() else 0
            msg.update(
                {
                    "encoding": True,
                    "encoding_file": p.name,
                    "encoding_path": enc_path,
                    "encoding_size": size,
                    "encoding_size_human": human_size(size),
                }
            )
        await manager.broadcast(msg)
