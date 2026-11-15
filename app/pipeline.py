"""The news → channel pipeline.

sources → time window → de-duplication → topic match → translation →
one message (with the news image) per item → archive + logs.

Topic matching runs *before* the per-run cap, so a rule that only wants sports
news never runs out of budget because the newest items happened to be political.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from datetime import datetime, timezone
from urllib.parse import urldefrag

from . import db, feeds, publish, topics as topics_mod, translate

RUN_LOCK = asyncio.Lock()
MAX_FEEDS_PER_RUN = 60
MAX_FILTERED_STORED = 25


def _load_topics() -> list[dict]:
    return db.query("SELECT * FROM topics WHERE enabled = 1 ORDER BY id")


def _load_feeds(job: dict) -> list[dict]:
    ids = [int(x) for x in db.load_list(job.get("feed_ids"))]
    if ids:
        marks = ",".join("?" * len(ids))
        return db.query(
            f"SELECT * FROM feeds WHERE id IN ({marks}) AND enabled = 1 ORDER BY sort_order, id", ids
        )
    return db.query("SELECT * FROM feeds WHERE enabled = 1 ORDER BY sort_order, id")


def _load_destinations(job: dict) -> list[dict]:
    ids = [int(x) for x in db.load_list(job.get("destination_ids"))]
    if ids:
        marks = ",".join("?" * len(ids))
        return db.query(f"SELECT * FROM destinations WHERE id IN ({marks}) AND enabled = 1", ids)
    return db.query("SELECT * FROM destinations WHERE enabled = 1")


def _destination_accepts(destination: dict, item: dict) -> bool:
    raw = (destination.get("topic_filter") or "").strip()
    if not raw:
        return True
    allowed = {p.strip() for p in raw.replace("،", ",").split(",") if p.strip()}
    topic = item.get("topic") or ""
    return not allowed or any(name in topic for name in allowed)


def _seen(hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    found: set[str] = set()
    for i in range(0, len(hashes), 400):
        chunk = hashes[i : i + 400]
        marks = ",".join("?" * len(chunk))
        for row in db.query(f"SELECT hash FROM items WHERE hash IN ({marks})", chunk):
            found.add(row["hash"])
    return found


def _insert_item(item: dict) -> int:
    return db.execute(
        """INSERT OR IGNORE INTO items
           (hash, job_id, feed_id, feed_title, title, link, summary, image, published,
            lang, topic, topic_key, title_fa, summary_fa, status, error, destinations,
            created_at, posted_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item["hash"],
            item.get("job_id"),
            item.get("feed_id"),
            item.get("feed_title", ""),
            item.get("title", ""),
            item.get("link", ""),
            item.get("summary", ""),
            item.get("image", ""),
            item.get("published", ""),
            item.get("lang", ""),
            item.get("topic", ""),
            item.get("topic_key", ""),
            item.get("title_fa", ""),
            item.get("summary_fa", ""),
            item.get("status", "new"),
            item.get("error", ""),
            db.dump(item.get("destinations", [])),
            db.now_iso(),
            item.get("posted_at", ""),
        ),
    )


def _update_item(item_id: int, **fields) -> None:
    if not fields:
        return
    if "destinations" in fields:
        fields["destinations"] = db.dump(fields["destinations"])
    assignments = ", ".join(f"{key} = ?" for key in fields)
    db.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*fields.values(), item_id))


def _image_hash(url: str) -> str:
    return hashlib.sha256(urldefrag(url.strip())[0].encode("utf-8")).hexdigest()


def _visual_fingerprint(data: bytes) -> tuple[str, str]:
    """Compact conservative near-duplicate signature; skip oversized/flat images."""
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > 20_000_000:
                return "", ""
            original.draft("RGB", (128, 128))
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((128, 128))
            color = image.resize((1, 1)).getpixel((0, 0))
            gray = image.convert("L").resize((17, 8))
            pixels = list(gray.get_flattened_data() if hasattr(gray, "get_flattened_data") else gray.getdata())
            if max(pixels) - min(pixels) < 30:
                return "", ""
            bits = 0
            for y in range(8):
                for x in range(16):
                    bits = (bits << 1) | int(pixels[y * 17 + x] > pixels[y * 17 + x + 1])
            return f"{bits:032x}", ",".join(str(v) for v in color)
    except Exception:  # noqa: BLE001 — fingerprint failure must not block publication
        return "", ""


