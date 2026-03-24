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
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD sh -c "python -c \"import os, importlib, sys; ok=True;\n\ntry:\n    importlib.import_module('src.bot')\nexcept Exception:\n    ok=False\n\nsys.exit(0 if ok and os.path.isdir('/data') else 1)\"" 

# Entrypoint
CMD ["python", "-m", "src.bot"]
