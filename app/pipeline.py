"""The one place the four transcribe endpoints' shared logic actually lives.

All four modes funnel through the same two steps -- resolve the source to
audio, then transcribe it -- and only branch on what to do with the result
afterward. This function is what /internal/process/{job_id} calls; nothing
in main.py duplicates any of this.
"""

from __future__ import annotations

from pathlib import Path

from . import claude_tasks, jobs, sources, transcription


def _transcribe_audio(audio_path: Path) -> transcription.TranscriptResult:
    try:
        return transcription.transcribe(audio_path)
    finally:
        audio_path.unlink(missing_ok=True)


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

        words: list[dict] = []
        caption_shortcut_used = False
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
        if mode != "words" and kind is sources.SourceKind.YOUTUBE:
            transcript_text = sources.get_youtube_transcript(source)
            caption_shortcut_used = True
            engine_used = "youtube_transcript_apify"
        else:
            audio_path = _resolve_audio_path(mode, source, kind)
            result = _transcribe_audio(audio_path)
            transcript_text = result.text
            words = [w.model_dump() for w in result.words]
            engine_used = result.engine

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

        jobs.mark_done(job_id, payload, engine_used, caption_shortcut_used)

    except Exception as exc:  # noqa: BLE001 -- last-resort catch so job status always resolves
        jobs.mark_failed(job_id, str(exc))
