"""News source reader: RSS / Atom first, plain-HTML fallback second."""

from __future__ import annotations

import calendar
import hashlib
import html as html_lib
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

import feedparser
import httpx

from . import config

TAG_RE = re.compile(r"<[^>]+>")
BROWSER_HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fa,en;q=0.8",
}
SPACE_RE = re.compile(r"\s+")


class FeedError(RuntimeError):
    pass


def clean_text(raw: str | None, limit: int = 900) -> str:
    if not raw:
        return ""
    text = html_lib.unescape(TAG_RE.sub(" ", raw))
    text = SPACE_RE.sub(" ", text).strip()
    return text[:limit]


def _clean_title(title: str) -> str:
    title = html_lib.unescape(TAG_RE.sub(" ", title or ""))
    return SPACE_RE.sub(" ", title).strip()


def _to_iso(entry: Any) -> str:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        value = getattr(entry, attr, None) or (entry.get(attr) if isinstance(entry, dict) else None)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc).replace(
                    microsecond=0
                ).isoformat()
            except (TypeError, ValueError, OverflowError):
                continue
    return ""


def _entry_image(entry: Any) -> str:
    candidates: list[str] = []
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            url = (media or {}).get("url")
            if url:
                candidates.append(url)
    for enc in entry.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            candidates.append(enc["href"])
    for block in ("summary", "description"):
        text = entry.get(block) or ""
        match = re.search(r'<img[^>]+src="([^"]+)"', text)
        if match:
            candidates.append(match.group(1))
    for content in entry.get("content") or []:
        match = re.search(r'<img[^>]+src="([^"]+)"', (content or {}).get("value") or "")
        if match:
            candidates.append(match.group(1))
    for link in entry.get("links") or []:
        href = (link or {}).get("href") or ""
        if str(link.get("type", "")).startswith("image") or re.search(
            r"\.(jpe?g|png|webp|gif)(\?|$)", href, re.I
        ):
            candidates.append(href)
    for candidate in candidates:
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if candidate.startswith(("http://", "https://")):
            return _upgrade_image(candidate)
    return ""


def _upgrade_image(url: str) -> str:
    """Ask image CDNs for a larger variant (240px thumbs are useless on a channel)."""
    return re.sub(r"(ichef\.bbci\.co\.uk/.*?/(?:ws|standard|news))/(\d{2,4})/", r"\1/800/", url)


META_TAG_RE = re.compile(r"<meta\s[^>]*>", re.I)
ATTR_RE = re.compile(r'([a-zA-Z:_-]+)\s*=\s*"([^"]*)"')
OG_KEYS = ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src")


