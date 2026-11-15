"""تحلیلگر ویدیو / Video analyzer — real analysis, **no time limit anywhere**.

Pipeline (every step is real work, not a placeholder)
----------------------------------------------------
1. classify  — YouTube / Vimeo / direct file / generic page / upload.
2. metadata  — title, description, author, duration, thumbnail and captions.
   YouTube: ``ytInitialPlayerResponse`` (+ oEmbed fallback) and the public
   timedtext track. Vimeo/page: OpenGraph tags and embedded ``text_tracks``.
3. media     — direct files (and ``og:video`` files) are streamed to a temp
   file, then probed: ``ffprobe`` if installed, otherwise a pure-Python MP4
   box parser (``mvhd``/``tkhd``/``stsd``) so specs work without ffmpeg.
4. frames    — ``ffmpeg`` grabs 6 evenly spaced frames; Pillow measures
   brightness, dominant colour and frame-to-frame scene change.
5. text      — language, word count, reading time, keywords, FA/EN sentiment
   and topic match through the panel's own keyword engine.
6. summary   — LLM (DeepSeek → OpenAI-compatible) with ``timeout=None`` and a
   map-reduce pass for long transcripts; keyless fallback is extractive.

No step has a deadline: ``NO_TIMEOUT`` is used for HTTP, subprocesses are
started without ``timeout=`` and the LLM client passes ``httpx.Timeout(None)``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html as html_lib
import io
import json
import logging
import re
import shutil
import struct
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import config, db, topics as topics_mod, translate

logger = logging.getLogger("rozaneh.video")

NO_TIMEOUT = httpx.Timeout(None)
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".ogv")
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB size cap; time stays unlimited
_PROBE_READ_LIMIT = 256 * 1024 * 1024  # read whole file up to this size…
PROBE_WINDOW = 8 * 1024 * 1024  # …otherwise just the header window
FRAME_COUNT = 6
SMART_BATCH = 3

_YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^#]*v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{6,20})"
)
_VIMEO_RE = re.compile(r"vimeo\.com/(?:video/)?(\d{6,})")
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[\w\u0600-\u06FF]+")

_TASKS: dict[int, asyncio.Task] = {}
_CANCELLED: set[int] = set()


class VideoError(RuntimeError):
    pass


# ── settings ────────────────────────────────────────────────────────────────


def settings() -> dict:
    return {
        "smart": (db.get_setting("video_smart") or "0") == "1",
        "interval_minutes": int(db.get_setting("video_interval_minutes") or "30"),
        "summarize": (db.get_setting("video_summarize") or "1") == "1",
        "frames": (db.get_setting("video_frames") or "1") == "1",
        "last_smart_run": db.get_setting("video_smart_last_run") or "",
    }


def set_settings(patch: dict) -> dict:
    if "smart" in patch:
        db.set_setting("video_smart", "1" if patch["smart"] else "0")
    if "interval_minutes" in patch:
        try:
            minutes = max(5, min(1440, int(patch["interval_minutes"])))
        except (TypeError, ValueError):
            minutes = 30
        db.set_setting("video_interval_minutes", str(minutes))
    if "summarize" in patch:
        db.set_setting("video_summarize", "1" if patch["summarize"] else "0")
    if "frames" in patch:
        db.set_setting("video_frames", "1" if patch["frames"] else "0")
    return settings()


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def status() -> dict:
    running = [i for i, t in _TASKS.items() if not t.done()]
    total = int((db.query_one("SELECT COUNT(*) AS c FROM video_analyses") or {}).get("c") or 0)
    done = int(
        (db.query_one("SELECT COUNT(*) AS c FROM video_analyses WHERE status='done'") or {}).get("c")
        or 0
    )
    return {"settings": settings(), "running": running, "ffmpeg": ffmpeg_available(),
            "total": total, "done": done}


# ── URL classification ──────────────────────────────────────────────────────


def classify_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "unknown"
    if url.startswith("upload:"):
        return "upload"
    if _YT_RE.search(url):
        return "youtube"
    if _VIMEO_RE.search(url):
        return "vimeo"
    if urlparse(url).path.lower().endswith(VIDEO_EXTS):
        return "direct"
    if url.startswith(("http://", "https://")):
        return "page"
    return "unknown"


def is_video_url(url: str) -> bool:
    return classify_url(url) in {"youtube", "vimeo", "direct"}


def url_hash(url: str) -> str:
    return hashlib.sha1((url or "").strip().encode("utf-8", "ignore")).hexdigest()[:32]


def _clean(text: str | None, limit: int = 4000) -> str:
    if not text:
        return ""
    text = html_lib.unescape(_TAG_RE.sub(" ", text))
    return _SPACE_RE.sub(" ", text).strip()[:limit]


def _og(html_text: str, keys: tuple[str, ...]) -> str:
    """Attribute-order-agnostic ``og:`` / ``twitter:`` meta reader."""
    for tag in re.findall(r"<meta\s[^>]*>", html_text, re.I):
        attrs = dict(re.findall(r'([a-zA-Z:_-]+)\s*=\s*"([^"]*)"', tag))
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        if key in keys and attrs.get("content"):
            return html_lib.unescape(attrs["content"].strip())
    return ""


async def _fetch_text(url: str, limit: int = 2_000_000) -> str:
    """GET a page with **no timeout** — however long the server takes."""
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fa,en;q=0.8",
    }
    async with httpx.AsyncClient(timeout=NO_TIMEOUT, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        if response.status_code >= 400:
            raise VideoError(f"http_{response.status_code}")
        return response.text[:limit]


async def _fetch_json(url: str) -> dict:
    async with httpx.AsyncClient(timeout=NO_TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": config.USER_AGENT}) as client:
        response = await client.get(url)
        if response.status_code >= 400:
            raise VideoError(f"http_{response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise VideoError("bad_json") from exc


# ── captions ────────────────────────────────────────────────────────────────


def _vtt_to_text(vtt: str) -> str:
    """Turn VTT/SRT cues into one de-duplicated paragraph."""
    seen: list[str] = []
    for line in (vtt or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "NOTE", "STYLE", "Kind:", "Language:")):
            continue
        if "-->" in line or re.fullmatch(r"\d+", line):
            continue
        line = html_lib.unescape(re.sub(r"<[^>]+>", "", line))
        if line and line not in seen[-12:]:
            seen.append(line)
    return _clean(" ".join(seen), 60000)


async def _caption_text(base_url: str) -> str:
    """Download a caption track as VTT; falls back to raw XML (timedtext)."""
    if not base_url:
        return ""
    try:
        raw = await _fetch_text(base_url + ("&fmt=vtt" if "?" in base_url else "?fmt=vtt"))
        text = _vtt_to_text(raw)
        if text:
            return text
    except VideoError:
        pass
    try:
        return _vtt_to_text(await _fetch_text(base_url))
    except VideoError:
        return ""


# ── YouTube ─────────────────────────────────────────────────────────────────


def _yt_initial(html_text: str) -> dict:
    """Parse ``ytInitialPlayerResponse`` with a real JSON decoder."""
    marker = "ytInitialPlayerResponse"
    idx = html_text.find(marker)
    while idx != -1:
        brace = html_text.find("{", idx)
        if brace == -1:
            return {}
        try:
            obj, _ = json.JSONDecoder().raw_decode(html_text[brace:])
            if isinstance(obj, dict) and obj:
                return obj
        except ValueError:
            pass
        idx = html_text.find(marker, idx + 1)
    return {}


async def _youtube_meta(video_id: str) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    info: dict[str, Any] = {"source": "youtube", "video_id": video_id}
    html_text = ""
    try:
        html_text = await _fetch_text(url)
    except VideoError as exc:
        info["notes"] = [f"صفحهٔ یوتیوب در دسترس نبود ({exc})"]
    player = _yt_initial(html_text) if html_text else {}
    details = player.get("videoDetails") or {}
    playability = (player.get("playabilityStatus") or {}).get("reason") or ""

    title = details.get("title") or _og(html_text, ("og:title", "twitter:title"))
    desc = details.get("shortDescription") or _og(html_text, ("og:description", "twitter:description"))
    author = details.get("author") or ""
    duration = 0.0
    try:
        duration = float(details.get("lengthSeconds") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    thumbs = ((details.get("thumbnail") or {}).get("thumbnails") or [])
    thumb = (thumbs[-1].get("url") if thumbs else "") or _og(html_text, ("og:image",))

    # oEmbed keeps titles working when the watch page is blocked or a bot wall
    if not title or not author:
        try:
            oembed = await _fetch_json(
                "https://www.youtube.com/oembed?format=json&url="
                + httpx.URL(url).__str__()
            )
            title = title or oembed.get("title", "")
            author = author or oembed.get("author_name", "")
            thumb = thumb or oembed.get("thumbnail_url", "")
        except (VideoError, httpx.HTTPError):
            pass
    if not title and playability:
        title = f"YouTube {video_id}"

    tracks = (((player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
              .get("captionTracks") or [])
    transcript, lang = "", ""
    for wanted in ("fa", "en"):
        track = next((t for t in tracks if (t.get("languageCode") or "").startswith(wanted)), None)
        if track and track.get("baseUrl"):
            transcript = await _caption_text(track["baseUrl"])
            if transcript:
                lang = (track.get("languageCode") or "")[:8]
                break
    if not transcript and tracks and tracks[0].get("baseUrl"):
        transcript = await _caption_text(tracks[0]["baseUrl"])
        lang = (tracks[0].get("languageCode") or "")[:8] if transcript else ""
    if not transcript:
        info["notes"] = info.get("notes", []) + ["زیرنویس عمومی برای این ویدیو پیدا نشد"]

    info.update({
        "title": _clean(title, 300) or url,
        "description": _clean(desc, 6000),
        "author": _clean(author, 160),
        "duration": duration,
        "thumbnail": thumb,
        "transcript": transcript[:60000],
        "transcript_lang": lang,
    })
    return info


# ── Vimeo / generic page ────────────────────────────────────────────────────


async def _page_meta(url: str, source: str) -> dict:
    html_text = await _fetch_text(url)
    title = _og(html_text, ("og:title", "twitter:title"))
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I)
        title = match.group(1) if match else ""
    duration = 0.0
    try:
        duration = float(_og(html_text, ("og:video:duration", "twitter:player:duration")) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    video_url = _og(html_text, ("og:video:url", "og:video:secure_url", "og:video",
                                "twitter:player:stream"))
    if not video_url:
        match = re.search(r"<video[^>]+src=[\"']([^\"']+\.(?:mp4|webm|m3u8)[^\"']*)", html_text, re.I)
        video_url = match.group(1) if match else ""
    transcript = ""
    if source == "vimeo":
        for blob in re.findall(r'"text_tracks"\s*:\s*(\[.*?\])', html_text, re.S)[:2]:
            try:
                tracks = json.loads(blob)
            except ValueError:
                continue
            for track in tracks if isinstance(tracks, list) else []:
                vtt_url = track.get("url") or track.get("uri") or ""
                if vtt_url.startswith("/"):
                    vtt_url = "https://vimeo.com" + vtt_url
                if vtt_url.startswith("http"):
                    transcript = await _caption_text(vtt_url)
                    if transcript:
                        break
            if transcript:
                break
    info = {
        "source": source,
        "title": _clean(title, 300) or url,
        "description": _clean(_og(html_text, ("og:description", "description",
                                             "twitter:description")), 6000),
        "author": _clean(_og(html_text, ("og:site_name", "author")), 160),
        "duration": duration,
        "thumbnail": _og(html_text, ("og:image", "og:image:secure_url", "twitter:image")),
        "transcript": transcript[:60000],
        "transcript_lang": "",
        "video_url": video_url,
    }
    if not transcript:
        info["notes"] = ["زیرنویس عمومی پیدا نشد"]
    return info


# ── media: download (no timeout) + probe ────────────────────────────────────


async def _download(url: str, dest: Path, on_progress=None, should_stop=None) -> int:
    headers = {"User-Agent": config.USER_AGENT, "Accept": "*/*"}
    total = 0
    async with httpx.AsyncClient(timeout=NO_TIMEOUT, follow_redirects=True, headers=headers) as client:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise VideoError(f"http_{response.status_code}")
            ctype = (response.headers.get("content-type") or "").lower()
            if ctype.startswith(("text/html", "application/json")):
                raise VideoError("not_a_video_file")
            with open(dest, "wb") as fh:
                async for chunk in response.aiter_bytes(256 * 1024):
                    if should_stop and should_stop():
                        raise VideoError("cancelled")
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise VideoError("file_too_large")
                    fh.write(chunk)
                    if on_progress:
                        on_progress(total)
    if total == 0:
        raise VideoError("empty_file")
    return total


def _run(cmd: list[str]) -> tuple[str, str]:
    """Run a subprocess with **no timeout** — ffmpeg/ffprobe take what they need."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.stdout or "", proc.stderr or ""


