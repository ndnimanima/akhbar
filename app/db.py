"""Rozaneh storage layer — SQLite in WAL mode, one connection, zero ORM."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS topics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    name_en    TEXT DEFAULT '',
    emoji      TEXT DEFAULT '',
    keywords   TEXT DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feeds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    language     TEXT DEFAULT 'auto',
    icon         TEXT DEFAULT '📰',
    color        TEXT DEFAULT 'teal',
    show_url     INTEGER NOT NULL DEFAULT 1,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    last_checked TEXT DEFAULT '',
    last_status  TEXT DEFAULT '',
    last_count   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS destinations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    platform     TEXT NOT NULL DEFAULT 'telegram',
    chat_id      TEXT NOT NULL,
    bot_token    TEXT DEFAULT '',
    topic_filter TEXT DEFAULT '',
    footer       TEXT DEFAULT '',
    parse_mode   TEXT NOT NULL DEFAULT 'HTML',
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    last_test_at TEXT DEFAULT '',
    last_test_ok INTEGER DEFAULT NULL,
    last_test_msg TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    schedule_times  TEXT DEFAULT '[]',
    interval_minutes INTEGER NOT NULL DEFAULT 0,
    window_hours    INTEGER NOT NULL DEFAULT 24,
    max_items       INTEGER NOT NULL DEFAULT 8,
    translate       INTEGER NOT NULL DEFAULT 1,
    include_summary INTEGER NOT NULL DEFAULT 1,
    send_image      INTEGER NOT NULL DEFAULT 1,
    only_unseen     INTEGER NOT NULL DEFAULT 1,
    feed_ids        TEXT DEFAULT '[]',
    topic_ids       TEXT DEFAULT '[]',
    destination_ids TEXT DEFAULT '[]',
    last_run        TEXT DEFAULT '',
    last_result     TEXT DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hash          TEXT NOT NULL UNIQUE,
    job_id        INTEGER DEFAULT NULL,
    feed_id       INTEGER DEFAULT NULL,
    feed_title    TEXT DEFAULT '',
    title         TEXT NOT NULL,
    link          TEXT DEFAULT '',
    summary       TEXT DEFAULT '',
    image         TEXT DEFAULT '',
    published     TEXT DEFAULT '',
    lang          TEXT DEFAULT '',
    topic         TEXT DEFAULT '',
    topic_key     TEXT DEFAULT '',
    title_fa      TEXT DEFAULT '',
    summary_fa    TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'new',
    error         TEXT DEFAULT '',
    destinations  TEXT DEFAULT '[]',
    created_at    TEXT NOT NULL,
    posted_at     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER DEFAULT NULL,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    code       TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS translations (
    hash       TEXT PRIMARY KEY,
    provider   TEXT NOT NULL,
    target     TEXT NOT NULL,
    source_txt TEXT DEFAULT '',
    result_txt TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS video_analyses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL DEFAULT '',
    title       TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'running',
    mode        TEXT NOT NULL DEFAULT 'manual',
    stage       TEXT DEFAULT 'queued',
    error       TEXT DEFAULT '',
    summary     TEXT DEFAULT '',
    report      TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL,
    finished_at TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_status  ON items(status);
CREATE INDEX IF NOT EXISTS idx_logs_created  ON logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_video_created ON video_analyses(created_at DESC);
"""

