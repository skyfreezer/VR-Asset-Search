"""
VR Asset Search — crawler.py
Crawls Jinxxy, Gumroad, Booth.pm, and Payhip for publicly listed VRChat avatars and Unity assets.
Uses requests + BeautifulSoup first; falls back to Playwright for JS-rendered pages.
Tracks seen URLs across runs and flags newly discovered items.
"""

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlencode, quote as urlquote
from html import unescape
from urllib.robotparser import RobotFileParser
import time
import json
import os
import re
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# ── Playwright lazy import ────────────────────────────────────────────────────

_playwright_available = None

def _check_playwright() -> bool:
    """Returns True only if Playwright is installed AND browser binaries are present.

    The result is cached for the life of the process, so after running
    `playwright install chromium` the app must be restarted to pick it up.
    """
    global _playwright_available
    if _playwright_available is None:
        try:
            from playwright.sync_api import sync_playwright
            # Verify the browser binary actually exists before claiming it's ready
            with sync_playwright() as pw:
                exe = pw.chromium.executable_path
            import os
            _playwright_available = os.path.exists(exe)
            if not _playwright_available:
                logger.warning(
                    "Playwright installed but browser not found. "
                    "Run: playwright install chromium"
                )
        except ImportError:
            _playwright_available = False
            logger.warning("Playwright not installed — JS fallback disabled.")
        except Exception:
            _playwright_available = False
    return _playwright_available


# ── Data Model ────────────────────────────────────────────────────────────────

@dataclass
class VRChatAsset:
    title:       str
    source:      str
    url:         str
    price:       str = "N/A"
    creator:     str = "N/A"
    description: str = "N/A"
    tags:        list = field(default_factory=list)
    image_url:   str = "N/A"
    asset_type:  str = "asset"
    rating:      str = "N/A"
    downloads:   str = "N/A"
    first_seen:  str = ""    # ISO-8601 UTC timestamp set at crawl time
    is_new:      bool = True  # True when URL wasn't seen in any prior run


# ── Asset type classifier ─────────────────────────────────────────────────────

# Order matters: guess_asset_type() returns the FIRST group with a matching
# keyword (dicts keep insertion order). Specific groups come first and the broad
# "avatar" group is last, so it only catches titles nothing else matched.
# Keywords are plain substrings, so short ones like "ear" or "anim" also match
# inside longer words (e.g. "wear", "anime").
ASSET_TYPE_KEYWORDS = {
    "avatar base":  ["avatar base", "base model", "base avatar", "full body"],
    "clothing":     ["clothing", "outfit", "dress", "shirt", "pants", "hoodie", "jacket"],
    "accessory":    ["accessory", "prop", "wing", "tail", "horn", "ear", "hat", "glasses"],
    "hair":         ["hair", "hairstyle"],
    "shader":       ["shader", "poiyomi", "liltoon", "material"],
    "animation":    ["animation", "gesture", "emote", "dance", "anim"],
    "prefab":       ["prefab", "unity prefab"],
    "world asset":  ["world", "environment", "skybox", "terrain"],
    "avatar":       ["avatar", "character", "model", "vrc"],
}

def guess_asset_type(text: str) -> str:
    """Classify a listing by keyword-matching its title. Falls back to "asset"."""
    t = text.lower()
    for atype, kws in ASSET_TYPE_KEYWORDS.items():
        if any(k in t for k in kws):
            return atype
    return "asset"


# ── Seen-URL persistence ──────────────────────────────────────────────────────

# Every product URL ever returned by a crawl. Comparing against this set is how
# items get flagged is_new. The file only grows; delete it to reset "new" tracking.
_SEEN_FILE = os.path.join(os.path.dirname(__file__), "seen_urls.json")

