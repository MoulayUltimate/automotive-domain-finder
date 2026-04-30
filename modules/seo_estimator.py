"""
seo_estimator.py — Step 5: Estimate SEO value WITHOUT SEMrush

Sources used (all free or freemium):
  1. Wayback Machine CDX API  — snapshot count, first seen date
  2. OpenPageRank API         — free domain authority proxy
  3. Majestic API (optional)  — citation flow / trust flow
  4. Moz API (optional)       — Domain Authority / Page Authority
  5. CommonCrawl index check  — presence in CC means crawlers found it
  6. DuckDuckGo "site:" check — rough index presence via DDG HTML
  7. Bing "site:" check       — confirm index presence
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote_plus
import requests
from bs4 import BeautifulSoup

import config
from modules.utils import get_logger, make_session, safe_get

logger = get_logger("seo_estimator")


# ── 1. Wayback Machine CDX API ────────────────────────────────────────────────

def _wayback_data(domain: str) -> dict:
    """
    Query CDX API for snapshot count and first capture date.
    Returns {"snapshots": int, "first_seen": str | None, "last_seen": str | None}
    """
    params = {
        "url": domain,
        "matchType": "domain",
        "output": "json",
        "fl": "timestamp",
        "limit": "500",       # cap to avoid huge responses
        "collapse": "timestamp:8",  # 1 per day de-dupe
        "filter": "statuscode:200",
    }
    session = make_session()
    resp = safe_get(config.WAYBACK_CDX_URL, session, params=params)
    if not resp:
        return {"snapshots": 0, "first_seen": None, "last_seen": None}
    try:
        rows = resp.json()
        # First row is header ["timestamp"]
        timestamps = [r[0] for r in rows[1:] if r]
        if not timestamps:
            return {"snapshots": 0, "first_seen": None, "last_seen": None}
        return {
            "snapshots": len(timestamps),
            "first_seen": timestamps[0][:8],   # YYYYMMDD
            "last_seen": timestamps[-1][:8],
        }
    except Exception:
        return {"snapshots": 0, "first_seen": None, "last_seen": None}


# ── 2. OpenPageRank (completely free) ─────────────────────────────────────────

def _openpagerank(domains: list[str]) -> dict[str, dict]:
    """
    Batch query OpenPageRank for up to 100 domains.
    Returns {domain: {"page_rank_integer": int, "page_rank_decimal": float}}
    """
    if not config.OPENPAGERANK_API_KEY:
        return {}
    # API supports up to 100 domains per call
    results = {}
    chunk_size = 100
    session = make_session()

    for i in range(0, len(domains), chunk_size):
        chunk = domains[i : i + chunk_size]
        params = [("domains[]", d) for d in chunk]
        try:
            resp = session.get(
                "https://openpagerank.com/api/v1.0/getPageRank",
                params=params,
                headers={"API-OPR": config.OPENPAGERANK_API_KEY},
                timeout=config.REQUEST_TIMEOUT,
            )
            data = resp.json()
            for entry in data.get("response", []):
                d = entry.get("domain", "")
                if d:
                    results[d] = {
                        "page_rank_integer": entry.get("page_rank_integer", 0),
                        "page_rank_decimal": entry.get("page_rank_decimal", 0.0),
                    }
        except Exception as e:
            logger.warning("OpenPageRank error: %s", e)
        time.sleep(0.3)

    return results


# ── 3. Majestic API (optional free tier) ──────────────────────────────────────

def _majestic_bulk(domains: list[str]) -> dict[str, dict]:
    """
    Fetch Citation Flow / Trust Flow via Majestic free API.
    Returns {domain: {"citation_flow": int, "trust_flow": int, "backlinks": int}}
    """
    if not config.MAJESTIC_API_KEY:
        return {}
    results = {}
    # Majestic free tier: GetBulkBacklinkData, max 1000 items
    chunk_size = 100
    session = make_session()

    for i in range(0, len(domains), chunk_size):
        chunk = domains[i : i + chunk_size]
        items = "&".join(f"item{j}={d}" for j, d in enumerate(chunk))
        url = (
            f"https://api.majestic.com/api/json?"
            f"app_api_key={config.MAJESTIC_API_KEY}"
            f"&cmd=GetBulkBacklinkData&datasource=fresh"
            f"&Count={len(chunk)}&{items}"
        )
        resp = safe_get(url, session)
        if not resp:
            continue
        try:
            data = resp.json()
            for item in data.get("DataTables", {}).get("Results", {}).get("Data", []):
                d = item.get("Item", "")
                if d:
                    results[d] = {
                        "citation_flow": item.get("CitationFlow", 0),
                        "trust_flow": item.get("TrustFlow", 0),
                        "backlinks": item.get("ExtBackLinks", 0),
                        "ref_domains": item.get("RefDomains", 0),
                    }
        except Exception as e:
            logger.warning("Majestic error: %s", e)
        time.sleep(1)

    return results


# ── 4. Moz API (optional free tier) ───────────────────────────────────────────

def _moz_domain_authority(domain: str) -> dict:
    """
    Fetch Domain Authority via Moz API v2 (Links API).
    Requires MOZ_ACCESS_ID and MOZ_SECRET_KEY in config.
    """
    if not (config.MOZ_ACCESS_ID and config.MOZ_SECRET_KEY):
        return {}

    url = "https://lsapi.seomoz.com/v2/url_metrics"
    session = make_session()
    try:
        resp = session.post(
            url,
            auth=(config.MOZ_ACCESS_ID, config.MOZ_SECRET_KEY),
            json={"targets": [f"https://{domain}/"]},
            timeout=config.REQUEST_TIMEOUT,
        )
        data = resp.json()
        results = data.get("results", [{}])
        if results:
            return {
                "domain_authority": results[0].get("domain_authority", 0),
                "page_authority": results[0].get("page_authority", 0),
                "linking_domains": results[0].get("linking_domains", 0),
            }
    except Exception as e:
        logger.warning("Moz error for %s: %s", domain, e)
    return {}


# ── 5. Index presence check (DuckDuckGo "site:") ─────────────────────────────

def _is_indexed(domain: str) -> bool:
    """
    Check if the domain has any indexed pages via DuckDuckGo site: search.
    Returns True if results are found.
    """
    query = f"site:{domain}"
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    session = make_session()
    resp = safe_get(url, session)
    if not resp:
        return False
    # If DDG shows no results it usually says "No results found"
    text = resp.text.lower()
    if "no results found" in text:
        return False
    soup = BeautifulSoup(resp.text, "html.parser")
    results = soup.select("a.result__url, .result__title")
    return len(results) > 0


# ── 6. CommonCrawl presence (heuristic via index API) ─────────────────────────

def _in_commoncrawl(domain: str) -> bool:
    """
    Check if domain appears in CommonCrawl latest index via CC Index API.
    """
    cc_index = "https://index.commoncrawl.org/CC-MAIN-2024-10-index"
    params = {
        "url": f"*.{domain}",
        "output": "json",
        "limit": "1",
        "fl": "url",
    }
    session = make_session()
    resp = safe_get(cc_index, session, params=params)
    if not resp:
        return False
    return bool(resp.text.strip())


# ── Main estimator ─────────────────────────────────────────────────────────────

def estimate_seo(domain: str, opr_cache: dict | None = None, majestic_cache: dict | None = None) -> dict:
    """
    Gather all available SEO signals for one domain.
    Returns a dict of raw signals (scoring happens in scorer.py).
    """
    opr_cache = opr_cache or {}
    majestic_cache = majestic_cache or {}

    signals: dict = {"domain": domain}

    # Wayback
    wb = _wayback_data(domain)
    signals.update(
        wayback_snapshots=wb["snapshots"],
        wayback_first_seen=wb["first_seen"],
        wayback_last_seen=wb["last_seen"],
        has_archive_history=wb["snapshots"] >= config.WAYBACK_MIN_SNAPSHOTS,
    )

    # OpenPageRank (from pre-loaded cache)
    opr = opr_cache.get(domain, {})
    signals["page_rank_integer"] = opr.get("page_rank_integer", 0)
    signals["page_rank_decimal"] = opr.get("page_rank_decimal", 0.0)

    # Majestic (from pre-loaded cache)
    maj = majestic_cache.get(domain, {})
    signals["citation_flow"] = maj.get("citation_flow", 0)
    signals["trust_flow"] = maj.get("trust_flow", 0)
    signals["backlinks"] = maj.get("backlinks", 0)
    signals["ref_domains"] = maj.get("ref_domains", 0)

    # Moz (individual call — only if configured)
    moz = _moz_domain_authority(domain)
    signals["domain_authority"] = moz.get("domain_authority", 0)
    signals["moz_linking_domains"] = moz.get("linking_domains", 0)

    # Index & crawl presence
    signals["is_indexed_ddg"] = _is_indexed(domain)
    signals["in_commoncrawl"] = _in_commoncrawl(domain)

    logger.debug("SEO signals for %s: %s", domain, signals)
    return signals


def estimate_seo_bulk(domains: list[str], workers: int | None = None) -> list[dict]:
    """
    Batch SEO estimation with pre-loaded OPR + Majestic caches.
    """
    workers = workers or config.MAX_WORKERS

    # Pre-load batch APIs
    logger.info("Pre-loading OpenPageRank data for %d domains…", len(domains))
    opr_cache = _openpagerank(domains)

    logger.info("Pre-loading Majestic data for %d domains…", len(domains))
    majestic_cache = _majestic_bulk(domains)

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(estimate_seo, d, opr_cache, majestic_cache): d
            for d in domains
        }
        for future in as_completed(futures):
            d = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                logger.error("SEO estimation error for %s: %s", d, exc)
                results.append({"domain": d})

    return results
