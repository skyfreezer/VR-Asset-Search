"""
db.py — SQLite storage for click history, favorites, and named lists

Unlike crawl results (in-memory, see app.py), everything here survives restarts.
Each history, favorite, and list row is its own snapshot of the asset, so it can
be shown even after the crawl that produced it is gone, and clearing history
doesn't touch favorites or lists.
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

CREATE TABLE IF NOT EXISTS favorites (
    url         TEXT PRIMARY KEY,
    title       TEXT,
    creator     TEXT,
    price       TEXT,
    price_value REAL,
    source      TEXT,
    asset_type  TEXT,
    image_url   TEXT,
    tags        TEXT,
    added_at    TEXT NOT NULL
);

-- Named lists. Items are keyed by list id, so renaming a list moves nothing.
CREATE TABLE IF NOT EXISTS lists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- "Shaders" and "shaders" clash
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS list_items (
    list_id     INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    title       TEXT,
    creator     TEXT,
    price       TEXT,
    price_value REAL,
    source      TEXT,
    asset_type  TEXT,
    image_url   TEXT,
    tags        TEXT,
    added_at    TEXT NOT NULL,
    PRIMARY KEY (list_id, url)
);
CREATE INDEX IF NOT EXISTS idx_list_items_url ON list_items(url);
"""

_SORTS = {
    "recent":       "last_clicked DESC",
    "most_clicked": "click_count DESC, last_clicked DESC",
    "title":        "title COLLATE NOCASE ASC",
    "price_asc":    "price_value ASC, title COLLATE NOCASE ASC",
    "price_desc":   "price_value DESC, title COLLATE NOCASE ASC",
}

# Sorts for favorites and list items
_SAVED_SORTS = {
    "added":      "added_at DESC",
    "title":      _SORTS["title"],
    "price_asc":  _SORTS["price_asc"],
    "price_desc": _SORTS["price_desc"],
}

# Asset fields copied into every history/favorite/list row
_SNAPSHOT_FIELDS = ("title", "creator", "price", "price_value", "source",
                    "asset_type", "image_url", "tags")


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot(asset: dict, price_value: float) -> dict:
    """Query params for the url + snapshot columns shared by every table."""
    return {
        "url":         asset["url"],
        "title":       asset.get("title"),
        "creator":     asset.get("creator"),
        "price":       asset.get("price"),
        "price_value": price_value,
        "source":      asset.get("source"),
        "asset_type":  asset.get("asset_type"),
        "image_url":   asset.get("image_url"),
        "tags":        json.dumps(asset.get("tags") or []),
    }


def _filters(q: str, source: str, asset_type: str,
             where: list | None = None, args: list | None = None) -> tuple[str, list]:
    """WHERE clause for the search/source/type filters. `where`/`args` can carry
    extra conditions (e.g. list_id = ?)."""
    where, args = list(where or []), list(args or [])
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
    return ("WHERE " + " AND ".join(where)) if where else "", args


def _page(table: str, where_sql: str, args: list, order_sql: str,
          page: int, limit: int) -> tuple[list, int]:
    """One page of `table` plus the total matching count."""
    with _db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM {table} {where_sql}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM {table} {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
            args + [limit, (page - 1) * limit],
        ).fetchall()
    return [_row_to_dict(r) for r in rows], total


def _save(conn: sqlite3.Connection, table: str, conflict: str, params: dict) -> None:
    """Insert a favorite/list row, or refresh its snapshot if it's already
    saved. added_at keeps its original value."""
    cols = list(params)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _SNAPSHOT_FIELDS)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)}) "
        f"ON CONFLICT({conflict}) DO UPDATE SET {updates}",
        params,
    )


# ── Click history ────────────────────────────────────────────────────────────

def record_click(asset: dict, price_value: float) -> None:
    """Insert the asset, or bump click_count/last_clicked if already present.
    The snapshot fields are refreshed so price/title stay current."""
    now = _now()
    params = {**_snapshot(asset, price_value), "now": now}
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
    where_sql, args = _filters(q, source, asset_type)
    return _page("history", where_sql, args, _SORTS.get(sort, _SORTS["recent"]), page, limit)


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


# ── Favorites ────────────────────────────────────────────────────────────────

