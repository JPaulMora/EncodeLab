"""Online Encoder API — FastAPI entrypoint."""
from __future__ import annotations

import asyncio
import base64
import gzip
import logging
import re
import shutil
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import zstandard as zstd
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import joinedload
from starlette.responses import Response

from app.config import (
    LIBRARY_BASE,
    LOG_FILE,
    MEDIA_BASE,
    OUTPUT_BASE,
    PRESET_MAP,
    PREVIEW_PAD_FRAMES,
    UPLOAD_TMP,
    disk_stats,
    ensure_dirs,
    human_size,
)
from app.db import SessionLocal, init_db
from app.diff import compare_frame_files
from app.encoder import (
    clear_cancel,
    extract_only,
    get_current_encode_path,
    get_current_job_id,
    is_queue_paused,
    request_cancel,
    set_queue_paused,
    signal_current_proc,
    wakeup_job_worker,
)
from app.frames import (
    compute_match_noise_graph,
    compute_preview_noise_score,
    extract_frame_at_time,
    preview_meta,
    preview_window_times,
    resolve_preview_pair,
    probe_video,
)
from app.logging_config import setup_logging
from app.models import ComparisonFrame, EncodeJob, LibraryFile
from app.paths import is_under
from app.watcher import broadcast_system_loop, job_worker
from app.ws import manager

# ── Logging ────────────────────────────────────────────────────────────────
ensure_dirs()
setup_logging(LOG_FILE)
log = logging.getLogger(__name__)