def _image_seen(destination_id: int, image_url: str, data_hash: str,
                visual_hash: str = "", visual_color: str = "") -> bool:
    url_hash = _image_hash(image_url)
    row = db.query_one(
        "SELECT 1 AS found FROM published_images WHERE destination_id = ? AND "
        "(image_url_hash = ? OR (image_data_hash != '' AND image_data_hash = ?)) LIMIT 1",
        (destination_id, url_hash, data_hash),
    )
    if row:
        return True
    if visual_hash and visual_color:
        recent = db.query("SELECT visual_hash, visual_color FROM published_images "
                          "WHERE destination_id=? AND visual_hash != '' ORDER BY created_at DESC LIMIT 100",
                          (destination_id,))
        for prior in recent:
            try:
                distance = (int(visual_hash, 16) ^ int(prior["visual_hash"], 16)).bit_count()
                color = [int(c) for c in visual_color.split(",")]
                old_color = [int(c) for c in prior["visual_color"].split(",")]
                if distance <= 3 and len(color) == len(old_color) == 3 and max(abs(a - b) for a, b in zip(color, old_color)) <= 12:
                    return True
            except (ValueError, TypeError):
                continue
    # Previous releases recorded the URL and successful destinations, but not
    # whether the photo or a text fallback succeeded. Be conservative on upgrade.
    rows = db.query("SELECT destinations FROM items WHERE status='sent' AND image=? ORDER BY id DESC LIMIT 50", (image_url,))
    return any(any(str(d.get("id")) == str(destination_id) and d.get("ok")
                   for d in db.load_list(row.get("destinations")) if isinstance(d, dict)) for row in rows)


def _remember_image(destination_id: int, image_url: str, data_hash: str,
                    visual_hash: str = "", visual_color: str = "") -> None:
    db.execute("INSERT OR REPLACE INTO published_images(destination_id,image_url_hash,image_data_hash,visual_hash,visual_color,created_at) VALUES(?,?,?,?,?,?)",
               (destination_id, _image_hash(image_url), data_hash, visual_hash, visual_color, db.now_iso()))


async def _deliver_item(destination: dict, item: dict, image, image_digest: str,
                        visual_hash: str = "", visual_color: str = "") -> tuple[bool, str, bool, bool]:
    """Send exactly one post when delivery is certain; repeated photos become text."""
    platform = (destination.get("platform") or "").lower()
    can_photo = bool(image and item.get("image") and publish.supports_photo(platform))
    repeated = can_photo and _image_seen(destination["id"], item["image"], image_digest, visual_hash, visual_color)
    if can_photo and not repeated:
        ok, detail = await publish.send_photo(destination, item["image"],
                                              publish.render_caption(item, destination), image=image)
        if ok:
            _remember_image(destination["id"], item["image"], image_digest, visual_hash, visual_color)
            return ok, detail, True, False
        if publish.delivery_uncertain(detail):
            return ok, detail, False, False
    ok, detail = await publish.send_message(destination, publish.render_item(item, destination))
    return ok, detail, False, bool(repeated)


