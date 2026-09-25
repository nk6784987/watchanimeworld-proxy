"""
app.py — WatchAnimeWorld Scraper Proxy API
--------------------------------------------
Production-ready Flask backend that scrapes anime listing/detail data from
https://watchanimeworld.one and exposes it as a clean JSON API.

Designed to run both locally (via `python app.py` / gunicorn) and as a
Vercel Python Serverless Function (via api/index.py).
"""

import re
import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS
from cachetools import TTLCache

# ---------------------------------------------------------------------------
# App / Logging setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)  # Allow all origins — this is a public read-only proxy API

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("watchanimeworld-proxy")

BASE_URL = "https://watchanimeworld.one"

# ---------------------------------------------------------------------------
# Cache — in-memory TTL cache (15 minutes). NOTE: on Vercel each serverless
# invocation may run in a fresh container, so this cache is best-effort and
# mainly helps warm/concurrent requests within the same execution context.
# ---------------------------------------------------------------------------

CACHE_TTL_SECONDS = 15 * 60
cache = TTLCache(maxsize=256, ttl=CACHE_TTL_SECONDS)

# ---------------------------------------------------------------------------
# HTTP session with realistic browser-like headers to reduce the chance of
# being blocked by Cloudflare / basic bot detection.
# ---------------------------------------------------------------------------

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Referer": BASE_URL + "/",
    "Connection": "keep-alive",
}

session = requests.Session()
session.headers.update(DEFAULT_HEADERS)

REQUEST_TIMEOUT = 15  # seconds


