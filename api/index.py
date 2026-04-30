"""
api/index.py — FastAPI backend for the Automotive Domain Finder web app.

Exposes four pipeline endpoints consumed by the SPA frontend:
  POST /api/filter  — Step 3: filter domains for automotive relevance
  POST /api/check   — Step 4: check domain availability
  POST /api/seo     — Step 5: gather SEO signals
  POST /api/score   — Step 6: score, rank, and apply threshold filters

Each endpoint is stateless; the browser manages intermediate state.
"""

import os
import sys
from pathlib import Path

# Make project-root modules importable from inside the api/ sub-directory.
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum
from pydantic import BaseModel, Field

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Automotive Domain Finder API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _apply_api_keys(req_dict: dict) -> None:
    """Push any API keys supplied in the request body into config."""
    import config  # noqa: PLC0415

    mapping = {
        "openpagerank_key": "OPENPAGERANK_API_KEY",
        "majestic_key":     "MAJESTIC_API_KEY",
        "moz_id":           "MOZ_ACCESS_ID",
        "moz_secret":       "MOZ_SECRET_KEY",
    }
    for field, attr in mapping.items():
        val = req_dict.get(field, "")
        if val:
            setattr(config, attr, val)


# ── /api/filter ───────────────────────────────────────────────────────────────

class FilterRequest(BaseModel):
    domains:     list[str]
    fast:        bool            = True
    skip_filter: bool            = False   # bypass automotive relevance check entirely
    workers:     int             = Field(default=5, ge=1, le=20)
    keywords:    list[str] | None = None


@app.post("/api/filter")
async def filter_step(req: FilterRequest):
    import config
    from modules.domain_filter import filter_automotive  # noqa: PLC0415

    domains = [d.strip().lower() for d in req.domains if d.strip()]
    if not domains:
        raise HTTPException(status_code=422, detail="No domains provided.")

    # When skip_filter=True the caller guarantees domains are already automotive —
    # pass them all through with a neutral relevance score of 1.
    if req.skip_filter:
        return {
            "filtered":    [[d, 1] for d in domains],
            "total_input": len(domains),
            "total_kept":  len(domains),
            "skipped":     True,
        }

    config.MAX_WORKERS = req.workers
    if req.keywords:
        config.AUTOMOTIVE_KEYWORDS = req.keywords

    filtered = filter_automotive(domains, slow_check=not req.fast, workers=req.workers)
    return {
        "filtered":    [[d, s] for d, s in filtered],
        "total_input": len(domains),
        "total_kept":  len(filtered),
        "skipped":     False,
    }


# ── /api/check ────────────────────────────────────────────────────────────────

class CheckRequest(BaseModel):
    domains:         list[str]
    workers:         int = Field(default=15, ge=1, le=30)
    request_timeout: int = Field(default=7,  ge=2, le=30)   # per-request HTTP timeout


@app.post("/api/check")
async def check_step(req: CheckRequest):
    import config
    from modules.domain_checker import check_domains_bulk  # noqa: PLC0415

    config.MAX_WORKERS      = req.workers
    config.REQUEST_TIMEOUT  = req.request_timeout
    domains = [d.strip().lower() for d in req.domains if d.strip()]
    if not domains:
        raise HTTPException(status_code=422, detail="No domains provided.")

    available = check_domains_bulk(domains, workers=req.workers)
    return {
        "available":       available,
        "total_input":     len(domains),
        "total_available": len(available),
    }


# ── /api/seo ──────────────────────────────────────────────────────────────────

class SEORequest(BaseModel):
    domains:          list[str]
    workers:          int = Field(default=15, ge=1, le=30)
    request_timeout:  int = Field(default=7,  ge=2, le=30)
    openpagerank_key: str = ""
    majestic_key:     str = ""
    moz_id:           str = ""
    moz_secret:       str = ""
    no_seo:           bool = False


