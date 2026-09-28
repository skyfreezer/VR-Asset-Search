"""
db.py — SQLite storage for click history

Unlike crawl results (in-memory, see app.py), history survives restarts.
Each history row is a snapshot of the asset at click time, so it can be shown
even after the crawl that produced it is gone.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_FILE = os.path.join(os.path.dirname(__file__), "vr_assets.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    url           TEXT PRIMARY KEY,
    title         TEXT,
    creator       TEXT,
    price         TEXT,
    price_value   REAL,              -- numeric sort key for price (see app._price_sort_key)
    source        TEXT,
    asset_type    TEXT,
    image_url     TEXT,
    tags          TEXT,              -- JSON-encoded list
    first_clicked TEXT NOT NULL,     -- ISO-8601 UTC timestamps
    last_clicked  TEXT NOT NULL,
    click_count   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_history_last ON history(last_clicked DESC);

-- One row per individual click, for a timeline of activity
CREATE TABLE IF NOT EXISTS clicks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL REFERENCES history(url) ON DELETE CASCADE,
    clicked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clicks_url ON clicks(url);
"""

_SORTS = {
    "recent":       "last_clicked DESC",
    "most_clicked": "click_count DESC, last_clicked DESC",
    "title":        "title COLLATE NOCASE ASC",
    "price_asc":    "price_value ASC, title COLLATE NOCASE ASC",
    "price_desc":   "price_value DESC, title COLLATE NOCASE ASC",
}


def get_conn() -> sqlite3.Connection:
    """Open a new connection. Use one per request: Waitress serves requests on
    several threads, and sqlite3 connections shouldn't be shared across them."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def _db():
    """Connection that commits on success, rolls back on error, and always closes.
    (sqlite3's own `with conn:` commits but leaves the connection open.)"""
    conn = get_conn()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if missing. WAL mode lets reads proceed during a write."""
    with _db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("price_value", None)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except ValueError:
        d["tags"] = []
    return d


def record_click(asset: dict, price_value: float) -> None:
    """Insert the asset, or bump click_count/last_clicked if already present.
    The snapshot fields are refreshed so price/title stay current."""
    now = datetime.now(timezone.utc).isoformat()
    params = {
        "url":         asset["url"],
        "title":       asset.get("title"),
        "creator":     asset.get("creator"),
        "price":       asset.get("price"),
        "price_value": price_value,
        "source":      asset.get("source"),
        "asset_type":  asset.get("asset_type"),
        "image_url":   asset.get("image_url"),
        "tags":        json.dumps(asset.get("tags") or []),
        "now":         now,
    }
    with _db() as conn:
        conn.execute("""
            INSERT INTO history (url, title, creator, price, price_value, source,
                                 asset_type, image_url, tags, first_clicked, last_clicked)
            VALUES (:url, :title, :creator, :price, :price_value, :source,
                    :asset_type, :image_url, :tags, :now, :now)
            ON CONFLICT(url) DO UPDATE SET
                title        = excluded.title,
                creator      = excluded.creator,
                price        = excluded.price,
                price_value  = excluded.price_value,
                source       = excluded.source,
                asset_type   = excluded.asset_type,
                image_url    = excluded.image_url,
                tags         = excluded.tags,
                last_clicked = excluded.last_clicked,
                click_count  = click_count + 1
        """, params)
        conn.execute("INSERT INTO clicks (url, clicked_at) VALUES (?, ?)",
                     (asset["url"], now))


def get_history(q: str = "", source: str = "all", asset_type: str = "all",
                sort: str = "recent", page: int = 1, limit: int = 24) -> tuple[list, int]:
    """Filtered, sorted, paginated history. Returns (rows, total_matching)."""
    where, args = [], []
    if source != "all":
        where.append("source = ?")
        args.append(source)
    if asset_type != "all":
        where.append("asset_type = ?")
        args.append(asset_type)
    if q:
        # Tags are stored as JSON text, so a substring match on it covers tag search
        where.append("(title LIKE ? OR creator LIKE ? OR tags LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like]
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = _SORTS.get(sort, _SORTS["recent"])

    with _db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM history {where_sql}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM history {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
            args + [limit, (page - 1) * limit],
        ).fetchall()
    return [_row_to_dict(r) for r in rows], total


def get_history_urls() -> list:
    with _db() as conn:
        return [r[0] for r in conn.execute("SELECT url FROM history")]


def delete_history(url: str | None = None) -> int:
    """Delete one entry by URL, or everything if url is None. Returns rows removed."""
    with _db() as conn:
        if url:
            cur = conn.execute("DELETE FROM history WHERE url = ?", (url,))
        else:
            conn.execute("DELETE FROM clicks")
            cur = conn.execute("DELETE FROM history")
        return cur.rowcount


def history_stats() -> dict:
    with _db() as conn:
        total  = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        clicks = conn.execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
        by_source = dict(conn.execute(
            "SELECT source, COUNT(*) FROM history GROUP BY source").fetchall())
        by_type = dict(conn.execute(
            "SELECT asset_type, COUNT(*) FROM history GROUP BY asset_type "
            "ORDER BY COUNT(*) DESC").fetchall())
        top = conn.execute(
            "SELECT * FROM history ORDER BY click_count DESC, last_clicked DESC LIMIT 5"
        ).fetchall()
    return {
        "total":        total,
        "total_clicks": clicks,
        "by_source":    by_source,
        "by_type":      by_type,
        "most_clicked": [_row_to_dict(r) for r in top],
    }