def fetch_html(url: str) -> BeautifulSoup:
    """
    Fetch a URL through the shared session and return a parsed BeautifulSoup
    document. Raises requests.RequestException on network / HTTP failure.
    """
    resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def fix_url(url: str, base: str = BASE_URL) -> str:
    """Normalize a possibly-relative or protocol-relative URL to https absolute."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    if url.startswith("http"):
        return url
    return urljoin(base, url)


def extract_image(tag) -> str:
    """
    Poster images on WP anime themes are frequently lazy-loaded, so check a
    range of common attributes before falling back to `src`.
    """
    if tag is None:
        return ""
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        val = tag.get(attr)
        if val and not val.startswith("data:image"):
            return fix_url(val)
    # srcset fallback — take the first candidate
    srcset = tag.get("srcset") or tag.get("data-srcset")
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        return fix_url(first)
    return ""


# ---------------------------------------------------------------------------
# Scraping logic
# ---------------------------------------------------------------------------

# Candidate container selectors seen across common WordPress anime/movie
# theme families (e.g. Animeku/AnymeX-style themes). We try each in order
# and use whichever yields results, so the scraper degrades gracefully if
# the theme markup changes slightly.
CONTAINER_SELECTORS = [
    ".listupd .bs",
    ".listupd article",
    ".listupd .animposx",
    ".listupd",
    ".animposx",
    "article.bs",
    "article",
    ".bs",
    ".post",
    ".result",
]


def _find_items(soup: BeautifulSoup):
    """Try each known container selector until one returns items."""
    for selector in CONTAINER_SELECTORS:
        items = soup.select(selector)
        if items:
            return items
    return []


def parse_card(tag) -> dict:
    """Parse a single anime 'card' element into a normalized dict."""
    # Link + title
    link_tag = tag.select_one("a[href]")
    link = fix_url(link_tag.get("href")) if link_tag else ""

    title = ""
    title_tag = (
        tag.select_one(".tt")
        or tag.select_one("h2")
        or tag.select_one("h3")
        or tag.select_one(".title")
    )
    if title_tag:
        title = title_tag.get_text(strip=True)
    elif link_tag:
        title = link_tag.get("title", "").strip() or link_tag.get_text(strip=True)

    # Image
    img_tag = tag.select_one("img")
    image = extract_image(img_tag)

    # Status / episode badge (SUB, DUB, Episode 12, etc.)
    status = ""
    status_tag = (
        tag.select_one(".status")
        or tag.select_one(".epx")
        or tag.select_one(".ep")
        or tag.select_one(".bt")
    )
    if status_tag:
        status = status_tag.get_text(" ", strip=True)

    # Description / synopsis excerpt, when present on listing cards
    description = ""
    desc_tag = tag.select_one(".excerpt") or tag.select_one("p")
    if desc_tag:
        description = desc_tag.get_text(" ", strip=True)

    if not title or not link:
        return None

    return {
        "title": title,
        "image": image,
        "link": link,
        "status": status,
        "description": description,
    }


def scrape_trending(search: str = None) -> list:
    """Scrape the trending/listing page, or a search results page."""
    if search:
        url = f"{BASE_URL}/?s={requests.utils.quote(search)}"
    else:
        url = BASE_URL + "/"

    soup = fetch_html(url)
    items = _find_items(soup)

    results = []
    seen_links = set()
    for tag in items:
        try:
            card = parse_card(tag)
        except Exception as exc:  # noqa: BLE001 — keep scraping resilient
            logger.warning("Failed to parse a card: %s", exc)
            continue
        if card and card["link"] not in seen_links:
            seen_links.add(card["link"])
            results.append(card)

    return results


def scrape_details(url: str) -> dict:
    """Scrape an anime/episode detail page for synopsis, episodes, and player embed."""
    soup = fetch_html(url)

    title_tag = soup.select_one("h1") or soup.select_one(".entry-title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    poster_tag = soup.select_one(".thumb img") or soup.select_one(".poster img") or soup.select_one("img")
    image = extract_image(poster_tag)

    # Synopsis: common containers for the description block
    synopsis = ""
    synopsis_tag = (
        soup.select_one(".entry-content")
        or soup.select_one(".synp .entry-content")
        or soup.select_one(".desc")
        or soup.select_one(".synopsis")
    )
    if synopsis_tag:
        synopsis = synopsis_tag.get_text(" ", strip=True)

    # Episode list — look for common episode-list containers/links
    episodes = []
    episode_containers = soup.select(".eplister li a") or soup.select(".episodelist li a") or soup.select("a[href*='episode']")
    seen_ep_links = set()
    for a in episode_containers:
        href = a.get("href")
        if not href:
            continue
        href = fix_url(href)
        if href in seen_ep_links:
            continue
        seen_ep_links.add(href)
        ep_title = a.get_text(" ", strip=True) or a.get("title", "").strip()
        num_tag = a.select_one(".epl-num")
        episodes.append({
            "title": ep_title or f"Episode",
            "number": num_tag.get_text(strip=True) if num_tag else "",
            "link": href,
        })

    # Iframe stream player — search main content, then whole page as fallback
    iframe_url = ""
    iframe_tag = soup.select_one("iframe[src]") or soup.select_one("iframe[data-src]")
    if iframe_tag:
        iframe_url = fix_url(iframe_tag.get("src") or iframe_tag.get("data-src"))
    else:
        # Some themes hide the player URL inside a script; do a light regex scan
        script_text = soup.get_text()
        match = re.search(r'(https?:)?//[^\s"\']+\.(m3u8|mp4)[^\s"\']*', str(soup))
        if match:
            iframe_url = fix_url(match.group(0))

    return {
        "title": title,
        "image": image,
        "synopsis": synopsis,
        "episodes": episodes,
        "player_url": iframe_url,
        "source_url": url,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "status": "online",
        "service": "watchanimeworld-scraper-proxy",
        "endpoints": ["/", "/api/anime", "/api/details"],
    })


@app.route("/api/anime", methods=["GET"])
def get_anime():
    search = request.args.get("search", "").strip()
    cache_key = f"anime:{search.lower()}" if search else "anime:trending"

    if cache_key in cache:
        return jsonify({
            "cached": True,
            "count": len(cache[cache_key]),
            "results": cache[cache_key],
        })

    try:
        results = scrape_trending(search=search or None)
    except requests.RequestException as exc:
        logger.error("Scrape failure on /api/anime: %s", exc)
        return jsonify({
            "error": "Failed to fetch data from source site.",
            "detail": str(exc),
        }), 502

    cache[cache_key] = results
    return jsonify({
        "cached": False,
        "count": len(results),
        "results": results,
    })


@app.route("/api/details", methods=["GET"])
def get_details():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing required query parameter: url"}), 400

    # Basic safety check — only allow scraping the target domain
    if "watchanimeworld.one" not in url:
        return jsonify({"error": "URL must be from watchanimeworld.one"}), 400

    url = fix_url(url)
    cache_key = f"details:{url}"

    if cache_key in cache:
        return jsonify({"cached": True, **cache[cache_key]})

    try:
        details = scrape_details(url)
    except requests.RequestException as exc:
        logger.error("Scrape failure on /api/details: %s", exc)
        return jsonify({
            "error": "Failed to fetch details from source site.",
            "detail": str(exc),
        }), 502

    cache[cache_key] = details
    return jsonify({"cached": False, **details})


# ---------------------------------------------------------------------------
# Global error handlers — always return JSON, never raw HTML error pages
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "Not found", "detail": str(e)}), 404


@app.errorhandler(500)
def handle_500(e):
    return jsonify({"error": "Internal server error", "detail": str(e)}), 500


@app.errorhandler(Exception)
def handle_uncaught(e):
    logger.exception("Uncaught exception")
    return jsonify({"error": "Unexpected server error", "detail": str(e)}), 500


# ---------------------------------------------------------------------------
# Local dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
