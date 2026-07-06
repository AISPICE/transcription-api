"""Job creation, status updates, and result storage in Firestore.

Schema (collection `jobs`, one document per job_id):

  status:                 "queued" | "processing" | "done" | "failed"
  mode:                    "text" | "words" | "translate" | "summary"
  source:                  original input string (URL or gs://...)
  target_language:         str | None   (only set for mode == "translate")
  result:                   dict | None  (shape depends on mode, see pipeline.py)
  error:                    str | None
  engine:                   str | None   ("assemblyai" | "youtube_captions")
  caption_shortcut_used:    bool
  created_at / updated_at:  UTC timestamps
  expires_at:               created_at + 7 days

`expires_at` is meant to back a Firestore TTL policy (a free, built-in
feature you enable once on the collection) so finished job documents delete
themselves after a week instead of accumulating forever. 7 days is a
starting guess for a beginner project -- shrink or drop it once you know how
long you actually need results to stay pollable.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from google.cloud import firestore

from .config import settings

_db = firestore.Client(project=settings.gcp_project_id)
_collection = _db.collection(settings.firestore_collection)


def create_job(mode: str, source: str, target_language: str | None = None) -> str:
    job_id = uuid.uuid4().hex
    now = dt.datetime.now(dt.timezone.utc)
    _collection.document(job_id).set(
        {
            "status": "queued",
            "mode": mode,
            "source": source,
            "target_language": target_language,
            "result": None,
            "error": None,
            "engine": None,
            "caption_shortcut_used": False,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + dt.timedelta(days=7),
        }
    )
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    snap = _collection.document(job_id).get()
    return snap.to_dict() if snap.exists else None


def mark_processing(job_id: str) -> None:
    _update(job_id, {"status": "processing"})


def mark_done(job_id: str, result: dict[str, Any], engine: str, caption_shortcut_used: bool) -> None:
    _update(
        job_id,
        {
            "status": "done",
            "result": result,
            "engine": engine,
            "caption_shortcut_used": caption_shortcut_used,
        },
    )


def mark_failed(job_id: str, error: str) -> None:
    _update(job_id, {"status": "failed", "error": error})


def _update(job_id: str, fields: dict[str, Any]) -> None:
    fields["updated_at"] = dt.datetime.now(dt.timezone.utc)
    _collection.document(job_id).update(fields)
