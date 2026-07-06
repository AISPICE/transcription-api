FROM python:3.12-slim

# ffmpeg is required by yt-dlp to extract/convert audio from downloaded
# video, and by AssemblyAI-bound files that need format normalization.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copied and installed before the app code so Docker's layer cache can skip
# reinstalling dependencies on every rebuild when only app/ changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1
# Cloud Run injects the PORT env var at runtime and expects the container to
# listen on it; 8080 is just the local default for `docker run` outside Cloud Run.
ENV PORT=8080
EXPOSE 8080

# Single uvicorn process, no extra --workers: Cloud Run's own concurrency
# setting controls how many simultaneous requests one instance handles, and
# FastAPI's async event loop already serves many concurrent requests within
# one process. Running multiple OS worker processes here would just split
# that single instance's CPU allocation between processes for no benefit.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
