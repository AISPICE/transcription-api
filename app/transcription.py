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
    # Field name and units deliberately match transcribe-assemblyai.js's
    # transcripts/<basename>.json output (word/start/end in seconds), which
    # is what edit.js and the rest of the video pipeline actually consume --
    # not AssemblyAI's raw wire shape (which uses "text" and milliseconds).
    # Getting this wrong doesn't error, it silently feeds edit.js's
    # second-denominated thresholds (silenceGateSec, etc.) millisecond
    # values, off by 1000x.
    word: str
    start: float
    end: float
    confidence: float | None = None


class TranscriptResult(BaseModel):
    text: str
    words: list[Word] = []
    engine: str
    language: str | None = None


def transcribe(audio_path: Path) -> TranscriptResult:
    """AssemblyAI's SDK submits the file, polls until the job finishes, and
    raises/returns based on final status -- all synchronously inside this
    call. That's fine here because this function only ever runs inside the
    /internal/process request triggered by Cloud Tasks, which is allowed to
    take minutes; see the deploy notes on --timeout.
    """
    transcriber = aai.Transcriber()
    transcript = transcriber.transcribe(str(audio_path))

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI transcription failed: {transcript.error}")

    words = [
        Word(word=w.text, start=w.start / 1000, end=w.end / 1000, confidence=w.confidence)
        for w in (transcript.words or [])
    ]

    return TranscriptResult(
        text=transcript.text or "",
        words=words,
        engine="assemblyai",
        language=(transcript.json_response or {}).get("language_code"),
    )