def _meta_images(text: str) -> str:
    """Pull the social preview image out of an HTML document (attribute order agnostic)."""
    for tag in META_TAG_RE.findall(text):
        attrs = {k.lower(): v for k, v in ATTR_RE.findall(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in OG_KEYS and attrs.get("content"):
            return html_lib.unescape(attrs["content"].strip())
    return ""


async def og_image(link: str) -> str:
    """Fallback: read the article page and take its social preview image."""
    if not link.startswith(("http://", "https://")):
        return ""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(7.0), follow_redirects=True, headers=BROWSER_HEADERS
        ) as client:
            async with client.stream("GET", link) as response:
                if response.status_code >= 400:
                    return ""
                parts: list[str] = []
                remaining = 500_000
                async for chunk in response.aiter_text(chunk_size=16384):
                    parts.append(chunk[:remaining])
                    remaining -= min(len(chunk), remaining)
                    if not remaining:
                        break
                text = "".join(parts)
    except Exception:  # noqa: BLE001 — enrichment must never break a run
        return ""
    url = _meta_images(text)
    if url.startswith("//"):
        url = "https:" + url
    return url if url.startswith(("http://", "https://")) else ""


def parse_feed(content: bytes) -> list[dict]:
    parsed = feedparser.parse(content)
    items: list[dict] = []
    for entry in parsed.entries or []:
        title = _clean_title(entry.get("title", ""))
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        summary = clean_text(entry.get("summary") or entry.get("description") or "")
        items.append(
            {
                "title": title[:400],
                "link": link,
                "summary": summary,
                "image": _entry_image(entry),
                "published": _to_iso(entry),
            }
        )
    return items


class _LinkCollector(HTMLParser):
    """Very small heuristic extractor used when a URL is a plain HTML page."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict] = []
        self._href: str | None = None
        self._buffer: list[str] = []
        self._meta: dict[str, str] = {}
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a":
            href = attrs_d.get("href", "")
            if href.startswith("http") or href.startswith("/"):
                self._href = href
                self._buffer = []
        elif tag == "meta":
            prop = (attrs_d.get("property") or attrs_d.get("name") or "").lower()
            if prop in {"og:published_time", "article:published_time", "date", "pubdate"}:
                self._meta.setdefault("published", attrs_d.get("content", ""))
            if prop in {"og:image", "twitter:image"}:
                self._meta.setdefault("image", attrs_d.get("content", ""))
        elif tag == "title":
            self._in_title = True
        elif tag == "time":
            value = attrs_d.get("datetime", "")
            if value:
                self._meta.setdefault("published", value)

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buffer.append(data)
        if self._in_title:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            text = SPACE_RE.sub(" ", " ".join(self._buffer)).strip()
            if len(text) >= 25:
                self.links.append({"title": text, "link": self._href})
            self._href = None
            self._buffer = []
        elif tag == "title":
            self._in_title = False

    @property
    def page_title(self) -> str:
        return SPACE_RE.sub(" ", " ".join(self._title_parts)).strip()


def parse_html(content: bytes, base_url: str) -> list[dict]:
    from urllib.parse import urljoin

    parser = _LinkCollector()
    try:
        parser.feed(content.decode("utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001 - html.parser never raises for us, be safe
        return []
    published = parser._meta.get("published", "")
    iso = ""
    if published:
        try:
            iso = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(
                timezone.utc
            ).replace(microsecond=0).isoformat()
        except ValueError:
            iso = ""
    seen: set[str] = set()
    items: list[dict] = []
    for link in parser.links:
        url = urljoin(base_url, link["link"])
        if url in seen:
            continue
        seen.add(url)
        items.append(
            {
                "title": link["title"][:400],
                "link": url,
                "summary": "",
                "image": parser._meta.get("image", ""),
                "published": iso,
            }
        )
        if len(items) >= 40:
            break
    return items


async def fetch_source(feed: dict) -> list[dict]:
    """Download a source and return normalized items (newest first when dated)."""
    url = (feed.get("url") or "").strip()
    if not url:
        raise FeedError("لینک منبع خالی است")
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.8, */*;q=0.5",
        "Accept-Language": "fa,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(
            timeout=config.HTTP_TIMEOUT, follow_redirects=True, headers=headers
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise FeedError(f"خطا در دریافت منبع: {exc.__class__.__name__}") from exc

    if response.status_code >= 400:
        raise FeedError(f"منبع کد {response.status_code} برگرداند")

    content = response.content
    if not content:
        raise FeedError("پاسخ منبع خالی بود")

    items = parse_feed(content)
    if not items:
        items = parse_html(content, str(response.url))
    if not items:
        raise FeedError("هیچ خبری در این منبع پیدا نشد (RSS یا HTML قابل خواندن نبود)")

    items.sort(key=published_sort_key, reverse=True)
    return items


def item_hash(feed_url: str, item: dict) -> str:
    seed = f"{feed_url}|{item.get('link') or ''}|{item.get('title') or ''}"
    return hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:32]


def within_window(item: dict, hours: int, now: datetime | None = None) -> bool:
    if hours <= 0:
        return True
    now = now or datetime.now(timezone.utc)
    published = item.get("published") or ""
    if not published:
        return True  # undated sources are still eligible; dedupe protects us
    try:
        stamp = datetime.fromisoformat(published)
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp >= now - timedelta(hours=hours)


def published_sort_key(item: dict) -> datetime:
    try:
        stamp = datetime.fromisoformat(item.get("published") or "")
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)
