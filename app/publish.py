"""Destination adapters + message rendering — real delivery, real limits.

Every adapter shares one contract::

    await send_message(destination, text) -> (ok, detail)
    await send_photo(destination, photo_url, caption) -> (ok, detail)
    await verify(destination) -> (ok, detail)

Platform rules that this module honours (checked against the public docs):

==========  ===========  =============  ====================  ==================
platform    text limit   caption limit  formatting            photo
==========  ===========  =============  ====================  ==================
Telegram    4096         1024           HTML / plain          URL or upload
Bale        4096         4096           plain only            URL or upload
Eitaa       4096         4096           plain only            upload / file id
Rubika      4000         4000           plain only (metadata) upload (3 steps)
webhook     no limit     no limit       plain + html both     not used
==========  ===========  =============  ====================  ==================

Rate limits (Telegram Bot FAQ): roughly one message per second into a single
chat, 20 messages per minute into a group/channel, ~30 per second overall.
``MIN_INTERVAL`` below keeps us politely under those numbers and any 429 that
still shows up is retried once after ``retry_after``.
"""

from __future__ import annotations

import asyncio
import html
import io
import re
import time
from typing import Any

import httpx

from . import config, db

TAG_RE = re.compile(r"<[^>]+>")

PLATFORM_META = {p["id"]: p for p in config.PLATFORMS}

# ── where each messenger actually lives ─────────────────────────────────────
TELEGRAM_API = "https://api.telegram.org"
BALE_API = "https://tapi.bale.ai"
EITAA_API = "https://eitaayar.ir/api"
RUBIKA_API = "https://botapi.rubika.ir/v3"

# ── the rules, in one table (used by the panel too) ─────────────────────────
PLATFORM_RULES: dict[str, dict[str, Any]] = {
    "telegram": {
        "text": 4096,
        "caption": 1024,
        "html": True,
        "markdown": True,
        "photo": "url_or_upload",
        "photo_max": 10 * 1024 * 1024,
        "min_interval": 3.0,
        "doc": "https://core.telegram.org/bots/api",
        "notes_fa": "متن تا ۴۰۹۶ کاراکتر، کپشن عکس تا ۱۰۲۴ کاراکتر؛ ربات باید در کانال ادمین باشد.",
        "notes_en": "4096-char text, 1024-char caption; the bot must be an admin of the channel.",
    },
    "bale": {
        "text": 4096,
        "caption": 4096,
        "html": False,
        "markdown": False,
        "photo": "url_or_upload",
        "photo_max": 10 * 1024 * 1024,
        "min_interval": 1.5,
        "doc": "https://docs.bale.ai/",
        "notes_fa": "متن ۱ تا ۴۰۹۶ کاراکتر؛ کپشن تا ۴۰۹۶. قالب بندی HTML ندارد، متن ساده می‌رود.",
        "notes_en": "Text 1–4096 characters, caption up to 4096. No HTML parse mode — plain text.",
    },
    "eitaa": {
        "text": 4096,
        "caption": 4096,
        "html": False,
        "markdown": False,
        "photo": "upload",
        "photo_max": 10 * 1024 * 1024,
        "min_interval": 2.0,
        "doc": "https://eitaayar.ir",
        "notes_fa": "روش ارسال sendMessage و sendFile است (sendPhoto ندارد)؛ آیدی کانال بدون @ و ربات باید ادمین باشد.",
        "notes_en": "Uses sendMessage/sendFile (no sendPhoto); channel id without @, bot must be admin.",
    },
    "rubika": {
        "text": 4000,
        "caption": 4000,
        "html": False,
        "markdown": False,
        "photo": "request_upload",
        "photo_max": 10 * 1024 * 1024,
        "min_interval": 2.0,
        "doc": "https://rubika.ir/botapi/methods",
        "notes_fa": "عکس باید سه مرحله آپلود شود (requestSendFile → upload → sendFile)؛ متن ساده.",
        "notes_en": "Photos need the 3-step upload (requestSendFile → upload → sendFile); plain text.",
    },
    "webhook": {
        "text": 20000,
        "caption": 20000,
        "html": True,
        "markdown": True,
        "photo": "none",
        "photo_max": 0,
        "min_interval": 0.0,
        "doc": "",
        "notes_fa": "هر آیتم به‌صورت JSON به آدرس شما POST می‌شود (متن و plain_text هر دو).",
        "notes_en": "Each item is POSTed to your URL as JSON (both text and plain_text).",
    },
}

