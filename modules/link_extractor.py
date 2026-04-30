"""
link_extractor.py — Step 2: Crawl each blog and extract outbound link domains

For each blog:
  - Fetch homepage
  - Find article links (heuristic: <a href> inside <main>, <article>, or
    links whose path looks like /YYYY/ or /post/ etc.)
  - Crawl up to MAX_PAGES_PER_BLOG pages
  - Extract ALL <a href> outbound links
  - Normalise to apex domains
  - Filter out internal + blacklisted domains
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import config
from modules.utils import (
    apex_domain,
    get_logger,
    is_internal,
    make_session,
    normalise_domain,
    safe_get,
)

logger = get_logger("link_extractor")

# Patterns that suggest a URL is an article page
ARTICLE_PATH_RE = re.compile(
    r"/(\d{4})/|/(post|article|blog|news|review|story|feature)/|"
    r"-review-|-vs-|-test-|-guide-",
    re.I,
)


def _get_article_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """
    Return internal article URLs found on the page.
    Looks for links with article-like path patterns.
    """
    base_host = urlparse(base_url).hostname or ""
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        abs_url = urljoin(base_url, href)
        host = urlparse(abs_url).hostname or ""
        # Must be same domain
        if base_host not in host and host not in base_host:
            continue
        path = urlparse(abs_url).path
        if ARTICLE_PATH_RE.search(path):
            links.append(abs_url)
    return list(dict.fromkeys(links))  # deduplicate, preserve order


def _extract_outbound_from_page(html: str, source_domain: str) -> set[str]:
    """Parse one page and return set of outbound apex domains."""
    soup = BeautifulSoup(html, "html.parser")
    out = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            continue
        d = normalise_domain(href)
        if not d:
            continue
        if d == normalise_domain(source_domain):
            continue  # internal
        if any(d == bl or d.endswith("." + bl) for bl in config.BLACKLISTED_DOMAINS):
            continue  # blacklisted
        out.add(d)
    return out


def crawl_blog(blog_domain: str) -> set[str]:
    """
    Crawl one blog.  Returns set of unique outbound apex domains found.
    """
    session = make_session(use_proxy=bool(config.PROXIES))
    base_url = f"https://{blog_domain}"
    all_outbound: set[str] = set()

    # --- Step A: fetch homepage ---
    resp = safe_get(base_url, session)
    if resp is None:
        # Try http fallback
        resp = safe_get(f"http://{blog_domain}", session)
    if resp is None:
        logger.warning("Unreachable: %s", blog_domain)
        return set()

    soup = BeautifulSoup(resp.text, "html.parser")
    all_outbound |= _extract_outbound_from_page(resp.text, blog_domain)

    # --- Step B: discover article pages ---
    article_urls = _get_article_links(soup, resp.url)
    # Also grab from common navigation patterns
    for a in soup.select("nav a[href], .menu a[href], header a[href]"):
        href = a["href"]
        abs_url = urljoin(resp.url, href)
        host = urlparse(abs_url).hostname or ""
        if blog_domain in host or host in blog_domain:
            article_urls.append(abs_url)

    article_urls = list(dict.fromkeys(article_urls))[: config.MAX_PAGES_PER_BLOG - 1]

    # --- Step C: crawl article pages ---
    for art_url in article_urls:
        art_resp = safe_get(art_url, session)
        if art_resp is None:
            continue
        all_outbound |= _extract_outbound_from_page(art_resp.text, blog_domain)
        logger.debug("  %s → %d outbound so far", art_url, len(all_outbound))

    logger.info("Crawled %s → %d outbound domains", blog_domain, len(all_outbound))
    return all_outbound


def extract_all_outbound(blogs: list[str]) -> dict[str, set[str]]:
    """
    Parallel crawl.  Returns {blog_domain: {outbound_domain, ...}}.
    """
    results: dict[str, set[str]] = {}
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        future_to_blog = {pool.submit(crawl_blog, b): b for b in blogs}
        for future in as_completed(future_to_blog):
            blog = future_to_blog[future]
            try:
                results[blog] = future.result()
            except Exception as exc:
                logger.error("Error crawling %s: %s", blog, exc)
                results[blog] = set()
    return results


def flatten_outbound(blog_map: dict[str, set[str]]) -> list[str]:
    """
    Merge all outbound domains into a single deduplicated list,
    sorted by how many source blogs link to them (popularity proxy).
    """
    freq: dict[str, int] = {}
    for domains in blog_map.values():
        for d in domains:
            freq[d] = freq.get(d, 0) + 1
    # Sort by frequency desc, then alphabetically
    return sorted(freq, key=lambda d: (-freq[d], d))
