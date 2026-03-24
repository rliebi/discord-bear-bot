# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Prevent Python from writing pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

# Create non-root user
RUN useradd -ms /bin/bash appuser

WORKDIR /app

# Install system deps (none heavy needed) and python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src ./src

# Prepare data directory and permissions
RUN mkdir -p /data && chown -R appuser:appuser /data /app
VOLUME ["/data"]

USER appuser

# Healthcheck: ensure Python can import and data dir exists
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python - <<'PY' || exit 1
import os, importlib
ok = True
try:
    importlib.import_module('src.bot')
except Exception:
    ok = False
print('OK' if ok and os.path.isdir('/data') else 'FAIL')
exit(0 if ok and os.path.isdir('/data') else 1)
PY

# Entrypoint
ENV DISCORD_TOKEN=""
CMD ["python", "-m", "src.bot"]
