# EncodeLab

HandBrake encoding lab with post-encode frame comparison — encode, preview, and measure quality in one place.

The compare workflow is based on **Bitrate Buddy**, the original `video-comparator` project (overlay, absdiff, SSIM maps, and side-by-side stills). EncodeLab folds that comparator into a full encode UI with library uploads, HandBrake presets, watch queues, and preview/noise scoring.

![EncodeLab frame comparator — absdiff heatmap and SSIM map](Screenshot%20Frame%20Comparator.png)

## Features

- **Library** — upload videos once; browse and re-use sources across presets
- **Encode** — HandBrakeCLI presets, watch-ticket queue, live WebSocket progress
- **Preview** — short encode range with frame align before committing to a full run
- **Compare** — source vs dest stills, opacity overlay, scrub + frame offset
- **Diff maps** — server absdiff heatmap and SSIM map (plus SSIM / PSNR / MSE scores)
- **Match noise** — auto-align offset and locked-offset noise score across sampled frames
- **External compare** — upload a source + encoded pair when encoding happened elsewhere

## Stack

| Layer | Tech |
|-------|------|
| API | FastAPI, asyncio, WebSockets |
| DB | SQLite (SQLAlchemy + Alembic) |
| Encode | HandBrakeCLI |
| Frames / diff | ffmpeg, OpenCV, scikit-image |
| UI | SvelteKit |

## Quick start (Docker)

```bash
make up
```

- UI: http://localhost:5173  
- API: http://localhost:8000  

```bash
make logs                    # follow api + svelte
make makemigrations m="…"    # after model changes
make migrate
```

Data lives in the `encoder_data` named volume under `/data` (`library/`, `watch/`, `output/`, `media/`).

## Local (no Docker)

Needs **HandBrakeCLI** and **ffmpeg** on `PATH`.

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend (another terminal)
cd frontend
npm install
npm run dev
```

Vite proxies `/api` and `/ws` to `:8000`.

## Nginx reverse proxy

Library and external-compare uploads POST **16 MiB chunks**. Nginx’s default `client_max_body_size` is **1m**, which returns **413 Request Entity Too Large**.

Use the example at [`config/nginx/encodelab.conf`](config/nginx/encodelab.conf):

1. Point `server_name` at your host.
2. Keep `client_max_body_size` at **64m** (or `0` to disable the limit). Chunks are 16 MiB plus multipart overhead — `1m` will fail.
3. Proxy `/api/` and `/media/` to the API (`:8000`), `/ws` with WebSocket upgrade headers, and `/` to the UI (`:5173`).
4. Leave `proxy_request_buffering off` so nginx streams each chunk instead of spooling it first.
5. Keep the long `proxy_read_timeout` — encodes can run for hours while the UI holds `/ws`.

```bash
sudo cp config/nginx/encodelab.conf /etc/nginx/sites-available/encodelab
# move the `map` block into http {} if this host already has an nginx.conf
sudo ln -sf /etc/nginx/sites-available/encodelab /etc/nginx/sites-enabled/encodelab
sudo nginx -t && sudo systemctl reload nginx
```

If nginx only fronts the UI (`:5173`) and Vite still proxies `/api`, you still need `client_max_body_size 64m` on that server — the browser hits nginx before Vite.

For HTTPS, wrap the same `location` blocks in a `listen 443 ssl` server (certbot / your own certs). Do not lower `client_max_body_size` on the SSL server.

## Layout

```
backend/app/     FastAPI — encoder, watcher, frames, diff
frontend/        SvelteKit — Encode + Compare
config/presets/  HandBrake preset JSON
```

## Credits

- **Bitrate Buddy** (`video-comparator`) — original frame comparator this app’s Compare tab builds on
- Local HandBrake encode UI reference (`encoder_webui`) — encode / watch-queue patterns
