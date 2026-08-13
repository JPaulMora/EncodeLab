"""Online Encoder API — FastAPI entrypoint."""
from __future__ import annotations

import asyncio
import gzip
import logging
import re
import shutil
import sys
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
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import joinedload

from app.config import (
    LOG_FILE,
    MEDIA_BASE,
    OUTPUT_BASE,
    PRESET_MAP,
    UPLOAD_TMP,
    WATCH_BASE,
    ensure_dirs,
    human_size,
)
from app.db import SessionLocal, init_db
from app.diff import compare_frame_files
from app.encoder import extract_only, get_current_encode_file
from app.models import ComparisonFrame, EncodeJob
from app.watcher import broadcast_system_loop, broadcast_watch_update, list_watch_files, watch_folders
from app.ws import manager

# ── Logging ────────────────────────────────────────────────────────────────
ensure_dirs()
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    init_db()
    asyncio.create_task(watch_folders())
    asyncio.create_task(broadcast_system_loop())
    yield


app = FastAPI(title="Online Encoder", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


def job_to_dict(job: EncodeJob) -> dict:
    return {
        "id": job.id,
        "filename": job.filename,
        "preset": job.preset,
        "status": job.status,
        "origin": job.origin,
        "progress": job.progress,
        "fps": job.fps,
        "eta_seconds": job.eta_seconds,
        "output_size": job.output_size,
        "output_size_human": human_size(job.output_size) if job.output_size else None,
        "error": job.error,
        "dest_path": job.dest_path,
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
    }


# ── Health / presets ───────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/presets")
@app.get("/presets")
async def get_presets():
    presets = sorted(PRESET_MAP.keys())
    # Prefer folders that exist on disk
    if WATCH_BASE.exists():
        on_disk = sorted(d.name for d in WATCH_BASE.iterdir() if d.is_dir())
        if on_disk:
            presets = on_disk
    return JSONResponse({"presets": presets})


# ── WebSocket ──────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_json({"type": "watch_update", "files": list_watch_files()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


# ── Upload (encode) ────────────────────────────────────────────────────────
@app.post("/api/upload/chunk")
@app.post("/upload/chunk")
async def upload_chunk(
    chunk: UploadFile = File(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
    preset: str = Form(...),
    compression: str = Form("none"),
    compressed: Optional[str] = Form(None),
):
    if compressed is not None and compression == "none":
        if compressed.lower() in ("true", "1", "yes"):
            compression = "gzip"

    if preset not in PRESET_MAP:
        raise HTTPException(status_code=400, detail=f"Invalid preset: {preset}")

    safe_filename = Path(filename).name
    tmp_path = UPLOAD_TMP / safe_filename

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
        dest_dir = WATCH_BASE / preset
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / safe_filename
        shutil.move(str(tmp_path), str(dest_path))
        size = dest_path.stat().st_size

        db = SessionLocal()
        try:
            job = EncodeJob(
                filename=safe_filename,
                preset=preset,
                status="queued",
                origin="encode",
                source_path=str(dest_path),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
        finally:
            db.close()

        await manager.broadcast(
            {
                "type": "upload_complete",
                "filename": safe_filename,
                "size": size,
                "job_id": job_id,
            }
        )
        return JSONResponse(
            {"status": "complete", "dest": str(dest_path), "job_id": job_id}
        )

    return JSONResponse({"status": "ok", "chunk": chunk_index, "total": total_chunks})


# ── Watch queue ────────────────────────────────────────────────────────────
@app.get("/api/watch_queue")
@app.get("/watch_queue")
async def get_watch_queue():
    return JSONResponse(list_watch_files())


@app.delete("/api/watch/{folder}/{filename}")
@app.delete("/watch/{folder}/{filename}")
async def delete_watch_file(folder: str, filename: str):
    safe_filename = Path(filename).name
    fp = WATCH_BASE / folder / safe_filename
    try:
        fp.resolve().relative_to(WATCH_BASE.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    if not fp.exists():
        raise HTTPException(status_code=404, detail="File not found")
    fp.unlink()
    await broadcast_watch_update()
    return JSONResponse({"status": "deleted"})


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
        if "encoding" in lo or "encode" in lo:
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

    return JSONResponse({"lines": parsed})


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
    if not OUTPUT_BASE.exists():
        return JSONResponse({"files": []})
    files = []
    for f in OUTPUT_BASE.rglob("*"):
        if f.is_file() and f.name != ".gitkeep":
            rel = f.relative_to(OUTPUT_BASE)
            parts = rel.parts
            preset = parts[0] if len(parts) > 1 else "unknown"
            fname = parts[-1]
            size = f.stat().st_size
            files.append(
                {
                    "name": fname,
                    "preset": preset,
                    "size": size,
                    "size_human": human_size(size),
                    "download_url": f"/download/{preset}/{fname}",
                    "delete_url": f"/delete/{preset}/{fname}",
                }
            )
    files.sort(key=lambda x: x["name"])
    return JSONResponse({"files": files})


@app.get("/api/download/{preset}/{filename}")
@app.get("/download/{preset}/{filename}")
async def download_file(preset: str, filename: str):
    fp = OUTPUT_BASE / preset / filename
    if not fp.exists() or not fp.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        fp.resolve().relative_to(OUTPUT_BASE.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    return FileResponse(str(fp), filename=filename)


@app.delete("/api/delete/{preset}/{filename}")
@app.delete("/delete/{preset}/{filename}")
async def delete_output_file(preset: str, filename: str):
    fp = OUTPUT_BASE / preset / filename
    try:
        fp.resolve().relative_to(OUTPUT_BASE.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    if not fp.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if get_current_encode_file() == filename:
        raise HTTPException(status_code=409, detail="File is currently encoding")
    fp.unlink()
    return JSONResponse({"status": "deleted"})


@app.get("/api/status")
@app.get("/status")
async def system_status():
    enc_file = get_current_encode_file()
    encoding_size = 0
    if enc_file:
        for f in OUTPUT_BASE.rglob(enc_file):
            if f.is_file():
                encoding_size = f.stat().st_size
                break
    return JSONResponse(
        {
            "cpu_pct": None,
            "encoding": enc_file is not None,
            "encoding_file": enc_file,
            "encoding_size": encoding_size,
            "encoding_size_human": human_size(encoding_size) if encoding_size else None,
        }
    )


# ── Jobs / comparison ──────────────────────────────────────────────────────
@app.get("/api/jobs")
async def list_jobs(limit: int = 50):
    db = SessionLocal()
    try:
        jobs = (
            db.query(EncodeJob)
            .options(joinedload(EncodeJob.frames))
            .order_by(EncodeJob.id.desc())
            .limit(min(limit, 200))
            .all()
        )
        return JSONResponse({"jobs": [job_to_dict(j) for j in jobs]})
    finally:
        db.close()


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int):
    db = SessionLocal()
    try:
        job = (
            db.query(EncodeJob)
            .options(joinedload(EncodeJob.frames))
            .filter(EncodeJob.id == job_id)
            .first()
        )
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return JSONResponse(job_to_dict(job))
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
            # Allow small float mismatch
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

        result = await asyncio.to_thread(compare_frame_files, src, dst, mode)  # type: ignore[arg-type]
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

    # Finalize this side
    final_path = session_dir / f"{side}_{safe_filename}"
    shutil.move(str(tmp_path), str(final_path))

    # Store filename marker
    (session_dir / f"{side}.name").write_text(safe_filename)

    source_name = session_dir / "source.name"
    dest_name = session_dir / "dest.name"
    if not (source_name.exists() and dest_name.exists()):
        return JSONResponse({"status": "waiting", "side": side})

    # Both sides ready — create job and extract
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