async def _probe(path: Path | str, should_stop=None) -> dict:
    """ffprobe when available, otherwise the pure-Python MP4 parser."""
    path = Path(path) if isinstance(path, str) else path
    if shutil.which("ffprobe"):
        try:
            stdout, _ = await asyncio.to_thread(
                _run,
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)],
            )
            data = json.loads(stdout or "{}")
            fmt = data.get("format") or {}
            streams = data.get("streams") or []
            video = next((s for s in streams if s.get("codec_type") == "video"), {})
            audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
            fps = 0.0
            rate = str(video.get("avg_frame_rate") or "")
            if "/" in rate:
                num, _, den = rate.partition("/")
                try:
                    fps = float(num) / float(den or 1)
                except (TypeError, ValueError, ZeroDivisionError):
                    fps = 0.0
            size = int(fmt.get("size") or 0)
            if not size:
                size = path.stat().st_size
            return {
                "duration": float(fmt.get("duration") or 0),
                "width": int(video.get("width") or 0),
                "height": int(video.get("height") or 0),
                "fps": round(fps, 2),
                "video_codec": video.get("codec_name") or "",
                "audio_codec": audio.get("codec_name") or "",
                "bitrate": int(fmt.get("bit_rate") or 0),
                "size": size,
            }
        except (ValueError, OSError) as exc:
            logger.warning("ffprobe failed, using MP4 parser: %s", exc)
    return _mp4_probe(path)


