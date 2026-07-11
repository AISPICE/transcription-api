# Transcription API

Shared transcription service for all content pipelines (Reels production,
competitor research, client automations). One service, one set of endpoints,
callable from any workflow instead of each one re-implementing
download/transcribe logic separately.

- **Base URL:** `https://transcription-api-610442899336.us-central1.run.app`
- **Project:** `cinn-automations` (region `us-central1`)
- **Auth:** every endpoint requires header `X-API-Key: <key>` (value in Secret Manager, secret name `API_KEY`)

This API only ever produces transcript data (text, word timing, translation,
summary). It never does frame extraction, image sampling, or Claude vision
calls -- any workflow needing those handles them on its own side, using this
API only for the transcript piece.

## Async job pattern

Every `/transcribe/*` call returns immediately with a `job_id`; the actual
work happens in the background (via Cloud Tasks) and can take anywhere from
a few seconds to a few minutes depending on content length. Poll
`/status/{job_id}` until `status` is `done` or `failed`.

```
POST /transcribe/text  {"source": "..."}
  -> {"job_id": "...", "status": "queued"}

GET /status/{job_id}
  -> {"status": "processing", ...}         (while running)
  -> {"status": "done", "result": {...}}    (finished)
  -> {"status": "failed", "error": "..."}   (finished, with a reason)
```

## Endpoints

### `POST /transcribe/text`
Plain transcript, no timing.
- Body: `{"source": "<url or gs://path>"}`
- Result: `{"text": "..."}`

### `POST /transcribe/words`
Word-level timing, matching the shape `edit.js`/`transcribe-assemblyai.js`
already expect: `{word, start, end}` per word, **start/end in seconds**
(not AssemblyAI's raw milliseconds), plus a bonus `confidence` field.
- Body: `{"source": "<url or gs://path>"}`
- Result: `{"text": "...", "words": [{"word": "...", "start": 1.28, "end": 1.56, "confidence": 0.98}, ...]}`
- This is the endpoint to use for anything needing frame-to-word alignment
  (e.g. a "watch this video" workflow) -- it accepts video sources directly
  across every supported platform, not audio-only.

### `POST /transcribe/translate`
Transcribes, then translates via Claude (Haiku 4.5).
- Body: `{"source": "...", "target_language": "Spanish"}`
- Result: `{"text": "...", "translation": "...", "target_language": "Spanish"}`

### `POST /transcribe/summary`
Transcribes, then summarizes via Claude (Haiku 4.5).
- Body: `{"source": "..."}`
- Result: `{"text": "...", "summary": "..."}`

### `POST /transcribe/text/sync`
Same as `/transcribe/text`, but synchronous: the HTTP response blocks until
transcription actually finishes and returns the transcript directly, instead
of a `job_id` to poll.
- Body: `{"source": "<url or gs://path>"}`
- Result: `{"text": "..."}`
- Requires the same `X-API-Key` auth as the other public routes.
- Cloud Run's request timeout is set to 900s (raised from the default 300s)
  to give this endpoint room to finish -- expect this call can take a while
  on long content. That timeout is service-wide, not specific to this route.
```
POST /transcribe/text/sync  {"source": "..."}
  -> {"text": "..."}   (after transcription finishes, no polling)
```

### `GET /status/{job_id}`
Returns the full job document (status, mode, source, result, error, engine, timestamps).

### `POST /uploads/init?filename=clip.mp4`
Step 1 of the large-file upload flow (Cloud Run caps direct request bodies at
32MB). Returns a signed URL to PUT the file straight to GCS, bypassing this
API entirely, plus the resulting `gs://` path to use as `source` afterward.
```
POST /uploads/init?filename=clip.mp4
  -> {"upload_url": "https://...", "gcs_path": "gs://.../uploads/xxxx.mp4"}
PUT <upload_url>  (raw file bytes, Content-Type: application/octet-stream)
POST /transcribe/text  {"source": "gs://.../uploads/xxxx.mp4"}
```

### `POST /internal/process/{job_id}`
Internal only -- called by Cloud Tasks, guarded by a separate secret
(`X-Internal-Key`, not the public API key). Never call this directly.

## What `source` accepts

Any of: a YouTube URL, an Instagram URL (post/reel), a TikTok URL, a direct
media URL (e.g. already resolved by Apify), a `gs://` or signed
`https://storage.googleapis.com/...` link, or an uploaded file's resulting
`gs://` path from `/uploads/init`.

Per-platform behavior, current state (all confirmed by live testing, not assumed):

| Platform | `/text` `/translate` `/summary` | `/words` |
|---|---|---|
| YouTube | Apify transcript actor (captions or AI-fallback transcription) -- direct yt-dlp from Cloud Run is IP-blocked by YouTube, confirmed | Apify audio downloader with residential proxy forced (also needed -- default/datacenter proxy gets blocked too) |
| Instagram | Apify scrape -> direct media URL -> AssemblyAI (raw yt-dlp doesn't work here, confirmed) | same Apify path |
| TikTok | Apify scrape -> direct media URL -> AssemblyAI | plain yt-dlp direct (confirmed working, no Apify needed) |
| Direct media / GCS / uploads | plain download -> AssemblyAI | same |

## Known limitations

- Instagram/TikTok CDN links from Apify scrapes are signed and expire in a
  few hours -- don't cache a `videoUrl` and reuse it later.
- YouTube `/words` uses a residential proxy (costs more than datacenter) --
  reserved for `/words` specifically since every other YouTube mode has a
  free path.
- A post/reel with no video at all (e.g. an all-photo Instagram carousel)
  fails cleanly with a "no video media in this post" error, not a crash.

## Module layout

- `app/main.py` -- FastAPI routes (thin, no transcription logic)
- `app/pipeline.py` -- shared orchestration every route funnels through
- `app/sources.py` -- input classification + all source resolution (yt-dlp, Apify scrapes, GCS, direct download)
- `app/transcription.py` -- AssemblyAI integration, swappable `transcribe()` engine
- `app/claude_tasks.py` -- translate/summarize via Claude
- `app/jobs.py` -- Firestore job state
- `app/tasks_queue.py` -- Cloud Tasks enqueueing
- `app/config.py` / `app/security.py` -- settings and API key checks

## Deploying changes

```bash
gcloud run deploy transcription-api \
  --source "." \
  --region us-central1 \
  --project cinn-automations
```
This rebuilds the container from the current source and deploys a new
revision. Secrets and env vars already set on the service carry over
automatically unless you pass `--update-secrets`/`--update-env-vars`.
