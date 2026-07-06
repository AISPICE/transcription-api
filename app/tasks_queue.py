"""Enqueues the background work as a Cloud Tasks task instead of running it
as a FastAPI BackgroundTask. See the architecture notes for why -- short
version: Cloud Run only guarantees CPU is allocated while it's actively
handling an inbound HTTP request. A BackgroundTask keeps running code *after*
the response has been sent, which falls outside that guarantee unless you
separately flip on "CPU always allocated" for the service. Enqueuing a Cloud
Tasks task that calls back into a new endpoint sidesteps the issue entirely,
because that callback is itself a normal inbound request -- CPU is allocated
for its full duration by Cloud Run's default behavior, no extra flags needed.
Cloud Tasks also retries the callback with backoff if it fails or times out,
and the queue's max-concurrent-dispatches setting caps how many transcription
jobs can run at once, protecting AssemblyAI's rate limits and your bill from
a burst of requests.
"""

from __future__ import annotations

import json

from google.cloud import tasks_v2

from .config import settings

_client = tasks_v2.CloudTasksClient()
_parent = _client.queue_path(
    settings.gcp_project_id, settings.cloud_tasks_location, settings.cloud_tasks_queue
)


def enqueue_job(job_id: str) -> None:
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{settings.service_url}/internal/process/{job_id}",
            "headers": {
                "Content-Type": "application/json",
                "X-Internal-Key": settings.internal_task_key,
            },
            "body": json.dumps({"job_id": job_id}).encode(),
        }
    }
    _client.create_task(request={"parent": _parent, "task": task})