DEFAULT_TOPICS: list[tuple[str, str, str, str]] = [
    ("سیاست و حکومت", "Politics & Government", "🏛", "سیاست,دولت,مجلس,وزیر,رئیس‌جمهور,انتخابات,تحریم,دیپلماسی,قوه قضائیه,شورای امنیت,politics,government,parliament,election,sanction,minister,president,diplomacy,cabinet"),
    ("اقتصاد و بازار", "Economy & Markets", "📈", "اقتصاد,تورم,دلار,یورو,بورس,طلا,سکه,نفت,بانک,بودجه,مالیات,صادرات,واردات,قیمت,بازار,economy,inflation,currency,dollar,euro,oil,bank,budget,market,trade,tariff,gold"),
    ("بین‌الملل و جهان", "World & International", "🌍", "بین‌الملل,سازمان ملل,آمریکا,اروپا,روسیه,چین,ترکیه,عربستان,اسرائیل,غزه,لبنان,اوکراین,آتش‌بس,مذاکرات هسته‌ای,international,united nations,white house,europe,european,nuclear,diplomat,russia,china,turkey,israel,gaza,lebanon,ukraine,ceasefire,world news"),
    ("فناوری و هوش مصنوعی", "Tech & AI", "💻", "فناوری,تکنولوژی,هوش مصنوعی,گوشی,اپل,گوگل,مایکروسافت,دیپ‌سیک,متا,اینترنت,فضای مجازی,استارتاپ,هک,امنیت سایبری,ربات,technology,tech,artificial intelligence, ai ,chatgpt,openai,deepseek,apple,google,microsoft,meta,internet,cyber,startup,smartphone,robot"),
    ("ورزش", "Sports", "⚽", "ورزش,فوتبال,لیگ,تیم ملی,المپیک,کشتی,والیبال,بسکتبال,فینال,مربی,ورزشکار,دربی,tennis,golf,nba,fifa,sport,sports,football,soccer,olympic,wrestling,volleyball,basketball,coach,player,premier league,champions league,world cup"),
    ("سلامت و پزشکی", "Health & Medicine", "🩺", "سلامت,پزشکی,کرونا,واکسن,بیمار,بیمارستان,دارو,وزارت بهداشت,درمان,آنفلوآنزا,دیابت,سرطان,health,medicine,medical,vaccine,hospital,drug,disease,covid,flu,diabetes,cancer,who"),
    ("علم و دانش", "Science", "🔬", "علم,دانش,پژوهش,ناسا,فضا,ماهواره,کشف,دانشگاه,مطالعه,فیزیک,زیست‌شناسی,science,research,nasa,space,satellite,study,discovery,physics,biology,university"),
    ("هنر و سینما", "Arts & Cinema", "🎬", "سینما,فیلم,جشنواره,بازیگر,موسیقی,سریال,کارگردان,تئاتر,کنسرت,هنرمند,کتاب,شعر,cinema,movie,film,festival,actor,actress,music,series,director,theater,concert,artist,book"),
    ("حوادث و جامعه", "Society & Incidents", "🚨", "حادثه,تصادف,زلزله,سیل,آتش‌سوزی,جرم,دستگیری,پلیس,اعتراض,اعتصاب,قتل,سرقت,مهاجرت,accident,crash,earthquake,flood,fire,police,protest,strike,crime,murder,arrest,migrant"),
    ("محیط زیست و اقلیم", "Climate & Environment", "🌱", "محیط زیست,آب و هوا,آلودگی,خشکسالی,تغییر اقلیم,گرمایش,انرژی تجدیدپذیر,حیات وحش,دریاچه,climate,environment,pollution,drought,emissions,wildlife,renewable,solar,weather"),
]

