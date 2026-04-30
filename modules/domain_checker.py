"""
domain_checker.py — Step 4: Check whether domains are available for registration

Strategy (no paid API, no SEMrush):
  1. RDAP (IANA bootstrap) — authoritative, rate-limit-safe, JSON API
  2. python-whois fallback — covers more obscure TLDs
  3. DNS resolution check — live domain ≠ available (fast preliminary filter)

A domain is considered "available" when:
  - RDAP returns a 404 (not found / not registered)
  - OR WHOIS shows no registrant and no expiry date
  - AND it does NOT resolve via DNS (extra sanity check)

We also detect "recently expired" (grace period) via expiry date parsing.
"""

import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
import whois as pywhois  # python-whois

import config
from modules.utils import get_logger, make_session, safe_get

logger = get_logger("domain_checker")

# ── DNS quick check ────────────────────────────────────────────────────────────

def _resolves_dns(domain: str) -> bool:
    """True if domain has at least one DNS A/AAAA record."""
    try:
        socket.setdefaulttimeout(2)   # 2 s is plenty — expired domains fail instantly
        socket.getaddrinfo(domain, None)
        return True
    except (socket.gaierror, OSError):
        return False


# ── RDAP check ────────────────────────────────────────────────────────────────

def _rdap_status(domain: str) -> dict:
    """
    Query RDAP.  Returns dict with:
      available: bool
      expiry_date: datetime | None
      status: list[str]
      registrar: str
    """
    url = f"{config.RDAP_BOOTSTRAP}{domain}"
    session = make_session()
    resp = safe_get(url, session)

    if resp is None:
        return {"available": None, "expiry_date": None, "status": [], "registrar": ""}

    if resp.status_code == 404:
        return {"available": True, "expiry_date": None, "status": [], "registrar": ""}

    try:
        data = resp.json()
    except Exception:
        return {"available": None, "expiry_date": None, "status": [], "registrar": ""}

    # Parse expiry date
    expiry = None
    for event in data.get("events", []):
        if event.get("eventAction") == "expiration":
            try:
                expiry = datetime.fromisoformat(
                    event["eventDate"].replace("Z", "+00:00")
                )
            except Exception:
                pass

    status = data.get("status", [])
    entities = data.get("entities", [])
    registrar = ""
    for ent in entities:
        roles = ent.get("roles", [])
        if "registrar" in roles:
            vcard = ent.get("vcardArray", [])
            try:
                for field in vcard[1]:
                    if field[0] == "fn":
                        registrar = field[3]
                        break
            except Exception:
                pass

    # Domain is "available" if RDAP says "inactive" or expiry is in the past
    now = datetime.now(timezone.utc)
    is_inactive = "inactive" in status
    is_expired = expiry is not None and expiry < now

    available = is_inactive or is_expired

    return {
        "available": available,
        "expiry_date": expiry,
        "status": status,
        "registrar": registrar,
    }


# ── python-whois fallback ─────────────────────────────────────────────────────

def _whois_status(domain: str) -> dict:
    """WHOIS fallback for TLDs not in RDAP bootstrap."""
    try:
        w = pywhois.whois(domain)
    except Exception:
        return {"available": None, "expiry_date": None}

    if not w or not w.domain_name:
        return {"available": True, "expiry_date": None}

    expiry = w.expiration_date
    if isinstance(expiry, list):
        expiry = expiry[0]

    now = datetime.now(timezone.utc)
    if expiry:
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        is_expired = expiry < now
    else:
        is_expired = False

    return {
        "available": is_expired,
        "expiry_date": expiry,
    }


# ── Main check ─────────────────────────────────────────────────────────────────

def check_domain(domain: str) -> dict:
    """
    Full availability check.
    Returns:
      {
        domain: str,
        available: bool,      # True = can be registered
        dns_resolves: bool,
        expiry_date: str | None,
        registrar: str,
        status_flags: list[str],
        check_method: str,    # "rdap" | "whois" | "dns_only"
      }
    """
    result = {
        "domain": domain,
        "available": False,
        "dns_resolves": False,
        "expiry_date": None,
        "registrar": "",
        "status_flags": [],
        "check_method": "",
    }

    # 1. Fast DNS pre-check: if it doesn't resolve, likely available
    resolves = _resolves_dns(domain)
    result["dns_resolves"] = resolves

    if not resolves:
        # High probability of available — confirm with RDAP
        rdap = _rdap_status(domain)
        if rdap["available"] is True:
            result.update(
                available=True,
                expiry_date=str(rdap["expiry_date"]) if rdap["expiry_date"] else None,
                registrar=rdap["registrar"],
                status_flags=rdap["status"],
                check_method="rdap",
            )
            return result

        # RDAP inconclusive — try WHOIS
        wo = _whois_status(domain)
        if wo["available"] is True:
            result.update(
                available=True,
                expiry_date=str(wo["expiry_date"]) if wo["expiry_date"] else None,
                check_method="whois",
            )
            return result

        # If DNS doesn't resolve AND RDAP/WHOIS are inconclusive,
        # treat as potentially available (flag it)
        result["available"] = True
        result["check_method"] = "dns_only"
        return result

    # Domain resolves → probably registered.  Still check RDAP for expiry soon.
    rdap = _rdap_status(domain)
    if rdap["expiry_date"]:
        result["expiry_date"] = str(rdap["expiry_date"])
        result["registrar"] = rdap["registrar"]
        result["status_flags"] = rdap["status"]
        result["check_method"] = "rdap"
    else:
        result["check_method"] = "dns_only"

    result["available"] = False
    return result


# ── Batch check ───────────────────────────────────────────────────────────────

def check_domains_bulk(
    domains: list[str],
    workers: int | None = None,
) -> list[dict]:
    """
    Check availability for all domains in parallel.
    Returns only domains where available=True.
    """
    workers = workers or config.MAX_WORKERS
    results = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_domain, d): d for d in domains}
        for future in as_completed(futures):
            d = futures[future]
            try:
                res = future.result()
                if res["available"]:
                    results.append(res)
                    logger.info("AVAILABLE: %s (method=%s)", d, res["check_method"])
                else:
                    logger.debug("Registered: %s", d)
            except Exception as exc:
                logger.error("Error checking %s: %s", d, exc)

    logger.info(
        "Domain availability check: %d available out of %d checked",
        len(results),
        len(domains),
    )
    return results
