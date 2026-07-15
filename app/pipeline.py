"""The one place the four transcribe endpoints' shared logic actually lives.

All four modes funnel through the same two steps -- resolve the source to
audio, then transcribe it -- and only branch on what to do with the result
afterward. This function is what /internal/process/{job_id} calls; nothing
in main.py duplicates any of this.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import claude_tasks, jobs, sources, transcription


def _transcribe_audio(audio_path: Path, disfluencies: bool) -> transcription.TranscriptResult:
    try:
        return transcription.transcribe(audio_path, disfluencies=disfluencies)
    finally:
        audio_path.unlink(missing_ok=True)


def _cache_key(identity: str, mode: str, disfluencies: bool) -> str:
    """Options that change what AssemblyAI is asked to do -- disfluencies,
    and the mode (which drives _resolve_audio_path's choice of resolver, so
    the same source string can legitimately resolve to different audio
    depending on mode, e.g. TikTok /words uses yt-dlp direct while every
    other TikTok mode goes through the Apify scrape) -- both have to be part
    of the key, or a disfluencies=false /text result could satisfy a
    disfluencies=true /words request for the identical source.
    """
    raw = f"{identity}|mode={mode}|disfluencies={disfluencies}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _get_transcript(
    mode: str, source: str, kind: sources.SourceKind, disfluencies: bool
) -> tuple[transcription.TranscriptResult, str | None, str | None]:
    """Cache check + transcription for every mode that actually calls
    AssemblyAI (i.e. everything except the YouTube caption shortcut, which
    never touches AssemblyAI in the first place). Keyed on input identity +
    disfluencies + mode (see _cache_key) and backed by the Firestore `jobs`
    collection so cached entries ride the same 7-day TTL as everything else
    (see jobs.py's module docstring). Returns (result, cache_key,
    path_cache_key) -- both key fields are meant to be handed straight to
    jobs.mark_done so a job this function transcribes can itself serve as a
    cache hit for the next matching call.

    NOTE -- known race, not a bug: this is check-then-write, and the key(s)
    are only persisted once the winning job reaches mark_done. Two identical
    requests that both arrive before either one finishes will both miss here
    and both pay for a real transcription/resolution. No locking is added to
    close this window (out of scope) -- see the caller if you're tempted to
    "fix" a double-charge you see in the logs.

    Every source is keyed on the source string itself (see _cache_key),
    which lets a cache hit skip resolution entirely -- notably avoiding a
    second paid Apify scrape/audio-download call for social sources, not
    just a second AssemblyAI call. GCS/uploaded sources are the one
    exception: the source string is a gs://path or storage.googleapis.com
    URL that can change across uploads of the *same* file, so on top of that
    cheap path-keyed pre-check, they're also keyed on a sha256 of the
    resolved bytes -- the fallback that makes the same file re-uploaded
    under a new path still hit the cache, at the cost of needing the
    download to compute it.
    """
    if kind is sources.SourceKind.GCS:
        # Cheap pre-check: `source` for a GCS kind IS already the resolved
        # gs://path or storage.googleapis.com URL (see sources.classify_source)
        # -- no download needed to know it. A hit here skips resolution
        # entirely, same as the non-GCS case below.
        path_cache_key = _cache_key(source, mode, disfluencies)
        cached = jobs.find_cached_transcript(path_cache_key, field="path_cache_key")
        if cached is not None:
            # cache_key (the byte-hash key) is left None here -- we never
            # downloaded, so there's no hash to report, and nothing to lose:
            # the job that originally computed it still owns that mapping.
            return transcription.TranscriptResult(**cached), None, path_cache_key

        audio_path = _resolve_audio_path(mode, source, kind)
        identity = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        cache_key = _cache_key(identity, mode, disfluencies)
        cached = jobs.find_cached_transcript(cache_key)
        if cached is not None:
            audio_path.unlink(missing_ok=True)
            return transcription.TranscriptResult(**cached), cache_key, path_cache_key
        return _transcribe_audio(audio_path, disfluencies), cache_key, path_cache_key

    cache_key = _cache_key(source, mode, disfluencies)
    cached = jobs.find_cached_transcript(cache_key)
    if cached is not None:
        return transcription.TranscriptResult(**cached), cache_key, None

    audio_path = _resolve_audio_path(mode, source, kind)
    return _transcribe_audio(audio_path, disfluencies), cache_key, None


def _resolve_audio_path(mode: str, source: str, kind: sources.SourceKind) -> Path:
    """Which resolver handles a given (mode, platform) combination -- each
    branch here reflects something confirmed by an actual live test, not an
    assumption (see the module docstrings in sources.py for the individual
    findings):

    - Instagram: yt-dlp direct never works (confirmed for both /words and
      non-words modes) -- always use the Apify scrape, regardless of mode.
    - YouTube + /words: yt-dlp direct is IP-blocked (confirmed); resolved via
      Apify's residential-proxy audio downloader instead (see
      resolve_youtube_audio's docstring for why that's reserved for /words
      specifically rather than used everywhere).
    - TikTok + non-words: existing Apify scrape path.
    - TikTok + /words: yt-dlp direct actually works here (confirmed live --
      unlike YouTube and Instagram, TikTok showed no blocking), so it's left
      on the free path rather than paying for Apify's video re-hosting.
    - DIRECT_MEDIA, GCS, uploads, any mode: unchanged, plain resolve_source().
    """
    if kind is sources.SourceKind.INSTAGRAM:
        return sources.resolve_social_media(source, kind)
    if mode == "words" and kind is sources.SourceKind.YOUTUBE:
        return sources.resolve_youtube_audio(source)
    if mode != "words" and kind is sources.SourceKind.TIKTOK:
        return sources.resolve_social_media(source, kind)
    return sources.resolve_source(source)


def run_job(job_id: str) -> None:
    job = jobs.get_job(job_id)
    if job is None:
        return

    jobs.mark_processing(job_id)

    try:
        mode = job["mode"]
        source = job["source"]
        disfluencies = bool(job.get("disfluencies", False))

        words: list[dict] = []
        caption_shortcut_used = False
        cache_key: str | None = None
        path_cache_key: str | None = None
        transcript_payload: dict | None = None
        kind = sources.classify_source(source)

        # YouTube, non-words modes: the only path, no fallthrough. The Apify
        # transcript actor covers both captioned and uncaptioned videos (AI
        # fallback), so there's nothing left to fall back to -- resolve_source()
        # + AssemblyAI is IP-blocked for YouTube anyway and would just fail.
        # get_youtube_transcript() raises for a genuinely unavailable video,
        # which the outer except turns into mark_failed. Every other
        # (mode, platform) combination -- including YouTube + /words, which
        # needs real audio rather than this actor's text-only output -- goes
        # through _resolve_audio_path(), which picks the right resolver per
        # combination (see its docstring for what's confirmed vs. free vs. paid).
        #
        # This shortcut never calls AssemblyAI, so it's outside the
        # transcript cache below (see _get_transcript) -- there's nothing to
        # cache a re-transcription against.
        if mode != "words" and kind is sources.SourceKind.YOUTUBE:
            transcript_text = sources.get_youtube_transcript(source)
            caption_shortcut_used = True
            engine_used = "youtube_transcript_apify"
        else:
            result, cache_key, path_cache_key = _get_transcript(mode, source, kind, disfluencies)
            transcript_text = result.text
            words = [w.model_dump() for w in result.words]
            engine_used = result.engine
            transcript_payload = result.model_dump()

        if mode == "text":
            payload = {"text": transcript_text}
        elif mode == "words":
            payload = {"text": transcript_text, "words": words}
        elif mode == "translate":
            translated = claude_tasks.translate(transcript_text, job["target_language"])
            payload = {
                "text": transcript_text,
                "translation": translated,
                "target_language": job["target_language"],
            }
        elif mode == "summary":
            summary = claude_tasks.summarize(transcript_text)
            payload = {"text": transcript_text, "summary": summary}
        else:
            raise ValueError(f"Unknown mode: {mode}")

        jobs.mark_done(
            job_id,
            payload,
            engine_used,
            caption_shortcut_used,
            cache_key=cache_key,
            path_cache_key=path_cache_key,
            transcript=transcript_payload,
        )

    except Exception as exc:  # noqa: BLE001 -- last-resort catch so job status always resolves
        jobs.mark_failed(job_id, str(exc))
