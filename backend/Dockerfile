FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    handbrake-cli \
    procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic.ini .
COPY alembic ./alembic
COPY logging.json ./logging.json

ENV ENCODER_DATA_DIR=/data
ENV HANDBRAKE_PRESET_JSON=/config/presets/Super8Scan.json
ENV PYTHONUNBUFFERED=1

RUN mkdir -p /data /config/presets

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-config logging.json --access-log"]