def _mp4_probe(path: Path) -> dict:
    """Minimal MP4 box walker: ``mvhd`` duration, ``tkhd`` size, ``stsd`` codec."""
    info = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0,
            "video_codec": "", "audio_codec": "", "bitrate": 0}
    try:
        info["size"] = path.stat().st_size
        if info["size"] <= _PROBE_READ_LIMIT:
            data = path.read_bytes()
        else:
            # very large file: `moov` sits at the head when the file is
            # faststart-encoded (the usual case for web video), so read a
            # bounded window instead of loading gigabytes into memory.
            with open(path, "rb") as fh:
                data = fh.read(PROBE_WINDOW)
    except OSError:
        info["size"] = 0
        return info

    def boxes(start: int, end: int):
        pos = start
        while pos + 8 <= end:
            size, kind = struct.unpack(">I4s", data[pos:pos + 8])
            header = 8
            if size == 1 and pos + 16 <= end:
                size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
                header = 16
            elif size == 0:
                size = end - pos
            if size < header or pos + size > end:
                break
            yield kind.decode("latin1", "ignore"), pos + header, pos + size
            pos += size

    def walk(start: int, end: int, depth: int = 0) -> None:
        if depth > 6:
            return
        for kind, body, box_end in boxes(start, end):
            if kind == "mvhd" and body + 20 <= box_end:
                if data[body] == 1 and body + 32 <= box_end:
                    timescale, duration = struct.unpack(">IQ", data[body + 20:body + 32])
                else:
                    timescale, duration = struct.unpack(">II", data[body + 12:body + 20])
                if timescale:
                    info["duration"] = duration / timescale
            elif kind == "tkhd" and body + 84 <= box_end:
                width, height = struct.unpack(">II", data[body + 76:body + 84])
                if width and height:
                    info["width"], info["height"] = width >> 16, height >> 16
            elif kind == "stsd" and body + 16 <= box_end:
                codec = data[body + 12:body + 16].decode("latin1", "ignore").strip()
                if codec in ("mp4a", "ac-3", "ec-3", "opus"):
                    info["audio_codec"] = info["audio_codec"] or codec
                elif codec and not info["video_codec"]:
                    info["video_codec"] = codec
            elif kind in ("moov", "trak", "mdia", "minf", "stbl"):
                walk(body, box_end, depth + 1)

    walk(0, len(data))
    if info["duration"] and info.get("size"):
        info["bitrate"] = int(info["size"] * 8 / info["duration"])
    return info


