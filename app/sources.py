"""Input-type detection and the resolve_source() fallback funnel.

Two separate mechanisms live here, and it's important they stay separate:

1. classify_source() + resolve_source() -- turns *any* of the supported input
   kinds into a local audio file on disk. This is the "get me something I can
   feed to a transcription engine" concern.

2. get_youtube_transcript() -- an entirely separate shortcut that produces
   transcript *text* directly and skips needing audio (and therefore skips
   resolve_source() and the transcription engine) altogether. It has to live
   outside resolve_source() because resolve_source()'s contract is "return
   audio" -- a function that sometimes returns text and sometimes returns an
   audio path would make every caller branch on the return type, which
   defeats the point of having a single funnel. Callers that want the cheap
   path decide *before* calling resolve_source(), not inside it.
"""

from __future__ import annotations

import enum
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp

from .config import settings


class SourceKind(str, enum.Enum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    DIRECT_MEDIA = "direct_media"
    GCS = "gcs"


_MEDIA_EXTENSIONS = {".mp4", ".mp3", ".m4a", ".wav", ".mov", ".webm", ".ogg", ".aac"}

_PLATFORM_HOSTS = {
    "youtube.com": SourceKind.YOUTUBE,
    "www.youtube.com": SourceKind.YOUTUBE,
    "m.youtube.com": SourceKind.YOUTUBE,
    "youtu.be": SourceKind.YOUTUBE,
    "instagram.com": SourceKind.INSTAGRAM,
    "www.instagram.com": SourceKind.INSTAGRAM,
    "tiktok.com": SourceKind.TIKTOK,
    "www.tiktok.com": SourceKind.TIKTOK,
    "vm.tiktok.com": SourceKind.TIKTOK,
}


def classify_source(source: str) -> SourceKind:
    """Decide which of the six input kinds we're looking at.

    The order of checks matters: gs:// and known "page" platforms (YouTube /
    Instagram / TikTok) are checked first because they're unambiguous. Anything
    left over that ends in a media file extension is treated as an
    already-resolved direct media URL -- this is exactly the Apify case: Apify
    has already done the page-scraping work and handed us the final CDN link,
    so by the time it reaches us it looks just like any other .mp4 URL.
    """
    if source.startswith("gs://"):
        return SourceKind.GCS

    parsed = urlparse(source)
    host = parsed.netloc.lower()

    if host in _PLATFORM_HOSTS:
        return _PLATFORM_HOSTS[host]

    # A storage.googleapis.com signed URL is a pre-authorized plain HTTPS
    # download -- no GCS client library or credentials needed to read it.
    if host == "storage.googleapis.com":
        return SourceKind.GCS

    ext = Path(parsed.path).suffix.lower()
    if ext in _MEDIA_EXTENSIONS:
        return SourceKind.DIRECT_MEDIA

    raise ValueError(f"Could not classify source: {source!r}")


def _tmp_path(suffix: str = "") -> Path:
    return Path(tempfile.gettempdir()) / f"{uuid.uuid4().hex}{suffix}"


_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Platform CDNs reliably reject or misbehave on requests that don't look like
# a browser -- this isn't a maybe, so the header goes on proactively rather
# than waiting to see a 403. Matched by a substring of the host, since CDN
# subdomains rotate (scontent-lga3-1.cdninstagram.com, etc.).
_REFERER_BY_HOST_SUBSTRING = {
    "cdninstagram.com": "https://www.instagram.com/",
    "fbcdn.net": "https://www.instagram.com/",
    "tiktokcdn": "https://www.tiktok.com/",
    "muscdn.com": "https://www.tiktok.com/",
}


def _download_headers(url: str) -> dict[str, str]:
    host = urlparse(url).netloc.lower()
    headers = {"User-Agent": _BROWSER_USER_AGENT}
    for substring, referer in _REFERER_BY_HOST_SUBSTRING.items():
        if substring in host:
            headers["Referer"] = referer
            break
    return headers


def _download_direct(url: str) -> Path:
    """Plain HTTP GET, streamed to disk. Used for Apify's already-resolved
    media URLs and for GCS signed HTTPS URLs. Deliberately does NOT go through
    yt-dlp: yt-dlp exists to scrape a *page* and figure out where the real
    media file lives. When the URL already *is* the media file, running it
    through yt-dlp adds latency, another dependency on yt-dlp's site-specific
    extractor code, and no benefit -- a GET gives you the identical bytes.

    Note for callers: Instagram/TikTok CDN URLs (e.g. from Apify's videoUrl)
    are signed and expire a few hours after they're issued -- they are NOT
    IP-bound like YouTube's, but they ARE time-limited. Feed a freshly
    scraped URL in promptly; a stale one 403s in a way that's indistinguishable
    from a missing-header problem unless you know to expect it (see below).
    """
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix or ".mp4"
    dest = _tmp_path(suffix)
    with httpx.stream(
        "GET", url, headers=_download_headers(url), follow_redirects=True, timeout=120
    ) as resp:
        if resp.status_code == 403:
            raise RuntimeError(
                f"403 Forbidden downloading {url!r}. For Instagram/TikTok CDN links this "
                "almost always means the signed URL expired (they're time-limited to a "
                "few hours from when the scraper issued them), not a missing header -- "
                "re-scrape and retry promptly if so."
            )
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
    return dest


def _download_from_gcs(gs_or_signed_url: str) -> Path:
    if gs_or_signed_url.startswith("gs://"):
        from google.cloud import storage  # imported lazily, only touched on this path

        client = storage.Client()  # auth comes from the attached runtime service
        # account via Application Default Credentials -- see the deploy notes.
        bucket_name, _, blob_path = gs_or_signed_url[len("gs://") :].partition("/")
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        suffix = Path(blob_path).suffix or ".mp4"
        dest = _tmp_path(suffix)
        blob.download_to_filename(str(dest))
        return dest
    return _download_direct(gs_or_signed_url)


def download_audio(url: str) -> Path:
    """Full fallback path for YouTube / Instagram / TikTok: let yt-dlp resolve
    the page, pick the best audio-only stream, and extract it with ffmpeg.

    For YouTube this is now only reached by /transcribe/words (see
    pipeline.py) -- text/translate/summary modes go through
    get_youtube_transcript() instead and never fall through to here. It's
    worth knowing this yt-dlp path is unreliable for YouTube specifically:
    Cloud Run's IP range gets blocked by YouTube's bot detection (confirmed
    separately), so calls made directly from this container routinely fail
    regardless of what's requested. For Instagram/TikTok raw URLs (as opposed
    to Apify-resolved direct media links) it's similarly unreliable without
    an authenticated session -- that's expected, not a bug; see sources.py's
    module docstring and the DIRECT_MEDIA path for the path that actually
    works for those platforms in production."""
    dest = _tmp_path()
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": f"{dest}.%(ext)s",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return dest.with_suffix(".mp3")


def resolve_source(source: str) -> Path:
    """The single funnel every input kind passes through on its way to
    becoming a local audio file. transcription.transcribe() never has to know
    or care whether the audio came from YouTube, a signed GCS URL, or an
    Apify CDN link -- that's the whole point of drawing this boundary here."""
    kind = classify_source(source)

    if kind in (SourceKind.YOUTUBE, SourceKind.INSTAGRAM, SourceKind.TIKTOK):
        return download_audio(source)
    if kind is SourceKind.DIRECT_MEDIA:
        return _download_direct(source)
    if kind is SourceKind.GCS:
        return _download_from_gcs(source)

    raise ValueError(f"Unhandled source kind: {kind}")


class NoVideoMediaError(Exception):
    """Raised when a scraped social post genuinely has no video track (e.g. an
    all-photo Instagram Sidecar carousel) -- a legitimate, expected outcome
    for that specific post, not a scraping failure."""


def _run_apify_actor(actor_id: str, input_payload: dict, timeout: float = 300) -> list[dict]:
    """Runs an Apify actor synchronously and returns its dataset items in one
    HTTP call -- Apify's run-sync-get-dataset-items endpoint handles the run
    and hands back results directly, no separate poll-for-completion step."""
    resp = httpx.post(
        f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items",
        params={"token": settings.apify_api_token},
        json=input_payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


_APIFY_YOUTUBE_ACTOR = "codepoetry~youtube-transcript-ai-scraper"

# Hard caps on the actor's AI-fallback (Whisper-based) transcription, so a
# stray very-long uncaptioned video can't quietly run up minutes/cost on a
# single request. skipAiFallbackIfLongerThan avoids even starting AI
# transcription on anything over an hour; maxAiMinutes is the backstop cap
# on total minutes transcribed for whatever run does proceed.
_APIFY_MAX_AI_MINUTES = 30
_APIFY_SKIP_AI_FALLBACK_OVER_MINUTES = 60


def get_youtube_transcript(url: str) -> str:
    """The only YouTube path for text/translate/summary modes (see
    pipeline.py) -- there is no fallthrough to resolve_source() + AssemblyAI
    for those modes anymore. Runs the 'youtube-transcript-ai-scraper' Apify
    actor, which tries YouTube's native captions first and, with
    enableAiFallback on, transcribes the audio itself (via Whisper) on
    Apify's own infrastructure when a video has none.

    That last point is *why* this exists rather than just calling yt-dlp
    directly: YouTube's bot detection flags Cloud Run's IP range at the
    network layer, so a yt-dlp call made from this container fails even for
    a metadata-only caption check (confirmed separately) -- running the
    equivalent check from Apify's infrastructure instead sidesteps that
    entirely. And because AI fallback now covers the no-native-captions case
    too, there's no longer a "shortcut unavailable, fall back to audio
    download" scenario for YouTube to handle -- this either returns a
    transcript or raises for a genuinely unavailable video (private,
    deleted, etc).

    Segment-level timing only (no word-level output) -- never call this for
    mode == "words"; that mode still goes through resolve_source() +
    transcribe() via download_audio(), unchanged and still YouTube-IP-blocked
    for now.
    """
    items = _run_apify_actor(
        _APIFY_YOUTUBE_ACTOR,
        {
            "startUrls": [{"url": url}],
            "enableAiFallback": True,
            "maxAiMinutes": _APIFY_MAX_AI_MINUTES,
            "skipAiFallbackIfLongerThan": _APIFY_SKIP_AI_FALLBACK_OVER_MINUTES,
            "outputFormats": ["llm", "text", "json"],
        },
        timeout=1200,
    )

    if not items:
        raise RuntimeError(f"No transcript returned for {url!r} (empty Apify result)")

    item = items[0]
    if item.get("error_code"):
        raise RuntimeError(
            f"Transcript unavailable for {url!r}: {item.get('error') or item['error_code']}"
        )

    text = item.get("transcript_llm") or item.get("transcript_text")
    if not text:
        segments = item.get("transcript_json") or []
        text = " ".join(seg["text"] for seg in segments if seg.get("text"))

    if not text:
        raise RuntimeError(f"No transcript returned for {url!r} (unexpected empty result)")

    return text


_APIFY_INSTAGRAM_ACTOR = "apify~instagram-scraper"


def extract_video_url(item: dict) -> str | None:
    """Pull a direct video CDN URL out of an apify/instagram-scraper result
    item. Handles both a Video/Reel post (top-level videoUrl) and a Sidecar
    carousel that contains a video among its childPosts -- a Sidecar with no
    video anywhere in it (all-photo carousel) correctly yields None."""
    if item.get("videoUrl"):
        return item["videoUrl"]
    for child in item.get("childPosts", []):
        if child.get("videoUrl"):
            return child["videoUrl"]
    return None


def _scrape_instagram_video_url(url: str) -> str | None:
    items = _run_apify_actor(
        _APIFY_INSTAGRAM_ACTOR,
        {"resultsType": "posts", "directUrls": [url], "resultsLimit": 1},
    )
    if not items:
        return None
    return extract_video_url(items[0])


_APIFY_TIKTOK_ACTOR = "clockworks~tiktok-scraper"


def _scrape_tiktok_video_url(url: str) -> str | None:
    """apidojo/tiktok-scraper (the actor originally proposed) turned out not
    to support single-video URLs at all -- confirmed by a real test call that
    came back as ten "noResults" placeholders, matching its docs' "must fetch
    at least 10 posts" constraint. clockworks/tiktok-scraper does support
    single posts (input field postURLs), but its own default output has no
    direct CDN link either -- webVideoUrl/submittedVideoUrl just echo back
    the tiktok.com page URL (also confirmed by a real test call). Only with
    shouldDownloadVideos=True does Apify itself download the video and
    re-host it, exposed as mediaUrls[0] / videoMeta.downloadAddr -- both
    point at Apify's own key-value store, not TikTok's CDN, and (confirmed)
    require the same API token to fetch, hence appending it below rather than
    relying on the CDN User-Agent/Referer handling in _download_headers().
    """
    items = _run_apify_actor(
        _APIFY_TIKTOK_ACTOR,
        {"postURLs": [url], "resultsPerPage": 1, "shouldDownloadVideos": True},
        timeout=300,
    )
    if not items:
        return None

    item = items[0]
    media_urls = item.get("mediaUrls") or []
    download_addr = (item.get("videoMeta") or {}).get("downloadAddr")
    direct_url = (media_urls[0] if media_urls else None) or download_addr
    if not direct_url:
        return None

    separator = "&" if "?" in direct_url else "?"
    return f"{direct_url}{separator}token={settings.apify_api_token}"


_APIFY_YOUTUBE_AUDIO_ACTOR = "lurkapi~youtube-to-mp3-audio-downloader"


def resolve_youtube_audio(url: str) -> Path:
    """The only working path for /transcribe/words on YouTube. Plain yt-dlp
    from Cloud Run is IP-blocked (confirmed live) even for a metadata-only
    caption check, and get_youtube_transcript()'s Apify actor only ever
    returns segment-level text, never audio -- it can't feed AssemblyAI for
    real word-level timing either. This actor actually downloads audio, but
    even IT got blocked by YouTube on Apify's default (datacenter) proxy pool
    during testing: "YouTube is blocking this video after 15 proxy
    rotations." Only forcing Apify's residential proxy group produced a real
    result (confirmed live). Residential proxy bandwidth costs meaningfully
    more than datacenter on Apify's side, which is why this is only used for
    /words -- every other YouTube mode has the free transcript-actor path.
    """
    items = _run_apify_actor(
        _APIFY_YOUTUBE_AUDIO_ACTOR,
        {
            "videoUrls": [url],
            "proxyConfiguration": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
        },
        timeout=600,
    )
    if not items:
        raise RuntimeError(f"No result returned for {url!r} (empty Apify result)")

    item = items[0]
    if item.get("error") or item.get("status") != "Success":
        raise RuntimeError(
            f"Audio download failed for {url!r}: {item.get('error') or item.get('status')}"
        )

    audio_url = item.get("audioFileUrl")
    if not audio_url:
        raise RuntimeError(f"No audioFileUrl returned for {url!r}")

    # Same as TikTok's re-hosted link: Apify's own key-value store, not
    # YouTube's CDN, and requires the same API token to fetch (confirmed --
    # despite docs describing it as "works from any server, any IP, any
    # time," that's about it not being time/IP-bound, not about being
    # unauthenticated-public).
    separator = "&" if "?" in audio_url else "?"
    return _download_direct(f"{audio_url}{separator}token={settings.apify_api_token}")


def resolve_social_media(source: str, kind: SourceKind) -> Path:
    """Raw Instagram/TikTok URL (no pre-resolution by the caller) -> local
    audio file: scrape the post via Apify to find the direct CDN media URL,
    then reuse the exact same _download_direct() path used for pre-resolved
    (caller-already-scraped) media links -- resultsType: 'posts' in the
    Instagram call and DIRECT_MEDIA's path both end up downloading identical
    bytes, they just differ in how the direct URL was found. Raises
    NoVideoMediaError for a post that genuinely has no video (e.g. an
    all-photo carousel) -- a normal, expected outcome, not a crash.
    """
    if kind is SourceKind.INSTAGRAM:
        direct_url = _scrape_instagram_video_url(source)
    elif kind is SourceKind.TIKTOK:
        direct_url = _scrape_tiktok_video_url(source)
    else:
        raise ValueError(f"resolve_social_media only handles INSTAGRAM/TIKTOK, got {kind}")

    if direct_url is None:
        raise NoVideoMediaError(f"No video media found in post: {source!r}")

    return _download_direct(direct_url)
