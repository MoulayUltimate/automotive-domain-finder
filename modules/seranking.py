"""
seranking.py — SE Ranking backlinks-summary API client.

Docs: https://seranking.com/api/data/backlinks/

Why this module exists:
  • SE Ranking's `/backlinks/summary` returns `domain_inlink_rank` — a 0-100
    authority score comparable to Moz DA / Ahrefs DR, but driven by
    SE Ranking's own crawler. It's the strongest single-number quality
    signal we have access to.
  • Bulk-friendly: up to 100 domains per request → cheap per-domain cost.
  • Single API key, no OAuth dance.

Endpoint:
  POST https://api.seranking.com/v1/backlinks/summary
  Header: Authorization: Token <api_key>
  Body:   { "targets": ["a.com","b.com",...], "mode": "domain" }

We use this *after* the cheap RDAP availability pass, so we don't waste
credits on the full candidate pool — just on the small set the user
might actually buy.

Field name fallbacks: SE Ranking's response keys have varied across API
versions. We try the documented name first, then accept common aliases,
then default to 0. This keeps the integration resilient to spec drift.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

import requests

logger = logging.getLogger("seranking")

BASE_URL          = "https://api.seranking.com/v1"
SUMMARY_ENDPOINT  = f"{BASE_URL}/backlinks/summary"
BULK_BATCH_SIZE   = 100      # SE Ranking accepts up to 100 targets per call
DEFAULT_TIMEOUT   = 20       # seconds


def _pick(d: dict, *keys, default=0):
    """Return the first non-None value found among `keys` in dict `d`."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _normalize_row(row: dict) -> dict:
    """Map an SE Ranking summary row to the field names our pipeline expects."""
    return {
        "domain":              str(_pick(row, "domain", "target", "host", default="")).lower(),
        # SE Ranking's 0-100 authority score → reuses the scorer's domain_authority field
        "domain_authority":    int(_pick(row, "domain_inlink_rank", "domain_rank", "rank", default=0)),
        "page_authority":      int(_pick(row, "page_inlink_rank",   "page_rank",  default=0)),
        "backlinks":           int(_pick(row, "total_backlinks",    "backlinks",  default=0)),
        "ref_domains":         int(_pick(row, "referring_domains",  "ref_domains", default=0)),
        "ref_ips":             int(_pick(row, "referring_ips",      "ref_ips",     default=0)),
        "dofollow_backlinks":  int(_pick(row, "dofollow_backlinks", "dofollow",    default=0)),
        "nofollow_backlinks":  int(_pick(row, "nofollow_backlinks", "nofollow",    default=0)),
        "first_seen":          str(_pick(row, "first_seen", default="") or ""),
        "last_seen":           str(_pick(row, "last_seen",  default="") or ""),
        "_source":             "seranking",
    }


def _summary_batch(
    targets: list[str],
    api_key: str,
    mode: str = "domain",
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict]:
    """One POST for up to 100 targets. Returns normalized rows."""
    if not targets:
        return []
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type":  "application/json",
    }
    # SE Ranking has used both `targets` (bulk) and `target` (single) in
    # different docs. Send both shapes; the API ignores unknown keys.
    payload = {"targets": targets, "target": targets[0], "mode": mode, "output": "json"}

    try:
        resp = requests.post(SUMMARY_ENDPOINT, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as e:
        logger.warning("SE Ranking network error: %s", e)
        return [{"domain": t, "error": f"network:{type(e).__name__}"} for t in targets]

    if resp.status_code == 401:
        return [{"domain": t, "error": "invalid_api_key"} for t in targets]
    if resp.status_code == 402 or resp.status_code == 429:
        return [{"domain": t, "error": "quota_or_rate_limit"} for t in targets]
    if not resp.ok:
        return [{"domain": t, "error": f"http_{resp.status_code}"} for t in targets]

    try:
        data = resp.json()
    except ValueError:
        return [{"domain": t, "error": "invalid_json"} for t in targets]

    # Response shapes seen in the wild:
    #   { "data": [ {...}, {...} ] }
    #   [ {...}, {...} ]                 (top-level list)
    #   { "domain.com": {...}, ... }     (object keyed by domain)
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        rows_raw = data["data"]
    elif isinstance(data, list):
        rows_raw = data
    elif isinstance(data, dict):
        rows_raw = [{"domain": k, **(v or {})} for k, v in data.items()
                    if isinstance(v, dict)]
    else:
        return [{"domain": t, "error": "unexpected_response"} for t in targets]

    normalized = [_normalize_row(r) for r in rows_raw if isinstance(r, dict)]
    # Fill in domains the response omitted so the caller always gets one row per target
    by_dom = {r["domain"]: r for r in normalized if r["domain"]}
    for t in targets:
        if t.lower() not in by_dom:
            normalized.append({"domain": t.lower(), "error": "no_data"})
    return normalized


def summary_bulk(
    targets: Iterable[str],
    api_key: str,
    mode: str = "domain",
    workers: int = 4,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Look up many domains; auto-chunks into 100-at-a-time POSTs in parallel."""
    targets = [str(t).strip().lower() for t in targets if str(t).strip()]
    if not targets or not api_key:
        return [{"domain": t, "error": "no_api_key"} for t in targets]

    batches = [targets[i:i + BULK_BATCH_SIZE] for i in range(0, len(targets), BULK_BATCH_SIZE)]
    out: list[dict] = []

    if len(batches) == 1:
        return _summary_batch(batches[0], api_key, mode, timeout)

    with ThreadPoolExecutor(max_workers=min(workers, len(batches))) as pool:
        futs = {pool.submit(_summary_batch, b, api_key, mode, timeout): b for b in batches}
        for fut in as_completed(futs):
            try:
                out.extend(fut.result())
            except Exception as e:
                out.extend([{"domain": t, "error": f"exception:{type(e).__name__}"} for t in futs[fut]])
    return out
