"""
whoisxml.py — WhoisXML API client for domain enrichment.

Docs: https://whois.whoisxmlapi.com/documentation/making-requests

We use this *after* the cheap RDAP/DNS pass to enrich a small set of
candidate domains (typically <100), so we don't burn credits on the
full keyword candidate pool.

Returned fields for each domain:
    domain                  str
    whoisxml_availability   "AVAILABLE" | "UNAVAILABLE" | "UNDETERMINED" | ""
    expires_date            ISO date string ("YYYY-MM-DD") or ""
    created_date            ISO date string or ""
    updated_date            ISO date string or ""
    registrar               str
    estimated_age_days      int
    estimated_age_years     float
    name_servers            list[str]
    status                  list[str]   (raw WHOIS status flags)
    error                   str          (HTTP / parsing error message if any)
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

WHOISXML_ENDPOINT = "https://www.whoisxmlapi.com/whoisserver/WhoisService"
DEFAULT_TIMEOUT   = 12   # seconds — Vercel function ceiling forces a tight budget


def _parse_date(s: str | None) -> str:
    """Return YYYY-MM-DD or '' for unparseable input."""
    if not s:
        return ""
    s = s.strip()
    # WhoisXML returns formats like "2024-06-15T00:00:00+0000" or "2024-06-15 00:00:00 UTC"
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S UTC",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s.replace("Z", "+0000"), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last-ditch ISO parse
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _empty_row(domain: str, error: str = "") -> dict:
    return {
        "domain":                 domain,
        "whoisxml_availability":  "",
        "expires_date":           "",
        "created_date":           "",
        "updated_date":           "",
        "registrar":              "",
        "estimated_age_days":     0,
        "estimated_age_years":    0.0,
        "name_servers":           [],
        "status":                 [],
        "error":                  error,
    }


def lookup_one(domain: str, api_key: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Look up a single domain. Never raises — returns row with `error` set on failure."""
    if not api_key:
        return _empty_row(domain, "no_api_key")

    try:
        resp = requests.get(
            WHOISXML_ENDPOINT,
            params={
                "apiKey":       api_key,
                "domainName":   domain,
                "outputFormat": "JSON",
                "da":           "2",   # full availability check
                "ip":           "0",
            },
            timeout=timeout,
        )
    except requests.RequestException as e:
        return _empty_row(domain, f"network:{type(e).__name__}")

    if resp.status_code == 429:
        return _empty_row(domain, "rate_limited")
    if not resp.ok:
        return _empty_row(domain, f"http_{resp.status_code}")

    try:
        data = resp.json()
    except ValueError:
        return _empty_row(domain, "invalid_json")

    rec = data.get("WhoisRecord") or {}
    if not rec:
        # Some error payloads come as {"ErrorMessage": {"errorCode": "...", "msg":"..."}}
        err = (data.get("ErrorMessage") or {}).get("msg") or "no_whois_record"
        return _empty_row(domain, err)

    # registryData is the registry-level copy; fall back to top-level
    registry = rec.get("registryData") or {}

    def _pick(*paths):
        """First non-empty value across rec / registry."""
        for src in (rec, registry):
            cur = src
            for p in paths:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(p)
            if cur:
                return cur
        return None

    expires  = _pick("expiresDate")  or _pick("expiresDateNormalized")
    created  = _pick("createdDate")  or _pick("createdDateNormalized")
    updated  = _pick("updatedDate")  or _pick("updatedDateNormalized")
    age_days = _pick("estimatedDomainAge") or 0
    try:
        age_days = int(age_days)
    except (TypeError, ValueError):
        age_days = 0

    registrar = (
        rec.get("registrarName")
        or registry.get("registrarName")
        or (rec.get("registrar") or {}).get("name", "")
        or ""
    )

    # Name servers
    ns_field = rec.get("nameServers") or registry.get("nameServers") or {}
    ns_list = ns_field.get("hostNames", []) if isinstance(ns_field, dict) else []

    # Status flags — WhoisXML returns a single string with multiple flags space-separated,
    # or sometimes a list. Normalize to list[str].
    status_field = rec.get("status") or registry.get("status") or ""
    if isinstance(status_field, str):
        status_list = re.split(r"[\s,]+", status_field.strip()) if status_field else []
    elif isinstance(status_field, list):
        status_list = [str(s) for s in status_field if s]
    else:
        status_list = []

    availability = (
        data.get("DomainInfo", {}).get("domainAvailability")
        or rec.get("domainAvailability")
        or ""
    )

    return {
        "domain":                 domain,
        "whoisxml_availability":  str(availability).upper(),
        "expires_date":           _parse_date(expires),
        "created_date":           _parse_date(created),
        "updated_date":           _parse_date(updated),
        "registrar":              registrar,
        "estimated_age_days":     age_days,
        "estimated_age_years":    round(age_days / 365.25, 1) if age_days else 0.0,
        "name_servers":           [n for n in ns_list if n][:6],
        "status":                 [s for s in status_list if s][:12],
        "error":                  "",
    }


def lookup_bulk(
    domains: list[str],
    api_key: str,
    workers: int = 8,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Parallel lookup. Order is NOT preserved — caller should re-key by domain."""
    if not domains:
        return []
    if not api_key:
        return [_empty_row(d, "no_api_key") for d in domains]

    workers = max(1, min(workers, 16))
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(lookup_one, d, api_key, timeout): d for d in domains}
        for fut in as_completed(futures):
            try:
                out.append(fut.result())
            except Exception as e:
                out.append(_empty_row(futures[fut], f"exception:{type(e).__name__}"))
    return out


def is_recently_expired_v2(row: dict, max_months: int = 24) -> bool:
    """
    Stronger freshness check using WhoisXML data:
      • If WhoisXML says AVAILABLE and expires_date is within the window → True
      • If expires_date present and within window → True
      • Otherwise → False
    """
    exp = row.get("expires_date") or ""
    if not exp:
        return False
    try:
        expiry = datetime.strptime(exp, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    cutoff = now - timedelta(days=max_months * 30)
    # Allow expiry slightly in the future (auto-renew already lapsed at registrar)
    return cutoff <= expiry <= now + timedelta(days=90)