DEFAULT_SETTINGS = {
    "panel_title": config.APP_NAME,
    "timezone": config.DEFAULT_TZ,
    # appearance
    "theme": "slate",
    "language": "fa",
    # translation
    "translate_provider": "deepseek",
    "translate_fallback": "google_free",
    "translate_target": "fa",
    "translate_post_edit": "1",
    "translate_preserve": "1",
    "translate_glossary": "دیپ‌سیک · اوپک · برنت · هوش مصنوعی مولد",
    "deepseek_base_url": "https://api.deepseek.com/v1",
    "deepseek_model": "deepseek-chat",
    "deepseek_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "openai_model": "gpt-4o-mini",
    "openai_api_key": "",
    # publishing
    "global_footer": "",
    "max_message_chars": "3800",
    "signature_enabled": "1",
    "feedback_webhook": "",
    # video analyzer: smart (auto) mode is OFF by default; manual always works.
    # There is intentionally no timeout setting — analysis runs as long as needed.
    "video_smart": "0",
    "video_interval_minutes": "30",
    "video_summarize": "1",
    "video_frames": "1",
    "video_smart_last_run": "",
    # security
    "password_hash": "",
    "password_salt": "",
    # creators
    "creator1_name": "",
    "creator1_role_fa": "طراح محصول و توسعهٔ پنل",
    "creator1_role_en": "Product design & panel development",
    "creator1_bio_fa": "",
    "creator1_bio_en": "",
    "creator1_telegram": "",
    "creator1_github": "",
    "creator1_email": "",
    "creator1_website": "",
    "creator2_name": "",
    "creator2_role_fa": "همکار — توسعه و پشتیبانی",
    "creator2_role_en": "Co-founder — development & support",
    "creator2_bio_fa": "",
    "creator2_bio_en": "",
    "creator2_telegram": "",
    "creator2_github": "",
    "creator2_email": "",
    "creator2_website": "",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_conn() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            _CONN = sqlite3.connect(str(config.DB_PATH), check_same_thread=False, timeout=30)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.execute("PRAGMA synchronous=NORMAL")
            _CONN.executescript(SCHEMA)
            _migrate()
            _seed()
        return _CONN


def _migrate() -> None:
    """Add columns introduced after the first release (idempotent)."""
    assert _CONN is not None
    wanted = {
        "feeds": {"icon": "TEXT DEFAULT '📰'", "color": "TEXT DEFAULT 'teal'", "show_url": "INTEGER DEFAULT 1", "sort_order": "INTEGER DEFAULT 0"},
        "topics": {"name_en": "TEXT DEFAULT ''"},
        "jobs": {"send_image": "INTEGER DEFAULT 1"},
        "items": {"topic_key": "TEXT DEFAULT ''"},
        "logs": {"code": "TEXT DEFAULT ''"},
    }
    for table, columns in wanted.items():
        existing = {row["name"] for row in _CONN.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                _CONN.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    _CONN.commit()


def _seed() -> None:
    assert _CONN is not None
    for key, value in DEFAULT_SETTINGS.items():
        _CONN.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
    count = _CONN.execute("SELECT COUNT(*) AS c FROM topics").fetchone()["c"]
    if not count:
        stamp = now_iso()
        _CONN.executemany(
            "INSERT INTO topics(name, name_en, emoji, keywords, enabled, created_at) VALUES(?,?,?,?,1,?)",
            [(n, en, e, k, stamp) for n, en, e, k in DEFAULT_TOPICS],
        )
    _CONN.commit()


# ── helpers ─────────────────────────────────────────────────────────────────


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _LOCK:
        return [dict(r) for r in get_conn().execute(sql, tuple(params)).fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with _LOCK:
        conn = get_conn()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return int(cur.lastrowid or 0)


def executemany(sql: str, rows: list[tuple]) -> None:
    with _LOCK:
        conn = get_conn()
        conn.executemany(sql, rows)
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def all_settings() -> dict[str, str]:
    return {r["key"]: r["value"] for r in query("SELECT key, value FROM settings")}


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def load_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def log(job_id: int | None, message: str, level: str = "info", code: str = "") -> None:
    execute(
        "INSERT INTO logs(job_id, level, message, code, created_at) VALUES(?,?,?,?,?)",
        (job_id, level, message[:2000], code[:60], now_iso()),
    )
    execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 3000)")


def translate_cache_get(key: str) -> str | None:
    row = query_one("SELECT result_txt FROM translations WHERE hash = ?", (key,))
    return row["result_txt"] if row else None


def translate_cache_put(key: str, provider: str, target: str, source: str, result: str) -> None:
    execute(
        "INSERT OR REPLACE INTO translations(hash, provider, target, source_txt, result_txt, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (key, provider, target, source[:2000], result, now_iso()),
    )
