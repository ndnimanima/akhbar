FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=8080

WORKDIR /app

# ffmpeg powers the video analyzer (frames + full media specs). It is optional:
# without it the analyzer falls back to its pure-Python MP4 probe.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY main.py ./

# The container runs as a non-root user; /data is a volume mount point.
RUN useradd --create-home --uid 10001 rozaneh \
    && mkdir -p /data \
    && chown -R rozaneh:rozaneh /app /data
USER rozaneh

EXPOSE 8080

HEALTHCHECK --interval=45s --timeout=6s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
