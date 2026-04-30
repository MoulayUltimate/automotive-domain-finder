"""
domain_filter.py — Step 3: Keep only automotive-relevant domains

Two-pass filter:
  Pass 1 — Fast: keyword scan on domain name itself
  Pass 2 — Slow: fetch the page (or its Wayback snapshot) and scan
            title + meta description + h1 for automotive keywords
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup

import config
from modules.utils import get_logger, make_session, safe_get

logger = get_logger("domain_filter")

# Pre-compiled keyword regex (word-boundary aware to avoid false matches)
_KW_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in config.AUTOMOTIVE_KEYWORDS) + r")\b",
    re.I,
)


def _domain_name_score(domain: str) -> int:
    """Count automotive keyword hits in domain name (ignoring TLD)."""
    stem = domain.split(".")[0]  # e.g. "motortrend" from "motortrend.com"
    return len(_KW_PATTERN.findall(stem))


def _page_content_score(domain: str) -> int:
    """
    Fetch the live page (or a Wayback snapshot) and scan text.
    Returns count of automotive keyword hits in title + meta + h1.
    Returns 0 on failure.
    """
    session = make_session()
    for url in (f"https://{domain}", f"http://{domain}"):
        resp = safe_get(url, session)
        if resp:
            break
    else:
        # Try Wayback most-recent snapshot
        wb_url = f"https://archive.org/wayback/available?url={domain}"
        wb_resp = safe_get(wb_url, make_session())
        if wb_resp:
            try:
                snap = wb_resp.json()
                closest = snap.get("archived_snapshots", {}).get("closest", {})
                if closest.get("available"):
                    resp = safe_get(closest["url"], make_session())
                else:
                    return 0
            except Exception:
                return 0
        else:
            return 0

    if not resp:
        return 0

    soup = BeautifulSoup(resp.text, "html.parser")
    text_parts = []
    if soup.title:
        text_parts.append(soup.title.get_text())
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"description|keywords", re.I)}):
        text_parts.append(meta.get("content", ""))
    for h1 in soup.find_all("h1"):
        text_parts.append(h1.get_text())

    combined = " ".join(text_parts)
    return len(_KW_PATTERN.findall(combined))


def _is_automotive(domain: str, slow_check: bool = False) -> tuple[bool, int]:
    """
    Returns (is_automotive, relevance_score).
    Fast path: domain name keywords.
    Slow path: fetch page content.
    """
    name_score = _domain_name_score(domain)
    if name_score >= 1:
        return True, name_score * 20   # domain name alone is strong signal

    if not slow_check:
        return False, 0

    page_score = _page_content_score(domain)
    if page_score >= 2:
        return True, min(page_score * 5, 40)

    return False, 0


def filter_automotive(
    domains: list[str],
    slow_check: bool = True,
    workers: int | None = None,
) -> list[tuple[str, int]]:
    """
    Filter down to automotive-relevant domains.

    Returns list of (domain, relevance_score) sorted by score desc.
    If slow_check=True, fetches pages for domains that didn't pass the fast
    keyword check (slower but catches non-obvious domains).
    """
    workers = workers or config.MAX_WORKERS

    # Fast pass — instant
    fast_pass: list[tuple[str, int]] = []
    slow_candidates: list[str] = []
    for d in domains:
        is_auto, score = _is_automotive(d, slow_check=False)
        if is_auto:
            fast_pass.append((d, score))
        else:
            slow_candidates.append(d)

    logger.info(
        "Fast pass: %d automotive / %d slow candidates",
        len(fast_pass),
        len(slow_candidates),
    )

    if not slow_check:
        logger.info("Slow check disabled — returning %d domains", len(fast_pass))
        return sorted(fast_pass, key=lambda x: -x[1])

    # Slow pass — concurrent fetch
    slow_results: list[tuple[str, int]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_page_content_score, d): d for d in slow_candidates}
        for future in as_completed(futures):
            d = futures[future]
            try:
                page_score = future.result()
                if page_score >= 2:
                    slow_results.append((d, min(page_score * 5, 40)))
            except Exception as exc:
                logger.debug("Error checking %s: %s", d, exc)

    logger.info("Slow pass added %d more automotive domains", len(slow_results))
    combined = fast_pass + slow_results
    return sorted(combined, key=lambda x: -x[1])
