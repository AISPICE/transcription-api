from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from . import jobs, pipeline, tasks_queue
from .config import settings
from .security import require_api_key, require_internal_key

app = FastAPI(title="Transcription API")


class TranscribeRequest(BaseModel):
    source: str  # YouTube/Instagram/TikTok URL, direct media URL, or gs://...


class TranslateRequest(TranscribeRequest):
    target_language: str


def _create_and_enqueue(mode: str, source: str, target_language: str | None = None) -> dict:
    job_id = jobs.create_job(mode=mode, source=source, target_language=target_language)
    tasks_queue.enqueue_job(job_id)
    return {"job_id": job_id, "status": "queued"}


@app.post("/transcribe/text", dependencies=[Depends(require_api_key)])
def transcribe_text(body: TranscribeRequest):
    return _create_and_enqueue("text", body.source)


@app.post("/transcribe/words", dependencies=[Depends(require_api_key)])
def transcribe_words(body: TranscribeRequest):
    return _create_and_enqueue("words", body.source)


@app.post("/transcribe/translate", dependencies=[Depends(require_api_key)])
def transcribe_translate(body: TranslateRequest):
    return _create_and_enqueue("translate", body.source, target_language=body.target_language)


@app.post("/transcribe/summary", dependencies=[Depends(require_api_key)])
def transcribe_summary(body: TranscribeRequest):
    return _create_and_enqueue("summary", body.source)


@app.post("/transcribe/text/sync", dependencies=[Depends(require_api_key)])
def transcribe_text_sync(body: TranscribeRequest):
    """Same as /transcribe/text but runs inline and blocks until done,
    returning the transcript directly instead of a job_id to poll. Still
    creates a job document (for Firestore visibility/debugging) but skips
    Cloud Tasks entirely -- see pipeline.run_job for the actual work."""
    job_id = jobs.create_job(mode="text", source=body.source)
    pipeline.run_job(job_id)
    job = jobs.get_job(job_id)
    if job["status"] == "failed":
        raise HTTPException(status_code=500, detail=job["error"])
    return {"text": job["result"]["text"]}


@app.get("/status/{job_id}", dependencies=[Depends(require_api_key)])
def get_status(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.post("/uploads/init", dependencies=[Depends(require_api_key)])
def init_upload(filename: str):
    """Step 1 of the large-file upload flow: hand back a signed GCS URL the
    client PUTs the file to directly, bypassing Cloud Run's 32MB request body
    cap entirely. The client then calls one of the /transcribe/* endpoints
    with {"source": gcs_path} from the response -- at that point it's just
    the ordinary GCS case in sources.py, no special-casing needed downstream.
    """
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(settings.upload_bucket)
    ext = Path(filename).suffix or ".mp4"
    blob_path = f"uploads/{uuid.uuid4().hex}{ext}"
    blob = bucket.blob(blob_path)
    upload_url = blob.generate_signed_url(
        version="v4",
        expiration=900,
        method="PUT",
        content_type="application/octet-stream",
    )
    return {"upload_url": upload_url, "gcs_path": f"gs://{settings.upload_bucket}/{blob_path}"}


@app.post("/internal/process/{job_id}", dependencies=[Depends(require_internal_key)])
def internal_process(job_id: str):
    """Called only by Cloud Tasks, never by a public client -- guarded by
    X-Internal-Key rather than X-API-Key. See tasks_queue.py."""
    pipeline.run_job(job_id)
    return {"ok": True}
