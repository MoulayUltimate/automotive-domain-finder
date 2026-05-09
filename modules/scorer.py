"""
scorer.py — Step 6: Combine signals into a clean 0–100 SEO score
            and remove spammy / irrelevant domains.

Scoring breakdown (see config.SCORE_WEIGHTS):
  wayback_snapshots  30 pts  — archive depth
  backlink_signals   30 pts  — OPR / Majestic / Moz
  domain_name_quality 20 pts — length, keywords, brandability
  indexed_signals    20 pts  — DDG/CC index presence

Then we apply spam/junk removal rules.
"""

import re
from modules.utils import get_logger

logger = get_logger("scorer")

# TLDs that can never be registered by the public — always skip them
BLOCKED_TLDS = {"gov", "mil", "edu"}

# Patterns strongly suggesting spammy / parked domains
SPAM_PATTERNS = re.compile(
    r"(free-?seo|buy-?domain|parked|domain-?for-?sale|"
    r"click-?here|best-?price|cheap-?domain|coupon|"
    r"casino|poker|loan|payday|pharma|pill|viagra|"
    r"xxx|porn|adult|sex|nude|escort)",
    re.I,
)

# Non-English TLD patterns (keep .com .net .org .io .ca .us .co .info .biz .news .media)
ALLOWED_TLDS = {
    "com", "net", "org", "io", "ca", "us", "co",
    "info", "biz", "news", "media", "blog", "auto",
    "car", "cars", "drive", "moto",
}

# Very long domain names are less brandable
MAX_DOMAIN_LENGTH = 35


def _wayback_score(signals: dict) -> float:
    """0–30 pts based on snapshot count."""
    n = signals.get("wayback_snapshots", 0)
    if n == 0:
        return 0
    if n >= 500:
        return 30
    if n >= 100:
        return 22
    if n >= 50:
        return 16
    if n >= 20:
        return 10
    if n >= 5:
        return 5
    return 2


def _backlink_score(signals: dict) -> float:
    """0–30 pts from OPR, Majestic, Moz — uses whichever is available."""
    pts = 0.0

    # OpenPageRank (0–10 integer scale)
    opr = signals.get("page_rank_integer", 0)
    if opr >= 7:
        pts = max(pts, 30)
    elif opr >= 5:
        pts = max(pts, 22)
    elif opr >= 3:
        pts = max(pts, 15)
    elif opr >= 1:
        pts = max(pts, 8)

    # Majestic Trust Flow (0–100 scale)
    tf = signals.get("trust_flow", 0)
    if tf >= 40:
        pts = max(pts, 30)
    elif tf >= 25:
        pts = max(pts, 22)
    elif tf >= 10:
        pts = max(pts, 14)
    elif tf >= 5:
        pts = max(pts, 7)

    # Majestic backlinks count
    bl = signals.get("backlinks", 0)
    if bl >= 10_000:
        pts = max(pts, 28)
    elif bl >= 1_000:
        pts = max(pts, 18)
    elif bl >= 100:
        pts = max(pts, 10)
    elif bl >= 10:
        pts = max(pts, 5)

    # Moz Domain Authority (0–100)
    da = signals.get("domain_authority", 0)
    if da >= 40:
        pts = max(pts, 30)
    elif da >= 25:
        pts = max(pts, 22)
    elif da >= 10:
        pts = max(pts, 12)
    elif da >= 5:
        pts = max(pts, 6)

    return pts


def _domain_name_score(domain: str) -> float:
    """0–20 pts for domain name quality."""
    stem = domain.split(".")[0]
    pts = 0.0

    # Length score (shorter = better)
    length = len(stem)
    if length <= 8:
        pts += 10
    elif length <= 12:
        pts += 7
    elif length <= 18:
        pts += 4
    elif length <= 25:
        pts += 2

    # Automotive keyword presence in name
    from config import AUTOMOTIVE_KEYWORDS
    kw_hits = sum(1 for kw in AUTOMOTIVE_KEYWORDS if kw in stem.lower())
    pts += min(kw_hits * 4, 8)

    # Hyphens reduce brandability
    pts -= stem.count("-") * 1.5

    # Numbers reduce brandability (unless it looks like a year like "2024cars")
    digit_count = sum(c.isdigit() for c in stem)
    if digit_count > 0 and not re.search(r"\d{4}", stem):
        pts -= digit_count * 1

    return max(pts, 0)