async def run_job(
    job: dict,
    *,
    dry_run: bool = False,
    force: bool = False,
    max_items_override: int | None = None,
) -> dict:
    """Execute one publication job and return a translatable report."""
    job_id = job.get("id")
    if not force and not int(job.get("enabled", 1)):
        return {"ok": False, "error_code": "job_disabled", "events": [{"code": "job_disabled"}]}

    async with RUN_LOCK:
        report: dict = {
            "ok": True,
            "job_id": job_id,
            "job_name": job.get("name", ""),
            "dry_run": dry_run,
            "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "feeds": 0,
            "fetched": 0,
            "in_window": 0,
            "matched": 0,
            "new": 0,
            "translated": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "images": 0,
            "repeated_images": 0,
            "summary_missing": 0,
            "previews": [],
            "events": [],
            "lines": [],
        }

        def note(code: str, level: str = "info", **params) -> None:
            report["events"].append({"code": code, "level": level, "params": params})
            text = {
                "run_start": f"شروع اجرا — {params.get('feeds')} منبع، {params.get('destinations')} کانال، بازه {params.get('window')} ساعت",
                "run_dry": "حالت آزمایشی (بدون ارسال)",
                "feed_error": f"منبع «{params.get('feed')}» خوانده نشد: {params.get('error')}",
                "cap_note": f"{params.get('eligible')} خبر واجد شرایط بود؛ {params.get('taken')} خبر اول انتخاب شد",
                "summary_missing": f"متن تکمیلی «{params.get('title')}» از منبع در دسترس نبود",
                "repeated_image": f"تصویر تکراری «{params.get('title')}» برای کانال «{params.get('destination')}» حذف شد؛ خبر متنی ارسال می‌شود",
                "no_fresh": "خبر تازه‌ای متناسب با موضوعات انتخابی پیدا نشد",
                "no_feeds": "منبع خبری فعالی انتخاب نشده است",
                "no_destinations": "کانال مقصد فعالی انتخاب نشده است",
                "send_failed": f"ارسال «{params.get('title')}» ناموفق بود: {params.get('detail')}",
                "run_done": (
                    f"پایان اجرا — تازه {params.get('new')}، ارسال {params.get('sent')}، "
                    f"ناموفق {params.get('failed')}، ردشده {params.get('skipped')}"
                ),
            }.get(code, code)
            report["lines"].append(text)
            if not dry_run:
                db.log(job_id, text, level=level, code=code)

        feed_list = _load_feeds(job)[:MAX_FEEDS_PER_RUN]
        destinations = _load_destinations(job)
        if not feed_list:
            report["ok"] = False
            report["error_code"] = "no_feeds"
            note("no_feeds", level="error")
            return report
        if not destinations:
            report["ok"] = False
            report["error_code"] = "no_destinations"
            note("no_destinations", level="error")
            return report

        topic_rows = _load_topics()
        selected_topics = {int(x) for x in db.load_list(job.get("topic_ids"))}
        window = int(job.get("window_hours") or 24)
        max_items = (
            max_items_override
            if max_items_override is not None
            else int(job.get("max_items") or 8)
        )
        translate_enabled = bool(int(job.get("translate", 1)))
        include_summary = bool(int(job.get("include_summary", 1)))
        send_image = bool(int(job.get("send_image", 1)))
        only_unseen = bool(int(job.get("only_unseen", 1)))

        note(
            "run_start",
            feeds=len(feed_list),
            destinations=len(destinations),
            window=window,
            dry=int(dry_run),
        )
        if dry_run:
            note("run_dry")

        # ── 1) gather fresh candidates ───────────────────────────────────────
        candidates: list[dict] = []
        for feed in feed_list:
            report["feeds"] += 1
            try:
                raw_items = await feeds.fetch_source(feed)
            except feeds.FeedError as exc:
                report["errors"] = report.get("errors", []) + [f"{feed.get('title')}: {exc}"]
                note("feed_error", level="warn", feed=feed.get("title", ""), error=str(exc))
                if not dry_run:
                    db.execute(
                        "UPDATE feeds SET last_checked = ?, last_status = ?, last_count = 0 WHERE id = ?",
                        (db.now_iso(), f"error: {exc}"[:200], feed["id"]),
                    )
                continue

            report["fetched"] += len(raw_items)
            in_window = [it for it in raw_items if feeds.within_window(it, window)]
            report["in_window"] += len(in_window)
            if not dry_run:
                db.execute(
                    "UPDATE feeds SET last_checked = ?, last_status = ?, last_count = ? WHERE id = ?",
                    (db.now_iso(), "ok", len(in_window), feed["id"]),
                )

            hashes = [feeds.item_hash(feed["url"], it) for it in in_window]
            seen = _seen(hashes) if only_unseen else set()
            for item, digest in zip(in_window, hashes):
                if digest in seen:
                    report["skipped"] += 1
                    continue
                item = dict(item)
                item["hash"] = digest
                item["feed_id"] = feed["id"]
                item["feed_title"] = feed.get("title", "")
                candidates.append(item)

        # ── 2) topic filter before the per-run cap ───────────────────────────
        eligible: list[dict] = []
        filtered_out: list[dict] = []
        for item in candidates:
            best_topic, _score, matched_ids = topics_mod.score_topics(
                topic_rows, item.get("title", ""), item.get("summary", "")
            )
            item["_topic"] = best_topic
            if selected_topics and not (matched_ids & selected_topics):
                report["skipped"] += 1
                # Only the first MAX_FILTERED_STORED entries are persisted;
                # retain none for dry runs, which never persist filtered items.
                if not dry_run and len(filtered_out) < MAX_FILTERED_STORED:
                    filtered_out.append(item)
                continue
            eligible.append(item)

        report["matched"] = len(eligible)
        eligible.sort(key=feeds.published_sort_key, reverse=True)
        if max_items > 0 and len(eligible) > max_items:
            note("cap_note", eligible=len(eligible), taken=max_items)
            eligible = eligible[:max_items]

        if not dry_run and filtered_out:
            for item in filtered_out[:MAX_FILTERED_STORED]:
                topic = item.get("_topic")
                _insert_item(
                    {
                        **item,
                        "job_id": job_id,
                        "status": "filtered",
                        "lang": translate.detect_language(item.get("title", "")),
                        "topic": topics_mod.topic_label(topic),
                        "topic_key": str((topic or {}).get("id", "")),
                    }
                )

        if not eligible:
            note("no_fresh")
            report["finished_at"] = db.now_iso()
            if not dry_run:
                _finish_job(job, report)
            return report

        # ── 3) translate + publish ───────────────────────────────────────────
        for item in eligible:
            item["_include_summary"] = include_summary
            needs_summary = include_summary and not feeds.summary_is_useful(item.get("summary", ""), item.get("title", ""))
            needs_image = send_image and not item.get("image")
            if (needs_summary or needs_image) and item.get("link"):
                details = await feeds.article_details(item["link"])
                if needs_summary and feeds.summary_is_useful(details.get("summary", ""), item.get("title", ""), min_length=30):
                    old_summary = item.get("summary") or ""
                    if len(details["summary"]) > len(old_summary) or old_summary.casefold().strip(" .!؟") == (item.get("title") or "").casefold().strip(" .!؟"):
                        item["summary"] = details["summary"]
                if needs_image and details.get("image"):
                    item["image"] = details["image"]
            if include_summary and feeds.clean_text(item.get("summary", ""), 900).casefold().strip(" .!؟") == feeds.clean_text(item.get("title", ""), 900).casefold().strip(" .!؟"):
                item["summary"] = ""
            if include_summary and not (item.get("summary") or "").strip():
                item["_summary_unavailable"] = True
                report["summary_missing"] += 1
                note("summary_missing", level="warn", title=(item.get("title") or "")[:60])
            best_topic = item.pop("_topic", None)
            label = topics_mod.topic_label(best_topic)
            item["topic"] = (
                f"{label} {topics_mod.hashtag(best_topic.get('name', ''))}".strip() if best_topic else ""
            )
            item["topic_key"] = str((best_topic or {}).get("id", ""))
            item["lang"] = translate.detect_language(
                f"{item.get('title', '')} {item.get('summary', '')}"
            )

            title_fa = item.get("title", "")
            summary_fa = item.get("summary", "") if include_summary else ""
            if translate_enabled:
                title_fa, did = await translate.maybe_translate(item.get("title", ""), True)
                if did:
                    report["translated"] += 1
                if summary_fa:
                    summary_fa, _ = await translate.maybe_translate(summary_fa, True)
            item["title_fa"] = title_fa if title_fa != item.get("title") else ""
            item["summary_fa"] = summary_fa if summary_fa != item.get("summary") else ""

            if dry_run:
                report["previews"].append(
                    {
                        "title": item.get("title"),
                        "title_fa": item.get("title_fa") or "",
                        "topic": item.get("topic", ""),
                        "topic_id": item.get("topic_key", ""),
                        "link": item.get("link", ""),
                        "image": item.get("image", ""),
                        "lang": item.get("lang", ""),
                        "message": publish.render_item(item, destinations[0]),
                    }
                )
                continue

            item["job_id"] = job_id
            item["status"] = "new"
            item_id = _insert_item(item)
            if not item_id:
                report["skipped"] += 1
                continue
            report["new"] += 1

            delivered: list[dict] = []
            failures: list[str] = []
            image = None
            image_checked = False
            image_digest = ""
            visual_hash, visual_color = "", ""
            for destination in destinations:
                if not _destination_accepts(destination, item):
                    continue
                platform = (destination.get("platform") or "").lower()
                use_photo = send_image and bool(item.get("image")) and publish.supports_photo(platform)
                if use_photo and not image_checked:
                    image_checked = True
                    image = await publish.fetch_image(item["image"])
                    if image:
                        image_digest = hashlib.sha256(image[0]).hexdigest()
                        visual_hash, visual_color = await asyncio.to_thread(_visual_fingerprint, image[0])
                # An unavailable or duplicate image becomes one text post.
                ok, detail, photo_sent, repeated = await _deliver_item(
                    destination, item, image if use_photo else None, image_digest, visual_hash, visual_color
                )
                if photo_sent:
                    report["images"] += 1
                if repeated:
                    report["repeated_images"] += 1
                    note("repeated_image", level="info", title=(item.get("title") or "")[:60],
                         destination=destination.get("name", ""))
                delivered.append(
                    {"id": destination["id"], "name": destination.get("name"), "ok": ok, "detail": detail}
                )
                if ok:
                    report["sent"] += 1
                else:
                    report["failed"] += 1
                    failures.append(f"{destination.get('name')}: {detail}")
                await asyncio.sleep(config_send_delay())

            if any(d["ok"] for d in delivered):
                _update_item(item_id, status="sent", posted_at=db.now_iso(), destinations=delivered)
            else:
                _update_item(
                    item_id,
                    status="failed",
                    error=" | ".join(failures)[:500] or "no_destination_accepted",
                    destinations=delivered,
                )
                if failures:
                    note(
                        "send_failed",
                        level="error",
                        title=(item.get("title") or "")[:60],
                        detail=failures[0],
                    )

        note(
            "run_done",
            new=report["new"],
            sent=report["sent"],
            failed=report["failed"],
            skipped=report["skipped"],
        )
        report["finished_at"] = db.now_iso()
        if not dry_run:
            _finish_job(job, report)
        return report


