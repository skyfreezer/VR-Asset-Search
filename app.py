"""
app.py — Flask frontend for VR Asset Search
"""

import json
import csv
import io
import os
import re
import threading
import queue
import time
from flask import (Flask, render_template, request, jsonify,
                   Response, stream_with_context, send_file)
from crawler import run_crawl, _check_playwright
import db
from waitress import serve

app = Flask(__name__)
db.init_db()  # create history, favorites, and list tables on first run

# In-memory store for the most recent crawl results.
# Everything here is lost on restart; seen_urls.json (see crawler.py) and the
# history, favorites, and lists in vr_assets.db (see db.py) persist.
_store: dict = {
    "assets": [],
    "status": "idle",   # idle | running | done | error
    "log":    [],       # full history of progress messages for the current crawl
    "error":  None,
}
_crawl_lock = threading.Lock()

# Hand-off channel between the crawl thread (producer) and /api/stream (consumer).
# Each message is delivered to exactly one reader, so the UI should keep a
# single EventSource open per crawl.
_log_queue: queue.Queue = queue.Queue()


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "status":        _store["status"],
        "asset_count":   len(_store["assets"]),
        "playwright_ok": _check_playwright(),
        "error":         _store["error"],
    })


@app.route("/api/start", methods=["POST"])
def api_start():
    """Start a crawl in a background thread."""
    if _store["status"] == "running":
        return jsonify({"error": "Crawl already running"}), 409

    body      = request.get_json(silent=True) or {}
    source    = body.get("source", "all")
    max_pages = min(int(body.get("max_pages", 3)), 10)
    types     = body.get("types") or None  # None = all types

    # Reset state before the thread starts so /api/status reports "running" immediately
    with _crawl_lock:
        _store["assets"] = []
        _store["status"] = "running"
        _store["log"]    = []
        _store["error"]  = None

    def do_crawl():
        # Runs in a background thread so the HTTP request returns right away.
        # The crawler calls progress() for every log line; each line is kept in
        # _store and pushed to the SSE queue for the browser.
        def progress(msg: str):
            _store["log"].append(msg)
            _log_queue.put(msg)

        try:
            results = run_crawl(source=source, max_pages=max_pages,
                                types=types, progress_cb=progress)
            with _crawl_lock:
                _store["assets"] = results
                _store["status"] = "done"
            # Sentinel messages (__DONE__/__ERROR__) tell the stream and the UI to stop listening
            _log_queue.put(f"__DONE__{len(results)}")
        except Exception as e:
            with _crawl_lock:
                _store["status"] = "error"
                _store["error"]  = str(e)
            _log_queue.put(f"__ERROR__{e}")

    threading.Thread(target=do_crawl, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/stream")
def api_stream():
    """Server-Sent Events stream of crawl log messages.

    Each SSE frame is "data: <message>\\n\\n". The stream closes itself after a
    __DONE__ or __ERROR__ sentinel, and sends __PING__ every 30 s of silence so
    browsers and proxies don't drop an idle connection during slow fetches.
    """
    def generate():
        yield "data: connected\n\n"
        while True:
            try:
                msg = _log_queue.get(timeout=30)
                yield f"data: {msg}\n\n"
                if msg.startswith("__DONE__") or msg.startswith("__ERROR__"):
                    break
            except queue.Empty:
                yield "data: __PING__\n\n"

    # no-cache + X-Accel-Buffering stop proxies (e.g. nginx) from buffering the stream
    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


def _price_sort_key(asset: dict) -> float:
    """Numeric sort key for display prices like "Free", "$12.00", "$5.00+", "1,500 JPY".

    Uses the first number in the string (the low end of a range). Currencies are
    not converted, and unparseable prices ("N/A") sort last. Note that thousands
    separators end the match, so "1,500 JPY" sorts as 1.
    """
    p = (asset.get("price") or "").lower().strip()
    if p in ("free", "$0.00", "0", "¥0"):
        return 0.0
    m = re.search(r"[\d.]+", p)
    return float(m.group()) if m else float("inf")


@app.route("/api/assets")
def api_assets():
    """Return filtered, sorted, paginated assets as JSON.

    Filters apply in order: source, type, new-only, then free-text search.
    Sorting and pagination run on the filtered list.
    """
    assets = _store["assets"]
    q      = request.args.get("q", "").lower()
    source = request.args.get("source", "all")
    atype  = request.args.get("type", "all")
    sort   = request.args.get("sort", "new_first")
    new_only = request.args.get("new_only", "false").lower() == "true"
    page   = max(1, int(request.args.get("page", 1)))
    limit  = min(50, int(request.args.get("limit", 24)))

    if source != "all":
        assets = [a for a in assets if a["source"] == source]
    if atype != "all":
        assets = [a for a in assets if a["asset_type"] == atype]
    if new_only:
        assets = [a for a in assets if a.get("is_new")]
    if q:
        assets = [a for a in assets
                  if q in a["title"].lower()
                  or q in a["creator"].lower()
                  or any(q in t.lower() for t in a.get("tags", []))]

    # new_first: new items on top (False sorts before True), then alphabetical
    if sort == "new_first":
        assets = sorted(assets, key=lambda a: (not a.get("is_new"), a.get("title", "").lower()))
    elif sort == "price_asc":
        assets = sorted(assets, key=_price_sort_key)
    elif sort == "price_desc":
        assets = sorted(assets, key=_price_sort_key, reverse=True)
    elif sort == "title":
        assets = sorted(assets, key=lambda a: a.get("title", "").lower())

    total  = len(assets)
    start  = (page - 1) * limit
    paged  = assets[start: start + limit]
    return jsonify({"assets": paged, "total": total, "page": page, "limit": limit})


@app.route("/api/stats")
def api_stats():
    """Summary counts for the sidebar Stats panel (always over the full, unfiltered result set)."""
    assets = _store["assets"]
    from collections import Counter
    by_type   = Counter(a["asset_type"] for a in assets)
    by_source = Counter(a["source"] for a in assets)
    free      = sum(1 for a in assets if (a.get("price") or "").lower() in ("free", "$0.00", "0", "¥0"))
    new_count = sum(1 for a in assets if a.get("is_new"))
    return jsonify({
        "total":     len(assets),
        "by_type":   dict(by_type.most_common()),
        "by_source": dict(by_source),
        "free":      free,
        "paid":      len(assets) - free,
        "new":       new_count,
    })


@app.route("/api/export/<fmt>")
def api_export(fmt: str):
    """Download the current results as a file. fmt: 'json' | 'csv'."""
    assets = _store["assets"]
    if not assets:
        return jsonify({"error": "No data to export"}), 400

    if fmt == "json":
        buf = io.BytesIO(json.dumps(assets, indent=2).encode())
        buf.seek(0)
        return send_file(buf, mimetype="application/json",
                         as_attachment=True,
                         download_name="vrchat_assets.json")

    if fmt == "csv":
        out = io.StringIO()
        fields = list(assets[0].keys())
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        for a in assets:
            row = dict(a)
            # CSV cells can't hold lists, so flatten tags into one comma-separated string
            row["tags"] = ", ".join(row.get("tags") or [])
            writer.writerow(row)
        buf = io.BytesIO(out.getvalue().encode())
        buf.seek(0)
        return send_file(buf, mimetype="text/csv",
                         as_attachment=True,
                         download_name="vrchat_assets.csv")

    return jsonify({"error": "Unknown format"}), 400


# ── Click history (persisted in SQLite, see db.py) ──────────────────────────

def _query_args(default_sort: str) -> dict:
    """Filter/sort/paging params shared by history, favorites, and list items."""
    return {
        "q":          request.args.get("q", "").strip(),
        "source":     request.args.get("source", "all"),
        "asset_type": request.args.get("type", "all"),
        "sort":       request.args.get("sort", default_sort),
        "page":       max(1, int(request.args.get("page", 1))),
        "limit":      min(50, int(request.args.get("limit", 24))),
    }


def _paged(fetch, args: dict):
    """Run a db getter with _query_args() output; respond in /api/assets shape."""
    rows, total = fetch(**args)
    return jsonify({"assets": rows, "total": total,
                    "page": args["page"], "limit": args["limit"]})


@app.route("/api/history", methods=["GET"])
def api_history():
    """Paginated click history. Same query params as /api/assets, except sort:
    recent | most_clicked | title | price_asc | price_desc."""
    return _paged(db.get_history, _query_args("recent"))


@app.route("/api/history", methods=["POST"])
def api_history_add():
    """Record a click. Body: the asset dict as returned by /api/assets."""
    asset = request.get_json(silent=True) or {}
    if not asset.get("url"):
        return jsonify({"error": "url is required"}), 400
    db.record_click(asset, _price_sort_key(asset))
    return jsonify({"ok": True})


@app.route("/api/history", methods=["DELETE"])
def api_history_delete():
    """Remove one entry (?url=...) or, with no url, clear all history."""
    removed = db.delete_history(request.args.get("url") or None)
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/history/urls")
def api_history_urls():
    """Every clicked URL, so the UI can mark already-viewed results."""
    return jsonify({"urls": db.get_history_urls()})


@app.route("/api/history/stats")
def api_history_stats():
    return jsonify(db.history_stats())


# ── Favorites and named lists (persisted in SQLite, see db.py) ──────────────
# Favorite and list-item sorts: added | title | price_asc | price_desc

@app.route("/api/favorites", methods=["GET"])
def api_favorites():
    return _paged(db.get_favorites, _query_args("added"))


@app.route("/api/favorites", methods=["POST"])
def api_favorites_add():
    """Favorite an asset. Body: the asset dict as returned by /api/assets."""
    asset = request.get_json(silent=True) or {}
    if not asset.get("url"):
        return jsonify({"error": "url is required"}), 400
    db.add_favorite(asset, _price_sort_key(asset))
    return jsonify({"ok": True})


@app.route("/api/favorites", methods=["DELETE"])
def api_favorites_delete():
    """Unfavorite one asset (?url=...)."""
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "url is required"}), 400
    return jsonify({"ok": True, "removed": db.remove_favorite(url)})


