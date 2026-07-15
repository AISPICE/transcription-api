"""Job creation, status updates, and result storage in Firestore.

Schema (collection `jobs`, one document per job_id):

  status:                 "queued" | "processing" | "done" | "failed"
  mode:                    "text" | "words" | "translate" | "summary"
  source:                  original input string (URL or gs://...)
  target_language:         str | None   (only set for mode == "translate")
  disfluencies:             bool         (whether AssemblyAI was asked to keep fillers)
  result:                   dict | None  (shape depends on mode, see pipeline.py)
  error:                    str | None
  engine:                   str | None   ("assemblyai" | "youtube_captions")
  caption_shortcut_used:    bool
  cache_key:                str | None   (byte-hash cache key for GCS sources, or the
                                           source-string key for everything else; set
                                           only when this job actually downloaded+hashed
                                           or transcribed -- see pipeline.py)
  path_cache_key:            str | None   (GCS sources only: key on the resolved gs://
                                           path/storage.googleapis.com URL + mode +
                                           disfluencies, set whenever the source is known
                                           -- lets a repeat call on the same path skip the
                                           download entirely, see pipeline.py)
  transcript:                dict | None  (raw TranscriptResult, mode-independent --
                                           what a cache hit on this job replays)
  created_at / updated_at:  UTC timestamps
  expires_at:               created_at + 7 days

`expires_at` is meant to back a Firestore TTL policy (a free, built-in
feature you enable once on the collection) so finished job documents delete
themselves after a week instead of accumulating forever. 7 days is a
starting guess for a beginner project -- shrink or drop it once you know how
long you actually need results to stay pollable. The per-source transcript
cache (see pipeline.py) deliberately reuses this same collection and TTL --
cache_key/transcript ride along on ordinary job documents rather than living
in a separate longer-lived store, so a cached result ages out after 7 days
exactly like everything else here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from google.cloud import firestore

from .config import settings

_db = firestore.Client(project=settings.gcp_project_id)
_collection = _db.collection(settings.firestore_collection)


def create_job(
    mode: str,
    source: str,
    target_language: str | None = None,
    disfluencies: bool = False,
) -> str:
    job_id = uuid.uuid4().hex
    now = dt.datetime.now(dt.timezone.utc)
    _collection.document(job_id).set(
        {
            "status": "queued",
            "mode": mode,
            "source": source,
            "target_language": target_language,
            "disfluencies": disfluencies,
            "result": None,
            "error": None,
            "engine": None,
            "caption_shortcut_used": False,
            "cache_key": None,
            "path_cache_key": None,
            "transcript": None,
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


def mark_done(
    job_id: str,
    result: dict[str, Any],
    engine: str,
    caption_shortcut_used: bool,
    cache_key: str | None = None,
    path_cache_key: str | None = None,
    transcript: dict[str, Any] | None = None,
) -> None:
    _update(
        job_id,
        {
            "status": "done",
            "result": result,
            "engine": engine,
            "caption_shortcut_used": caption_shortcut_used,
            "cache_key": cache_key,
            "path_cache_key": path_cache_key,
            "transcript": transcript,
        },
    )


def mark_failed(job_id: str, error: str) -> None:
    _update(job_id, {"status": "failed", "error": error})


def find_cached_transcript(cache_key: str, *, field: str = "cache_key") -> dict[str, Any] | None:
    """Look for a prior "done" job whose transcript was produced under the
    same cache key (same resolved input + disfluencies + mode -- see
    pipeline.py for how the key is built). Both filters are plain equality,
    so this doesn't need a composite Firestore index. Returns the raw
    TranscriptResult dict (not the mode-shaped `result` payload) so the
    caller can replay it for any mode.

    `field` selects which key to match against: the default "cache_key" is
    the byte-hash key used by every source kind; GCS sources additionally
    pass field="path_cache_key" for the cheap pre-download check on the
    resolved gs://path/storage.googleapis.com URL -- see pipeline.py's
    _get_transcript for why GCS needs both.
    """
    docs = list(
        _collection.where(field, "==", cache_key).where("status", "==", "done").limit(1).stream()
    )
    if not docs:
        return None
    return docs[0].to_dict().get("transcript")


def _update(job_id: str, fields: dict[str, Any]) -> None:
    fields["updated_at"] = dt.datetime.now(dt.timezone.utc)
    _collection.document(job_id).update(fields)
