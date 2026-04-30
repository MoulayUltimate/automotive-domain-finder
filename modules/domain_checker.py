"""
domain_checker.py — Step 4: Check whether domains are available for registration

Priority chain (highest confidence first):
  1. RDAP (IANA)   — authoritative, free, no keys.  Correctly reads redemption-
                     period / pendingDelete / serverHold status flags so expired
                     domains in grace periods are NOT marked as purchasable.
  2. python-whois  — fallback for TLDs not covered by RDAP bootstrap
  3. DNS only      — last resort (no RDAP/WHOIS response)

Key fix (vs naive implementations): a domain can be *expired* but still
locked in redemption period (~30 days) or pending-delete (~5 days) — during
which it cannot be registered.  We read RDAP `status` flags explicitly and
reject any domain carrying those "locked" statuses.
"""

import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

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

    # ── Status flags that mean "NOT purchasable right now" ────────────────────
    # Even if the domain is expired, these flags mean the registry still
    # holds it — it's in grace/redemption/pending-delete and can't be bought.
    LOCKED_STATUSES = {
        "redemptionPeriod",       # ~30-day window, only original registrant can restore
        "pendingRestore",         # registrant initiated restore — back in redemption
        "pendingDelete",          # ~5-day window before deletion (can't be registered yet)
        "serverHold",             # registry placed on hold (legal/compliance)
        "clientHold",             # registrar placed on hold
        "serverDeleteProhibited", # registry lock — not going anywhere soon
        "serverTransferProhibited",
        "serverUpdateProhibited",
    }

    status_lower = {s.lower() for s in status}
    locked_flags = {s for s in LOCKED_STATUSES if s.lower() in status_lower}

    if locked_flags:
        # Domain exists and is in a non-purchasable grace/lock state
        logger.debug("RDAP locked (%s): %s — flags: %s", domain, ", ".join(locked_flags), status)
        return {
            "available": False,
            "expiry_date": expiry,
            "status": status,
            "registrar": registrar,
        }

    now = datetime.now(timezone.utc)
    is_inactive = "inactive" in status_lower
    is_expired  = expiry is not None and expiry < now

    # Only mark as available when expired/inactive AND no locked flags
    available = is_inactive or is_expired

    return {
        "available": available,
        "expiry_date": expiry,
        "status": status,
        "registrar": registrar,
    }


# ── python-whois fallback ─────────────────────────────────────────────────────

_WHOIS_LOCKED = {
    "redemptionperiod", "pendingrestore", "pendingdelete",
    "serverhold",       "clienthold",
}

def _whois_status(domain: str) -> dict:
    """WHOIS fallback for TLDs not covered by RDAP bootstrap."""
    try:
        w = pywhois.whois(domain)
    except Exception:
        return {"available": None, "expiry_date": None}

    if not w or not w.domain_name:
        return {"available": True, "expiry_date": None}

    # Check for redemption / pending-delete flags in WHOIS status
    raw_status = w.status or []
    if isinstance(raw_status, str):
        raw_status = [raw_status]
    status_lower = {s.lower().split()[0] for s in raw_status if s}  # strip URL suffixes

    if status_lower & _WHOIS_LOCKED:
        logger.debug("WHOIS locked: %s — %s", domain, status_lower & _WHOIS_LOCKED)
        return {"available": False, "expiry_date": None}

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
    Full availability check for a single domain.
    Returns:
      {
        domain: str,
        available: bool,          # True = can be registered right now
        dns_resolves: bool,
        expiry_date: str | None,
        registrar: str,
        status_flags: list[str],
        check_method: str,        # "rdap" | "whois" | "dns_only"
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

    # 1. Fast DNS pre-check
    resolves = _resolves_dns(domain)
    result["dns_resolves"] = resolves

    # 2. RDAP — always attempt; it tells us locked statuses too
    rdap = _rdap_status(domain)

    if resolves:
        # Domain has live DNS → almost certainly registered
        # Still record RDAP metadata for context, but not available
        if rdap["expiry_date"]:
            result["expiry_date"] = str(rdap["expiry_date"])
            result["registrar"]   = rdap["registrar"]
            result["status_flags"] = rdap["status"]
            result["check_method"] = "rdap"
        else:
            result["check_method"] = "dns_only"
        result["available"] = False
        return result

    # No DNS → might be available
    if rdap["available"] is True:
        result.update(
            available=True,
            expiry_date=str(rdap["expiry_date"]) if rdap["expiry_date"] else None,
            registrar=rdap["registrar"],
            status_flags=rdap["status"],
            check_method="rdap",
        )
        return result

    if rdap["available"] is False:
        # RDAP confirmed it's locked/registered (e.g. redemptionPeriod)
        result["status_flags"] = rdap["status"]
        result["registrar"]    = rdap["registrar"]
        result["check_method"] = "rdap"
        result["available"]    = False
        return result

    # RDAP inconclusive (None) — try WHOIS
    wo = _whois_status(domain)
    if wo["available"] is True:
        result.update(
            available=True,
            expiry_date=str(wo["expiry_date"]) if wo["expiry_date"] else None,
            check_method="whois",
        )
        return result

    if wo["available"] is False:
        result["check_method"] = "whois"
        result["available"]    = False
        return result

    # Both RDAP and WHOIS inconclusive, DNS doesn't resolve →
    # conservative: treat as potentially available but flag it
    result["available"]    = True
    result["check_method"] = "dns_only"
    return result


# ── Batch check ───────────────────────────────────────────────────────────────

def check_domains_bulk(
    domains: list[str],
    workers: int | None = None,
) -> list[dict]:
    """
    Check availability for all domains in parallel using RDAP → WHOIS → DNS.
    Correctly rejects domains in redemptionPeriod / pendingDelete / serverHold.
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
                    logger.debug("Registered/Locked: %s flags=%s", d, res.get("status_flags"))
            except Exception as exc:
                logger.error("Error checking %s: %s", d, exc)

    logger.info(
        "Domain availability: %d available out of %d checked",
        len(results),
        len(domains),
    )
    return results
