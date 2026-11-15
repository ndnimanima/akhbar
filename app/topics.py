"""Keyword based topic matching.

No machine learning, no external service: each topic owns a keyword list that
the admin can edit in the panel. Persian text is normalised first so that
Arabic Yeh/Kaf variants and half-spaces do not break matching.
"""

from __future__ import annotations

import re

_AR_MAP = str.maketrans({"ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "ؤ": "و", "إ": "ا", "أ": "ا", "ٱ": "ا"})
_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0640]")
_NON_WORD = re.compile(r"[^\w\u0600-\u06FF\s]+", re.UNICODE)
_SPACE = re.compile(r"\s+")
_LATIN_KW = re.compile(r"^[a-z0-9][a-z0-9 .'\-+]*$")


def normalize(text: str) -> str:
    text = (text or "").translate(_AR_MAP)
    text = _DIACRITICS.sub("", text)
    text = text.replace("\u200c", " ").replace("\u200f", " ").replace("\u200e", " ")
    text = _NON_WORD.sub(" ", text)
    text = _SPACE.sub(" ", text)
    return text.strip().lower()


def split_keywords(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[,،\n;]+", raw)
    return [p.strip() for p in parts if p.strip()]


def _keyword_hit(keyword: str, haystack_title: str, haystack_body: str) -> int:
    kw = normalize(keyword)
    if not kw:
        return 0
    score = 0
    if _LATIN_KW.match(kw):
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")
        if pattern.search(haystack_title):
            score += 3
        elif pattern.search(haystack_body):
            score += 1
    else:
        if kw in haystack_title:
            score += 3
        elif kw in haystack_body:
            score += 1
    return score


def score_topic(topic: dict, title: str, summary: str) -> int:
    t_title = normalize(title)
    t_body = normalize(summary)
    total = 0
    for kw in split_keywords(topic.get("keywords")):
        total += _keyword_hit(kw, t_title, t_body)
    return total


def classify(topics: list[dict], title: str, summary: str) -> tuple[dict | None, int, list[str]]:
    """Return (best_topic, score, all_matched_topic_names)."""
    best: dict | None = None
    best_score = 0
    matched: list[str] = []
    for topic in topics:
        if not int(topic.get("enabled", 1)):
            continue
        score = score_topic(topic, title, summary)
        if score > 0:
            matched.append(topic.get("name", ""))
        if score > best_score:
            best, best_score = topic, score
    return best, best_score, matched


def score_topics(
    topics: list[dict], title: str, summary: str
) -> tuple[dict | None, int, set[int]]:
    """Score every enabled topic once and return (best, best_score, matched_ids)."""
    best: dict | None = None
    best_score = 0
    matched: set[int] = set()
    for topic in topics:
        if not int(topic.get("enabled", 1)):
            continue
        score = score_topic(topic, title, summary)
        if score > 0:
            matched.add(int(topic["id"]))
        if score > best_score:
            best, best_score = topic, score
    return best, best_score, matched


def topic_label(topic: dict | None) -> str:
    if not topic:
        return ""
    emoji = (topic.get("emoji") or "").strip()
    name = (topic.get("name") or "").strip()
    return f"{emoji} {name}".strip()


def hashtag(name: str) -> str:
    clean = re.sub(r"[^\w\u0600-\u06FF]+", "_", (name or "").strip())
    clean = re.sub(r"_+", "_", clean).strip("_")
    return f"#{clean}" if clean else ""
