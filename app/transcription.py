"""The shared AssemblyAI integration, behind a swappable transcribe() function.

Every /transcribe/* route funnels into transcribe(audio_path) exactly once.
Adding a second engine (e.g. Groq's Whisper API) later means writing a
groq_transcribe(audio_path) -> TranscriptResult function with this identical
signature and changing one line in pipeline.py -- nothing in main.py,
sources.py, or the Firestore schema needs to change.
"""

from __future__ import annotations

from pathlib import Path

import assemblyai as aai
from pydantic import BaseModel

from .config import settings

aai.settings.api_key = settings.assemblyai_api_key


class Word(BaseModel):
    # start/end are raw integer milliseconds, exactly as AssemblyAI returns
    # them -- no conversion. This API has no consumers relying on the old
    # seconds-denominated output, so there's no compatibility mode here;
    # every caller must treat these as milliseconds.
    word: str
    start: int
    end: int
    confidence: float | None = None


class TranscriptResult(BaseModel):
    text: str
    words: list[Word] = []
    engine: str
    language: str | None = None


def transcribe(audio_path: Path, disfluencies: bool = False) -> TranscriptResult:
    """AssemblyAI's SDK submits the file, polls until the job finishes, and
    raises/returns based on final status -- all synchronously inside this
    call. That's fine here because this function only ever runs inside the
    /internal/process request triggered by Cloud Tasks, which is allowed to
    take minutes; see the deploy notes on --timeout.

    disfluencies=True keeps AssemblyAI's fillers/false-starts/repeats (um,
    uh, "I- I mean") in the output instead of stripping them -- off by
    default so /transcribe/text and /translate/summary keep getting a clean
    transcript unless a caller opts in.
    """
    transcriber = aai.Transcriber()
    config = aai.TranscriptionConfig(disfluencies=True) if disfluencies else None
    transcript = transcriber.transcribe(str(audio_path), config=config)

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI transcription failed: {transcript.error}")

    words = [
        Word(word=w.text, start=w.start, end=w.end, confidence=w.confidence)
        for w in (transcript.words or [])
    ]

    return TranscriptResult(
        text=transcript.text or "",
        words=words,
        engine="assemblyai",
        language=(transcript.json_response or {}).get("language_code"),
    )
