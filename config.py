"""
config.py — Central configuration for the Automotive Domain Finder
"""

from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent

# On Vercel (and other serverless platforms) the project root is read-only;
# only /tmp is writable.  Set VERCEL=1 (Vercel sets this automatically) or
# ADF_WRITABLE_ROOT to redirect cache/output to a writable location.
import os as _os
_writable = Path(_os.environ["ADF_WRITABLE_ROOT"]) if _os.environ.get("ADF_WRITABLE_ROOT") else (
    Path("/tmp") if _os.environ.get("VERCEL") else BASE_DIR
)

DATA_DIR   = _writable / "adf_data"
OUTPUT_DIR = _writable / "adf_output"
LOGS_DIR   = _writable / "adf_logs"

for d in (DATA_DIR, OUTPUT_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── Concurrency ────────────────────────────────────────────────────────────────
MAX_WORKERS = 10          # Thread pool size for I/O-bound tasks
REQUEST_TIMEOUT = 15      # seconds
CRAWL_DELAY = 1.2         # seconds between requests per worker (be polite)
MAX_PAGES_PER_BLOG = 15   # homepage + up to N article pages

# ── HTTP ───────────────────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# Optional: list proxies as "http://user:pass@host:port"
PROXIES: list[str] = []

# ── Domain Blacklist (big platforms to drop) ───────────────────────────────────
BLACKLISTED_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "linkedin.com", "pinterest.com", "tiktok.com", "reddit.com", "tumblr.com",
    "amazon.com", "ebay.com", "etsy.com", "walmart.com", "bestbuy.com",
    "google.com", "google.ca", "google.co.uk", "bing.com", "yahoo.com",
    "apple.com", "microsoft.com", "github.com", "stackoverflow.com",
    "wordpress.com", "wordpress.org", "blogger.com", "medium.com",
    "wikipedia.org", "wikimedia.org", "wikidata.org",
    "t.co", "bit.ly", "ow.ly", "buff.ly", "dlvr.it",
    "gravatar.com", "wp.com", "feedburner.com", "mailchimp.com",
    "cloudflarechallenge.com", "cloudflare.com", "akamaized.net",
    "bootstrapcdn.com", "jsdelivr.net", "cdnjs.cloudflare.com",
    "fonts.googleapis.com", "fonts.gstatic.com",
    "autotrader.com", "cars.com", "cargurus.com", "kbb.com",
    "edmunds.com", "carfax.com", "truecar.com",
    # Canadian big sites
    "autotrader.ca", "kijiji.ca", "craigslist.org",
}

# ── Automotive Keywords ────────────────────────────────────────────────────────
AUTOMOTIVE_KEYWORDS = [
    # English — vehicles
    "car", "cars", "auto", "autos", "automotive", "vehicle", "vehicles",
    "motor", "motors", "motorcar", "motoring",
    "drive", "driver", "driving", "driveways",
    "wheel", "wheels", "rim", "rims",
    "tire", "tires", "tyre", "tyres",
    "engine", "engines", "transmission", "gearbox",
    "truck", "trucks", "pickup",
    "suv", "crossover", "sedan", "coupe", "hatchback", "minivan", "van",
    "hybrid", "electric", "ev", "bev", "phev",
    "race", "racing", "dragstrip", "nascar", "formula", "rally",
    "garage", "mechanic", "repair", "service", "detailing",
    "horsepower", "torque", "turbo", "supercharge",
    "dealership", "dealer", "fleet",
    "bmw", "mercedes", "audi", "ford", "chevy", "chevrolet", "gmc",
    "toyota", "honda", "nissan", "subaru", "mazda", "kia", "hyundai",
    "jeep", "dodge", "chrysler", "ram", "cadillac", "lincoln", "buick",
    "tesla", "rivian", "lucid", "polestar", "volvo",
    "porsche", "ferrari", "lamborghini", "mclaren", "bugatti",
    "moto", "motorcycle", "bike", "biker",
    # French (Canadian bilingual blogs)
    "voiture", "camion", "moteur", "conduite",
]

# ── Scoring Weights ────────────────────────────────────────────────────────────
SCORE_WEIGHTS = {
    "wayback_snapshots": 0.30,   # Archive.org presence
    "backlink_signals": 0.30,    # Majestic/Moz/CommonCrawl
    "domain_name_quality": 0.20, # Short, brandable, niche keyword
    "indexed_signals": 0.20,     # Bing/DDG index check
}

# ── Scoring Thresholds ─────────────────────────────────────────────────────────
MIN_SCORE_TO_KEEP = 25           # 0–100 scale; drop anything below

# ── Wayback Machine ────────────────────────────────────────────────────────────
WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_MIN_SNAPSHOTS = 5        # require at least N snapshots

# ── Majestic (free Bulk Backlink Checker, 1 000 req/month on free tier) ───────
# Leave empty to skip. Sign up at https://developer.majestic.com/
MAJESTIC_API_KEY = ""

# ── Moz (free tier: 10 req/min) ───────────────────────────────────────────────
# https://moz.com/products/api
MOZ_ACCESS_ID = ""
MOZ_SECRET_KEY = ""

# ── OpenPageRank (completely free) ────────────────────────────────────────────
# https://www.domcop.com/openpagerank/
OPENPAGERANK_API_KEY = ""   # free at openpagerank.com

# ── RDAP / WHOIS domain-availability check ────────────────────────────────────
# We use IANA RDAP bootstrap + python-whois as fallback. No API key needed.
RDAP_BOOTSTRAP = "https://rdap.org/domain/"

# ── Google Custom Search API (optional, for blog discovery) ──────────────────
# Leave empty to fall back to DuckDuckGo HTML scraping
GOOGLE_API_KEY = ""
GOOGLE_CX = ""               # Custom Search Engine ID

# ── Output ─────────────────────────────────────────────────────────────────────
OUTPUT_CSV = OUTPUT_DIR / "automotive_domains.csv"
SEED_BLOGS_FILE = DATA_DIR / "seed_blogs.txt"
