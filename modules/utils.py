"""
utils.py — Shared HTTP helpers, logging setup, domain normalisation
"""

import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import CRAWL_DELAY, LOGS_DIR, PROXIES, REQUEST_TIMEOUT, USER_AGENTS

# ── Logging ───────────────────────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
    log_file = LOGS_DIR / f"{name}.log"
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.addHandler(fh)
    return logger

# ── HTTP session ──────────────────────────────────────────────────────────────

def make_session(use_proxy: bool = False) -> requests.Session:
    """Return a requests Session with retry logic and a random UA."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": random.choice(USER_AGENTS)})

    if use_proxy and PROXIES:
        proxy = random.choice(PROXIES)
        session.proxies = {"http": proxy, "https": proxy}

    return session


def safe_get(url: str, session: requests.Session | None = None, **kwargs):
    """GET with timeout + delay + error swallowing. Returns Response or None."""
    if session is None:
        session = make_session()
    try:
        time.sleep(random.uniform(CRAWL_DELAY * 0.8, CRAWL_DELAY * 1.4))
        resp = session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp
    except Exception:
        return None


# ── Domain helpers ─────────────────────────────────────────────────────────────

_SCHEME_RE = re.compile(r"^https?://", re.I)

def normalise_domain(raw: str) -> str | None:
    """
    Return lowercase apex domain, or None if the input is not a valid URL/domain.
    Strips www., path, query, fragment.
    """
    raw = raw.strip()
    if not raw:
        return None
    if not _SCHEME_RE.match(raw):
        raw = "http://" + raw
    try:
        host = urlparse(raw).hostname or ""
    except Exception:
        return None
    host = host.lower().lstrip("www.")
    # Basic sanity: must have at least one dot and no spaces
    if "." not in host or " " in host:
        return None
    # Must end with a real TLD (at least 2 chars)
    parts = host.split(".")
    if len(parts[-1]) < 2:
        return None
    return host


def apex_domain(url: str) -> str | None:
    """Extract apex domain from any URL."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return None
    return host.lower().lstrip("www.") if host else None


def is_internal(link_host: str, source_host: str) -> bool:
    """True if link_host belongs to the same apex as source_host."""
    return normalise_domain(link_host) == normalise_domain(source_host)


def load_lines(path: Path) -> list[str]:
    """Read non-empty, non-comment lines from a text file."""
    if not path.exists():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines
