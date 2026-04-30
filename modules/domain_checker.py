"""
domain_checker.py — Step 4: Check whether domains are available for registration

Priority chain (highest confidence first):
  1. Namecheap API  — real registrar answer, batch 50 domains, XML
  2. Spaceship API  — real registrar answer, batch 50 domains, JSON REST
  3. RDAP (IANA)    — authoritative but doesn't know redemption periods
  4. python-whois   — fallback for TLDs not in RDAP bootstrap
  5. DNS only       — last resort (no RDAP/WHOIS response)

RDAP/WHOIS false-positives (domain shows expired but is in redemption grace
period) are eliminated when Namecheap or Spaceship keys are provided.
"""

import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

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


# ── Namecheap API batch check ────────────────────────────────────────────────

def _namecheap_batch(
    domains: list[str],
    api_key: str,
    username: str,
    client_ip: str = "127.0.0.1",
) -> dict[str, bool]:
    """
    Batch availability check via Namecheap domains.check (up to 50 per call).
    Returns {domain: purchasable}.

    Note: the Namecheap account must have API enabled and either whitelist
    the server IP or allow all IPs (Settings → API Access → Whitelist).
    """
    results: dict[str, bool] = {}
    session = make_session()

    for i in range(0, len(domains), 50):
        chunk = domains[i : i + 50]
        params = {
            "ApiUser":    username,
            "ApiKey":     api_key,
            "UserName":   username,
            "Command":    "namecheap.domains.check",
            "ClientIp":   client_ip,
            "DomainList": ",".join(chunk),
        }
        resp = safe_get("https://api.namecheap.com/xml.response", session, params=params)
        if not resp:
            continue
        try:
            root = ET.fromstring(resp.text)
            # Iterate all elements; tag looks like {namespace}DomainCheckResult
            for el in root.iter():
                if el.tag.endswith("DomainCheckResult"):
                    d  = el.get("Domain", "").lower()
                    av = el.get("Available", "false").lower() == "true"
                    if d:
                        results[d] = av
        except Exception as exc:
            logger.warning("Namecheap XML parse error: %s", exc)
        time.sleep(0.15)

    logger.info("Namecheap checked %d domains: %d available", len(results), sum(results.values()))
    return results


# ── Spaceship API batch check ──────────────────────────────────────────────────

def _spaceship_batch(
    domains: list[str],
    api_key: str,
    api_secret: str,
) -> dict[str, bool]:
    """
    Batch availability check via Spaceship REST API (1 000 lookups / day free).
    Returns {domain: purchasable}.

    Spaceship API docs: https://docs.spaceship.com/
    Auth: X-Account-Email (your account email) + X-Account-Api-Key (API key).
    """
    results: dict[str, bool] = {}
    session = make_session()

    for i in range(0, len(domains), 50):
        chunk = domains[i : i + 50]
        try:
            resp = session.get(
                "https://api.spaceship.com/v1/domains/availability",
                # Spaceship uses repeated query params: ?names[]=a.com&names[]=b.com
                params=[("names[]", d) for d in chunk],
                headers={
                    "X-Account-Email":   api_key,    # account email
                    "X-Account-Api-Key": api_secret,
                    "Accept":            "application/json",
                },
                timeout=config.REQUEST_TIMEOUT,
            )
            if resp.ok:
                data = resp.json()
                # Response: {"results": [{"name": "domain.com", "purchasable": true, ...}]}
                for item in data.get("results", []):
                    d  = item.get("name", "").lower()
                    av = item.get("purchasable", False)
                    if d:
                        results[d] = bool(av)
            else:
                logger.warning("Spaceship API %s: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Spaceship error: %s", exc)
        time.sleep(0.15)

    logger.info("Spaceship checked %d domains: %d available", len(results), sum(results.values()))
    return results


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

def check_domain(domain: str, registrar_avail: dict[str, bool] | None = None) -> dict:
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
        check_method: str,    # "namecheap" | "spaceship" | "rdap" | "whois" | "dns_only"
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

    # 0. Registrar API answer is highest confidence — use it directly.
    if registrar_avail is not None and domain in registrar_avail:
        result["available"]    = registrar_avail[domain]
        result["check_method"] = registrar_avail.get("__source__", "registrar")
        return result

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
    namecheap_key: str = "",
    namecheap_user: str = "",
    spaceship_key: str = "",
    spaceship_secret: str = "",
) -> list[dict]:
    """
    Check availability for all domains in parallel.

    Priority:
      1. Namecheap API (if namecheap_key + namecheap_user provided)
      2. Spaceship API (if spaceship_key + spaceship_secret provided)
      3. RDAP → WHOIS → DNS fallback (per-domain, parallel)

    Returns only domains where available=True.
    """
    workers = workers or config.MAX_WORKERS

    # ── Pre-batch with registrar API ──────────────────────────────────────────
    registrar_avail: dict[str, bool] = {}
    source_label = "registrar"

    if namecheap_key and namecheap_user:
        registrar_avail = _namecheap_batch(domains, namecheap_key, namecheap_user)
        source_label = "namecheap"
        logger.info("Using Namecheap for %d domains", len(registrar_avail))
    elif spaceship_key and spaceship_secret:
        registrar_avail = _spaceship_batch(domains, spaceship_key, spaceship_secret)
        source_label = "spaceship"
        logger.info("Using Spaceship for %d domains", len(registrar_avail))

    # Embed source label so check_domain can set check_method correctly
    if registrar_avail:
        registrar_avail["__source__"] = source_label  # type: ignore[assignment]

    # ── Per-domain checks (parallel) ──────────────────────────────────────────
    results = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(check_domain, d, registrar_avail or None): d
            for d in domains
        }
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
