# Automotive Expired-Domain Finder

A fully automated, production-grade Python tool that discovers expired and available
domains in the automotive niche by analysing outbound links from authority car blogs
in the USA and Canada.

No SEMrush. No paid subscriptions required.

---

## Architecture

```
Step 1  Blog Discovery      → discover_blogs()        blog_discovery.py
Step 2  Outbound Extraction → extract_all_outbound()   link_extractor.py
Step 3  Auto-niche Filter   → filter_automotive()      domain_filter.py
Step 4  Availability Check  → check_domains_bulk()     domain_checker.py
Step 5  SEO Estimation      → estimate_seo_bulk()      seo_estimator.py
Step 6  Scoring             → score_all()              scorer.py
Step 7  CSV Export          → to_csv()                 exporter.py
```

Each step caches its output in `data/cache_*.json` so you can resume or re-run
individual steps without repeating expensive network calls.

---

## Installation

**Python 3.10+ required.**

```bash
cd tools/automotive-domain-finder
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## Quick Start

```bash
# Full pipeline (steps 1–7)
python main.py

# Fast mode — keyword-only niche filter, no page fetches in step 3
python main.py --fast

# No external SEO API calls — uses Wayback + DNS only
python main.py --no-seo

# Process a pre-made domain list (skip blog discovery + crawling)
python main.py --bulk my_domains.txt

# Override output path
python main.py --output /tmp/results.csv

# Re-run ignoring all caches
python main.py --no-cache

# Combine flags
python main.py --fast --no-seo --workers 20
```

---

## Optional API Keys (all free tiers available)

Set as environment variables or edit `config.py` directly.

| Signal source    | Env var              | Free tier              | Sign-up URL                              |
|------------------|----------------------|------------------------|------------------------------------------|
| OpenPageRank     | `OPENPAGERANK_KEY`   | Unlimited (free)       | https://www.domcop.com/openpagerank/     |
| Majestic         | `MAJESTIC_KEY`       | 1 000 req/month        | https://developer.majestic.com/          |
| Moz Links API    | `MOZ_ID` + `MOZ_SECRET` | 10 req/min          | https://moz.com/products/api             |
| Google CSE       | `GOOGLE_API_KEY` + `GOOGLE_CX` | 100 free/day | https://programmablesearchengine.google.com/ |

All sources are optional — the tool runs without any keys using:
- Wayback Machine CDX API (free, no key)
- DuckDuckGo HTML scraping (free, no key)
- CommonCrawl Index API (free, no key)
- RDAP / WHOIS (free, no key)

---

## Proxy Support

Add proxy strings to `PROXIES` in `config.py` or pass via environment:

```bash
PROXIES="http://user:pass@proxy1:8080,http://user:pass@proxy2:8080" python main.py
```

Each worker picks a random proxy from the list.

---

## Output

Results are written to `output/automotive_domains.csv`.

| Column                | Description                               |
|-----------------------|-------------------------------------------|
| `domain`              | Apex domain (e.g. `motorclassics.com`)    |
| `seo_score`           | 0–100 composite score                     |
| `available`           | `Available` or `Registered`               |
| `has_archive_history` | `Yes` / `No` (≥5 Wayback snapshots)       |
| `wayback_snapshots`   | Total daily snapshots found               |
| `wayback_first_seen`  | YYYYMMDD of first capture                 |
| `is_indexed_ddg`      | `Yes` if found via DuckDuckGo site: search|
| `in_commoncrawl`      | `Yes` if found in CommonCrawl index       |
| `page_rank_integer`   | OpenPageRank 0–10 (if key provided)       |
| `citation_flow`       | Majestic Citation Flow (if key provided)  |
| `trust_flow`          | Majestic Trust Flow (if key provided)     |
| `backlinks`           | Majestic inbound link count               |
| `domain_authority`    | Moz DA (if keys provided)                 |
| `score_wayback`       | Sub-score contribution (0–30)             |
| `score_backlinks`     | Sub-score contribution (0–30)             |
| `score_domain_name`   | Sub-score contribution (0–20)             |
| `score_indexed`       | Sub-score contribution (0–20)             |
| `auto_relevance_score`| Automotive niche relevance (0–40)         |

---

## Scoring Model

```
Total score (0–100) =
    Wayback depth     (0–30) +
    Backlink signals  (0–30) +
    Domain name quality (0–20) +
    Index presence    (0–20) +
    Automotive bonus  (0–5)
```

Domains below **25/100** are discarded automatically.
Spam/junk domains (casino, pharma, gibberish) are also removed.

---

## Tuning

Edit `config.py` to adjust:

| Constant              | Default | Effect                                    |
|-----------------------|---------|-------------------------------------------|
| `MAX_WORKERS`         | 10      | Thread pool size (raise for faster runs)  |
| `MAX_PAGES_PER_BLOG`  | 15      | Pages crawled per blog                    |
| `WAYBACK_MIN_SNAPSHOTS` | 5     | Minimum daily snapshots for archive signal|
| `MIN_SCORE_TO_KEEP`   | 25      | Drop anything below this score            |
| `CRAWL_DELAY`         | 1.2 s   | Polite delay between requests per worker  |

---

## Example Output (truncated)

```
==========================================================================================
DOMAIN                               SCORE   ARCHIVE  INDEXED      BL   OPR
==========================================================================================
motorgarage.com                       78.5       Yes      Yes   12400     6
autoclassics.net                      71.2       Yes      Yes    4200     5
carreviewnews.com                     64.8       Yes       No     800     3
xtremecars.com                        61.0       Yes      Yes    1100     4
canadianmotorhead.ca                  57.3       Yes      Yes     320     3
…
```

---

## File Structure

```
automotive-domain-finder/
├── main.py               # Entry point + pipeline orchestrator
├── config.py             # All configuration (keys, weights, thresholds)
├── requirements.txt
├── README.md
├── modules/
│   ├── __init__.py
│   ├── blog_discovery.py # Step 1: find authority car blogs
│   ├── link_extractor.py # Step 2: crawl + extract outbound links
│   ├── domain_filter.py  # Step 3: automotive NLP filter
│   ├── domain_checker.py # Step 4: RDAP/WHOIS availability
│   ├── seo_estimator.py  # Step 5: Wayback / OPR / Majestic / Moz
│   ├── scorer.py         # Step 6: composite score + spam filter
│   ├── exporter.py       # Step 7: CSV writer
│   └── utils.py          # Shared HTTP / domain helpers
├── data/
│   ├── seed_blogs.txt    # Pre-loaded seed blog list
│   ├── cache_blogs.json          # Step 1 cache
│   ├── cache_outbound.json       # Step 2 cache
│   ├── cache_auto_filtered.json  # Step 3 cache
│   ├── cache_available.json      # Step 4 cache
│   └── cache_seo_signals.json    # Step 5 cache
└── output/
    └── automotive_domains.csv    # Final results
```

---

## Legal & Ethics

- This tool sends HTTP requests to public websites. Respect `robots.txt` and Terms of
  Service. Use `CRAWL_DELAY` to avoid overloading servers.
- Domain availability data is queried via public RDAP/WHOIS servers — no scraping of
  registrar purchase flows.
- The tool does **not** attempt to purchase, redirect, or exploit discovered domains.
