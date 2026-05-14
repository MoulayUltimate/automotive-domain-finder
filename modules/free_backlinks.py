"""
free_backlinks.py — Free backlink-authority data, used when no Majestic key is set.

Source: Majestic Million — a daily-updated CSV of the top 1M domains ranked
by unique referring subnets. Free, no API key required.

    https://downloads.majestic.com/majestic_million.csv

Why this matters for our pipeline:
  • RefSubNets is a hard-to-spoof backlink-authority proxy (counts unique /24
    blocks linking in, not raw URL count). Free tier Majestic doesn't expose
    Trust Flow / Citation Flow, but RefSubNets correlates strongly with both.
  • Most expired-domain candidates won't be in the top 1M → and that's fine:
    "in the list at all" is itself a strong positive signal (real authority).

Strategy:
  • Lazy-download the CSV to /tmp on first call (warm-container amortized).
  • Cache the parsed dict in-memory; refresh after 24h.
  • Fail soft: any network/parse error → return empty dict, pipeline continues
    with zeros (same behaviour as a missing paid Majestic key).

Mapping to existing pipeline fields:
    ref_domains    ← RefSubNets
    backlinks      ← RefSubNets × 20  (Majestic typically shows ~10-50× more
                                       raw backlinks than ref subnets)
    trust_flow     ← derived from GlobalRank (0-100 scale)
    citation_flow  ← derived from RefSubNets bucket
"""

from __future__ import annotations

import csv
import logging
import os
import threading
import time
import requests

logger = logging.getLogger("free_backlinks")

MAJESTIC_MILLION_URL = "https://downloads.majestic.com/majestic_million.csv"
CACHE_PATH = "/tmp/majestic_million.csv"
CACHE_TTL_SECONDS = 24 * 3600           # refresh daily
DOWNLOAD_TIMEOUT  = 25                  # seconds — must fit inside Vercel's 60s budget

_lock = threading.Lock()
_INDEX: dict[str, dict] | None = None
_LOAD_ERROR: str = ""


# ── Download + parse ─────────────────────────────────────────────────────────

def _download_if_stale() -> bool:
    """Download CSV if missing or older than CACHE_TTL_SECONDS. Returns True if file is usable."""
    if os.path.exists(CACHE_PATH):
        age = time.time() - os.path.getmtime(CACHE_PATH)
        if age < CACHE_TTL_SECONDS:
            return True

    logger.info("Downloading Majestic Million CSV → %s", CACHE_PATH)
    try:
        # requests auto-decompresses gzip/deflate from the server
        resp = requests.get(
            MAJESTIC_MILLION_URL,
            headers={"User-Agent": "automotive-domain-finder/1.0"},
            timeout=DOWNLOAD_TIMEOUT,
            stream=True,
        )
        resp.raise_for_status()
        tmp_path = CACHE_PATH + ".part"
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
        os.replace(tmp_path, CACHE_PATH)
        return True
    except Exception as e:
        logger.warning("Majestic Million download failed: %s", e)
        # If we already have a stale file, keep using it
        return os.path.exists(CACHE_PATH)


def _build_index() -> dict[str, dict]:
    """Parse CSV into {domain: {global_rank, ref_subnets, ref_ips, tld}}."""
    global _LOAD_ERROR
    out: dict[str, dict] = {}
    if not _download_if_stale():
        _LOAD_ERROR = "download_failed"
        return out
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                d = (row.get("Domain") or "").lower().strip()
                if not d:
                    continue
                try:
                    rank = int(row.get("GlobalRank") or 0)
                    rsn  = int(row.get("RefSubNets") or 0)
                    rips = int(row.get("RefIPs") or 0)
                except (TypeError, ValueError):
                    continue
                out[d] = {
                    "global_rank": rank,
                    "ref_subnets": rsn,
                    "ref_ips":     rips,
                    "tld":         (row.get("TLD") or "").lower(),
                }
        _LOAD_ERROR = ""
    except Exception as e:
        logger.warning("Majestic Million parse failed: %s", e)
        _LOAD_ERROR = f"parse:{type(e).__name__}"
    logger.info("Majestic Million index loaded: %d domains", len(out))
    return out


def get_index() -> dict[str, dict]:
    """Lazy-loaded singleton (per-container)."""
    global _INDEX
    if _INDEX is None:
        with _lock:
            if _INDEX is None:
                _INDEX = _build_index()
    return _INDEX


# ── Public lookup API ────────────────────────────────────────────────────────