@app.post("/api/seo")
async def seo_step(req: SEORequest):
    import config
    from modules.seo_estimator import (  # noqa: PLC0415
        _wayback_data,
        _is_indexed,
        _in_commoncrawl,
        estimate_seo_bulk,
    )

    config.MAX_WORKERS     = req.workers
    config.REQUEST_TIMEOUT = req.request_timeout
    _apply_api_keys(req.model_dump())

    domains = [d.strip().lower() for d in req.domains if d.strip()]
    if not domains:
        raise HTTPException(status_code=422, detail="No domains provided.")

    if req.no_seo:
        # Lightweight mode: Wayback + index checks only (no external API keys needed)
        signals = []
        for d in domains:
            wb = _wayback_data(d)
            signals.append({
                "domain":               d,
                "wayback_snapshots":    wb["snapshots"],
                "wayback_first_seen":   wb["first_seen"],
                "wayback_last_seen":    wb["last_seen"],
                "has_archive_history":  wb["snapshots"] >= config.WAYBACK_MIN_SNAPSHOTS,
                "is_indexed_ddg":       _is_indexed(d),
                "in_commoncrawl":       _in_commoncrawl(d),
                "page_rank_integer":    0,
                "page_rank_decimal":    0.0,
                "citation_flow":        0,
                "trust_flow":           0,
                "backlinks":            0,
                "ref_domains":          0,
                "domain_authority":     0,
                "moz_linking_domains":  0,
            })
        return {"signals": signals, "total": len(signals)}

    signals = estimate_seo_bulk(domains, workers=req.workers)
    return {"signals": signals, "total": len(signals)}


# ── /api/score ────────────────────────────────────────────────────────────────

class ScoreRequest(BaseModel):
    signals:           list[dict]
    relevance:         dict[str, int] = {}
    min_score:         int   = Field(default=25, ge=0,  le=100)
    min_da:            int   = Field(default=0,  ge=0,  le=100)
    min_tf:            int   = Field(default=0,  ge=0,  le=100)
    min_opr:           int   = Field(default=0,  ge=0,  le=10)
    min_wayback:       int   = Field(default=0,  ge=0)
    min_backlinks:     int   = Field(default=0,  ge=0)
    max_domain_length: int   = Field(default=35, ge=5,  le=63)


@app.post("/api/score")
async def score_step(req: ScoreRequest):
    import config
    import modules.scorer as scorer_module  # noqa: PLC0415
    from modules.scorer import score_all    # noqa: PLC0415

    config.MIN_SCORE_TO_KEEP = req.min_score
    scorer_module.MAX_DOMAIN_LENGTH = req.max_domain_length

    # Attach relevance score before scoring
    signals = []
    for sig in req.signals:
        s = dict(sig)
        s.setdefault("auto_relevance_score", req.relevance.get(s.get("domain", ""), 0))
        signals.append(s)

    scored = score_all(signals, req.relevance)

    # ── Compute effective_da ───────────────────────────────────────────────────
    # Moz DA is the gold standard but requires paid credentials.  Fall back to:
    #   OPR × 10  (OpenPageRank 0-10 → 0-100 scale)   if Moz DA = 0
    #   Majestic Citation Flow                          if OPR also = 0
    for s in scored:
        moz = s.get("domain_authority", 0) or 0
        opr = s.get("page_rank_integer", 0) or 0
        cf  = s.get("citation_flow",    0) or 0
        if moz > 0:
            s["effective_da"]  = moz
            s["da_source"]     = "moz"
        elif opr > 0:
            s["effective_da"]  = min(round(opr * 10), 100)
            s["da_source"]     = "opr"
        elif cf > 0:
            s["effective_da"]  = min(round(cf), 100)
            s["da_source"]     = "cf"
        else:
            s["effective_da"]  = 0
            s["da_source"]     = "none"

    # Apply per-metric floor filters (use effective_da for the DA filter)
    if req.min_da        > 0:
        scored = [s for s in scored if s.get("effective_da",      0) >= req.min_da]
    if req.min_tf        > 0:
        scored = [s for s in scored if s.get("trust_flow",        0) >= req.min_tf]
    if req.min_opr       > 0:
        scored = [s for s in scored if s.get("page_rank_integer", 0) >= req.min_opr]
    if req.min_wayback   > 0:
        scored = [s for s in scored if s.get("wayback_snapshots", 0) >= req.min_wayback]
    if req.min_backlinks > 0:
        scored = [s for s in scored if s.get("backlinks",         0) >= req.min_backlinks]

    return {"scored": scored, "total": len(scored)}


# ── /api/history ──────────────────────────────────────────────────────────────