def _console(msg: str) -> None:
    print(msg, flush=True)
    log.info(msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Re-apply after uvicorn's --log-config may have reset handlers
    setup_logging(LOG_FILE)
    ensure_dirs()
    _console(
        f"API starting — library={LIBRARY_BASE} output={OUTPUT_BASE} media={MEDIA_BASE}"
    )
    init_db()
    _console("DB ready")
    asyncio.create_task(job_worker())
    asyncio.create_task(broadcast_system_loop())
    _console("Job worker + system broadcast started")
    yield
    _console("API shutting down")


app = FastAPI(title="Online Encoder", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next) -> Response:
    # Skip chatty health checks
    path = request.url.path
    if path in ("/health", "/api/status", "/status"):
        return await call_next(request)

    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(
            f"Unhandled error on {request.method} {path} ({elapsed_ms:.0f}ms)",
            flush=True,
        )
        log.exception(
            "Unhandled error on %s %s (%.0fms)",
            request.method,
            path,
            elapsed_ms,
        )
        raise
    elapsed_ms = (time.perf_counter() - t0) * 1000
    line = f"{request.method} {path} → {response.status_code} ({elapsed_ms:.0f}ms)"
    print(line, flush=True)
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    log.log(level, line)
    return response

app.mount("/media", StaticFiles(directory=str(MEDIA_BASE)), name="media")


# ── Serializers ────────────────────────────────────────────────────────────
def _media_url(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    try:
        rel = p.resolve().relative_to(MEDIA_BASE.resolve())
        return f"/media/{rel.as_posix()}"
    except ValueError:
        return None


def _unlink_under(path: str | Path | None, base: Path) -> None:
    if not path:
        return
    fp = Path(path)
    if not is_under(fp, base):
        return
    try:
        if fp.is_dir() and not fp.is_symlink():
            shutil.rmtree(fp, ignore_errors=True)
        elif fp.exists() or fp.is_symlink():
            fp.unlink(missing_ok=True)
    except OSError:
        log.warning("Could not remove %s", fp, exc_info=True)


def _cleanup_job_files(job: EncodeJob) -> None:
    """Remove job-owned compare artifacts. Never touch library originals."""
    job_dir = MEDIA_BASE / "jobs" / str(job.id)
    _unlink_under(job_dir, MEDIA_BASE)

    if job.origin == "external":
        _unlink_under(job.source_path, UPLOAD_TMP)
        _unlink_under(job.dest_path, UPLOAD_TMP)
        if job.source_path:
            parent = Path(job.source_path).parent
            ext_root = UPLOAD_TMP / "external"
            if is_under(parent, ext_root):
                _unlink_under(parent, UPLOAD_TMP)
    elif job.kind == "encode" and job.dest_path:
        _unlink_under(job.dest_path, OUTPUT_BASE)


def _source_label(job: EncodeJob, db) -> str:
    if job.library_file_id:
        lib = job.library_file or db.get(LibraryFile, job.library_file_id)
        if lib:
            return lib.original_filename
    if job.parent_job_id:
        parent = job.parent_job or db.get(EncodeJob, job.parent_job_id)
        if parent:
            return f"from job #{parent.id} ({parent.preset})"
        return f"from job #{job.parent_job_id}"
    return job.filename


def _source_size_bytes(job: EncodeJob, db) -> int | None:
    if job.library_file_id:
        lib = job.library_file or db.get(LibraryFile, job.library_file_id)
        if lib and lib.size:
            return int(lib.size)
    if job.source_path:
        try:
            p = Path(job.source_path)
            if p.exists():
                return int(p.stat().st_size)
        except OSError:
            pass
    return None


def job_to_dict(job: EncodeJob, db=None) -> dict:
    close = False
    if db is None:
        db = SessionLocal()
        close = True
    try:
        source_size = _source_size_bytes(job, db)
        output_size = job.output_size
        compression_ratio = job.compression_ratio
        if compression_ratio is None and source_size and output_size and source_size > 0:
            # Legacy rows from before the stored column
            compression_ratio = round(output_size / source_size, 6)

        return {
            "id": job.id,
            "filename": job.filename,
            "preset": job.preset,
            "status": job.status,
            "origin": job.origin,
            "kind": job.kind,
            "progress": job.progress,
            "fps": job.fps,
            "eta_seconds": job.eta_seconds,
            "output_size": output_size,
            "output_size_human": human_size(output_size) if output_size else None,
            "source_size": source_size,
            "source_size_human": human_size(source_size) if source_size else None,
            "compression_ratio": compression_ratio,
            "error": job.error,
            "dest_path": job.dest_path,
            "source_path": job.source_path,
            "library_file_id": job.library_file_id,
            "parent_job_id": job.parent_job_id,
            "source_label": _source_label(job, db),
            "frame_offset": job.frame_offset,
            "offset_locked": bool(job.offset_locked),
            "align_confidence": job.align_confidence,
            "has_preview_clips": bool(job.source_clip_path and job.dest_clip_path),
            "noise_score": job.noise_ssim_mean,
            "noise_ssim_mean": job.noise_ssim_mean,
            "noise_ssim_std": job.noise_ssim_std,
            "noise_psnr_mean": job.noise_psnr_mean,
            "noise_psnr_std": job.noise_psnr_std,
            "noise_mse_mean": job.noise_mse_mean,
            "noise_mse_std": job.noise_mse_std,
            "noise_frame_count": job.noise_frame_count,
            "encode_duration_seconds": job.encode_duration_seconds,
            "keep_tracks": bool(job.keep_tracks),
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "frames": [
                {
                    "id": f.id,
                    "position": f.position,
                    "label": f"{int(f.position * 100)}%",
                    "source_url": _media_url(f.source_png),
                    "dest_url": _media_url(f.dest_png),
                }
                for f in (job.frames or [])
            ],
            "download_url": (
                f"/api/jobs/{job.id}/download"
                if job.status in ("done", "preview_ready") and job.dest_path
                else None
            ),
        }
    finally:
        if close:
            db.close()


def library_to_dict(lib: LibraryFile) -> dict:
    return {
        "id": lib.id,
        "original_filename": lib.original_filename,
        "size": lib.size,
        "size_human": human_size(lib.size),
        "created_at": lib.created_at.isoformat() if lib.created_at else None,
        "download_url": f"/api/library/{lib.id}/download",
    }


# ── Health / presets ───────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/presets")
@app.get("/presets")
async def get_presets():
    presets = sorted(PRESET_MAP.keys())
    formats = {k: PRESET_MAP[k][1] for k in PRESET_MAP}
    return JSONResponse({"presets": presets, "formats": formats})


# ── WebSocket ──────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_json({"type": "hello"})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


# ── Library ingest ─────────────────────────────────────────────────────────
@app.post("/api/library/chunk")
async def library_chunk(
    chunk: UploadFile = File(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
    compression: str = Form("none"),
    compressed: Optional[str] = Form(None),
):
    if compressed is not None and compression == "none":
        if compressed.lower() in ("true", "1", "yes"):
            compression = "gzip"

    safe_filename = Path(filename).name
    tmp_path = UPLOAD_TMP / f"library_{safe_filename}"

    chunk_data = await chunk.read()
    wire_bytes = len(chunk_data)

    if compression == "gzip":
        try:
            chunk_data = gzip.decompress(chunk_data)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Gzip decompression failed: {e}")
    elif compression == "zstd":
        try:
            dctx = zstd.ZstdDecompressor()
            chunk_data = dctx.decompress(chunk_data)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Zstd decompression failed: {e}")

    decompressed_bytes = len(chunk_data)
    write_mode = "ab" if chunk_index > 0 else "wb"
    with open(tmp_path, write_mode) as f:
        f.write(chunk_data)

    await manager.broadcast(
        {
            "type": "chunk_ack",
            "index": chunk_index,
            "total": total_chunks,
            "server_bytes": wire_bytes,
            "decompressed_bytes": decompressed_bytes,
        }
    )

    if chunk_index == total_chunks - 1:
        size = tmp_path.stat().st_size
        LIBRARY_BASE.mkdir(parents=True, exist_ok=True)
        db = SessionLocal()
        try:
            lib = LibraryFile(
                original_filename=safe_filename,
                stored_path="",
                size=size,
            )
            db.add(lib)
            db.commit()
            db.refresh(lib)
            dest = LIBRARY_BASE / f"{lib.id}_{safe_filename}"
            shutil.move(str(tmp_path), str(dest))
            lib.stored_path = str(dest)
            lib.size = dest.stat().st_size
            db.commit()
            lib_id = lib.id
        finally:
            db.close()

        await manager.broadcast(
            {
                "type": "library_upload_complete",
                "filename": safe_filename,
                "size": size,
                "library_id": lib_id,
            }
        )
        return JSONResponse({"status": "complete", "id": lib_id})

    return JSONResponse({"status": "ok", "chunk": chunk_index, "total": total_chunks})


@app.get("/api/library")
async def list_library():
    db = SessionLocal()
    try:
        rows = db.query(LibraryFile).order_by(LibraryFile.id.desc()).all()
        return JSONResponse({"files": [library_to_dict(r) for r in rows]})
    finally:
        db.close()


@app.get("/api/library/{lib_id}/download")
async def download_library(lib_id: int):
    db = SessionLocal()
    try:
        lib = db.get(LibraryFile, lib_id)
        if not lib or not lib.stored_path:
            raise HTTPException(status_code=404, detail="Library file not found")
        fp = Path(lib.stored_path)
        if not fp.exists():
            raise HTTPException(status_code=404, detail="File missing on disk")
        return FileResponse(str(fp), filename=lib.original_filename)
    finally:
        db.close()


@app.delete("/api/library/{lib_id}")
async def delete_library(lib_id: int):
    db = SessionLocal()
    try:
        lib = db.get(LibraryFile, lib_id)
        if not lib:
            raise HTTPException(status_code=404, detail="Library file not found")
        active = (
            db.query(EncodeJob)
            .filter(
                EncodeJob.library_file_id == lib_id,
                EncodeJob.status.in_(("queued", "encoding", "previewing")),
            )
            .count()
        )
        if active:
            raise HTTPException(
                status_code=409,
                detail="Library file is used by an active job",
            )
        fp = Path(lib.stored_path) if lib.stored_path else None
        db.delete(lib)
        db.commit()
        if fp and fp.exists():
            fp.unlink(missing_ok=True)
        return JSONResponse({"status": "deleted"})
    finally:
        db.close()


# ── Create encode / preview job ────────────────────────────────────────────
class JobSource(BaseModel):
    type: str  # library | job
    id: int


class CreateJobBody(BaseModel):
    source: JobSource
    preset: str
    kind: str = Field(default="encode")  # encode | preview
    keep_tracks: bool = False


@app.post("/api/jobs")
async def create_job(body: CreateJobBody):
    if body.preset not in PRESET_MAP:
        raise HTTPException(status_code=400, detail=f"Invalid preset: {body.preset}")
    if body.kind not in ("encode", "preview"):
        raise HTTPException(status_code=400, detail="kind must be encode or preview")

    db = SessionLocal()
    try:
        library_file_id: int | None = None
        parent_job_id: int | None = None
        real_source: Path
        display_name: str

        if body.source.type == "library":
            lib = db.get(LibraryFile, body.source.id)
            if not lib or not lib.stored_path:
                raise HTTPException(status_code=409, detail="Library file missing")
            real_source = Path(lib.stored_path)
            if not real_source.exists():
                raise HTTPException(status_code=409, detail="Library file missing on disk")
            library_file_id = lib.id
            display_name = lib.original_filename
        elif body.source.type == "job":
            parent = db.get(EncodeJob, body.source.id)
            if not parent:
                raise HTTPException(status_code=409, detail="Parent job not found")
            # Only finished full encodes can be re-encoded as a source
            if parent.status != "done" or parent.kind != "encode":
                raise HTTPException(
                    status_code=409,
                    detail="Parent job is not a finished encode",
                )
            if not parent.dest_path:
                raise HTTPException(status_code=409, detail="Parent job has no output")
            real_source = Path(parent.dest_path)
            if not real_source.exists():
                raise HTTPException(status_code=409, detail="Parent output missing on disk")
            parent_job_id = parent.id
            display_name = parent.filename
            library_file_id = parent.library_file_id
        else:
            raise HTTPException(status_code=400, detail="source.type must be library or job")

        preset_name, fmt, _extra = PRESET_MAP[body.preset]
        stem = Path(display_name).stem

        job = EncodeJob(
            filename=display_name,
            preset=body.preset,
            status="queued",
            origin="encode",
            kind=body.kind,
            source_path=str(real_source),
            library_file_id=library_file_id,
            parent_job_id=parent_job_id,
            keep_tracks=body.keep_tracks,
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        if body.kind == "encode":
            out_dir = OUTPUT_BASE / body.preset
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = out_dir / f"{stem}.{job.id}.{fmt}"
            if dest.exists():
                db.delete(job)
                db.commit()
                raise HTTPException(status_code=409, detail="Destination already exists")
            job.dest_path = str(dest)
            db.commit()

        wakeup_job_worker()
        await manager.broadcast(
            {"type": "job_queued", "job_id": job.id, "file": job.filename, "kind": job.kind}
        )
        return JSONResponse(job_to_dict(job, db))
    finally:
        db.close()


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int):
    db = SessionLocal()
    try:
        job = db.get(EncodeJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if job.status == "queued":
            job.status = "cancelled"
            job.error = "Cancelled"
            db.commit()
            await manager.broadcast(
                {"type": "encode_cancelled", "job_id": job_id, "file": job.filename}
            )
            return JSONResponse(job_to_dict(job, db))

        if job.status in ("encoding", "previewing"):
            request_cancel(job_id)
            await signal_current_proc(job_id)
            # Status finalized in encoder when proc exits; also force here if idle
            if get_current_job_id() != job_id:
                job.status = "cancelled"
                job.error = "Cancelled"
                if job.kind == "encode" and job.dest_path:
                    Path(job.dest_path).unlink(missing_ok=True)
                if job.kind == "preview":
                    if job.dest_clip_path:
                        Path(job.dest_clip_path).unlink(missing_ok=True)
                    if job.source_clip_path:
                        Path(job.source_clip_path).unlink(missing_ok=True)
                db.commit()
                clear_cancel(job_id)
            return JSONResponse({"status": "cancelling", "job_id": job_id})

        if job.status == "extracting":
            request_cancel(job_id)
            job.status = "cancelled"
            job.error = "Cancelled during extract"
            db.commit()
            clear_cancel(job_id)
            return JSONResponse(job_to_dict(job, db))

        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel job in status {job.status}",
        )
    finally:
        db.close()


@app.post("/api/encode/pause")
async def pause_encode_queue():
    """Stop picking up new jobs after the current encode/preview finishes."""
    set_queue_paused(True)
    log.info("Encode queue paused — current job will finish, then drain")
    await manager.broadcast({"type": "queue_paused", "paused": True})
    return JSONResponse(
        {
            "queue_paused": True,
            "encoding": get_current_encode_path() is not None,
            "job_id": get_current_job_id(),
        }
    )


@app.post("/api/encode/resume")
async def resume_encode_queue():
    """Resume draining queued encode/preview jobs."""
    set_queue_paused(False)
    log.info("Encode queue resumed")
    await manager.broadcast({"type": "queue_paused", "paused": False})
    return JSONResponse(
        {
            "queue_paused": False,
            "encoding": get_current_encode_path() is not None,
            "job_id": get_current_job_id(),
        }
    )


@app.get("/api/jobs/{job_id}/download")
async def download_job_output(job_id: int):
    db = SessionLocal()
    try:
        job = db.get(EncodeJob, job_id)
        if not job or not job.dest_path:
            raise HTTPException(status_code=404, detail="Job output not found")
        if job.kind == "preview":
            raise HTTPException(status_code=400, detail="Preview clips are not downloadable outputs")
        fp = Path(job.dest_path)
        if not fp.exists():
            raise HTTPException(status_code=404, detail="File missing on disk")
        # Download as original stem + dest suffix
        download_name = f"{Path(job.filename).stem}{fp.suffix}"
        return FileResponse(str(fp), filename=download_name)
    finally:
        db.close()


@app.get("/api/jobs/{job_id}/preview-frame")
async def get_preview_frame(job_id: int, index: int = 0, offset: int | None = None):
    """Return dest[index] paired with source[pad + index + offset].

    Large offsets are residual drift beyond the source-clip pad. When the short
    source clip cannot cover the requested index, we seek the original
    library/source file if available.
    """
    db = SessionLocal()
    try:
        job = db.get(EncodeJob, job_id)
        if not job or job.kind != "preview":
            raise HTTPException(status_code=404, detail="Preview job not found")
        if not job.source_clip_path or not job.dest_clip_path:
            raise HTTPException(status_code=404, detail="Preview clips not ready")
        src = Path(job.source_clip_path)
        dst = Path(job.dest_clip_path)
        if not src.exists() or not dst.exists():
            raise HTTPException(status_code=404, detail="Clip files missing")

        off = job.frame_offset if offset is None else offset
        full_source = Path(job.source_path) if job.source_path else None
        clip_start: float | None = None
        if full_source and full_source.exists():
            try:
                full_meta = await asyncio.to_thread(probe_video, full_source)
                _hb_start, _hb_len, usable_abs = preview_window_times(full_meta["duration"])
                fps = full_meta["fps"] or 30.0
                clip_start = max(0.0, usable_abs - (PREVIEW_PAD_FRAMES / fps))
            except Exception:
                clip_start = None

        def _resolve():
            return resolve_preview_pair(
                source_clip=src,
                dest_clip=dst,
                index=index,
                offset=off,
                full_source=full_source if full_source and full_source.exists() else None,
                source_clip_start=clip_start,
            )

        result = await asyncio.to_thread(_resolve)
        return JSONResponse(
            {
                "index": result["index"],
                "offset": result["offset"],
                "source_index": result["source_index"],
                "raw_source_index": result["raw_source_index"],
                "usable_frame_count": result["usable_frame_count"],
                "source_frame_count": result["source_frame_count"],
                "source_oob": result["source_oob"],
                "source_shortfall": result["source_shortfall"],
                "used_full_source": result["used_full_source"],
                "source": base64.b64encode(result["source"]).decode("ascii"),
                "dest": base64.b64encode(result["dest"]).decode("ascii"),
            }
        )
    finally:
        db.close()


@app.get("/api/jobs/{job_id}/preview-noise")
async def get_preview_noise(job_id: int):
    """Noise (MSE) of source-center frame vs every usable dest frame.

    The valley (best_index) is the encoded frame that matches the source
    center; suggested_offset locks that pair for the whole clip.
    """
    db = SessionLocal()
    try:
        job = db.get(EncodeJob, job_id)
        if not job or job.kind != "preview":
            raise HTTPException(status_code=404, detail="Preview job not found")
        if not job.source_clip_path or not job.dest_clip_path:
            raise HTTPException(status_code=404, detail="Preview clips not ready")
        src = Path(job.source_clip_path)
        dst = Path(job.dest_clip_path)
        if not src.exists() or not dst.exists():
            raise HTTPException(status_code=404, detail="Clip files missing")

        log.info(
            "preview-noise start job=%s src=%s dest=%s",
            job_id,
            src.name,
            dst.name,
        )
        t0 = time.perf_counter()
        result = await asyncio.to_thread(compute_match_noise_graph, src, dst)
        log.info(
            "preview-noise done job=%s frames=%s best=%s offset=%s cuts=%s (%.1fs)",
            job_id,
            result.get("frame_count"),
            result.get("best_index"),
            result.get("suggested_offset"),
            len(result.get("scene_cuts") or []),
            time.perf_counter() - t0,
        )
        return JSONResponse(result)
    except ValueError as exc:
        log.warning("preview-noise failed job=%s: %s", job_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        db.close()


@app.post("/api/jobs/{job_id}/preview-diff")
async def preview_diff(
    job_id: int,
    index: int = 0,
    offset: int | None = None,
    mode: str = "absdiff",
):
    if mode not in ("absdiff", "ssim_map"):
        raise HTTPException(status_code=400, detail="mode must be absdiff or ssim_map")
    log.info(
        "preview-diff start job=%s index=%s offset=%s mode=%s",
        job_id,
        index,
        offset,
        mode,
    )
    t0 = time.perf_counter()
    # Reuse frame endpoint logic then compare in memory
    frame_resp = await get_preview_frame(job_id, index=index, offset=offset)
    data = frame_resp.body
    import json as _json

    payload = _json.loads(data)
    import tempfile

    from app.diff import compare_frame_files as _cmp

    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "s.png"
        dp = Path(td) / "d.png"
        sp.write_bytes(base64.b64decode(payload["source"]))
        dp.write_bytes(base64.b64decode(payload["dest"]))
        result = await asyncio.to_thread(_cmp, sp, dp, mode)  # type: ignore[arg-type]
        log.info(
            "preview-diff done job=%s mode=%s ssim=%.4f mse=%.2f (%.1fs)",
            job_id,
            mode,
            result.get("ssim") or 0,
            result.get("mse") or 0,
            time.perf_counter() - t0,
        )
        return JSONResponse(result)


@app.patch("/api/jobs/{job_id}/frame-offset")
async def set_frame_offset(job_id: int, offset: int = 0, locked: bool | None = None):
    db = SessionLocal()
    try:
        job = db.get(EncodeJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        prev = job.frame_offset
        job.frame_offset = offset
        if locked is not None:
            job.offset_locked = locked
        from datetime import datetime, timezone

        job.updated_at = datetime.now(timezone.utc)
        db.commit()
        log.info(
            "frame-offset job=%s %s → %s locked=%s",
            job_id,
            prev,
            offset,
            job.offset_locked,
        )
        return JSONResponse(job_to_dict(job, db))
    finally:
        db.close()


@app.post("/api/jobs/{job_id}/preview-noise-score")
async def post_preview_noise_score(job_id: int, offset: int | None = None):
    """Average SSIM / PSNR / MSE over usable frames at the locked offset."""
    db = SessionLocal()
    try:
        job = db.get(EncodeJob, job_id)
        if not job or job.kind != "preview":
            raise HTTPException(status_code=404, detail="Preview job not found")
        if not job.source_clip_path or not job.dest_clip_path:
            raise HTTPException(status_code=404, detail="Preview clips not ready")
        src = Path(job.source_clip_path)
        dst = Path(job.dest_clip_path)
        if not src.exists() or not dst.exists():
            raise HTTPException(status_code=404, detail="Clip files missing")

        off = job.frame_offset if offset is None else offset
        full_source = Path(job.source_path) if job.source_path else None
        clip_start: float | None = None
        if full_source and full_source.exists():
            try:
                full_meta = await asyncio.to_thread(probe_video, full_source)
                _hb_start, _hb_len, usable_abs = preview_window_times(full_meta["duration"])
                fps = full_meta["fps"] or 30.0
                clip_start = max(0.0, usable_abs - (PREVIEW_PAD_FRAMES / fps))
            except Exception:
                clip_start = None

        log.info(
            "noise-score start job=%s offset=%s src=%s dest=%s full_source=%s",
            job_id,
            off,
            src.name,
            dst.name,
            bool(full_source and full_source.exists()),
        )
        t0 = time.perf_counter()

        def _score():
            return compute_preview_noise_score(
                source_clip=src,
                dest_clip=dst,
                offset=off,
                full_source=full_source if full_source and full_source.exists() else None,
                source_clip_start=clip_start,
            )

        try:
            result = await asyncio.to_thread(_score)
        except ValueError as exc:
            log.warning("noise-score failed job=%s: %s", job_id, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        job.frame_offset = off
        job.noise_ssim_mean = result["ssim_mean"]
        job.noise_ssim_std = result["ssim_std"]
        job.noise_psnr_mean = result["psnr_mean"]
        job.noise_psnr_std = result["psnr_std"]
        job.noise_mse_mean = result["mse_mean"]
        job.noise_mse_std = result["mse_std"]
        job.noise_frame_count = result["frame_count"]
        from datetime import datetime, timezone

        job.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(job)

        log.info(
            "noise-score done job=%s frames=%s skipped=%s "
            "ssim=%.4f±%.4f psnr=%s mse=%.2f (%.1fs)",
            job_id,
            result["frame_count"],
            result["skipped"],
            result["ssim_mean"],
            result["ssim_std"],
            f"{result['psnr_mean']:.2f}" if result["psnr_mean"] is not None else "—",
            result["mse_mean"],
            time.perf_counter() - t0,
        )
        return JSONResponse({**result, "job": job_to_dict(job, db)})
    finally:
        db.close()


# ── Activity log ───────────────────────────────────────────────────────────
@app.get("/api/queue")
@app.get("/queue")
async def get_queue():
    if not LOG_FILE.exists():
        return JSONResponse({"lines": []})
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines()
    except Exception as e:
        return JSONResponse({"lines": [], "error": str(e)})

    parsed = []
    for line in lines[-80:]:
        line = line.strip()
        if not line:
            continue
        entry: dict = {"raw": line, "status": "info", "progress": None}
        lo = line.lower()
        if "encoding" in lo or "encode" in lo or "preview" in lo:
            entry["status"] = "encoding"
        elif "done" in lo or "complete" in lo or "muxing" in lo:
            entry["status"] = "done"
        elif "error" in lo or "fail" in lo:
            entry["status"] = "error"
        elif "detected" in lo or "queued" in lo:
            entry["status"] = "queued"
        m = re.search(r"(\d+\.\d+)\s*%", line)
        if m:
            entry["progress"] = float(m.group(1))
            entry["status"] = "encoding"
        parsed.append(entry)

    return JSONResponse({"lines": parsed[-12:]})


@app.post("/api/queue/clear")
@app.post("/queue/clear")
async def clear_queue():
    try:
        LOG_FILE.write_text("")
        return JSONResponse({"status": "cleared"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Outputs ────────────────────────────────────────────────────────────────
@app.get("/api/outputs")
@app.get("/outputs")
async def list_outputs():
    db = SessionLocal()
    try:
        jobs = (
            db.query(EncodeJob)
            .filter(
                EncodeJob.kind == "encode",
                EncodeJob.status == "done",
                EncodeJob.dest_path.isnot(None),
            )
            .order_by(EncodeJob.id.desc())
            .all()
        )
        files = []
        for job in jobs:
            fp = Path(job.dest_path) if job.dest_path else None
            if not fp or not fp.exists():
                continue
            size = fp.stat().st_size
            files.append(
                {
                    "job_id": job.id,
                    "name": fp.name,
                    "display_name": job.filename,
                    "preset": job.preset,
                    "size": size,
                    "size_human": human_size(size),
                    "source_label": _source_label(job, db),
                    "download_url": f"/api/jobs/{job.id}/download",
                    "delete_url": f"/api/delete/{job.preset}/{fp.name}",
                }
            )
        return JSONResponse({"files": files})
    finally:
        db.close()


@app.get("/api/download/{preset}/{filename}")
@app.get("/download/{preset}/{filename}")
async def download_file(preset: str, filename: str):
    fp = OUTPUT_BASE / preset / filename
    if not fp.exists() or not fp.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if not is_under(fp, OUTPUT_BASE):
        raise HTTPException(status_code=403, detail="Access denied")
    # Prefer original filename from job
    db = SessionLocal()
    try:
        job = (
            db.query(EncodeJob)
            .filter(EncodeJob.dest_path == str(fp))
            .order_by(EncodeJob.id.desc())
            .first()
        )
        dl_name = f"{Path(job.filename).stem}{fp.suffix}" if job else filename
    finally:
        db.close()
    return FileResponse(str(fp), filename=dl_name)


@app.delete("/api/delete/{preset}/{filename}")
@app.delete("/delete/{preset}/{filename}")
async def delete_output_file(preset: str, filename: str):
    fp = OUTPUT_BASE / preset / filename
    if not is_under(fp, OUTPUT_BASE):
        raise HTTPException(status_code=403, detail="Access denied")
    if not fp.exists():
        raise HTTPException(status_code=404, detail="File not found")

    dest_str = str(fp)
    current = get_current_encode_path()
    if current and Path(current).resolve() == fp.resolve():
        raise HTTPException(status_code=409, detail="File is currently encoding")

    db = SessionLocal()
    try:
        # Find job owning this dest
        owner = (
            db.query(EncodeJob)
            .filter(EncodeJob.dest_path == dest_str)
            .order_by(EncodeJob.id.desc())
            .first()
        )
        if owner:
            child_active = (
                db.query(EncodeJob)
                .filter(
                    EncodeJob.parent_job_id == owner.id,
                    EncodeJob.status.in_(("queued", "encoding", "previewing")),
                )
                .count()
            )
            if child_active:
                raise HTTPException(
                    status_code=409,
                    detail="Output is source of an active child job",
                )
        fp.unlink()
        return JSONResponse({"status": "deleted"})
    finally:
        db.close()


@app.get("/api/status")
@app.get("/status")
async def system_status():
    enc_path = get_current_encode_path()
    encoding_size = 0
    if enc_path:
        p = Path(enc_path)
        if p.exists():
            encoding_size = p.stat().st_size
    return JSONResponse(
        {
            "cpu_pct": None,
            "encoding": enc_path is not None,
            "encoding_file": Path(enc_path).name if enc_path else None,
            "encoding_path": enc_path,
            "encoding_size": encoding_size,
            "encoding_size_human": human_size(encoding_size) if encoding_size else None,
            "job_id": get_current_job_id(),
            "queue_paused": is_queue_paused(),
            **disk_stats(),
        }
    )


# ── Jobs / comparison ──────────────────────────────────────────────────────
@app.get("/api/jobs")
async def list_jobs(limit: int = 50):
    db = SessionLocal()
    try:
        jobs = (
            db.query(EncodeJob)
            .options(
                joinedload(EncodeJob.frames),
                joinedload(EncodeJob.library_file),
                joinedload(EncodeJob.parent_job),
            )
            .order_by(EncodeJob.id.desc())
            .limit(min(limit, 200))
            .all()
        )
        return JSONResponse({"jobs": [job_to_dict(j, db) for j in jobs]})
    finally:
        db.close()


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int):
    db = SessionLocal()
    try:
        job = (
            db.query(EncodeJob)
            .options(
                joinedload(EncodeJob.frames),
                joinedload(EncodeJob.library_file),
                joinedload(EncodeJob.parent_job),
            )
            .filter(EncodeJob.id == job_id)
            .first()
        )
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return JSONResponse(job_to_dict(job, db))
    finally:
        db.close()


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: int):
    db = SessionLocal()
    try:
        job = db.get(EncodeJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if job.status in ("queued", "encoding", "previewing", "extracting"):
            raise HTTPException(
                status_code=409,
                detail="Job is still running — cancel it first",
            )
        if get_current_job_id() == job_id:
            raise HTTPException(status_code=409, detail="Job is currently encoding")

        children = (
            db.query(EncodeJob).filter(EncodeJob.parent_job_id == job_id).count()
        )
        if children:
            raise HTTPException(
                status_code=409,
                detail="Job is the source of another encode",
            )

        _cleanup_job_files(job)
        db.delete(job)
        db.commit()
        await manager.broadcast({"type": "job_deleted", "job_id": job_id})
        return JSONResponse({"status": "deleted", "job_id": job_id})
    finally:
        db.close()


@app.post("/api/comparisons/{job_id}/diff")
async def run_diff(job_id: int, position: float = 0.5, mode: str = "absdiff"):
    if mode not in ("absdiff", "ssim_map"):
        raise HTTPException(status_code=400, detail="mode must be absdiff or ssim_map")
    db = SessionLocal()
    try:
        frame = (
            db.query(ComparisonFrame)
            .filter(
                ComparisonFrame.job_id == job_id,
                ComparisonFrame.position == position,
            )
            .first()
        )
        if not frame:
            frames = (
                db.query(ComparisonFrame)
                .filter(ComparisonFrame.job_id == job_id)
                .all()
            )
            frame = next(
                (f for f in frames if abs(f.position - position) < 0.01), None
            )
        if not frame:
            raise HTTPException(status_code=404, detail="Comparison frame not found")

        src = Path(frame.source_png)
        dst = Path(frame.dest_png)
        if not src.exists() or not dst.exists():
            raise HTTPException(status_code=404, detail="Frame image missing on disk")

        t0 = time.perf_counter()
        result = await asyncio.to_thread(compare_frame_files, src, dst, mode)  # type: ignore[arg-type]
        log.info(
            "compare-diff job=%s pos=%.2f mode=%s ssim=%.4f (%.1fs)",
            job_id,
            position,
            mode,
            result.get("ssim") or 0,
            time.perf_counter() - t0,
        )
        return JSONResponse(result)
    finally:
        db.close()


# ── External compare upload ────────────────────────────────────────────────
@app.post("/api/comparisons/external/chunk")
async def external_compare_chunk(
    background_tasks: BackgroundTasks,
    chunk: UploadFile = File(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
    side: str = Form(...),  # "source" | "dest"
    session_id: str = Form(...),
    compression: str = Form("none"),
):
    if side not in ("source", "dest"):
        raise HTTPException(status_code=400, detail="side must be source or dest")

    safe_filename = Path(filename).name
    session_dir = UPLOAD_TMP / "external" / Path(session_id).name
    session_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = session_dir / f"{side}.part"

    chunk_data = await chunk.read()
    if compression == "gzip":
        chunk_data = gzip.decompress(chunk_data)
    elif compression == "zstd":
        chunk_data = zstd.ZstdDecompressor().decompress(chunk_data)

    write_mode = "ab" if chunk_index > 0 else "wb"
    with open(tmp_path, write_mode) as f:
        f.write(chunk_data)

    if chunk_index < total_chunks - 1:
        return JSONResponse({"status": "ok", "chunk": chunk_index, "side": side})

    final_path = session_dir / f"{side}_{safe_filename}"
    shutil.move(str(tmp_path), str(final_path))
    (session_dir / f"{side}.name").write_text(safe_filename)

    source_name = session_dir / "source.name"
    dest_name = session_dir / "dest.name"
    if not (source_name.exists() and dest_name.exists()):
        return JSONResponse({"status": "waiting", "side": side})

    src_final = next(session_dir.glob("source_*"), None)
    dst_final = next(session_dir.glob("dest_*"), None)
    if not src_final or not dst_final:
        raise HTTPException(status_code=500, detail="Upload finalize failed")

    db = SessionLocal()
    try:
        job = EncodeJob(
            filename=f"{source_name.read_text()} ↔ {dest_name.read_text()}",
            preset="external",
            status="queued",
            origin="external",
            kind="encode",
            source_path=str(src_final),
            dest_path=str(dst_final),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    background_tasks.add_task(extract_only, job_id, src_final, dst_final)
    return JSONResponse({"status": "complete", "job_id": job_id})