def _derive(rank: int, rsn: int) -> dict:
    """Convert (rank, ref_subnets) → Majestic-compatible signal bundle."""
    # Trust Flow approximation by global-rank bucket
    if rank <= 1_000:
        tf = 78
    elif rank <= 10_000:
        tf = 58
    elif rank <= 100_000:
        tf = 38
    elif rank <= 500_000:
        tf = 22
    else:
        tf = 10

    # Citation Flow approximation by ref-subnet count
    if rsn >= 10_000:
        cf = 80
    elif rsn >= 1_000:
        cf = 60
    elif rsn >= 100:
        cf = 40
    elif rsn >= 25:
        cf = 25
    elif rsn >= 5:
        cf = 12
    else:
        cf = 5

    return {
        "backlinks":        rsn * 20,    # Majestic's raw backlinks ≈ 10-50× ref_subnets
        "ref_domains":      rsn,
        "trust_flow":       tf,
        "citation_flow":    cf,
        "global_rank":      rank,
        "backlinks_source": "free:majestic_million",
    }


def lookup(domain: str) -> dict:
    """Return Majestic-compatible signals for one domain (zeros if not in list)."""
    rec = get_index().get(domain.lower())
    if not rec:
        return {
            "backlinks":        0,
            "ref_domains":      0,
            "trust_flow":       0,
            "citation_flow":    0,
            "global_rank":      0,
            "backlinks_source": "free:none",
        }
    return _derive(rec["global_rank"], rec["ref_subnets"])


def lookup_bulk(domains: list[str]) -> dict[str, dict]:
    return {d: lookup(d) for d in domains}


def search_by_keyword(
    keyword: str,
    limit: int = 400,
    min_ref_subnets: int = 5,
    tlds: list[str] | None = None,
) -> list[dict]:
    """
    Find REAL domains in Majestic Million whose stem contains `keyword`.

    Every match is a domain that had measurable backlink authority at some
    point — so even if it's currently expired, it almost certainly had
    real traffic during its active years. This is the killer signal the
    keyword combo-generator can't produce.

    Returns rows sorted by ref_subnets descending (highest authority first):
        [{ "domain": "carshub.com", "ref_subnets": 1234, "global_rank": 5421, "tld": "com" }, ...]
    """
    kw = keyword.lower().strip()
    if not kw:
        return []

    tld_filter = {t.lstrip(".").lower() for t in tlds} if tlds else None
    hits: list[dict] = []
    for d, rec in get_index().items():
        # Domain stem = part before the first dot
        stem = d.split(".", 1)[0]
        if kw not in stem:
            continue
        if rec["ref_subnets"] < min_ref_subnets:
            continue
        if tld_filter and rec["tld"] not in tld_filter:
            continue
        hits.append({
            "domain":       d,
            "ref_subnets":  rec["ref_subnets"],
            "ref_ips":      rec["ref_ips"],
            "global_rank":  rec["global_rank"],
            "tld":          rec["tld"],
        })

    hits.sort(key=lambda r: -r["ref_subnets"])
    return hits[:limit]


def search_by_keywords(
    keywords: list[str],
    per_keyword_limit: int = 400,
    min_ref_subnets: int = 5,
    tlds: list[str] | None = None,
) -> dict:
    """
    Bulk version: searches multiple keywords, dedupes domains across keywords,
    and tags each hit with which keyword matched it first.

    Returns:
        {
          "candidates":   [{"domain": "...", "matched_keyword": "...", ...}, ...],
          "domains":      ["domain1", "domain2", ...],
          "keyword_map":  {"domain1": "kw1", ...},
          "total":        int,
        }
    """
    seen: set[str] = set()
    candidates: list[dict] = []
    for kw in keywords:
        for rec in search_by_keyword(kw, per_keyword_limit, min_ref_subnets, tlds):
            d = rec["domain"]
            if d in seen:
                continue
            seen.add(d)
            candidates.append({**rec, "matched_keyword": kw, "pattern": "majestic_million"})
    return {
        "candidates":   candidates,
        "domains":      [c["domain"] for c in candidates],
        "keyword_map":  {c["domain"]: c["matched_keyword"] for c in candidates},
        "total":        len(candidates),
    }


def status() -> dict:
    """Quick health check — used by /api/health and the UI status badge."""
    return {
        "loaded":     _INDEX is not None,
        "size":       len(_INDEX) if _INDEX else 0,
        "cache_path": CACHE_PATH,
        "cache_age":  (time.time() - os.path.getmtime(CACHE_PATH))
                      if os.path.exists(CACHE_PATH) else None,
        "error":      _LOAD_ERROR,
    }