MAX_IMAGE_BYTES = 10 * 1024 * 1024
_last_sent: dict[str, float] = {}


def rules_for(platform: str) -> dict[str, Any]:
    return PLATFORM_RULES.get((platform or "").lower(), PLATFORM_RULES["telegram"])


def supports_photo(platform: str) -> bool:
    platform = (platform or "").lower()
    if PLATFORM_META.get(platform, {}).get("supports_photo"):
        return True
    return rules_for(platform)["photo"] != "none"


def supports_html(platform: str) -> bool:
    return bool(rules_for(platform)["html"])


def strip_tags(text: str) -> str:
    return html.unescape(TAG_RE.sub("", text or ""))


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[-60:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip() + "…"


def platform_limit(platform: str, kind: str = "text") -> int:
    """Effective character budget: hard platform rule ∩ admin setting."""
    hard = int(rules_for(platform)[kind])
    if kind != "text":
        return hard
    configured = int(db.get_setting("max_message_chars") or 3800)
    return min(configured, hard)


# ── plumbing ────────────────────────────────────────────────────────────────


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=config.HTTP_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": config.USER_AGENT},
    )


def _reply_error(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return (response.text or "").strip()[:300] or f"http {response.status_code}"
    if isinstance(data, dict):
        for key in ("description", "error", "status_det", "message", "detail"):
            if data.get(key):
                return str(data[key])[:300]
        return str(data)[:300]
    return str(data)[:300]


def _retry_after(response: httpx.Response) -> float:
    header = response.headers.get("retry-after")
    if header and header.isdigit():
        return min(float(header), 8.0)
    try:
        data = response.json()
        params = (data or {}).get("parameters") or {}
        if isinstance(params, dict) and params.get("retry_after"):
            return min(float(params["retry_after"]), 8.0)
    except ValueError:
        pass
    return 3.0


def _check_generic(response: httpx.Response) -> tuple[bool, str]:
    """Telegram-shaped envelopes: {"ok": true|false, "description": ...}."""
    if response.status_code == 429:
        return False, f"rate_limited:{int(_retry_after(response))}s"
    if response.status_code >= 400:
        return False, f"http_{response.status_code}:{_reply_error(response)}"
    try:
        data = response.json()
    except ValueError:
        return True, "ok"
    if isinstance(data, dict) and data.get("ok") is False:
        return False, _reply_error(response)
    return True, "ok"


def _check_rubika(response: httpx.Response) -> tuple[bool, str]:
    if response.status_code >= 400:
        return False, f"http_{response.status_code}:{_reply_error(response)}"
    try:
        data = response.json()
    except ValueError:
        return True, "ok"
    if isinstance(data, dict):
        status = str(data.get("status", "")).upper()
        if status and status != "OK":
            return False, str(data.get("status_det") or data.get("dev_message") or status)[:300]
    return True, "ok"


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
    headers: dict | None = None,
    checker=_check_generic,
) -> tuple[bool, str, dict]:
    """POST with one polite retry when the platform answers 429."""
    for attempt in (0, 1):
        try:
            response = await client.request(
                method, url, json=json, data=data, files=files, headers=headers or {}
            )
        except httpx.TimeoutException:
            return False, "network_error:timeout", {}
        except httpx.HTTPError as exc:
            return False, f"network_error:{exc.__class__.__name__}", {}
        if response.status_code == 429 and attempt == 0:
            await asyncio.sleep(_retry_after(response))
            continue
        ok, detail = checker(response)
        payload: dict = {}
        if ok:
            try:
                body = response.json()
                payload = body if isinstance(body, dict) else {"raw": body}
            except ValueError:
                payload = {}
        return ok, detail, payload
    return False, "rate_limited", {}


async def _throttle(destination: dict) -> None:
    """Keep at least ``min_interval`` seconds between sends to one chat."""
    key = str(destination.get("id") or destination.get("chat_id") or "")
    gap = float(rules_for(destination.get("platform", "")).get("min_interval") or 0)
    if not key or gap <= 0:
        return
    last = _last_sent.get(key)
    if last is not None:
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(min(wait, gap))
    _last_sent[key] = time.monotonic()


def _token_of(destination: dict) -> str:
    return (destination.get("bot_token") or "").strip()


def _chat_of(destination: dict) -> str:
    return (destination.get("chat_id") or "").strip()