@app.route("/api/favorites/urls")
def api_favorites_urls():
    return jsonify({"urls": db.get_favorite_urls()})


_MAX_LIST_NAME = 60


def _list_name() -> tuple[str | None, tuple | None]:
    """(name, None) from the JSON body, or (None, error_response)."""
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not name:
        return None, (jsonify({"error": "name is required"}), 400)
    if len(name) > _MAX_LIST_NAME:
        return None, (jsonify({"error": f"name must be {_MAX_LIST_NAME} characters or fewer"}), 400)
    return name, None


def _duplicate(name: str):
    return jsonify({"error": f'A list named "{name}" already exists'}), 409


def _no_list():
    return jsonify({"error": "List not found"}), 404


@app.route("/api/lists", methods=["GET"])
def api_lists():
    return jsonify({"lists": db.get_lists()})


@app.route("/api/lists", methods=["POST"])
def api_lists_create():
    """Create a list. Body: {"name": "..."}. Names are unique, ignoring case."""
    name, err = _list_name()
    if err:
        return err
    try:
        return jsonify({"ok": True, "list": db.create_list(name)}), 201
    except db.DuplicateListName:
        return _duplicate(name)


@app.route("/api/lists/<int:list_id>", methods=["PATCH"])
def api_lists_rename(list_id: int):
    name, err = _list_name()
    if err:
        return err
    try:
        if not db.rename_list(list_id, name):
            return _no_list()
    except db.DuplicateListName:
        return _duplicate(name)
    return jsonify({"ok": True, "list": db.get_list(list_id)})


