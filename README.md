# Online Encoder

HandBrake encoding web UI with post-encode frame comparison.

Built by merging the encoder dashboard and Bitrate Buddy comparator into one FastAPI + SQLite + SvelteKit app.

## Features

- **Library** — upload videos once (no preset); browse and re-use sources
- **Encode** — pick a library file or prior output + HandBrake preset; watch-ticket queue
- **Preview** — short HandBrake range around 25% with auto frame-align before a full encode
- **Compare** — source vs dest stills (full encode) or scrubbed preview clips with offset
- Overlay with opacity, side-by-side, abs-diff, and server SSIM / heatmap maps
- **External compare** — upload source + encoded pair when encoding happened elsewhere

## Stack

| Layer | Tech |
|-------|------|
| API | FastAPI, asyncio, WebSockets |
| DB | SQLite (SQLAlchemy) |
| Encode | HandBrakeCLI |
| Frames / diff | ffmpeg, OpenCV, scikit-image |
| UI | SvelteKit 5 |

## Quick start (local)

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Needs HandBrakeCLI + ffmpeg on PATH
uvicorn app.main:app --reload --port 8000

# Frontend (another terminal)
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 (Vite proxies `/api` and `/ws` to :8000).

## Docker

```bash
make up
make makemigrations m="description"   # after model changes
make migrate
```

Data lives in the `encoder_data` named volume under `/data` (`library/`, `watch/`, `output/`).

## Layout

```
backend/app/     FastAPI API, encoder, watcher, frames, diff
frontend/        SvelteKit Encode + Compare tabs
config/presets/  HandBrake preset JSON (e.g. Super8Scan.json)
```