class HistoryRequest(BaseModel):
    domain:      str
    ahrefs_key:  str = ""
    semrush_key: str = ""


@app.post("/api/history")
async def domain_history(req: HistoryRequest):
    import requests as _requests
    import config
    from modules.utils import make_session, safe_get  # noqa: PLC0415

    domain = req.domain.strip().lower()

    # ── 1. Wayback Machine — monthly breakdown ─────────────────────────────────
    params = {
        "url":       domain,
        "matchType": "domain",
        "output":    "json",
        "fl":        "timestamp",
        "limit":     "5000",
        "collapse":  "timestamp:6",   # one record per calendar month
        "filter":    "statuscode:200",
    }
    session = make_session()
    resp    = safe_get(config.WAYBACK_CDX_URL, session, params=params)

    wayback_by_year:  dict[str, int] = {}
    wayback_by_month: dict[str, int] = {}
    first_seen = last_seen = None

    if resp:
        try:
            rows = resp.json()
            timestamps = [r[0] for r in rows[1:] if r]
            if timestamps:
                first_seen = timestamps[0][:8]
                last_seen  = timestamps[-1][:8]
                for ts in timestamps:
                    yr  = ts[:4]
                    mo  = f"{ts[:4]}-{ts[4:6]}"
                    wayback_by_year[yr] = wayback_by_year.get(yr, 0) + 1
                    wayback_by_month[mo] = wayback_by_month.get(mo, 0) + 1
        except Exception:
            pass

    result: dict = {
        "domain":            domain,
        "wayback_by_year":   dict(sorted(wayback_by_year.items())),
        "wayback_by_month":  dict(sorted(wayback_by_month.items())),
        "first_seen":        first_seen,
        "last_seen":         last_seen,
        "ahrefs":            None,
        "semrush":           None,
        "ahrefs_error":      None,
        "semrush_error":     None,
    }

    # ── 2. Ahrefs organic history (v3 API) ────────────────────────────────────
    if req.ahrefs_key.strip():
        from datetime import datetime, timedelta  # noqa: PLC0415
        today        = datetime.now().strftime("%Y-%m-%d")
        two_years_ago = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
        try:
            ar = _requests.get(
                "https://api.ahrefs.com/v3/site-explorer/metrics-history",
                params={
                    "target":      domain,
                    "date_from":   two_years_ago,
                    "date_to":     today,
                    "volume_mode": "monthly",
                    "mode":        "domain",
                },
                headers={"Authorization": f"Bearer {req.ahrefs_key.strip()}"},
                timeout=15,
            )
            if ar.ok:
                metrics = ar.json().get("metrics", [])
                result["ahrefs"] = {
                    "traffic_history":  [{"date": m["date"], "traffic":  m.get("org_traffic",  0)} for m in metrics],
                    "keywords_history": [{"date": m["date"], "keywords": m.get("org_keywords", 0)} for m in metrics],
                }
            else:
                result["ahrefs_error"] = f"{ar.status_code}: {ar.text[:200]}"
        except Exception as e:
            result["ahrefs_error"] = str(e)

    # ── 3. Semrush organic history ────────────────────────────────────────────
    if req.semrush_key.strip() and not result["ahrefs"]:
        try:
            sr = _requests.get(
                "https://api.semrush.com/",
                params={
                    "type":            "domain_rank_history",
                    "key":             req.semrush_key.strip(),
                    "export_columns":  "Dt,Or,Ot",
                    "domain":          domain,
                    "database":        "us",
                    "display_limit":   "24",
                },
                timeout=15,
            )
            if sr.ok:
                lines = sr.text.strip().split("\r\n")
                history = []
                for line in lines[1:]:
                    parts = line.split(";")
                    if len(parts) >= 3:
                        try:
                            history.append({
                                "date":     parts[0],
                                "keywords": int(parts[1]),
                                "traffic":  int(parts[2]),
                            })
                        except ValueError:
                            pass
                result["semrush"] = {"history": history}
            else:
                result["semrush_error"] = f"{sr.status_code}: {sr.text[:200]}"
        except Exception as e:
            result["semrush_error"] = str(e)

    return result


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ── Vercel / Lambda handler ───────────────────────────────────────────────────
handler = Mangum(app, lifespan="off")
