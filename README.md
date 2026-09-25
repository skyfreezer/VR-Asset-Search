# VR Asset Search

A self-hosted web app for finding VRChat avatars and Unity assets across popular creator marketplaces: **Jinxxy**, **Gumroad**, **Booth.pm**, and **Payhip**. It crawls public listing pages, normalizes the results into one searchable catalog, and highlights items that are new since your last run.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Flask](https://img.shields.io/badge/flask-3.x-lightgrey)
![Playwright](https://img.shields.io/badge/playwright-optional-green)

---

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running the App](#running-the-app)
- [Usage Example](#usage-example)
- [REST API](#rest-api)
- [Data Model](#data-model)
- [Project Structure](#project-structure)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Responsible Use](#responsible-use)

---

## Features

- **Four marketplaces, one search.** Jinxxy, Gumroad, Booth.pm, and Payhip results are combined into a single catalog.
- **Asset type filtering.** Limit a crawl to avatar bases, avatars, clothing, accessories, hair, shaders, animations, prefabs, or world assets. The crawler only queries the marketplace categories and search terms that match your selection.
- **"New since last run" tracking.** URLs are saved to `seen_urls.json` between runs, so items you haven't seen before are flagged as **New**.
- **Live progress.** Crawl logs stream to the browser in real time over Server-Sent Events.
- **Search, sort, and filter.** Search by title, creator, or tag. Filter by source, type, or new items only. Sort by newest, title, or price.
- **Export.** Download results as JSON or CSV.
- **Lightweight fetching.** Jinxxy, Gumroad, and Booth.pm are read from plain HTML with `requests`. Playwright (headless Chromium) is used only for Payhip, which renders its listings with JavaScript.
- **Polite crawling.** Checks `robots.txt` and waits between requests.

---

## How It Works

```
 Browser UI (templates/index.html)
        │  POST /api/start  ·  GET /api/stream (SSE)  ·  GET /api/assets
        ▼
 Flask app (app.py, served by Waitress)
        │  runs the crawl in a background thread
        ▼
 Crawler (crawler.py)
   ├── Jinxxy   → server-rendered HTML           (requests + BeautifulSoup)
   ├── Gumroad  → Inertia.js JSON in data-page   (requests + JSON parse)
   ├── Booth.pm → search result HTML             (requests + BeautifulSoup)
   └── Payhip   → client-rendered marketplace    (Playwright)
        │
        ▼
 Normalized VRChatAsset records  →  in-memory store  →  UI / export
                                 →  seen_urls.json (new-item tracking)
```

### Source strategies

| Source   | Method | What gets crawled |
|----------|--------|-------------------|
| Jinxxy   | Static HTML | `/market/{avatars, clothing, avatar-props, shaders, materials, worlds, world-assets}`, sorted newest |
| Gumroad  | Embedded JSON | `/discover` search, using VRChat-specific queries for each asset type |
| Booth.pm | Static HTML | `/en/search/{query}`, sorted by new arrivals |
| Payhip   | Playwright | `/marketplace/3d/vrchat` (skipped if Playwright is not available) |

### Asset classification

Each item's title is matched against keyword groups (for example, `poiyomi` or `liltoon` → **shader**, `outfit` or `hoodie` → **clothing**). The first match sets the item's `asset_type`. Items that match nothing are labeled `asset`.

### Playwright fallback

`crawler.py` includes a general `smart_fetch()` helper for adding new sources. None of the current crawlers use it: Payhip calls Playwright directly. It works like this:

1. `smart_fetch()` requests the page with plain `requests`.
2. It counts elements that look like product cards.
3. If it finds fewer than 3 and the page has a nearly empty React/Next.js root (`#root`, `#__next`, `#app`), it treats the page as JS-rendered.
4. Playwright opens the URL in headless Chromium, waits for `networkidle`, and scrolls to trigger lazy loading.
5. The rendered HTML goes to BeautifulSoup, the same as a normal response.

---

## Requirements

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.10+ | Developed and tested on 3.12 |
| pip | Latest recommended | |
| Chromium (via Playwright) | — | **Optional.** Only needed for Payhip and JS-rendered pages |
| Internet access | — | Outbound HTTPS to the four marketplaces |

Python packages (from [`requirements.txt`](requirements.txt)):

| Package | Purpose |
|---------|---------|
| `flask>=3.0` | Web framework and REST API |
| `waitress` | Production-grade WSGI server |
| `requests>=2.31` | HTTP client |
| `beautifulsoup4>=4.12` | HTML parsing |
| `lxml>=5.0` | Fast parser backend for BeautifulSoup |
| `playwright>=1.40` | Headless browser for JS-rendered pages |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/skyfreezer/VR-Asset-Search.git
cd VR-Asset-Search
```

### 2. Create and activate a virtual environment

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Install the Playwright browser (recommended)

```bash
playwright install chromium
```

> Without this step the app still runs. Payhip is skipped and the JS fallback is disabled. The **Playwright** indicator in the sidebar shows whether the browser was found.

---

## Running the App

```bash
python app.py
```

Then open **http://localhost:8081** in your browser.

The app is served by [Waitress](https://docs.pylonsproject.org/projects/waitress/) and listens on `localhost` only. See [Configuration](#configuration) to change the host or port.

---

## Usage Example

### 1. Configure a crawl

In the left sidebar:

| Setting | Example | Description |
|---------|---------|-------------|
| **Source** | `All sources` | Crawl every marketplace or just one |
| **Pages per query** | `3` | Pages fetched per category or search query (1–10) |
| **Asset types** | `Clothing`, `Shader` | Type toggles. Deselect types to narrow the crawl and make it faster |

Click **▶ START CRAWL**.

### 2. Watch the live log

The console bar shows progress as each source is crawled:

```
[Jinxxy] /market/clothing page=1
[Jinxxy] +24 assets (total 24)
[Jinxxy] /market/shaders page=1
[Jinxxy] +24 assets (total 48)
[Gumroad] query='vrchat clothing' page=1
[Gumroad] +36 assets (total 36)
[Gumroad] query='vrchat shader' page=1
[Gumroad] +33 assets (total 69)
[Booth] query='vrchat clothing' page=1
[Booth] +60 assets (total 60)
[Payhip] /marketplace/3d/vrchat page=1
[Payhip] +20 assets (total 20)
New items this run: 41 of 187
```

### 3. Browse the results

When the crawl finishes, results appear as cards with thumbnail, title, creator, price, source, and type. You can:

- Type in the **search bar**, for example `poiyomi` or a creator's name.
- Click a **source pill** (Jinxxy, Gumroad, Booth.pm, Payhip) to filter by marketplace.
- Toggle **✦ New only** to show only items that weren't in previous runs.
- Change the sort to **Price low–high** to find free assets first.
- Check the sidebar **Stats** panel for totals by source, free vs. paid, and type.

### 4. Export

Click **JSON** or **CSV** in the sidebar to download the full result set (`vrchat_assets.json` / `vrchat_assets.csv`).

### Scripted example (API)

You can run the same workflow without the UI:

```bash
# Start a crawl limited to Booth.pm shaders, 2 pages per query
curl -X POST http://localhost:8081/api/start \
     -H "Content-Type: application/json" \
     -d '{"source": "booth", "max_pages": 2, "types": ["shader"]}'

# Follow live progress (Ctrl+C when you see __DONE__)
curl -N http://localhost:8081/api/stream

# Search the results
curl "http://localhost:8081/api/assets?q=liltoon&sort=price_asc&limit=5"

# Download everything as CSV
curl -o assets.csv http://localhost:8081/api/export/csv
```

---

## REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/` | Web UI |
| `GET`  | `/api/status` | Crawl status, result count, Playwright availability |
| `POST` | `/api/start` | Start a crawl in the background |
| `GET`  | `/api/stream` | Server-Sent Events stream of crawl log messages |
| `GET`  | `/api/assets` | Filtered, sorted, paginated results |
| `GET`  | `/api/stats` | Summary counts by source, type, price, and new items |
| `GET`  | `/api/export/json` | Download all results as JSON |
| `GET`  | `/api/export/csv` | Download all results as CSV |

### `POST /api/start`

```json
{
  "source": "all",
  "max_pages": 3,
  "types": ["avatar base", "clothing"]
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `source` | string | `"all"` | `all`, `jinxxy`, `gumroad`, `booth`, or `payhip` |
| `max_pages` | int | `3` | Pages per query or category (capped at 10) |
| `types` | string[] | all | Any of: `avatar base`, `avatar`, `clothing`, `accessory`, `hair`, `shader`, `animation`, `prefab`, `world asset`, `asset` |

Returns `409 Conflict` if a crawl is already running.

### `GET /api/stream`

The stream sends one `data:` line per log message. It also sends:

- `connected`: sent when the connection opens
- `__PING__`: keep-alive, sent every 30 s while idle
- `__DONE__<count>`: the crawl finished with `<count>` results
- `__ERROR__<message>`: the crawl failed

### `GET /api/assets`

| Param | Default | Description |
|-------|---------|-------------|
| `q` | — | Case-insensitive match on title, creator, or tags |
| `source` | `all` | Filter by marketplace |
| `type` | `all` | Filter by asset type |
| `new_only` | `false` | `true` returns only new items |
| `sort` | `new_first` | `new_first`, `title`, `price_asc`, `price_desc` |
| `page` | `1` | Page number |
| `limit` | `24` | Results per page (max 50) |

Response:

```json
{
  "assets": [ { "...": "..." } ],
  "total": 187,
  "page": 1,
  "limit": 24
}
```

### `GET /api/stats`

```json
{
  "total": 187,
  "by_type":   { "clothing": 102, "shader": 61, "asset": 24 },
  "by_source": { "jinxxy": 48, "gumroad": 69, "booth": 60, "payhip": 20 },
  "free": 23,
  "paid": 164,
  "new": 41
}
```

---

## Data Model

Each result is a `VRChatAsset` record. Sample (illustrative values):

```json
{
  "title": "Casual Hoodie Outfit",
  "source": "gumroad",
  "url": "https://example.gumroad.com/l/casual-hoodie",
  "price": "$12.00",
  "creator": "Example Creator",
  "description": "N/A",
  "tags": [],
  "image_url": "https://public-files.gumroad.com/...",
  "asset_type": "clothing",
  "rating": "4.9 (38)",
  "downloads": "N/A",
  "first_seen": "2026-09-25T14:03:11.482913+00:00",
  "is_new": true
}
```

| Field | Description |
|-------|-------------|
| `title` | Product name |
| `source` | `jinxxy`, `gumroad`, `booth`, or `payhip` |
| `url` | Canonical product URL, with query strings removed |
| `price` | Display price (`Free`, `$12.00`, `$5.00+` for pay-what-you-want, `1,500 JPY`, or a range) |
| `creator` | Seller or shop name |
| `asset_type` | Type assigned from title keywords |
| `rating` | Average rating and review count, when the source provides it (Gumroad) |
| `first_seen` | UTC timestamp of the crawl that returned this item |
| `is_new` | `true` if the URL wasn't in `seen_urls.json` before this run |

`N/A` means the source didn't provide that value.

---

## Project Structure

```
VR-Asset-Search/
├── app.py              # Flask app: REST API, SSE stream, export, Waitress entry point
├── crawler.py          # Marketplace crawlers, classifier, robots.txt, Playwright fallback
├── requirements.txt    # Python dependencies
├── seen_urls.json      # Generated: URLs from earlier runs (for "new" detection, git-ignored)
├── .gitignore          # Keeps .venv, caches, exports, and seen_urls.json out of the repo
├── .gitattributes      # Normalizes line endings across Windows and macOS/Linux
├── templates/
│   └── index.html      # Single-page frontend (HTML/CSS/JS)
└── docs/
    └── README.md       # Additional notes
```

---

## Configuration

These settings are constants in the source:

| Setting | Location | Default |
|---------|----------|---------|
| Host / port | `app.py`, `serve(app, host=..., port=...)` | `localhost:8081` |
| Max pages per query | `app.py`, `api_start()` | Capped at `10` |
| Request delay | `crawler.py`, `time.sleep(...)` in each crawler | 1.0 s (Jinxxy), 1.5 s (others) |
| Search queries per type | `crawler.py`, `GUMROAD_QUERIES_BY_TYPE`, `BOOTH_QUERIES_BY_TYPE` | VRChat-focused terms |
| Jinxxy categories | `crawler.py`, `JINXXY_MARKETS` | 7 market categories |
| Type keywords | `crawler.py`, `ASSET_TYPE_KEYWORDS` | See source |
| Seen-URL file | `crawler.py`, `_SEEN_FILE` | `seen_urls.json` next to `crawler.py` |

**Reset "new" tracking:** delete `seen_urls.json`. On the next crawl, every item will be marked new.

**Debug mode:** in `app.py`, comment out the `serve(...)` line and uncomment `app.run(debug=True, ...)` to use Flask's development server with auto-reload.

> Results are kept in memory. Restarting the server clears the current result set, but `seen_urls.json` is kept.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Sidebar says Playwright is unavailable | Run `playwright install chromium` inside your virtual environment, then restart the app. |
| Payhip returns 0 results | Payhip requires Playwright. See above. |
| A source returns 0 results | The marketplace may have changed its page layout or be rate-limiting you. Try fewer pages, wait, and retry. The selectors in `crawler.py` may need updating. |
| `409 Crawl already running` | Only one crawl can run at a time. Wait for `__DONE__` or restart the server. |
| `Activate.ps1 cannot be loaded` on Windows | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then activate again. |
| Port 8081 already in use | Change the `port` in `app.py`. |

---

## Responsible Use

This tool is for **discovering** publicly listed products. It is not for bypassing store protections.

- Crawls only public pages that don't require a login
- Checks each site's `robots.txt` before crawling a path
- Waits 1 to 1.5 seconds between requests
- Does not download paid files, bypass paywalls, or collect personal data

Follow each marketplace's Terms of Service, keep crawl sizes reasonable, and support creators by buying assets through their official store pages.
