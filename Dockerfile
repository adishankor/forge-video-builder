# FORGE Video Builder — Docker image for Render.com
# Installs ffmpeg (video assembly) and espeak-ng (TTS fallback)

FROM python:3.11-slim

# Install system packages needed for video processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer — only rebuilds if requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app.py .

# Gunicorn: 1 worker (free tier RAM limit), 10 min timeout for long video builds
CMD gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --workers 1 \
    --timeout 600 \
    --keep-alive 5 \
    --log-level info