def _index_score(signals: dict) -> float:
    """0–20 pts for search engine index presence."""
    pts = 0.0
    if signals.get("is_indexed_ddg"):
        pts += 12
    if signals.get("in_commoncrawl"):
        pts += 8
    return pts


def _is_spammy(domain: str, signals: dict) -> bool:
    """True if the domain should be discarded as spam / junk."""
    stem = domain.split(".")[0]
    tld = domain.split(".")[-1]

    # Government / military / education TLDs — cannot be privately registered
    if tld in BLOCKED_TLDS:
        return True

    # Spam pattern in name
    if SPAM_PATTERNS.search(domain):
        return True

    # Too long
    if len(domain) > MAX_DOMAIN_LENGTH:
        return True

    # Non-English / unlikely TLD
    if tld not in ALLOWED_TLDS and len(tld) > 3:
        # Allow ccTLDs up to 2 chars (e.g. .ca .us)
        if len(tld) > 2:
            return True

    # Gibberish stem: mostly random consonants, no vowels
    vowels = set("aeiou")
    if len(stem) >= 5 and not any(c in vowels for c in stem.lower()):
        return True

    # Very low archive AND no index AND no backlinks → likely junk
    wayback = signals.get("wayback_snapshots", 0)
    indexed = signals.get("is_indexed_ddg", False)
    backlinks = signals.get("backlinks", 0)
    opr = signals.get("page_rank_integer", 0)
    if wayback == 0 and not indexed and backlinks == 0 and opr == 0:
        return True

    return False


def score_domain(domain: str, signals: dict, auto_relevance: int = 0) -> dict:
    """
    Compute final score for one domain.
    Returns enriched dict with "seo_score" (0–100) and "keep" flag.
    """
    result = dict(signals)

    if _is_spammy(domain, signals):
        result["seo_score"] = 0
        result["keep"] = False
        result["discard_reason"] = "spam/junk"
        return result

    wb = _wayback_score(signals)
    bl = _backlink_score(signals)
    dn = _domain_name_score(domain)
    idx = _index_score(signals)

    raw = wb + bl + dn + idx  # 0–100
    # Bonus: automotive relevance from NLP filter (0–40) → mapped to 0–5 bonus
    bonus = min(auto_relevance / 40 * 5, 5)
    score = min(raw + bonus, 100)

    result["score_wayback"] = round(wb, 1)
    result["score_backlinks"] = round(bl, 1)
    result["score_domain_name"] = round(dn, 1)
    result["score_indexed"] = round(idx, 1)
    result["seo_score"] = round(score, 1)
    result["keep"] = score >= 25  # config.MIN_SCORE_TO_KEEP
    result["discard_reason"] = "" if result["keep"] else "low_score"

    return result


def score_all(
    seo_signals: list[dict],
    auto_relevance_map: dict[str, int] | None = None,
) -> list[dict]:
    """
    Score a list of signal dicts.
    auto_relevance_map: {domain: relevance_score_from_step3}
    Returns sorted list (highest score first), only "keep=True" entries.
    """
    auto_relevance_map = auto_relevance_map or {}
    scored = []
    for signals in seo_signals:
        domain = signals.get("domain", "")
        relevance = auto_relevance_map.get(domain, 0)
        result = score_domain(domain, signals, relevance)
        scored.append(result)
        logger.debug(
            "%s → score=%.1f keep=%s",
            domain,
            result.get("seo_score", 0),
            result.get("keep"),
        )

    kept = [r for r in scored if r.get("keep")]
    discarded = len(scored) - len(kept)
    logger.info(
        "Scoring complete: %d kept / %d discarded",
        len(kept),
        discarded,
    )
    return sorted(kept, key=lambda r: -r.get("seo_score", 0))
