"""The second step for /transcribe/translate and /transcribe/summary: a
Claude call over the transcript text produced by the shared pipeline.

Using Claude instead of a dedicated translation API (e.g. Google Cloud
Translation) avoids adding a second vendor relationship, a second API key to
manage, and a second billing surface for something a general-purpose LLM
already does well -- translating a transcript isn't a high-volume, latency-
critical, structured-data task where a specialized API's throughput/cost
advantage would matter. You already have the Anthropic relationship; reusing
it here is strictly less complexity for the same result.
"""

from __future__ import annotations

from anthropic import Anthropic

from .config import settings

_client = Anthropic(api_key=settings.anthropic_api_key)

# Haiku 4.5: summarizing/translating a transcript is a bounded, non-creative
# task that runs on every single request through this service -- Haiku's
# quality is more than sufficient and it costs a fraction of Sonnet per call.
_MODEL = "claude-haiku-4-5-20251001"


def summarize(text: str) -> str:
    resp = _client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": (
                    "Summarize the following transcript in a few tight paragraphs. "
                    "Preserve concrete facts, numbers, and names; drop filler and "
                    "repetition.\n\n" + text
                ),
            }
        ],
    )
    return resp.content[0].text


def translate(text: str, target_language: str) -> str:
    resp = _client.messages.create(
        model=_MODEL,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Translate the following transcript into {target_language}. "
                    "Return only the translation, with no preamble or commentary."
                    "\n\n" + text
                ),
            }
        ],
    )
    return resp.content[0].text
