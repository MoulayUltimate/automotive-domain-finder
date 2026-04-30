"""
exporter.py — Step 7: Write clean CSV output
"""

import csv
from pathlib import Path

import config
from modules.utils import get_logger

logger = get_logger("exporter")

CSV_COLUMNS = [
    "domain",
    "seo_score",
    "available",
    "has_archive_history",
    "wayback_snapshots",
    "wayback_first_seen",
    "wayback_last_seen",
    "is_indexed_ddg",
    "in_commoncrawl",
    "page_rank_integer",
    "page_rank_decimal",
    "citation_flow",
    "trust_flow",
    "backlinks",
    "ref_domains",
    "domain_authority",
    "moz_linking_domains",
    "dns_resolves",
    "expiry_date",
    "registrar",
    "check_method",
    "score_wayback",
    "score_backlinks",
    "score_domain_name",
    "score_indexed",
    "auto_relevance_score",
]


def to_csv(records: list[dict], path: Path | None = None) -> Path:
    """
    Write records to CSV.  Returns the output path.
    """
    out_path = path or config.OUTPUT_CSV
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=CSV_COLUMNS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for rec in records:
            # Ensure boolean columns render as Yes/No for readability
            row = dict(rec)
            for bool_col in ("has_archive_history", "is_indexed_ddg", "in_commoncrawl", "dns_resolves"):
                row[bool_col] = "Yes" if row.get(bool_col) else "No"
            row["available"] = "Available" if row.get("available") else "Registered"
            writer.writerow(row)

    logger.info("Exported %d domains → %s", len(records), out_path)
    return out_path


def print_summary(records: list[dict]) -> None:
    """Print a quick terminal summary table."""
    print("\n" + "=" * 90)
    print(f"{'DOMAIN':<35} {'SCORE':>6}  {'ARCHIVE':>7}  {'INDEXED':>7}  {'BL':>6}  {'OPR':>4}")
    print("=" * 90)
    for r in records[:50]:   # show top 50
        print(
            f"{r.get('domain', ''):<35}"
            f" {r.get('seo_score', 0):>6.1f}"
            f"  {'Yes' if r.get('has_archive_history') else 'No':>7}"
            f"  {'Yes' if r.get('is_indexed_ddg') else 'No':>7}"
            f"  {r.get('backlinks', 0):>6}"
            f"  {r.get('page_rank_integer', 0):>4}"
        )
    if len(records) > 50:
        print(f"  … and {len(records) - 50} more (see CSV)")
    print("=" * 90 + "\n")