def add_favorite(asset: dict, price_value: float) -> None:
    with _db() as conn:
        _save(conn, "favorites", "url",
              {**_snapshot(asset, price_value), "added_at": _now()})


def remove_favorite(url: str) -> int:
    with _db() as conn:
        return conn.execute("DELETE FROM favorites WHERE url = ?", (url,)).rowcount


def get_favorites(q: str = "", source: str = "all", asset_type: str = "all",
                  sort: str = "added", page: int = 1, limit: int = 24) -> tuple[list, int]:
    where_sql, args = _filters(q, source, asset_type)
    return _page("favorites", where_sql, args,
                 _SAVED_SORTS.get(sort, _SAVED_SORTS["added"]), page, limit)


def get_favorite_urls() -> list:
    with _db() as conn:
        return [r[0] for r in conn.execute("SELECT url FROM favorites")]


# ── Named lists ──────────────────────────────────────────────────────────────

class DuplicateListName(ValueError):
    pass


def get_lists() -> list:
    """Every list with its item count, alphabetical."""
    with _db() as conn:
        rows = conn.execute("""
            SELECT l.*, COUNT(i.url) AS count
            FROM lists l LEFT JOIN list_items i ON i.list_id = l.id
            GROUP BY l.id
            ORDER BY l.name COLLATE NOCASE
        """).fetchall()
    return [dict(r) for r in rows]


def get_list(list_id: int) -> dict | None:
    with _db() as conn:
        row = conn.execute("""
            SELECT l.*, (SELECT COUNT(*) FROM list_items WHERE list_id = l.id) AS count
            FROM lists l WHERE l.id = ?
        """, (list_id,)).fetchone()
    return dict(row) if row else None


def create_list(name: str) -> dict:
    """Raises DuplicateListName if a list with that name (any case) exists."""
    now = _now()
    try:
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO lists (name, created_at, updated_at) VALUES (?, ?, ?)",
                (name, now, now))
    except sqlite3.IntegrityError:
        raise DuplicateListName(name) from None
    return get_list(cur.lastrowid)


def rename_list(list_id: int, name: str) -> bool:
    """False if the list doesn't exist. Raises DuplicateListName on a clash."""
    try:
        with _db() as conn:
            cur = conn.execute("UPDATE lists SET name = ?, updated_at = ? WHERE id = ?",
                               (name, _now(), list_id))
    except sqlite3.IntegrityError:
        raise DuplicateListName(name) from None
    return cur.rowcount > 0


def delete_list(list_id: int) -> bool:
    """Delete the list; its items go with it (ON DELETE CASCADE)."""
    with _db() as conn:
        return conn.execute("DELETE FROM lists WHERE id = ?", (list_id,)).rowcount > 0


def add_to_list(list_id: int, asset: dict, price_value: float) -> bool:
    """False if the list doesn't exist. Adding an item that's already there
    refreshes its snapshot."""
    now = _now()
    with _db() as conn:
        cur = conn.execute("UPDATE lists SET updated_at = ? WHERE id = ?", (now, list_id))
        if not cur.rowcount:
            return False
        _save(conn, "list_items", "list_id, url",
              {"list_id": list_id, **_snapshot(asset, price_value), "added_at": now})
    return True


def remove_from_list(list_id: int, url: str) -> int:
    with _db() as conn:
        cur = conn.execute("DELETE FROM list_items WHERE list_id = ? AND url = ?",
                           (list_id, url))
        if cur.rowcount:
            conn.execute("UPDATE lists SET updated_at = ? WHERE id = ?", (_now(), list_id))
        return cur.rowcount


def get_list_items(list_id: int, q: str = "", source: str = "all", asset_type: str = "all",
                   sort: str = "added", page: int = 1, limit: int = 24) -> tuple[list, int]:
    where_sql, args = _filters(q, source, asset_type, ["list_id = ?"], [list_id])
    return _page("list_items", where_sql, args,
                 _SAVED_SORTS.get(sort, _SAVED_SORTS["added"]), page, limit)


def get_list_memberships() -> dict:
    """{url: [list_id, ...]} for every URL in any list."""
    out: dict = {}
    with _db() as conn:
        for url, list_id in conn.execute("SELECT url, list_id FROM list_items"):
            out.setdefault(url, []).append(list_id)
    return out
