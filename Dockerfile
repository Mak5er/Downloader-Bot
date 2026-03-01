# syntax=docker/dockerfile:1.7

############################
# 1) Builder stage
# This stage builds Python dependency wheels
############################
FROM python:3.10-slim AS builder

# Environment optimizations
ENV TZ=UTC \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install build dependencies (only needed to compile Python packages)
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc \
      build-essential \
      libffi-dev \
      libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements first to maximize layer caching
COPY requirements.txt .

# Build dependency wheels (faster rebuilds with cache)
RUN pip install -U pip setuptools wheel && \
    pip wheel --wheel-dir /wheels -r requirements.txt


############################
# 2) Runtime stage
# Final lightweight image
############################
FROM python:3.10-slim

ENV TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install only runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy pre-built wheels from builder stage
COPY --from=builder /wheels /wheels
COPY requirements.txt .

# Install dependencies from wheels (no compilation here)
RUN pip install -U pip && \
    pip install --no-cache-dir /wheels/*

# Copy application source code
COPY . .

# Default container entrypoint
CMD ["python", "main.py"]
