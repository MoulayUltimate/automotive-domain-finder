#!/usr/bin/env python3
"""
main.py — Automotive Expired-Domain Finder
==========================================

Run modes
---------
  python main.py                       # Full pipeline (all 7 steps)
  python main.py --step 1              # Only blog discovery
  python main.py --step 2              # Only link extraction (requires step 1 output)
  python main.py --step 3-7            # Filter + check + score + export
  python main.py --bulk domains.txt    # Skip steps 1–2, process a pre-made domain list
  python main.py --fast                # Skip slow page-content NLP check (step 3)
  python main.py --no-seo              # Skip SEO API calls (step 5), score on archive + DNS only

Environment overrides
---------------------
  OPENPAGERANK_KEY=xxx python main.py
  MAJESTIC_KEY=xxx python main.py
  MOZ_ID=xxx MOZ_SECRET=xxx python main.py
  PROXIES="http://p1:8080,http://p2:8080" python main.py
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── Apply environment overrides before importing config ───────────────────────
import config  # noqa: E402  (must come after stdlib imports)

if os.environ.get("OPENPAGERANK_KEY"):
    config.OPENPAGERANK_API_KEY = os.environ["OPENPAGERANK_KEY"]
if os.environ.get("MAJESTIC_KEY"):
    config.MAJESTIC_API_KEY = os.environ["MAJESTIC_KEY"]
if os.environ.get("MOZ_ID"):
    config.MOZ_ACCESS_ID = os.environ["MOZ_ID"]
if os.environ.get("MOZ_SECRET"):
    config.MOZ_SECRET_KEY = os.environ["MOZ_SECRET"]
if os.environ.get("PROXIES"):
    config.PROXIES = os.environ["PROXIES"].split(",")

# ── Module imports ─────────────────────────────────────────────────────────────
from modules.blog_discovery import discover_blogs
from modules.domain_checker import check_domains_bulk
from modules.domain_filter import filter_automotive
from modules.exporter import print_summary, to_csv
from modules.link_extractor import extract_all_outbound, flatten_outbound
from modules.scorer import score_all
from modules.seo_estimator import estimate_seo_bulk
from modules.utils import get_logger, load_lines

logger = get_logger("main")

# ── Intermediate-result cache files ───────────────────────────────────────────
CACHE = {
    "blogs": config.DATA_DIR / "cache_blogs.json",
    "outbound": config.DATA_DIR / "cache_outbound.json",
    "auto_filtered": config.DATA_DIR / "cache_auto_filtered.json",
    "available": config.DATA_DIR / "cache_available.json",
    "seo_signals": config.DATA_DIR / "cache_seo_signals.json",
}


def _save(path: Path, data) -> None:
    path.write_text(json.dumps(data, default=str, indent=2), encoding="utf-8")
    logger.info("Cached → %s (%d items)", path.name, len(data))


def _load(path: Path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


# ── Pipeline steps ────────────────────────────────────────────────────────────

def step1_discover(args) -> list[str]:
    cached = _load(CACHE["blogs"])
    if cached and not args.no_cache:
        logger.info("[Step 1] Using cached blog list (%d blogs)", len(cached))
        return cached

    logger.info("[Step 1] Discovering automotive blogs…")
    blogs = discover_blogs(max_blogs=300)
    _save(CACHE["blogs"], blogs)
    return blogs


def step2_extract(blogs: list[str], args) -> list[str]:
    cached = _load(CACHE["outbound"])
    if cached and not args.no_cache:
        logger.info("[Step 2] Using cached outbound domains (%d)", len(cached))
        return cached

    logger.info("[Step 2] Extracting outbound links from %d blogs…", len(blogs))
    blog_map = extract_all_outbound(blogs)
    flat = flatten_outbound(blog_map)
    _save(CACHE["outbound"], flat)
    return flat


def step3_filter(domains: list[str], args) -> list[tuple[str, int]]:
    cached = _load(CACHE["auto_filtered"])
    if cached and not args.no_cache:
        logger.info("[Step 3] Using cached automotive filter (%d domains)", len(cached))
        return [tuple(x) for x in cached]

    logger.info("[Step 3] Filtering for automotive relevance (%d domains)…", len(domains))
    slow = not args.fast
    filtered = filter_automotive(domains, slow_check=slow)
    _save(CACHE["auto_filtered"], [[d, s] for d, s in filtered])
    return filtered


def step4_check(filtered: list[tuple[str, int]], args) -> tuple[list[dict], dict[str, int]]:
    cached = _load(CACHE["available"])
    relevance_map = {d: s for d, s in filtered}

    if cached and not args.no_cache:
        logger.info("[Step 4] Using cached availability results (%d available)", len(cached))
        return cached, relevance_map

    domains = [d for d, _ in filtered]
    logger.info("[Step 4] Checking domain availability for %d domains…", len(domains))
    available = check_domains_bulk(domains)
    _save(CACHE["available"], available)
    return available, relevance_map


def step5_seo(available: list[dict], args) -> list[dict]:
    cached = _load(CACHE["seo_signals"])
    if cached and not args.no_cache:
        logger.info("[Step 5] Using cached SEO signals (%d)", len(cached))
        return cached

    domains = [r["domain"] for r in available]
    if args.no_seo:
        # Minimal signals: only Wayback + DNS (no external API calls)
        from modules.seo_estimator import _wayback_data, _is_indexed, _in_commoncrawl
        signals = []
        for r in available:
            d = r["domain"]
            wb = _wayback_data(d)
            sig = {
                "domain": d,
                "wayback_snapshots": wb["snapshots"],
                "wayback_first_seen": wb["first_seen"],
                "wayback_last_seen": wb["last_seen"],
                "has_archive_history": wb["snapshots"] >= config.WAYBACK_MIN_SNAPSHOTS,
                "is_indexed_ddg": _is_indexed(d),
                "in_commoncrawl": _in_commoncrawl(d),
            }
            signals.append(sig)
    else:
        logger.info("[Step 5] Estimating SEO signals for %d domains…", len(domains))
        signals = estimate_seo_bulk(domains)

    # Merge availability data back into signals
    avail_by_domain = {r["domain"]: r for r in available}
    for sig in signals:
        avail = avail_by_domain.get(sig["domain"], {})
        sig.update(
            available=avail.get("available", True),
            dns_resolves=avail.get("dns_resolves", False),
            expiry_date=avail.get("expiry_date"),
            registrar=avail.get("registrar", ""),
            check_method=avail.get("check_method", ""),
            status_flags=avail.get("status_flags", []),
        )

    _save(CACHE["seo_signals"], signals)
    return signals


def step6_score(signals: list[dict], relevance_map: dict[str, int]) -> list[dict]:
    logger.info("[Step 6] Scoring %d domains…", len(signals))
    # Attach relevance score to each signal dict
    for sig in signals:
        sig["auto_relevance_score"] = relevance_map.get(sig["domain"], 0)

    scored = score_all(signals, relevance_map)
    logger.info("[Step 6] %d domains passed the score threshold", len(scored))
    return scored


def step7_export(scored: list[dict]) -> Path:
    logger.info("[Step 7] Exporting %d results…", len(scored))
    out_path = to_csv(scored)
    print_summary(scored)
    return out_path


# ── Bulk mode ─────────────────────────────────────────────────────────────────

def run_bulk(domain_list_path: str, args) -> None:
    """
    Skip blog discovery + link extraction.
    Process a pre-made plain-text list of domains directly.
    """
    domains = load_lines(Path(domain_list_path))
    logger.info("Bulk mode: loaded %d domains from %s", len(domains), domain_list_path)

    filtered = step3_filter(domains, args)
    available, relevance_map = step4_check(filtered, args)
    signals = step5_seo(available, args)
    scored = step6_score(signals, relevance_map)
    out = step7_export(scored)
    print(f"\nDone!  Results saved to: {out}\n")


# ── Full pipeline ─────────────────────────────────────────────────────────────

def run_full(args) -> None:
    t0 = time.time()

    blogs = step1_discover(args)
    outbound = step2_extract(blogs, args)
    filtered = step3_filter(outbound, args)
    available, relevance_map = step4_check(filtered, args)
    signals = step5_seo(available, args)
    scored = step6_score(signals, relevance_map)
    out = step7_export(scored)

    elapsed = time.time() - t0
    m, s = divmod(int(elapsed), 60)
    print(f"\nFull pipeline completed in {m}m {s}s")
    print(f"Results saved to: {out}\n")


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Automotive Expired-Domain Finder — finds available domains "
                    "with residual SEO value in the car niche."
    )
    parser.add_argument(
        "--bulk",
        metavar="FILE",
        help="Path to a plain-text domain list (one per line). "
             "Skips blog discovery and link extraction.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip slow NLP page-content check in Step 3 (keyword-in-name only).",
    )
    parser.add_argument(
        "--no-seo",
        action="store_true",
        dest="no_seo",
        help="Skip external SEO API calls; use Wayback + DNS only.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        dest="no_cache",
        help="Ignore intermediate cache files and re-run all steps.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=config.MAX_WORKERS,
        help=f"Number of parallel workers (default: {config.MAX_WORKERS}).",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Override output CSV path.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Apply CLI overrides
    if args.workers:
        config.MAX_WORKERS = args.workers
    if args.output:
        config.OUTPUT_CSV = Path(args.output)

    if args.bulk:
        run_bulk(args.bulk, args)
    else:
        run_full(args)
