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

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum
from pydantic import BaseModel, Field
from typing import Optional

# Make sibling modules in the api/ folder importable as top-level modules
sys.path.insert(0, str(Path(__file__).resolve().parent))
from auth import (  # noqa: E402
    hash_password, verify_password, create_token, verify_token,
    valid_email, valid_password, public_user,
)
from auth_store import get_user_store  # noqa: E402

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Automotive Domain Finder API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth dependencies ─────────────────────────────────────────────────────────

async def get_current_user(authorization: Optional[str] = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")
    token = authorization.split(" ", 1)[1]
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    user = get_user_store().get_user(payload["email"])
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists.")
    return public_user(user)


async def get_admin_user(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


# ── /api/auth/* ───────────────────────────────────────────────────────────────

class RegisterReq(BaseModel):
    email:     str
    password:  str
    full_name: str = ""


class LoginReq(BaseModel):
    email:    str
    password: str


@app.post("/api/auth/register")
async def auth_register(req: RegisterReq):
    if not valid_email(req.email):
        raise HTTPException(status_code=422, detail="Invalid email address.")
    ok, msg = valid_password(req.password)
    if not ok:
        raise HTTPException(status_code=422, detail=msg)

    store = get_user_store()
    if store.get_user(req.email):
        raise HTTPException(status_code=409, detail="Email already registered.")

    # First user becomes admin
    role = "admin" if not store.list_users() else "member"
    try:
        user = store.create_user(
            email=req.email,
            password_hash=hash_password(req.password),
            full_name=req.full_name or req.email.split("@")[0],
            role=role,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    public = public_user(user)
    return {"token": create_token(public), "user": public}


@app.post("/api/auth/login")
async def auth_login(req: LoginReq):
    if not valid_email(req.email):
        raise HTTPException(status_code=422, detail="Invalid email address.")
    store = get_user_store()
    user = store.get_user(req.email)
    if not user or not verify_password(req.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    from datetime import datetime, timezone
    store.update_user(req.email, last_login=datetime.now(timezone.utc).isoformat())
    public = public_user(user)
    public["last_login"] = datetime.now(timezone.utc).isoformat()
    return {"token": create_token(public), "user": public}


@app.get("/api/auth/me")
async def auth_me(user: dict = Depends(get_current_user)):
    return {"user": user}


# ── /api/admin/users ──────────────────────────────────────────────────────────

class RoleUpdate(BaseModel):
    role: str


@app.get("/api/admin/users")
async def admin_list_users(_admin: dict = Depends(get_admin_user)):
    users = [public_user(u) for u in get_user_store().list_users()]
    return {"users": users, "total": len(users)}


@app.post("/api/admin/users/{email}/role")
async def admin_update_role(email: str, body: RoleUpdate, admin: dict = Depends(get_admin_user)):
    if body.role not in ("admin", "member"):
        raise HTTPException(status_code=422, detail="Role must be 'admin' or 'member'.")
    if email.lower() == admin["email"].lower() and body.role != "admin":
        raise HTTPException(status_code=400, detail="You cannot remove your own admin role.")
    store = get_user_store()
    if not store.get_user(email):
        raise HTTPException(status_code=404, detail="User not found.")
    user = store.update_user(email, role=body.role)
    return {"user": public_user(user)}


@app.delete("/api/admin/users/{email}")
async def admin_delete_user(email: str, admin: dict = Depends(get_admin_user)):
    if email.lower() == admin["email"].lower():
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    store = get_user_store()
    if not store.delete_user(email):
        raise HTTPException(status_code=404, detail="User not found.")
    return {"deleted": True, "email": email}


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
async def filter_step(req: FilterRequest, _user: dict = Depends(get_current_user)):
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
async def check_step(req: CheckRequest, _user: dict = Depends(get_current_user)):
    import config
    from modules.domain_checker import check_domains_bulk  # noqa: PLC0415

    config.MAX_WORKERS     = req.workers
    config.REQUEST_TIMEOUT = req.request_timeout
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
    deep_seo:         bool = False   # if True, do DDG + CommonCrawl (slow!)


@app.post("/api/seo")
async def seo_step(req: SEORequest, _user: dict = Depends(get_current_user)):
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
        # Lightweight mode: just Wayback (no external keys, no DDG/CC slow calls)
        signals = []
        for d in domains:
            wb = _wayback_data(d)
            signals.append({
                "domain":               d,
                "wayback_snapshots":    wb["snapshots"],
                "wayback_first_seen":   wb["first_seen"],
                "wayback_last_seen":    wb["last_seen"],
                "has_archive_history":  wb["snapshots"] >= config.WAYBACK_MIN_SNAPSHOTS,
                "is_indexed_ddg":       False,
                "in_commoncrawl":       False,
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

    signals = estimate_seo_bulk(domains, workers=req.workers, deep=req.deep_seo)
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
async def score_step(req: ScoreRequest, _user: dict = Depends(get_current_user)):
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


# ── /api/whoisxml ────────────────────────────────────────────────────────────
#
# WhoisXML enrichment pass — call this on the *small* set of domains that
# already passed the cheap RDAP availability check, so we don't burn 1000+
# credits on the full candidate pool.
#
# Returns one row per domain:
#   whoisxml_availability  AVAILABLE | UNAVAILABLE | UNDETERMINED
#   expires_date           YYYY-MM-DD
#   created_date           YYYY-MM-DD
#   updated_date           YYYY-MM-DD
#   registrar              str
#   estimated_age_days     int
#   estimated_age_years    float
#   name_servers           list[str]
#   status                 list[str]    raw WHOIS status flags
#   error                  str          set if the call failed (rate_limited, http_403…)

class WhoisxmlRequest(BaseModel):
    domains:         list[str]
    whoisxml_key:    str
    workers:         int = Field(default=6, ge=1, le=12)
    request_timeout: int = Field(default=12, ge=3, le=30)


@app.post("/api/whoisxml")
async def whoisxml_step(
    req: WhoisxmlRequest,
    _user: dict = Depends(get_current_user),
):
    from modules.whoisxml import lookup_bulk  # noqa: PLC0415

    domains = [d.strip().lower() for d in req.domains if d.strip()]
    if not domains:
        raise HTTPException(status_code=422, detail="No domains provided.")
    if not req.whoisxml_key.strip():
        raise HTTPException(status_code=422, detail="WhoisXML API key required.")

    rows = lookup_bulk(
        domains, req.whoisxml_key.strip(),
        workers=req.workers, timeout=req.request_timeout,
    )
    by_domain = {r["domain"]: r for r in rows}

    confirmed_available = sum(
        1 for r in rows if r.get("whoisxml_availability") == "AVAILABLE"
    )
    errors = sum(1 for r in rows if r.get("error"))

    return {
        "rows":                rows,
        "by_domain":           by_domain,
        "total":               len(rows),
        "confirmed_available": confirmed_available,
        "errors":              errors,
    }


# ── /api/keyword-generate ────────────────────────────────────────────────────
#
# Step 1 of the keyword-based expired-domain finder.
# Takes a list of seed keywords and returns candidate domains built from
# keyword + prefix/suffix/combination patterns.  Downstream the SPA feeds
# these into /api/check → /api/seo → /api/score for the full pipeline.

class KeywordGenerateRequest(BaseModel):
    keywords:        list[str]
    tlds:            list[str] | None = None
    max_per_keyword: int  = Field(default=200, ge=10, le=600)
    include_combos:  bool = True


@app.post("/api/keyword-generate")
async def keyword_generate_step(
    req: KeywordGenerateRequest,
    _user: dict = Depends(get_current_user),
):
    from modules.keyword_generator import generate_candidates  # noqa: PLC0415

    seeds = [k.strip() for k in req.keywords if k.strip()]
    if not seeds:
        raise HTTPException(status_code=422, detail="No keywords provided.")

    candidates = generate_candidates(
        seeds,
        tlds=req.tlds,
        max_per_keyword=req.max_per_keyword,
        include_combos=req.include_combos,
    )
    # Build a quick lookup so the SPA can attach matched_keyword to each
    # available domain after /api/check returns.
    return {
        "candidates":   candidates,
        "domains":      [c["domain"] for c in candidates],
        "keyword_map":  {c["domain"]: c["matched_keyword"] for c in candidates},
        "total":        len(candidates),
        "keywords":     seeds,
    }


# ── /api/freshness ───────────────────────────────────────────────────────────
#
# Lightweight filter: takes the `available` list returned by /api/check and
# drops any domain whose RDAP expiry is older than `max_months` (default 24).
# Domains with no expiry date are kept by default (set `require_expiry=true`
# to drop them).

class FreshnessRequest(BaseModel):
    available:        list[dict]
    max_months:       int  = Field(default=24, ge=1, le=120)
    require_expiry:   bool = False


@app.post("/api/freshness")
async def freshness_step(
    req: FreshnessRequest,
    _user: dict = Depends(get_current_user),
):
    from modules.keyword_generator import is_recently_expired  # noqa: PLC0415

    fresh: list[dict] = []
    stale: list[dict] = []
    unknown: list[dict] = []

    for row in req.available:
        # Prefer WhoisXML-enriched expires_date (YYYY-MM-DD) over RDAP expiry_date
        exp = (row.get("expires_date") or row.get("expiry_date") or "").strip()
        if not exp:
            (stale if req.require_expiry else unknown).append(row)
            continue
        (fresh if is_recently_expired(exp, req.max_months) else stale).append(row)

    return {
        "fresh":   fresh + unknown,   # caller treats unknown-expiry as kept
        "stale":   stale,
        "total_in":  len(req.available),
        "total_fresh": len(fresh) + len(unknown),
    }


# ── /api/keyword-score ───────────────────────────────────────────────────────
#
# Final quality + traffic-potential scoring for the keyword finder.
# Operates on the merged signal list (post-SEO).  Computes:
#
#   • traffic_potential = ref_domains*0.4 + indexed_pages*0.3 +
#                         recent_snapshots*0.2 + authority*0.1
#   • quality_score     = authority*2 + live_backlinks*3 +
#                         recent_archive_bonus*5 + keyword_match_bonus*2 -
#                         spam_penalty*10
#
# Spam patterns (casino/pharma/adult/etc.) are already filtered upstream by
# modules/scorer.py — this endpoint applies one more pass and ranks results.

class KeywordScoreRequest(BaseModel):
    signals:           list[dict]
    keyword_map:       dict[str, str] = {}
    min_authority:     int = Field(default=0,  ge=0, le=100)
    min_backlinks:     int = Field(default=0,  ge=0)
    max_spam_score:    int = Field(default=70, ge=0, le=100)
    recently_dropped:  bool = False
    only_available:    bool = True


@app.post("/api/keyword-score")
async def keyword_score_step(
    req: KeywordScoreRequest,
    _user: dict = Depends(get_current_user),
):
    import re as _re

    SPAM_ANCHOR = _re.compile(
        r"(casino|poker|viagra|pharma|cialis|loan|payday|"
        r"bet(t?ing)?|porn|adult|xxx|escort|gambl|crypto-?pump)",
        _re.I,
    )

    out: list[dict] = []
    for sig in req.signals:
        d        = (sig.get("domain") or "").lower()
        kw       = req.keyword_map.get(d, sig.get("matched_keyword", ""))
        opr      = int(sig.get("page_rank_integer", 0) or 0)
        da       = int(sig.get("domain_authority",  0) or 0)
        cf       = int(sig.get("citation_flow",     0) or 0)
        bl       = int(sig.get("backlinks",         0) or 0)
        refd     = int(sig.get("ref_domains",       0) or 0)
        wb_total = int(sig.get("wayback_snapshots", 0) or 0)
        wb_last  = (sig.get("wayback_last_seen") or "")[:4]
        indexed  = bool(sig.get("is_indexed_ddg") or sig.get("in_commoncrawl"))

        # Authority — prefer Moz DA, then OPR×10, then Citation Flow
        authority = da if da > 0 else (opr * 10 if opr > 0 else cf)

        # Recent-archive bonus: archive activity within last 2 years
        try:
            wb_year = int(wb_last) if wb_last.isdigit() else 0
        except ValueError:
            wb_year = 0
        from datetime import datetime
        this_year = datetime.utcnow().year
        recent_archive_bonus = 1 if (this_year - wb_year) <= 2 and wb_year else 0

        # Keyword match: stem of domain contains a seed keyword
        stem = d.split(".")[0]
        keyword_match_bonus = 0
        if kw:
            for tok in kw.replace("+", " ").split():
                if tok and tok in stem:
                    keyword_match_bonus = 1
                    break

        # Spam penalty: anchor text from backlink scanner (if present) or
        # spammy stem characters
        spam_penalty = 0
        anchors = sig.get("anchor_text") or sig.get("anchors") or []
        if isinstance(anchors, list):
            for a in anchors:
                if isinstance(a, str) and SPAM_ANCHOR.search(a):
                    spam_penalty += 1
        if SPAM_ANCHOR.search(stem):
            spam_penalty += 2

        # Indexed-pages proxy: Wayback snapshots scaled, capped at 50
        indexed_pages = min(wb_total // 4, 50) + (10 if indexed else 0)

        # Recent snapshots proxy: 1 if archived within 2 years and has ≥5 snaps
        recent_snapshots = 5 if (recent_archive_bonus and wb_total >= 5) else 0

        traffic_potential = round(
            refd * 0.4 + indexed_pages * 0.3 + recent_snapshots * 0.2 + authority * 0.1,
            1,
        )

        quality_score = (
            authority * 2
            + min(bl, 5000) * 3 / 100        # scale backlinks so they don't dominate
            + recent_archive_bonus * 5
            + keyword_match_bonus * 2
            - spam_penalty * 10
        )
        quality_score = max(0, round(quality_score, 1))

        # Spam score 0–100 (rough estimate)
        spam_score = min(100, spam_penalty * 25)

        # Apply filters
        if req.only_available and not sig.get("available", True):
            continue
        if authority < req.min_authority:
            continue
        if bl < req.min_backlinks:
            continue
        if spam_score > req.max_spam_score:
            continue

        out.append({
            **sig,
            "matched_keyword":    kw,
            "authority":          authority,
            "live_backlinks":     bl,
            "ref_domains":        refd,
            "archive_freshness":  wb_last or "unknown",
            "recent_archive":    bool(recent_archive_bonus),
            "traffic_potential":  traffic_potential,
            "spam_score":         spam_score,
            "quality_score":      quality_score,
        })

    out.sort(key=lambda r: (-r["quality_score"], -r["traffic_potential"]))
    return {"scored": out, "total": len(out)}


# ── /api/history ──────────────────────────────────────────────────────────────

class HistoryRequest(BaseModel):
    domain:      str
    ahrefs_key:  str = ""
    semrush_key: str = ""


@app.post("/api/history")
async def domain_history(req: HistoryRequest, _user: dict = Depends(get_current_user)):
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
