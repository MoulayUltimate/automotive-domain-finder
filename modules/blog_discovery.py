"""
blog_discovery.py — Step 1: Build a seed list of authority automotive blogs

Strategy (no paid API required):
  1. Load hardcoded seed list from data/seed_blogs.txt
  2. DuckDuckGo HTML scraping (falls back gracefully if blocked)
  3. Google Custom Search API if keys are configured
  4. Wikipedia automotive media list
  5. Reddit r/cars / r/autos "wiki" / sidebar scraping
"""

import re
import time
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

import config
from modules.utils import get_logger, load_lines, make_session, normalise_domain, safe_get

logger = get_logger("blog_discovery")

# Queries to use for DuckDuckGo / Google search
SEARCH_QUERIES = [
    "top car blogs USA",
    "best automotive websites USA 2024",
    "car news sites North America",
    "top automotive review blogs",
    "best car enthusiast blogs",
    "top Canadian car blogs",
    "best truck and SUV news sites",
    "top EV electric car blogs",
    "best classic car blogs USA",
    "motorsport news websites USA",
    "car modification blogs North America",
    "best drag racing news sites",
    "auto detailing blogs USA",
    "best off-road truck websites",
]

# Wikipedia pages known to list automotive media
WIKIPEDIA_PAGES = [
    "https://en.wikipedia.org/wiki/List_of_automotive_magazines",
    "https://en.wikipedia.org/wiki/List_of_car-related_websites",
]

# Reddit pages
REDDIT_WIKI_PAGES = [
    "https://www.reddit.com/r/cars/wiki/index",
    "https://www.reddit.com/r/Autos/wiki/index",
    "https://old.reddit.com/r/cars/",
    "https://old.reddit.com/r/autos/",
]


# ── DuckDuckGo HTML scraping ───────────────────────────────────────────────────

def _ddg_search(query: str, max_results: int = 15) -> list[str]:
    """
    Scrape DuckDuckGo HTML results.  Returns list of result URLs.
    DDG does not require an API key and is generally bot-tolerant.
    """
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    session = make_session()
    resp = safe_get(url, session, headers={"Accept-Language": "en-US,en;q=0.9"})
    if not resp:
        logger.warning("DDG: no response for query '%s'", query)
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    urls = []
    for a in soup.select("a.result__url"):
        href = a.get("href", "")
        d = normalise_domain(href)
        if d:
            urls.append(d)
        if len(urls) >= max_results:
            break
    logger.info("DDG '%s' → %d results", query, len(urls))
    return urls


# ── Google Custom Search API ───────────────────────────────────────────────────

def _google_search(query: str, max_results: int = 10) -> list[str]:
    if not (config.GOOGLE_API_KEY and config.GOOGLE_CX):
        return []
    url = (
        "https://www.googleapis.com/customsearch/v1"
        f"?key={config.GOOGLE_API_KEY}&cx={config.GOOGLE_CX}"
        f"&q={quote_plus(query)}&num={min(max_results, 10)}"
    )
    session = make_session()
    resp = safe_get(url, session)
    if not resp:
        return []
    data = resp.json()
    domains = []
    for item in data.get("items", []):
        d = normalise_domain(item.get("link", ""))
        if d:
            domains.append(d)
    logger.info("Google CSE '%s' → %d results", query, len(domains))
    return domains


# ── Wikipedia ─────────────────────────────────────────────────────────────────

def _scrape_wikipedia() -> list[str]:
    domains = []
    session = make_session()
    for wiki_url in WIKIPEDIA_PAGES:
        resp = safe_get(wiki_url, session)
        if not resp:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.select("a[href]"):
            href = a["href"]
            # Wikipedia external links are like //example.com or https://
            if href.startswith("//"):
                href = "https:" + href
            d = normalise_domain(href)
            if d and "wikipedia" not in d and "wikimedia" not in d:
                domains.append(d)
    logger.info("Wikipedia → %d candidate domains", len(domains))
    return domains


# ── Reddit ─────────────────────────────────────────────────────────────────────

def _scrape_reddit() -> list[str]:
    domains = []
    session = make_session()
    session.headers["Accept"] = "application/json"
    for page_url in REDDIT_WIKI_PAGES:
        # Try JSON API first
        json_url = page_url.rstrip("/") + ".json"
        resp = safe_get(json_url, session)
        if resp:
            try:
                data = resp.json()
                # Wiki pages return content_md
                md = ""
                if "data" in data:
                    md = data["data"].get("content_md", "")
                    if not md:
                        # Listings: look for link domains in posts
                        children = data["data"].get("children", [])
                        for child in children[:30]:
                            url = child.get("data", {}).get("url", "")
                            d = normalise_domain(url)
                            if d:
                                domains.append(d)
                        continue
                # Extract URLs from markdown
                for match in re.findall(r"https?://[^\s\)\]\"]+", md):
                    d = normalise_domain(match)
                    if d:
                        domains.append(d)
                continue
            except Exception:
                pass
        # Fallback: scrape HTML
        resp = safe_get(page_url, make_session())
        if not resp:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.select("a[href]"):
            href = a["href"]
            if "reddit.com" not in href and href.startswith("http"):
                d = normalise_domain(href)
                if d:
                    domains.append(d)
    logger.info("Reddit → %d candidate domains", len(domains))
    return domains


# ── Main discovery function ────────────────────────────────────────────────────

def discover_blogs(max_blogs: int = 300) -> list[str]:
    """
    Returns a deduplicated list of automotive blog domains (no scheme).
    Combines seed list + search scraping + Wikipedia + Reddit.
    """
    seen: set[str] = set()
    results: list[str] = []

    def add(d: str | None) -> None:
        if d and d not in seen:
            seen.add(d)
            results.append(d)

    # 1. Seed file
    for raw in load_lines(config.SEED_BLOGS_FILE):
        d = normalise_domain(raw)
        add(d)
    logger.info("Seed list loaded: %d blogs", len(results))

    # 2. Search engines
    for query in SEARCH_QUERIES:
        for d in _ddg_search(query):
            add(d)
        for d in _google_search(query):
            add(d)
        if len(results) >= max_blogs:
            break
        time.sleep(0.5)

    # 3. Wikipedia
    for d in _scrape_wikipedia():
        add(d)

    # 4. Reddit
    for d in _scrape_reddit():
        add(d)

    logger.info("Total discovered blogs (before quality filter): %d", len(results))

    # Light quality filter: keep only domains that look like real websites
    # (not CDN nodes, not bare IPs, at least one known TLD)
    final = [d for d in results if _looks_like_website(d)]
    final = final[:max_blogs]
    logger.info("Final blog list: %d", len(final))
    return final


def _looks_like_website(domain: str) -> bool:
    if not domain:
        return False
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", domain):   # bare IP
        return False
    parts = domain.split(".")
    if len(parts) < 2:
        return False
    tld = parts[-1]
    if len(tld) < 2 or len(tld) > 6:
        return False
    return True
