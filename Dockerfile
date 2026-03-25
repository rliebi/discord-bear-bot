# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Prevent Python from writing pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

# Workdir
WORKDIR /app

# Install python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src ./src
COPY main /app/main
RUN chmod +x /app/main

# Do NOT create /data or change ownership here to avoid chown on volumes
# (The runtime volume mount will create it as needed.)

# Healthcheck: simple liveness check (PID 1 running)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD sh -c "kill -0 1 || exit 1"

# Entrypoint
CMD ["/app/main"]