def config_send_delay() -> float:
    from . import config

    return config.SEND_DELAY


def _finish_job(job: dict, report: dict) -> None:
    db.execute(
        "UPDATE jobs SET last_run = ?, last_result = ? WHERE id = ?",
        (
            db.now_iso(),
            db.dump(
                {
                    "new": report["new"],
                    "sent": report["sent"],
                    "failed": report["failed"],
                    "at": report["finished_at"],
                }
            ),
            job["id"],
        ),
    )


async def resend_item(item_id: int, destination_ids: list[int] | None = None) -> dict:
    async with RUN_LOCK:
        return await _resend_item(item_id, destination_ids)


async def _resend_item(item_id: int, destination_ids: list[int] | None = None) -> dict:
    item = db.query_one("SELECT * FROM items WHERE id = ?", (item_id,))
    if not item:
        return {"ok": False, "error_code": "item_not_found"}
    if destination_ids:
        marks = ",".join("?" * len(destination_ids))
        destinations = db.query(f"SELECT * FROM destinations WHERE id IN ({marks})", destination_ids)
    else:
        destinations = db.query("SELECT * FROM destinations WHERE enabled = 1")
    if not destinations:
        return {"ok": False, "error_code": "no_destinations"}
    results = []
    image = await publish.fetch_image(item["image"]) if item.get("image") else None
    image_digest = hashlib.sha256(image[0]).hexdigest() if image else ""
    visual_hash, visual_color = await asyncio.to_thread(_visual_fingerprint, image[0]) if image else ("", "")
    for destination in destinations:
        ok, detail, _, _ = await _deliver_item(destination, item, image, image_digest, visual_hash, visual_color)
        results.append(
            {"id": destination["id"], "name": destination.get("name"), "ok": ok, "detail": detail}
        )
        await asyncio.sleep(config_send_delay())
    if any(r["ok"] for r in results):
        _update_item(item_id, status="sent", posted_at=db.now_iso(), destinations=results)
    return {"ok": any(r["ok"] for r in results), "results": results}


async def preview_item(item_id: int, destination_id: int | None = None) -> dict:
    item = db.query_one("SELECT * FROM items WHERE id = ?", (item_id,))
    if not item:
        return {"ok": False, "error_code": "item_not_found"}
    if destination_id:
        destination = db.query_one("SELECT * FROM destinations WHERE id = ?", (destination_id,))
    else:
        destination = db.query_one("SELECT * FROM destinations WHERE enabled = 1 ORDER BY id LIMIT 1")
    destination = destination or {"platform": "telegram", "parse_mode": "HTML", "footer": "", "name": "preview"}
    return {
        "ok": True,
        "message": publish.render_item(item, destination),
        "caption": publish.render_caption(item, destination),
        "has_image": bool(item.get("image")),
        "image": item.get("image") or "",
    }
