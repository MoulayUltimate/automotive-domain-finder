"""
keyword_generator.py — Generate candidate domains from seed keywords.

Used by the "Keyword Domain Finder" pipeline to produce a pool of candidate
expired/droppable domains for a set of keywords (e.g. cars, crypto, baby,
lawyer, nursing, gaming).

Generation strategies:
  • keyword + prefix          (best, top, my, get, …)
  • keyword + suffix          (hub, world, zone, store, …)
  • prefix + keyword
  • suffix + keyword
  • keyword1 + keyword2       (when ≥2 keywords given)
  • keyword1-keyword2         (hyphenated combos)
  • bare keyword              (e.g. cars.com — usually taken but worth checking)

The output is the full candidate pool — availability + freshness is filtered
downstream by /api/check (RDAP) and the UI freshness filter.

NOTE: keep the candidate count modest (a few hundred per keyword) so the
serverless pipeline can run the full check+SEO+score in <60s.
"""

from __future__ import annotations
import re

# ── Vocabulary ────────────────────────────────────────────────────────────────

PREFIXES = [
    "best", "top", "my", "the", "go", "get", "smart", "pro", "easy", "fast",
    "online", "hub", "world", "global", "super", "ultra", "mega", "ace",
    "new", "fresh", "daily",
]

SUFFIXES = [
    "hub", "world", "zone", "spot", "lab", "labs", "store", "shop", "site",
    "ebooks", "guide", "news", "blog", "tips", "wiki", "pro", "online",
    "central", "club", "expert", "academy", "review", "reviews", "today",
    "now", "daily", "weekly", "magazine", "post", "report", "directory",
    "garage", "gear", "shop",
]

DEFAULT_TLDS = ["com", "net", "org", "io", "co", "info"]

# Slug-safe pattern: lowercase letters + digits only (no hyphens in keyword body)
_SAFE = re.compile(r"[^a-z0-9]+")

def _slug(s: str) -> str:
    return _SAFE.sub("", s.lower().strip())


# ── Generator ─────────────────────────────────────────────────────────────────

def generate_candidates(
    keywords: list[str],
    tlds: list[str] | None = None,
    max_per_keyword: int = 250,
    include_combos: bool = True,
) -> list[dict]:
    """
    Build candidate domains for a list of keywords.

    Returns a list of dicts:
        {"domain": "bestcarshub.com", "matched_keyword": "cars", "pattern": "prefix+kw+suffix"}

    Caller is expected to feed `domain` strings into /api/check next.
    """
    tlds = [t.lstrip(".").lower() for t in (tlds or DEFAULT_TLDS)]
    cleaned = [_slug(k) for k in keywords if _slug(k)]
    if not cleaned:
        return []

    out: list[dict] = []
    seen: set[str] = set()

    def _push(stem: str, kw: str, pattern: str) -> None:
        stem = _slug(stem)
        if not stem or len(stem) < 3 or len(stem) > 30:
            return
        for tld in tlds:
            domain = f"{stem}.{tld}"
            if domain in seen:
                continue
            seen.add(domain)
            out.append({
                "domain":          domain,
                "matched_keyword": kw,
                "pattern":         pattern,
            })

    for kw in cleaned:
        per_kw_start = len(out)

        # Bare keyword (e.g. cars.com — usually taken, but worth checking)
        _push(kw, kw, "bare")

        # prefix + kw
        for p in PREFIXES:
            _push(p + kw, kw, "prefix+kw")
        # kw + suffix
        for s in SUFFIXES:
            _push(kw + s, kw, "kw+suffix")
        # kw + 's' + suffix  (e.g. carsworld already covered; carsstore etc.)
        # prefix + kw + suffix (sparingly — explodes combinatorially)
        for p in PREFIXES[:8]:
            for s in SUFFIXES[:6]:
                if len(out) - per_kw_start >= max_per_keyword:
                    break
                _push(p + kw + s, kw, "prefix+kw+suffix")
            if len(out) - per_kw_start >= max_per_keyword:
                break

        # Trim oversize batches
        if len(out) - per_kw_start > max_per_keyword:
            out = out[: per_kw_start + max_per_keyword]

    # Cross-keyword combinations
    if include_combos and len(cleaned) >= 2:
        for i, a in enumerate(cleaned):
            for b in cleaned[i + 1 :]:
                _push(a + b,        f"{a}+{b}", "kw+kw")
                _push(b + a,        f"{a}+{b}", "kw+kw")
                _push(f"{a}{b}hub", f"{a}+{b}", "kw+kw+suffix")
                _push(f"best{a}{b}", f"{a}+{b}", "prefix+kw+kw")

    return out


# ── Helper used by the API to attach freshness info to /api/check results ────

def is_recently_expired(expiry_iso: str | None, months: int = 24) -> bool:
    """
    True if the domain's expiry date is within the last `months` months.
    Used to filter out long-abandoned domains.

    Returns False if no expiry date is available (caller decides default).
    """
    if not expiry_iso:
        return False
    from datetime import datetime, timezone, timedelta
    try:
        expiry = datetime.fromisoformat(expiry_iso.replace("Z", "+00:00"))
    except Exception:
        try:
            expiry = datetime.strptime(expiry_iso[:19], "%Y-%m-%d %H:%M:%S")
            expiry = expiry.replace(tzinfo=timezone.utc)
        except Exception:
            return False
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=months * 30)
    return cutoff <= expiry <= now + timedelta(days=30)