async def _extract_frames(source: str, duration: float, count: int = FRAME_COUNT,
                          should_stop=None) -> tuple[list[dict], dict]:
    """Grab evenly spaced frames and measure them. Works on a file or a URL."""
    if not shutil.which("ffmpeg"):
        return [], {"error": "no_ffmpeg"}
    if not duration or duration <= 0:
        return [], {"error": "no_duration"}
    frames: list[dict] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="rozaneh-frames-"))
    try:
        stamps = [duration * (i + 1) / (count + 1) for i in range(count)]
        for idx, stamp in enumerate(stamps):
            if should_stop and should_stop():
                break
            out = tmpdir / f"f{idx}.jpg"
            await asyncio.to_thread(
                _run,
                ["ffmpeg", "-nostdin", "-y", "-ss", f"{stamp:.2f}", "-i", source,
                 "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(out)],
            )
            if not out.exists() or out.stat().st_size == 0:
                continue
            frames.append(await asyncio.to_thread(_frame_stats, out, stamp))
        for i in range(1, len(frames)):
            frames[i]["scene_change"] = round(
                abs(frames[i]["brightness"] - frames[i - 1]["brightness"]), 1
            )
        if frames:
            frames[0]["scene_change"] = 0.0
        summary = {
            "count": len(frames),
            "avg_brightness": round(
                sum(f["brightness"] for f in frames) / len(frames), 1
            ) if frames else 0.0,
            "motion": round(
                sum(f.get("scene_change", 0) for f in frames) / max(len(frames) - 1, 1), 1
            ) if len(frames) > 1 else 0.0,
        }
        return frames, summary
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _frame_stats(path: Path, stamp: float) -> dict:
    from PIL import Image

    img = Image.open(path).convert("RGB")
    small = img.resize((64, 64))
    pixels = list(small.getdata())
    avg = tuple(sum(p[i] for p in pixels) // len(pixels) for i in range(3))
    brightness = round(0.299 * avg[0] + 0.587 * avg[1] + 0.114 * avg[2], 1)
    buckets: Counter = Counter()
    for r, g, b in pixels:
        buckets[(r // 64, g // 64, b // 64)] += 1
    (br, bg, bb), _ = buckets.most_common(1)[0]
    dominant = "#%02x%02x%02x" % (br * 64 + 32, bg * 64 + 32, bb * 64 + 32)
    thumb = img.copy()
    thumb.thumbnail((320, 320))
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=70)
    return {
        "at": round(stamp, 1),
        "brightness": brightness,
        "dominant": dominant,
        "scene_change": 0.0,
        "thumb": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(),
    }


# ── text analysis ───────────────────────────────────────────────────────────

STOP_FA = set("در از به با که و یا آن این را است هستند بود شده می شود یک برای تا هم خود".split())
STOP_EN = set("the a an and or of to in is are was were be been it its this that with for on "
              "at by from as we you they he she i not".split())

POS_FA = ["خوب", "عالی", "موفق", "پیروزی", "رشد", "بهبود", "امید", "قوی", "زیبا", "مثبت", "پیشرفت"]
NEG_FA = ["بد", "ناموفق", "بحران", "جنگ", "مرگ", "کاهش", "مشکل", "خطا", "ضعیف", "نگران", "فاجعه"]
POS_EN = ["good", "great", "success", "win", "growth", "improve", "hope", "strong", "beautiful",
          "positive", "breakthrough"]
NEG_EN = ["bad", "fail", "crisis", "war", "death", "decline", "problem", "error", "weak",
          "worry", "disaster"]


def analyse_text(title: str, description: str, transcript: str) -> dict:
    """Language, keywords, tone and topic of everything we know about the video."""
    full = f"{title}\n{description}\n{transcript}".strip()
    words = [w for w in _WORD_RE.findall(full.lower()) if len(w) > 2]
    stop = STOP_FA | STOP_EN
    freq = Counter(w for w in words if w not in stop)
    lowered = full.lower()
    pos = sum(lowered.count(w) for w in POS_FA + POS_EN)
    neg = sum(lowered.count(w) for w in NEG_FA + NEG_EN)
    sentiment = "neutral"
    if pos > neg + 1:
        sentiment = "positive"
    elif neg > pos + 1:
        sentiment = "negative"
    topic_rows = db.query("SELECT * FROM topics WHERE enabled = 1 ORDER BY id")
    best, score, _matched = topics_mod.classify(
        topic_rows, title, description + " " + transcript[:4000]
    )
    return {
        "language": translate.detect_language(full),
        "word_count": len(words),
        "reading_minutes": round(len(words) / 180, 1) if words else 0.0,
        "keywords": [w for w, _ in freq.most_common(12)],
        "sentiment": sentiment,
        "sentiment_pos": pos,
        "sentiment_neg": neg,
        "topic": topics_mod.topic_label(best),
        "topic_id": int(best["id"]) if best else 0,
        "topic_score": score,
        "has_transcript": bool(transcript.strip()),
    }


def extractive_summary(title: str, description: str, transcript: str, meta: dict) -> str:
    """Keyless fallback: best-scoring sentences plus the hard facts."""
    source = f"{description} {transcript}".strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?؟…])\s+", source) if len(s.strip()) > 30]
    freq = Counter(_WORD_RE.findall(source.lower()))
    scored = sorted(
        sentences,
        key=lambda s: sum(freq[w] for w in _WORD_RE.findall(s.lower())),
        reverse=True,
    )
    parts: list[str] = []
    if title:
        parts.append(f"«{title}»")
    if scored:
        parts.append(" ".join(scored[:3]))
    dur = float(meta.get("duration") or 0)
    facts = []
    if dur:
        facts.append(f"مدت ویدیو {int(dur // 60)} دقیقه و {int(dur % 60)} ثانیه است")
    if meta.get("width") and meta.get("height"):
        facts.append(f"کیفیت تصویر {int(meta['width'])}×{int(meta['height'])} است")
    if facts:
        parts.append("، ".join(facts) + ".")
    text = " ".join(p for p in parts if p).strip()
    return (text[:2000] or (title or "تحلیل ویدیو انجام شد."))


def build_caption(report: dict) -> str:
    """A ready-to-publish Persian caption built from the report."""
    title = report.get("title") or ""
    summary = report.get("summary") or ""
    words = report.get("text") or {}
    lines = [f"🎬 {title}".strip()]
    if summary:
        lines.append(summary)
    tags = []
    if words.get("topic"):
        tag = topics_mod.hashtag(str(words["topic"]).split(" ")[-1])
        if tag:
            tags.append(tag)
    for kw in (words.get("keywords") or [])[:3]:
        if len(kw) > 3:
            tags.append(topics_mod.hashtag(kw))
    tags = [t for t in dict.fromkeys(tags) if t]
    if tags:
        lines.append(" ".join(tags[:4]))
    if report.get("url"):
        lines.append(report["url"])
    return "\n\n".join(p for p in lines if p).strip()


# ── main analysis (no timeouts anywhere) ────────────────────────────────────


async def analyse(url: str, *, title_hint: str = "", transcript_hint: str = "",
                  local_file: str | None = None, on_progress=None,
                  should_stop=None) -> dict:
    """Analyse one video. Runs as long as it needs — there is no timeout."""
    url = (url or "").strip()
    if not url and not transcript_hint and not local_file:
        raise VideoError("url_required")
    source = "upload" if local_file else classify_url(url)
    if source == "unknown" and not transcript_hint:
        raise VideoError("unsupported_source")
    notes: list[str] = []

    def stage(name: str) -> None:
        if should_stop and should_stop():
            raise VideoError("cancelled")
        if on_progress:
            on_progress(name)

    stage("fetching")
    meta: dict[str, Any] = {"source": source}
    tmpdir: Path | None = None
    media: str | None = local_file  # what ffmpeg will read: a file path or a URL

    try:
        if source == "youtube":
            meta.update(await _youtube_meta(_YT_RE.search(url).group(1)))
        elif source in ("vimeo", "page"):
            meta.update(await _page_meta(url, source))
            if meta.get("video_url"):
                media = meta["video_url"]
                if not meta["video_url"].endswith(".m3u8"):
                    try:
                        tmpdir = Path(tempfile.mkdtemp(prefix="rozaneh-video-"))
                        local = tmpdir / "video.bin"
                        meta["size"] = await _download(
                            meta["video_url"], local, should_stop=should_stop
                        )
                        media = str(local)
                        notes.append("فایل ویدیوی صفحه دانلود و بررسی شد")
                    except VideoError as exc:
                        notes.append(f"دانلود فایل ویدیوی صفحه ممکن نشد ({exc})")
                        media = None
                else:
                    notes.append("پخش‌کنندهٔ HLS تشخیص داده شد")
        elif source == "direct":
            tmpdir = Path(tempfile.mkdtemp(prefix="rozaneh-video-"))
            local = tmpdir / ("video" + (Path(urlparse(url).path).suffix or ".mp4"))
            meta["size"] = await _download(url, local, should_stop=should_stop)
            media = str(local)
            notes.append("فایل ویدیو دانلود شد")
        elif source == "upload" and local_file:
            media = local_file

        if title_hint:
            meta["title"] = title_hint
        if transcript_hint:
            meta["transcript"] = transcript_hint[:60000]
            meta["transcript_lang"] = meta.get("transcript_lang") or translate.detect_language(
                transcript_hint
            )
            notes.append("متن ورودی کاربر استفاده شد")

        needs_probe = media and (
            (meta.get("duration") or 0) <= 0 or not meta.get("width") or not meta.get("size")
        )
        if needs_probe:
            stage("probing")
            try:
                probed = await _probe(media, should_stop)
                meta.update({k: v for k, v in probed.items() if v})
            except (ValueError, OSError) as exc:
                notes.append(f"خواندن مشخصات فایل ممکن نشد ({exc})")

        stage("frames")
        frames: list[dict] = []
        frame_meta: dict = {}
        if media and settings()["frames"]:
            frames, frame_meta = await _extract_frames(
                media, float(meta.get("duration") or 0), should_stop=should_stop
            )
            if frame_meta.get("error") == "no_ffmpeg":
                notes.append("ffmpeg نصب نیست؛ فریم استخراج نشد")
            elif frame_meta.get("error") == "no_duration":
                notes.append("مدت ویدیو مشخص نشد؛ فریم استخراج نشد")
            elif frames:
                notes.append(f"{len(frames)} فریم در طول ویدیو بررسی شد")
        elif not media:
            notes.append("فایل ویدیو در دسترس نبود؛ فقط متن و متادیتا تحلیل شد")

        stage("text")
        text_info = analyse_text(
            meta.get("title", ""), meta.get("description", ""), meta.get("transcript", "")
        )

        stage("summary")
        summary = ""
        summary_engine = "extractive"
        if settings()["summarize"]:
            base = (meta.get("transcript") or meta.get("description") or meta.get("title") or "")
            summary = await translate.summarize_no_timeout(base, title=meta.get("title", ""))
            if summary:
                summary_engine = "llm"
        if not summary:
            summary = extractive_summary(
                meta.get("title", ""), meta.get("description", ""),
                meta.get("transcript", ""), meta,
            )

        report = {
            "url": url,
            "source": source,
            "title": meta.get("title") or url or "video",
            "author": meta.get("author", ""),
            "description": meta.get("description", ""),
            "duration": round(float(meta.get("duration") or 0), 1),
            "width": int(meta.get("width") or 0),
            "height": int(meta.get("height") or 0),
            "fps": float(meta.get("fps") or 0),
            "video_codec": meta.get("video_codec", ""),
            "audio_codec": meta.get("audio_codec", ""),
            "bitrate": int(meta.get("bitrate") or 0),
            "size": int(meta.get("size") or 0),
            "thumbnail": meta.get("thumbnail", ""),
            "transcript": meta.get("transcript", ""),
            "transcript_lang": meta.get("transcript_lang", ""),
            "summary": summary,
            "summary_engine": summary_engine,
            "frames": frames,
            "frames_meta": frame_meta,
            "text": text_info,
            "ffmpeg": ffmpeg_available(),
            "notes": notes,
            "analysed_at": _now(),
        }
        report["caption"] = build_caption(report)
        return report
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── background jobs ─────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _set_job(job_id: int, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE video_analyses SET {assignments} WHERE id = ?",
               (*fields.values(), job_id))


def get_job(job_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM video_analyses WHERE id = ?", (job_id,))
    if not row:
        return None
    row["report"] = json.loads(row.get("report") or "{}")
    return row


def list_jobs(limit: int = 30) -> list[dict]:
    rows = db.query("SELECT * FROM video_analyses ORDER BY id DESC LIMIT ?", (min(limit, 100),))
    for row in rows:
        row["report"] = json.loads(row.get("report") or "{}")
    return rows


def start_analysis(url: str, *, title_hint: str = "", transcript_hint: str = "",
                   mode: str = "manual", local_file: str | None = None,
                   cleanup_dir: str | None = None) -> int:
    """Insert a job row and run it in the background. Returns immediately."""
    job_id = db.execute(
        "INSERT INTO video_analyses(url, title, status, mode, stage, error, report, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ((url or "")[:2000], (title_hint or "")[:400], "running", mode, "queued", "", "{}", _now()),
    )
    _CANCELLED.discard(job_id)

    async def runner() -> None:
        def progress(name: str) -> None:
            _set_job(job_id, stage=name)

        def stopped() -> bool:
            return job_id in _CANCELLED

        try:
            report = await analyse(
                url, title_hint=title_hint, transcript_hint=transcript_hint,
                local_file=local_file, on_progress=progress, should_stop=stopped,
            )
            _set_job(job_id, status="done", stage="done", title=report["title"][:400],
                     summary=report["summary"][:8000],
                     report=json.dumps(report, ensure_ascii=False), finished_at=_now())
            db.log(None, f"🎬 تحلیل ویدیو تمام شد: {report['title'][:80]}", code="video_done")
        except VideoError as exc:
            if str(exc) == "cancelled" or job_id in _CANCELLED:
                _set_job(job_id, status="cancelled", stage="cancelled", error="cancelled",
                         finished_at=_now())
            else:
                _set_job(job_id, status="failed", stage="failed", error=str(exc)[:500],
                         finished_at=_now())
                db.log(None, f"🎬 تحلیل ویدیو ناموفق بود: {exc}", level="error",
                       code="video_failed")
        except asyncio.CancelledError:
            _set_job(job_id, status="cancelled", stage="cancelled", error="cancelled",
                     finished_at=_now())
            raise
        except Exception as exc:  # noqa: BLE001 — a job must never kill the loop
            logger.exception("video analysis %s failed", job_id)
            _set_job(job_id, status="failed", stage="failed", error=str(exc)[:500],
                     finished_at=_now())
        finally:
            _TASKS.pop(job_id, None)
            _CANCELLED.discard(job_id)
            if cleanup_dir:
                shutil.rmtree(cleanup_dir, ignore_errors=True)

    _TASKS[job_id] = asyncio.create_task(runner(), name=f"video-{job_id}")
    return job_id


def cancel_analysis(job_id: int) -> bool:
    task = _TASKS.get(job_id)
    if task is None or task.done():
        return False
    _CANCELLED.add(job_id)
    task.cancel()
    return True


def delete_analysis(job_id: int) -> None:
    cancel_analysis(job_id)
    db.execute("DELETE FROM video_analyses WHERE id = ?", (job_id,))


# ── smart mode: auto-analyse new video links from the archive ───────────────


def pending_video_urls(limit: int = SMART_BATCH) -> list[dict]:
    """Archive items with a video link that has never been analysed."""
    rows = db.query(
        "SELECT link, title FROM items WHERE link LIKE '%http%' ORDER BY id DESC LIMIT 400"
    )
    seen = set()
    for row in db.query("SELECT url FROM video_analyses"):
        seen.add(url_hash(row["url"]))
    out: list[dict] = []
    for row in rows:
        link = (row.get("link") or "").strip()
        if not link or not is_video_url(link) or url_hash(link) in seen:
            continue
        seen.add(url_hash(link))
        out.append({"url": link, "title": row.get("title") or ""})
        if len(out) >= limit:
            break
    return out


async def maybe_smart_tick() -> list[int]:
    """Called from the scheduler tick. No-op unless smart mode is ON."""
    cfg = settings()
    if not cfg["smart"]:
        return []
    last = cfg["last_smart_run"]
    try:
        last_dt = datetime.fromisoformat(last) if last else None
    except ValueError:
        last_dt = None
    now = datetime.now(timezone.utc)
    if last_dt and (now - last_dt).total_seconds() < cfg["interval_minutes"] * 60:
        return []
    db.set_setting("video_smart_last_run", _now())
    started: list[int] = []
    for item in pending_video_urls():
        started.append(start_analysis(item["url"], title_hint=item["title"], mode="auto"))
    if started:
        db.log(None, f"🎬 حالت هوشمند: {len(started)} ویدیو برای تحلیل انتخاب شد",
               code="video_smart")
    return started