def _parse_mode(destination: dict) -> str | None:
    """Only Telegram understands HTML; everyone else gets clean plain text."""
    platform = (destination.get("platform") or "").lower()
    mode = (destination.get("parse_mode") or "HTML").upper()
    rules = rules_for(platform)
    if mode == "HTML" and rules["html"]:
        return "HTML"
    if mode == "MARKDOWN" and rules["markdown"]:
        return "Markdown"
    return None


# ── images ──────────────────────────────────────────────────────────────────


async def fetch_image(url: str) -> tuple[bytes, str, str] | None:
    """Download a feed image and return bytes that every messenger accepts.

    Telegram/Bale only take JPEG (or PNG) photos, while many news CDNs answer
    with WebP/AVIF — those get converted here so the photo is never the reason
    a headline is dropped.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        async with _client() as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    return None
                ctype = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype and not ctype.startswith("image/"):
                    return None
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > min(MAX_IMAGE_BYTES, int(rules_for("telegram")["photo_max"])):
                        return None
    except (httpx.HTTPError, OSError):
        return None
    data = bytes(buffer)
    if not data:
        return None
    if ctype in {"image/jpeg", "image/jpg", "image/png"} and len(data) <= 9 * 1024 * 1024:
        return data, "photo." + ("png" if ctype.endswith("png") else "jpg"), ctype
    try:  # WebP / AVIF / GIF / oversized JPEG → clean JPEG
        from PIL import Image  # imported lazily so the app still boots without pillow
    except Exception:  # pragma: no cover - pillow is in requirements
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((2560, 2560))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=86, optimize=True)
        return out.getvalue(), "photo.jpg", "image/jpeg"
    except Exception:
        return None


# ── adapters ────────────────────────────────────────────────────────────────


async def _telegram_like(
    destination: dict, method: str, payload: dict, files: dict | None = None
) -> tuple[bool, str]:
    url = f"{TELEGRAM_API}/bot{_token_of(destination)}/{method}"
    async with _client() as client:
        ok, detail, _ = await _request(client, "POST", url, json=payload, files=files)
    return ok, detail


async def send_telegram(destination: dict, text: str) -> tuple[bool, str]:
    payload: dict[str, Any] = {
        "chat_id": _chat_of(destination),
        "text": text,
        "disable_web_page_preview": False,
    }
    mode = _parse_mode(destination)
    if mode:
        payload["parse_mode"] = mode
    return await _telegram_like(destination, "sendMessage", payload)


async def send_bale(destination: dict, text: str) -> tuple[bool, str]:
    url = f"{BALE_API}/bot{_token_of(destination)}/sendMessage"
    payload = {"chat_id": _chat_of(destination), "text": strip_tags(text)}
    async with _client() as client:
        ok, detail, _ = await _request(client, "POST", url, json=payload)
    return ok, detail


async def send_eitaa(destination: dict, text: str) -> tuple[bool, str]:
    url = f"{EITAA_API}/{_token_of(destination)}/sendMessage"
    payload = {"chat_id": _chat_of(destination), "text": strip_tags(text)}
    async with _client() as client:
        ok, detail, _ = await _request(client, "POST", url, json=payload)
    return ok, detail


async def send_rubika(destination: dict, text: str) -> tuple[bool, str]:
    url = f"{RUBIKA_API}/{_token_of(destination)}/sendMessage"
    payload = {"chat_id": _chat_of(destination), "text": strip_tags(text)}
    async with _client() as client:
        ok, detail, _ = await _request(client, "POST", url, json=payload, checker=_check_rubika)
    return ok, detail


async def send_webhook(destination: dict, text: str) -> tuple[bool, str]:
    url = _chat_of(destination)
    if not url.startswith("http"):
        return False, "webhook_url_invalid"
    payload = {
        "platform": "webhook",
        "chat_id": destination.get("name", ""),
        "text": text,
        "plain_text": strip_tags(text),
        "token": destination.get("bot_token", ""),
    }
    headers = {"X-Token": destination["bot_token"]} if destination.get("bot_token") else {}
    async with _client() as client:
        ok, detail, _ = await _request(client, "POST", url, json=payload, headers=headers)
    return ok, detail


PLATFORMS = {
    "telegram": send_telegram,
    "bale": send_bale,
    "eitaa": send_eitaa,
    "rubika": send_rubika,
    "webhook": send_webhook,
}


# ── photos ──────────────────────────────────────────────────────────────────


async def _send_photo_telegram(destination: dict, image, caption: str) -> tuple[bool, str]:
    chat_id = _chat_of(destination)
    payload: dict[str, Any] = {"chat_id": chat_id, "caption": caption}
    # Captions are rendered as plain text; do not re-interpret them as HTML.
    if image:
        data, filename, mime = image
        files = {"photo": (filename, data, mime)}
        url = f"{TELEGRAM_API}/bot{_token_of(destination)}/sendPhoto"
        async with _client() as client:
            ok, detail, _ = await _request(client, "POST", url, data=payload, files=files)
        return ok, detail
    return False, "image_unavailable"


async def _send_photo_bale(destination: dict, image, caption: str, photo_url: str) -> tuple[bool, str]:
    chat_id = _chat_of(destination)
    url = f"{BALE_API}/bot{_token_of(destination)}/sendPhoto"
    if image:
        data, filename, mime = image
        async with _client() as client:
            ok, detail, _ = await _request(
                client,
                "POST",
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": (filename, data, mime)},
            )
        return ok, detail
    if photo_url:
        async with _client() as client:
            ok, detail, _ = await _request(
                client,
                "POST",
                url,
                json={"chat_id": chat_id, "photo": photo_url, "caption": caption},
            )
        return ok, detail
    return False, "image_unavailable"


async def _send_photo_eitaa(destination: dict, image, caption: str, photo_url: str) -> tuple[bool, str]:
    url = f"{EITAA_API}/{_token_of(destination)}/sendFile"
    chat_id = _chat_of(destination)
    if image:
        data, filename, mime = image
        async with _client() as client:
            ok, detail, _ = await _request(
                client,
                "POST",
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"file": (filename, data, mime)},
            )
        return ok, detail
    # eitaayar also accepts a public URL or a file_id in the same field
    async with _client() as client:
        ok, detail, _ = await _request(
            client,
            "POST",
            url,
            data={"chat_id": chat_id, "file": photo_url, "caption": caption},
        )
    return ok, detail


async def _send_photo_rubika(destination: dict, image, caption: str, photo_url: str) -> tuple[bool, str]:
    """requestSendFile → POST bytes to the returned upload_url → sendFile."""
    token = _token_of(destination)
    chat_id = _chat_of(destination)
    text = caption
    async with _client() as client:
        ok, detail, body = await _request(
            client,
            "POST",
            f"{RUBIKA_API}/{token}/requestSendFile",
            json={"type": "Image"},
            checker=_check_rubika,
        )
        if not ok:
            return ok, detail
        info = (body.get("data") or {}) if isinstance(body, dict) else {}
        upload_url = info.get("upload_url") or ""
        file_id = info.get("file_id") or ""
        if not upload_url or not file_id or not image:
            return False, "rubika_upload_unavailable"
        data, filename, mime = image
        up = await client.request(
            "POST", upload_url, files={"file": (filename, data, mime)}
        )
        if up.status_code >= 400:
            return False, f"http_{up.status_code}:{_reply_error(up)}"
        ok, detail, _ = await _request(
            client,
            "POST",
            f"{RUBIKA_API}/{token}/sendFile",
            json={"chat_id": chat_id, "file_id": file_id, "text": text},
            checker=_check_rubika,
        )
    return ok, detail


def delivery_uncertain(detail: str) -> bool:
    """A timeout/server error might have occurred after the post was accepted."""
    return str(detail or "").startswith(("network_error:", "http_5"))


async def send_photo(destination: dict, photo_url: str, caption: str, *,
                     image: tuple[bytes, str, str] | None = None) -> tuple[bool, str]:
    """Send one photo + caption; callers may pass already downloaded bytes."""
    platform = (destination.get("platform") or "telegram").lower()
    rules = rules_for(platform)
    if rules["photo"] == "none":
        return await send_message(destination, caption)
    caption = _truncate_units(caption, platform_limit(platform, "caption"))
    await _throttle(destination)
    if image is None and photo_url:
        image = await fetch_image(photo_url)
    if platform == "telegram":
        ok, detail = await _send_photo_telegram(destination, image, caption)
        if not ok and not image and photo_url and not delivery_uncertain(detail):
            # last resort: let Telegram fetch the URL itself (JPEG only, ≤5 MB)
            async with _client() as client:
                ok, detail, _ = await _request(
                    client,
                    "POST",
                    f"{TELEGRAM_API}/bot{_token_of(destination)}/sendPhoto",
                    json={"chat_id": _chat_of(destination), "photo": photo_url, "caption": caption},
                )
        return ok, detail
    if platform == "bale":
        return await _send_photo_bale(destination, image, caption, photo_url)
    if platform == "eitaa":
        return await _send_photo_eitaa(destination, image, caption, photo_url)
    if platform == "rubika":
        return await _send_photo_rubika(destination, image, caption, photo_url)
    return await send_message(destination, caption)


# ── public API ──────────────────────────────────────────────────────────────


async def send_message(destination: dict, text: str) -> tuple[bool, str]:
    platform = (destination.get("platform") or "telegram").lower()
    handler = PLATFORMS.get(platform)
    if handler is None:
        return False, "platform_unsupported"
    if platform != "webhook" and not _token_of(destination):
        return False, "token_missing"
    if not _chat_of(destination):
        return False, "chat_id_missing"
    text = _truncate(text, platform_limit(platform))
    if not text.strip():
        return False, "empty_message"
    if not supports_html(platform):
        text = strip_tags(text)
    await _throttle(destination)
    return await handler(destination, text)


async def verify(destination: dict) -> tuple[bool, str, dict]:
    """Talk to the platform *about* the bot: is the token alive, can it post?

    Returns the bot identity when the platform exposes it, so the panel can show
    “@my_bot → کانال خبری” instead of a blind “sent”.
    """
    platform = (destination.get("platform") or "telegram").lower()
    token = _token_of(destination)
    chat_id = _chat_of(destination)
    if platform == "webhook":
        return (True, "webhook_ready", {"url": chat_id}) if chat_id.startswith("http") else (
            False,
            "webhook_url_invalid",
            {},
        )
    if not token:
        return False, "token_missing", {}
    if not chat_id:
        return False, "chat_id_missing", {}

    async with _client() as client:
        if platform in {"telegram", "bale"}:
            base = TELEGRAM_API if platform == "telegram" else BALE_API
            ok, detail, body = await _request(client, "GET", f"{base}/bot{token}/getMe")
            if not ok:
                return False, f"getMe:{detail}", {}
            me = body.get("result") or {}
            info = {"bot": me.get("username") or me.get("first_name") or "", "id": me.get("id")}
            if platform == "telegram":
                ok, detail, body = await _request(
                    client,
                    "POST",
                    f"{base}/bot{token}/getChat",
                    json={"chat_id": chat_id},
                )
                if not ok:
                    return False, f"getChat:{detail}", info
                chat = body.get("result") or {}
                info["chat"] = chat.get("title") or chat.get("username") or chat_id
                info["type"] = chat.get("type") or ""
            return True, "ready", info

        if platform == "eitaa":
            ok, detail, body = await _request(client, "GET", f"{EITAA_API}/{token}/getMe")
            if ok:
                me = body.get("result") or body.get("data") or {}
                return True, "ready", {"bot": me.get("bot") or me.get("name") or "eitaa"}
            if detail.startswith("network_error"):
                # the eitaayar host never answered — say so instead of “ready”
                return False, detail, {}
            # getMe is not exposed on every eitaayar plan; the token shape is fine
            return True, "token_unverified", {"chat": chat_id, "note": detail}

        if platform == "rubika":
            ok, detail, body = await _request(
                client, "POST", f"{RUBIKA_API}/{token}/getMe", json={}, checker=_check_rubika
            )
            if not ok:
                return False, f"getMe:{detail}", {}
            data = (body.get("data") or {}) if isinstance(body, dict) else {}
            bot = data.get("bot") or {}
            return True, "ready", {"bot": bot.get("bot_title") or bot.get("username") or "rubika"}

    return False, "platform_unsupported", {}


async def test_destination(destination: dict) -> tuple[bool, str]:
    """A real send, not a simulation — same code path the schedule uses."""
    text = (
        "✅ روزنه وصل شد.\n"
        "این یک پیام آزمایشی است؛ سرخط‌های واقعی طبق زمان‌بندی همین‌جا منتشر می‌شوند.\n\n"
        "✅ Rozaneh is connected.\n"
        "This is a test message — real headlines follow your schedule."
    )
    return await send_message(destination, text)


# ── rendering ───────────────────────────────────────────────────────────────


def _signature_enabled() -> bool:
    return (db.get_setting("signature_enabled") or "1") != "0"


def _use_html(destination: dict) -> bool:
    return _parse_mode(destination) == "HTML"


def render_item(item: dict, destination: dict, with_signature: bool | None = None) -> str:
    """Full message body for one news item."""
    if with_signature is None:
        with_signature = _signature_enabled()
    use_html = _use_html(destination)

    title = item.get("title_fa") or item.get("title") or ""
    summary = (item.get("summary_fa") or item.get("summary") or "").strip() if item.get("_include_summary", True) else ""
    if not summary and item.get("_summary_unavailable"):
        summary = "متن تکمیلی این خبر از منبع در دسترس نبود."
    if len(summary) > 420:
        summary = summary[:417].rstrip() + "…"

    link = item.get("link") or ""
    topic = item.get("topic") or ""
    feed_title = item.get("feed_title") or ""

    if use_html:
        parts = [f"<b>{html.escape(title, quote=False)}</b>"]
        if summary:
            parts.append(html.escape(summary, quote=False))
        if topic:
            parts.append(html.escape(topic, quote=False))
        if link:
            parts.append(
                f'🔗 <a href="{html.escape(link, quote=True)}">'
                f"{html.escape(feed_title or 'source', quote=False)}</a>"
            )
    else:
        parts = [title]
        if summary:
            parts.append(summary)
        if topic:
            parts.append(topic)
        if link:
            parts.append(f"🔗 {feed_title or 'source'}: {link}")

    if with_signature:
        footer = (destination.get("footer") or "").strip()
        global_footer = (db.get_setting("global_footer") or "").strip()
        if footer:
            parts.append(f"<i>{html.escape(footer, quote=False)}</i>" if use_html else f"— {footer}")
        if global_footer:
            parts.append(
                f"<i>{html.escape(global_footer, quote=False)}</i>" if use_html else global_footer
            )

    return "\n\n".join(p for p in parts if p)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _truncate_units(text: str, limit: int) -> str:
    if _utf16_len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    result, used = [], 0
    for char in text:
        width = _utf16_len(char)
        if used + width > limit - 1:
            break
        result.append(char)
        used += width
    return "".join(result).rstrip() + "…"


def render_caption(item: dict, destination: dict, with_signature: bool | None = None) -> str:
    """One plain-text photo caption, prioritising headline and explanation.

    Telegram's caption budget counts UTF-16 units. Never cut a source URL;
    omit it if there is not enough room, instead of sacrificing the summary.
    """
    limit = platform_limit((destination.get("platform") or "telegram").lower(), "caption")
    title = _truncate_units(str(item.get("title_fa") or item.get("title") or "").strip(), min(240, limit))
    summary = (item.get("summary_fa") or item.get("summary") or "").strip() if item.get("_include_summary", True) else ""
    if not summary and item.get("_summary_unavailable"):
        summary = "متن تکمیلی این خبر از منبع در دسترس نبود."
    parts = [title] if title else []
    if summary:
        room = max(0, limit - _utf16_len("\n\n".join(parts)) - 2)
        if room:
            parts.append(_truncate_units(summary, min(420, room)))
    link = item.get("link") or ""
    extras = [item.get("topic") or "", f"🔗 {item.get('feed_title') or 'source'}: {link}" if link else ""]
    if with_signature is None:
        with_signature = _signature_enabled()
    if with_signature:
        extras.extend([(destination.get("footer") or "").strip(), (db.get_setting("global_footer") or "").strip()])
    for extra in extras:
        if extra and _utf16_len("\n\n".join(parts + [extra])) <= limit:
            parts.append(extra)
    return "\n\n".join(parts)


def rules_payload() -> list[dict]:
    """Hand the limits to the frontend so users see them before typing."""
    out = []
    for pid, rule in PLATFORM_RULES.items():
        meta = PLATFORM_META.get(pid, {})
        out.append(
            {
                "id": pid,
                "text": rule["text"],
                "caption": rule["caption"],
                "html": rule["html"],
                "photo": rule["photo"],
                "min_interval": rule["min_interval"],
                "doc": rule["doc"],
                "notes_fa": rule["notes_fa"],
                "notes_en": rule["notes_en"],
                "name_fa": meta.get("name_fa", pid),
                "name_en": meta.get("name_en", pid),
            }
        )
    return out