@app.route("/api/lists/<int:list_id>", methods=["DELETE"])
def api_lists_delete(list_id: int):
    if not db.delete_list(list_id):
        return _no_list()
    return jsonify({"ok": True})


@app.route("/api/lists/<int:list_id>/items", methods=["GET"])
def api_list_items(list_id: int):
    if not db.get_list(list_id):
        return _no_list()
    return _paged(lambda **a: db.get_list_items(list_id, **a), _query_args("added"))


@app.route("/api/lists/<int:list_id>/items", methods=["POST"])
def api_list_items_add(list_id: int):
    """Add an asset to a list. Body: the asset dict as returned by /api/assets."""
    asset = request.get_json(silent=True) or {}
    if not asset.get("url"):
        return jsonify({"error": "url is required"}), 400
    if not db.add_to_list(list_id, asset, _price_sort_key(asset)):
        return _no_list()
    return jsonify({"ok": True})


@app.route("/api/lists/<int:list_id>/items", methods=["DELETE"])
def api_list_items_delete(list_id: int):
    """Remove one asset (?url=...) from a list."""
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "url is required"}), 400
    return jsonify({"ok": True, "removed": db.remove_from_list(list_id, url)})


@app.route("/api/lists/memberships")
def api_list_memberships():
    """{url: [list_id, ...]}, so the UI can show which lists each card is in."""
    return jsonify({"memberships": db.get_list_memberships()})


if __name__ == "__main__":
    # Waitress is the default server. For auto-reload while developing, swap to the
    # commented app.run(...) line. Note it binds 0.0.0.0, which exposes the app on your LAN.
    #app.run(debug=True, port=8081, host="0.0.0.0", threaded=True)
    serve(app, host="localhost", port=8081)