def load_seen_urls() -> set:
    """Load previously seen URLs. A missing or corrupt file is treated as empty."""
    if os.path.exists(_SEEN_FILE):
        try:
            with open(_SEEN_FILE, encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_seen_urls(urls: set) -> None:
    """Persist seen URLs (sorted, so the file stays stable and diff-friendly)."""
    try:
        with open(_SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(urls), f)
    except Exception as e:
        logger.warning(f"Could not save seen URLs: {e}")


# ── HTTP helpers ──────────────────────────────────────────────────────────────

# Honest bot identity. Used for robots.txt checks, Jinxxy, and Playwright.
HEADERS = {
    "User-Agent": (
        "VRChatAssetResearchBot/1.0 "
        "(public data aggregator; respects robots.txt)"
    )
}

# Browser-like headers required by Gumroad and Booth to avoid blocks
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# One parsed robots.txt per site, kept for the life of the process
_robots_cache: dict[str, RobotFileParser] = {}

def can_crawl(base_url: str, path: str) -> bool:
    """Check the site's robots.txt for our bot User-Agent before fetching `path`.

    If robots.txt can't be fetched (404, network error), everything is allowed.
    """
    if base_url not in _robots_cache:
        rp = RobotFileParser()
        robots_url = urljoin(base_url, "/robots.txt")
        try:
            # Use requests so our User-Agent header is sent — urllib's default
            # agent gets blocked (403) on some sites, which makes the parser
            # set disallow_all=True and silently block everything.
            resp = requests.get(robots_url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                rp.set_url(robots_url)
                rp.parse(resp.text.splitlines())
            else:
                # 404 or other — assume everything is allowed
                rp.parse(["User-agent: *", "Allow: /"])
        except Exception:
            rp.parse(["User-agent: *", "Allow: /"])
        _robots_cache[base_url] = rp
    return _robots_cache[base_url].can_fetch(HEADERS["User-Agent"], urljoin(base_url, path))


def fetch_html(url: str, params: dict = None) -> Optional[str]:
    """Fetch raw HTML via requests. Returns None on any error (logged at DEBUG)."""
    try:
        res = requests.get(url, headers=HEADERS, params=params, timeout=15)
        res.raise_for_status()
        return res.text
    except Exception as e:
        logger.debug(f"requests failed for {url}: {e}")
        return None


def fetch_soup(url: str, params: dict = None) -> Optional[BeautifulSoup]:
    html = fetch_html(url, params)
    return BeautifulSoup(html, "lxml") if html else None


def fetch_soup_playwright(url: str, params: dict = None) -> Optional[BeautifulSoup]:
    """Playwright fallback — renders JS, then returns BeautifulSoup.

    Launches a fresh headless Chromium for each call. That is slow (a few
    seconds per page) but keeps each fetch isolated and thread-safe.
    """
    if not _check_playwright():
        return None
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    full_url = url
    if params:
        full_url = url + "?" + urlencode(params)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=HEADERS["User-Agent"],
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
            )
            # networkidle = no requests for 500 ms, i.e. the client-side render has settled
            page.goto(full_url, wait_until="networkidle", timeout=30_000)
            # scroll once to trigger lazy-loads
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            html = page.content()
            browser.close()
        return BeautifulSoup(html, "lxml")
    except PWTimeout:
        logger.warning(f"Playwright timeout: {full_url}")
    except Exception as e:
        logger.warning(f"Playwright error for {full_url}: {e}")
    return None


def smart_fetch(url: str, params: dict = None,
                progress_cb: Optional[Callable[[str], None]] = None) -> Optional[BeautifulSoup]:
    """Try requests first; fall back to Playwright if page looks empty / JS-only.

    Currently no crawler calls this (Jinxxy/Gumroad/Booth parse static HTML and
    Payhip goes straight to Playwright). It's kept as a general helper for
    adding new sources.
    """
    if progress_cb:
        progress_cb(f"Fetching {url} …")

    soup = fetch_soup(url, params)

    # Heuristic: if fewer than 3 product-like elements found, try Playwright
    if soup:
        cards = (soup.select(".product-card") or soup.select("article") or
                 soup.select("[class*='product']") or soup.select("li[class*='item']"))
        if len(cards) >= 3:
            return soup
        # Check for React root — sign that JS hasn't rendered yet
        react_root = soup.find(id="root") or soup.find(id="__next") or soup.find(id="app")
        if react_root and len(react_root.get_text(strip=True)) < 200:
            if progress_cb:
                progress_cb(f"JS-rendered page detected — switching to Playwright …")
            return fetch_soup_playwright(url, params)

    return soup


# ── Jinxxy Crawler ────────────────────────────────────────────────────────────
# Jinxxy uses Next.js App Router with SSR — content is in the static HTML.
# Cards are <div class="... bg-card shadow-sm ..."> containing a /{creator}/{slug} link.
# URLs: /market/<category>?platforms=vrchat&page=N&sort=new

JINXXY_BASE = "https://jinxxy.com"

# (path, extra_params, asset_types_covered)
# asset_types_covered is used to skip irrelevant markets when a type filter is active.
JINXXY_MARKETS: list[tuple[str, dict, set[str]]] = [
    ("/market/avatars",      {"platforms": "vrchat"}, {"avatar", "avatar base"}),
    ("/market/clothing",     {"platforms": "vrchat"}, {"clothing"}),
    ("/market/avatar-props", {},                       {"accessory", "hair", "prefab"}),
    ("/market/shaders",      {},                       {"shader"}),
    ("/market/materials",    {},                       {"shader"}),
    ("/market/worlds",       {},                       {"world asset"}),
    ("/market/world-assets", {},                       {"world asset"}),
]

_JINXXY_SKIP_PATHS = {"/market", "/my", "/cart", "/login", "/signup"}

def _is_jinxxy_product_link(href: Optional[str]) -> bool:
    """True for product links shaped like /{creator}/{slug}.

    "/a/b".split("/") → ["", "a", "b"], so a product link has exactly 3 parts.
    Site sections like /market/... or /my/... have the same shape, so they're excluded.
    Also passed to BeautifulSoup's find() as an href filter.
    """
    if not href or not href.startswith("/"):
        return False
    parts = href.split("/")
    return (len(parts) == 3 and bool(parts[1]) and bool(parts[2])
            and not any(href.startswith(p) for p in _JINXXY_SKIP_PATHS))


def parse_jinxxy_card(card) -> Optional[VRChatAsset]:
    """Build an asset from one Jinxxy card <div>. Returns None if it isn't a product."""
    try:
        link = card.find("a", href=_is_jinxxy_product_link)
        if not link:
            return None
        href    = link["href"]                          # /{creator}/{slug}
        url     = urljoin(JINXXY_BASE, href)
        creator = href.split("/")[1]

        img       = card.find("img")
        alt       = img.get("alt", "") if img else ""
        # img alt is often "Title By Creator" — strip the creator suffix
        title     = re.sub(r"\s+[Bb]y\s+\S.*$", "", alt).strip() or alt or "Unknown"
        image_url = img.get("src", "N/A") if img else "N/A"

        # Price: find "N.NN USD" patterns in card text; pick lowest non-zero
        text   = card.get_text(" ", strip=True)
        prices = re.findall(r"(\d+\.?\d*)\s*USD", text)
        if prices:
            non_zero = [float(p) for p in prices if float(p) > 0]
            if non_zero:
                lo = min(non_zero)
                hi = max(non_zero)
                price = f"${lo:.2f}" if lo == hi else f"${lo:.2f} – ${hi:.2f}"
            else:
                price = "Free"
        elif re.search(r"\bfree\b", text, re.IGNORECASE):
            price = "Free"
        else:
            price = "N/A"

        # Every listing here is VRChat-related. The " vrchat" suffix hits the "vrc"
        # keyword, so titles that match no specific group become "avatar", not "asset".
        atype = guess_asset_type(title + " vrchat")
        return VRChatAsset(title=title, source="jinxxy", url=url,
                           price=price, creator=creator, image_url=image_url,
                           tags=[], asset_type=atype)
    except Exception as e:
        logger.debug(f"Jinxxy card parse error: {e}")
        return None


def crawl_jinxxy(max_pages: int = 3,
                 types: Optional[set[str]] = None,
                 progress_cb: Optional[Callable[[str], None]] = None) -> list[VRChatAsset]:
    """Walk each Jinxxy market category, newest first, up to max_pages each.

    Pagination for a category stops early when a fetch fails, a page has no
    product cards, or a page adds nothing new (past the last page, the site may
    repeat content). All crawlers follow this pattern.
    """
    # `seen` de-duplicates within this crawl; the cross-run seen_urls.json is handled in run_crawl()
    assets, seen = [], set()
    for path, base_params, market_types in JINXXY_MARKETS:
        # Skip markets whose types don't overlap with the requested filter
        if types and not types.intersection(market_types):
            continue
        if not can_crawl(JINXXY_BASE, path):
            logger.info(f"robots.txt disallows {path}")
            continue
        for page in range(1, max_pages + 1):
            if progress_cb:
                progress_cb(f"[Jinxxy] {path} page={page}")
            params = {**base_params, "page": page, "sort": "new"}
            html = fetch_html(f"{JINXXY_BASE}{path}", params=params)
            if not html:
                break
            soup = BeautifulSoup(html, "lxml")
            # Tailwind classes are shared with non-product panels, so keep only
            # divs that actually contain a product link
            all_divs   = soup.select("div.bg-card.shadow-sm")
            prod_cards = [d for d in all_divs if d.find("a", href=_is_jinxxy_product_link)]
            if not prod_cards:
                break
            added = 0
            for card in prod_cards:
                a = parse_jinxxy_card(card)
                if a and a.url not in seen:
                    seen.add(a.url); assets.append(a); added += 1
            if progress_cb:
                progress_cb(f"[Jinxxy] +{added} assets (total {len(assets)})")
            if added == 0:
                break
            time.sleep(1.0)  # politeness delay between page requests
    return assets


# ── Gumroad Crawler ───────────────────────────────────────────────────────────
# Gumroad uses Inertia.js — product data is server-embedded as JSON in a
# data-page="..." attribute on the app root div, so no Playwright needed.

GUMROAD_BASE   = "https://gumroad.com"
GUMROAD_SEARCH = "https://gumroad.com/discover"

# Search terms sent to Gumroad for each requested asset type. With no type
# filter, GUMROAD_QUERIES_ALL is used instead.
GUMROAD_QUERIES_BY_TYPE: dict[str, list[str]] = {
    "avatar":      ["vrchat avatar", "3d avatar vrchat"],
    "avatar base": ["vrc avatar base", "vrchat avatar base"],
    "clothing":    ["vrchat clothing", "vrchat outfit"],
    "accessory":   ["vrchat accessory", "vrchat prop"],
    "hair":        ["vrchat hair"],
    "shader":      ["vrchat shader", "poiyomi vrchat"],
    "animation":   ["vrchat animation", "vrchat emote"],
    "prefab":      ["vrchat prefab"],
    "world asset": ["vrchat world asset"],
    "asset":       ["vrchat asset", "unity vrchat"],
}
GUMROAD_QUERIES_ALL = [
    "vrchat avatar", "vrchat asset", "vrc avatar base",
    "unity vrchat", "vrchat clothing", "vrchat accessory",
    "vrchat shader", "3d avatar vrchat",
]


def _fetch_gumroad_inertia(query: str, page: int) -> Optional[dict]:
    """Fetch Gumroad discover page and extract the Inertia.js embedded JSON props."""
    try:
        r = requests.get(
            GUMROAD_SEARCH,
            headers=BROWSER_HEADERS,
            params={"query": query, "page": page, "sort": "newest"},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        logger.debug(f"Gumroad fetch error: {e}")
        return None

    # Inertia embeds page data as HTML-escaped JSON in data-page="..." on the app div.
    # Inside the attribute, quotes are escaped as &quot;, so the first real `"`
    # followed by `>` marks the end. unescape() turns the entities back into JSON.
    m = re.search(r'data-page="(.*?)"(?=\s*>)', r.text, re.DOTALL)
    if not m:
        logger.debug("Gumroad: data-page attribute not found in HTML")
        return None
    try:
        return json.loads(unescape(m.group(1)))
    except Exception as e:
        logger.debug(f"Gumroad JSON parse error: {e}")
        return None


def parse_gumroad_product(p: dict) -> VRChatAsset:
    """Map one product dict from Gumroad's search_results JSON to a VRChatAsset."""
    name = p.get("name", "Unknown")
    url = p.get("url", GUMROAD_BASE)
    # Strip tracking/recommendation query params so the same product always has one URL
    if "?" in url:
        url = url.split("?")[0]
    price_cents = p.get("price_cents", 0)
    is_pwyw = p.get("is_pay_what_you_want", False)
    if price_cents == 0:
        price = "Free"
    elif is_pwyw:
        price = f"${price_cents / 100:.2f}+"
    else:
        price = f"${price_cents / 100:.2f}"
    seller = p.get("seller") or {}
    creator = seller.get("name", "N/A") if isinstance(seller, dict) else "N/A"
    image_url = p.get("thumbnail_url") or "N/A"
    ratings = p.get("ratings") or {}
    rating = (
        f"{ratings['average']:.1f} ({ratings['count']})"
        if ratings.get("count") else "N/A"
    )
    return VRChatAsset(
        title=name, source="gumroad", url=url,
        price=price, creator=creator, image_url=image_url,
        rating=rating, asset_type=guess_asset_type(name),
    )


def crawl_gumroad(max_pages: int = 3,
                  types: Optional[set[str]] = None,
                  progress_cb: Optional[Callable[[str], None]] = None) -> list[VRChatAsset]:
    """Run each Gumroad search query, newest first, up to max_pages each."""
    # Build a de-duplicated query list from the requested types; fall back to the
    # general query set if none of the types have queries.
    if types:
        seen_q: set[str] = set()
        queries: list[str] = []
        for t in types:
            for q in GUMROAD_QUERIES_BY_TYPE.get(t, []):
                if q not in seen_q:
                    queries.append(q); seen_q.add(q)
        if not queries:
            queries = GUMROAD_QUERIES_ALL
    else:
        queries = GUMROAD_QUERIES_ALL

    assets, seen = [], set()
    for query in queries:
        if not can_crawl(GUMROAD_BASE, "/discover"):
            logger.info("robots.txt disallows /discover")
            break
        for page in range(1, max_pages + 1):
            if progress_cb:
                progress_cb(f"[Gumroad] query='{query}' page={page}")
            data = _fetch_gumroad_inertia(query, page)
            if not data:
                break
            # Inertia JSON shape: {"component": ..., "props": {"search_results": {"products": [...]}}}
            products = (
                data.get("props", {})
                    .get("search_results", {})
                    .get("products", [])
            )
            if not products:
                break
            added = 0
            for p in products:
                a = parse_gumroad_product(p)
                if a.url not in seen:
                    seen.add(a.url); assets.append(a); added += 1
            if progress_cb:
                progress_cb(f"[Gumroad] +{added} assets (total {len(assets)})")
            if added == 0:
                break
            time.sleep(1.5)
    return assets


# ── Booth.pm Crawler ──────────────────────────────────────────────────────────
# Booth's /en/items.json API now redirects to HTML. We scrape the search page
# at /en/search/{query} instead. Cards are <li data-product-id="..."> elements.

BOOTH_BASE = "https://booth.pm"

# Same structure as the Gumroad queries. "prefab" has no Booth query, so a
# prefab-only crawl falls back to BOOTH_QUERIES_ALL.
BOOTH_QUERIES_BY_TYPE: dict[str, list[str]] = {
    "avatar":      ["vrchat avatar", "vrc avatar"],
    "avatar base": ["vrchat avatar base"],
    "clothing":    ["vrchat clothing"],
    "accessory":   ["vrchat accessory"],
    "hair":        ["vrchat hair"],
    "shader":      ["vrchat shader"],
    "animation":   ["vrchat animation", "vrchat emote"],
    "prefab":      [],
    "world asset": ["vrchat world"],
    "asset":       ["vrchat asset", "unity vrchat"],
}
BOOTH_QUERIES_ALL = [
    "vrchat avatar", "vrchat asset", "vrc avatar",
    "vrchat clothing", "vrchat accessory", "unity vrchat",
    "vrchat shader", "avatar base vrchat",
]


def parse_booth_card(card) -> Optional[VRChatAsset]:
    """Build an asset from one Booth <li data-product-id> search result."""
    try:
        product_id = card.get("data-product-id")
        if not product_id:
            return None
        # Build the canonical URL from the ID. Card links point at shop subdomains,
        # so the same item could otherwise appear under several URLs.
        url = f"{BOOTH_BASE}/en/items/{product_id}"

        title_el = card.select_one(
            ".item-card__title-anchor--multiline, .item-card__title"
        )
        title = title_el.get_text(strip=True) if title_el else "Unknown"

        price_el = card.select_one(".price")
        price_text = price_el.get_text(strip=True) if price_el else "N/A"
        price = "Free" if price_text in ("0 JPY", "Free") else price_text

        shop_el = card.select_one(
            ".item-card__shop-name-anchor, .item-card__shop-name"
        )
        creator = shop_el.get_text(strip=True) if shop_el else "N/A"

        # Thumbnail: first .js-thumbnail-image anchor carries data-original
        thumb = card.select_one(".js-thumbnail-image")
        image_url = thumb.get("data-original", "N/A") if thumb else "N/A"

        return VRChatAsset(
            title=title, source="booth", url=url,
            price=price, creator=creator, image_url=image_url,
            asset_type=guess_asset_type(title),
        )
    except Exception as e:
        logger.debug(f"Booth card parse error: {e}")
        return None


def crawl_booth(max_pages: int = 3,
                types: Optional[set[str]] = None,
                progress_cb: Optional[Callable[[str], None]] = None) -> list[VRChatAsset]:
    """Run each Booth search query sorted by new arrivals, up to max_pages each."""
    # Same query-selection logic as crawl_gumroad()
    if types:
        seen_q: set[str] = set()
        queries: list[str] = []
        for t in types:
            for q in BOOTH_QUERIES_BY_TYPE.get(t, []):
                if q not in seen_q:
                    queries.append(q); seen_q.add(q)
        if not queries:
            queries = BOOTH_QUERIES_ALL
    else:
        queries = BOOTH_QUERIES_ALL

    assets, seen = [], set()
    for query in queries:
        # Booth puts the query in the path, not a ?q= param, so it must be URL-encoded
        search_path = f"/en/search/{urlquote(query)}"
        if not can_crawl(BOOTH_BASE, search_path):
            logger.info(f"robots.txt disallows Booth {search_path}")
            continue
        search_url = f"{BOOTH_BASE}{search_path}"
        for page in range(1, max_pages + 1):
            if progress_cb:
                progress_cb(f"[Booth] query='{query}' page={page}")
            try:
                r = requests.get(
                    search_url,
                    headers=BROWSER_HEADERS,
                    params={"sort": "new_arrivals", "page": page},
                    timeout=15,
                )
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "lxml")
            except Exception as e:
                logger.debug(f"Booth fetch error: {e}")
                break
            cards = soup.select("li[data-product-id]")
            if not cards:
                break
            added = 0
            for card in cards:
                a = parse_booth_card(card)
                if a and a.url not in seen:
                    seen.add(a.url); assets.append(a); added += 1
            if progress_cb:
                progress_cb(f"[Booth] +{added} assets (total {len(assets)})")
            if added == 0:
                break
            time.sleep(1.5)
    return assets


# ── Payhip Crawler ────────────────────────────────────────────────────────────
# Payhip's marketplace category pages render their product grid client-side
# (the initial HTML ships an empty, hidden .product-list-core), so we render
# with Playwright directly rather than trying requests first. Cards are
# <div class="card-wrapper product-card-wrapper"> under a single VRChat
# category; individual asset types are guessed from the title like Gumroad/Booth.

PAYHIP_BASE = "https://payhip.com"
PAYHIP_VRCHAT_CATEGORY = "/marketplace/3d/vrchat"


def parse_payhip_card(card) -> Optional[VRChatAsset]:
    """Build an asset from one rendered Payhip product card."""
    try:
        title_el = card.select_one(".product-name a") or card.select_one("a[href*='/b/']")
        if not title_el:
            return None
        title = title_el.get_text(strip=True) or "Unknown"
        url   = urljoin(PAYHIP_BASE, title_el["href"]).split("?")[0]

        store_el = card.select_one(".store-attribution a")
        creator  = store_el.get_text(strip=True) if store_el else "N/A"

        price_el   = card.select_one(".price-block")
        price_text = price_el.get_text(" ", strip=True) if price_el else "N/A"
        price = "Free" if "free" in price_text.lower() else (price_text or "N/A")

        img       = card.find("img")
        image_url = img.get("src", "N/A") if img else "N/A"

        return VRChatAsset(
            title=title, source="payhip", url=url,
            price=price, creator=creator, image_url=image_url,
            asset_type=guess_asset_type(title),
        )
    except Exception as e:
        logger.debug(f"Payhip card parse error: {e}")
        return None


def crawl_payhip(max_pages: int = 3,
                 types: Optional[set[str]] = None,
                 progress_cb: Optional[Callable[[str], None]] = None) -> list[VRChatAsset]:
    """Render the Payhip VRChat category page by page with Playwright.

    `types` is accepted for a consistent signature but isn't used here. Payhip
    has one VRChat category, and run_crawl() applies the type filter afterward.
    """
    if not _check_playwright():
        if progress_cb:
            progress_cb("[Payhip] Playwright unavailable — skipping (JS-rendered marketplace)")
        return []

    if not can_crawl(PAYHIP_BASE, PAYHIP_VRCHAT_CATEGORY):
        logger.info(f"robots.txt disallows {PAYHIP_VRCHAT_CATEGORY}")
        return []

    assets, seen = [], set()
    for page in range(1, max_pages + 1):
        if progress_cb:
            progress_cb(f"[Payhip] {PAYHIP_VRCHAT_CATEGORY} page={page}")
        soup = fetch_soup_playwright(f"{PAYHIP_BASE}{PAYHIP_VRCHAT_CATEGORY}", params={"page": page})
        if not soup:
            break
        cards = soup.select(".card-wrapper.product-card-wrapper")
        if not cards:
            break
        added = 0
        for card in cards:
            a = parse_payhip_card(card)
            if a and a.url not in seen:
                seen.add(a.url); assets.append(a); added += 1
        if progress_cb:
            progress_cb(f"[Payhip] +{added} assets (total {len(assets)})")
        if added == 0:
            break
        time.sleep(1.5)
    return assets


# ── Public entry point ────────────────────────────────────────────────────────

def run_crawl(source: str = "all",
              max_pages: int = 3,
              types: Optional[list[str]] = None,
              progress_cb: Optional[Callable[[str], None]] = None) -> list[dict]:
    """
    Run the crawl and return a list of dicts ready for JSON serialization.
    source: 'all' | 'jinxxy' | 'gumroad' | 'booth' | 'payhip'
    types:  None = all asset types; otherwise a list of type names to restrict the crawl.
    New items (URLs not seen in prior runs) are flagged with is_new=True.
    """
    # Pipeline: load history → crawl each source in turn → type post-filter
    #           → stamp first_seen / is_new → save merged history → serialize
    previously_seen = load_seen_urls()
    types_set: Optional[set[str]] = set(types) if types else None

    # Sources run one after another (not in parallel) to keep request rates low
    assets: list[VRChatAsset] = []
    if source in ("all", "jinxxy"):
        assets += crawl_jinxxy(max_pages=max_pages, types=types_set, progress_cb=progress_cb)
    if source in ("all", "gumroad"):
        assets += crawl_gumroad(max_pages=max_pages, types=types_set, progress_cb=progress_cb)
    if source in ("all", "booth"):
        assets += crawl_booth(max_pages=max_pages, types=types_set, progress_cb=progress_cb)
    if source in ("all", "payhip"):
        assets += crawl_payhip(max_pages=max_pages, types=types_set, progress_cb=progress_cb)

    # Post-filter: keep only requested types. Search queries and categories are
    # approximate, so a "shader" query can still return items classified as clothing.
    if types_set:
        assets = [a for a in assets if a.asset_type in types_set]

    # first_seen is the time of THIS crawl, not when the URL was first seen historically
    # (seen_urls.json stores URLs only, no timestamps)
    now = datetime.now(timezone.utc).isoformat()
    all_urls: set[str] = set()
    for a in assets:
        a.first_seen = now
        a.is_new = a.url not in previously_seen
        all_urls.add(a.url)

    # Merge, don't replace: a narrow crawl (one source/type) mustn't forget other URLs
    save_seen_urls(previously_seen | all_urls)

    new_count = sum(1 for a in assets if a.is_new)
    if progress_cb:
        progress_cb(f"New items this run: {new_count} of {len(assets)}")

    return [asdict(a) for a in assets]
