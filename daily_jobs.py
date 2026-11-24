"""
Pull new roles: scrape job boards with JobSpy, apply your preferences, save new
matches to jobs.db as "Not applied", then let Claude screen them (ai_screen.py).

Preferences come from the Preferences page in the web UI (stored in jobs.db).

  1. Search    - JobSpy searches each search term x each location (+ remote),
                 limited to postings from the last N hours.
  2. Rules     - MANDATORY preferences that can be checked reliably from the
                 scraped data are applied here (title words, location, stated
                 years, stated pay, job type, excluded companies).
                 Non-mandatory preferences are never used to drop a role here.
  3. AI screen - Claude reads each new role's full description against all
                 preferences + your prompt and auto-marks clear misses
                 "Not a fit" (see ai_screen.py).

Usage:
  .venv\\Scripts\\python.exe daily_jobs.py          # normal run
  .venv\\Scripts\\python.exe daily_jobs.py --reuse  # re-filter today's cached scrape
  .venv\\Scripts\\python.exe daily_jobs.py --no-ai  # skip the AI screen
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import re
import sys
import time
from pathlib import Path

import db

HERE = Path(__file__).resolve().parent

NYC_PLACES = {
    "new york", "new york city", "nyc", "manhattan", "brooklyn", "queens",
    "bronx", "the bronx", "staten island", "long island city",
}
US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire",
    "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee",
    "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}
# Level numbers that mean senior+: "Engineer 5", "Engineer IV", "E5", "L6", "Stf"
LEVEL_NUM_RE = re.compile(r"\b((?:engineer|developer)\s+(?:[5-9]|iv|v|vi)|[el][5-9]|stf)\b", re.I)
AI_TERMS = re.compile(
    r"\b(ai|a\.i\.|ml|machine learning|llm|llms|genai|gen ai|generative|agentic|"
    r"agents?|nlp|deep learning|applied ai|artificial intelligence|rag|"
    r"computer vision|foundation models?)\b", re.I)
JOB_TYPE_ALIASES = {"fulltime": "fulltime", "full-time": "fulltime", "full time": "fulltime",
                    "contract": "contract", "contractor": "contract", "parttime": "parttime",
                    "part-time": "parttime", "internship": "internship", "temporary": "contract"}


def words_re(words: list[str]):
    words = [w.strip() for w in words if w and w.strip()]
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(w).replace(r"\ ", r"[\s-]+") + r"\.?"
                                        for w in sorted(words, key=len, reverse=True)) + r")\b", re.I)


# ----------------------------------------------------------------------------
# Fetching (JobSpy)
# ----------------------------------------------------------------------------
def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except (TypeError, ValueError, ImportError):
        pass
    return v


# With "Limit results per search" off, ask for this many per search: about the most
# the boards return for one search, so in practice everything in the date window.
NO_LIMIT_RESULTS = 1000


def fetch_jobs(prefs: dict, log=print, stats: dict | None = None) -> list[dict]:
    """Run every search; stats (optional) receives the number of searches and the failed ones."""
    from jobspy import scrape_jobs
    stats = stats if stats is not None else {}
    stats.update(searches=0, failed=[])
    results_wanted = (NO_LIMIT_RESULTS if prefs.get("results_all")
                      else int(prefs.get("results_per_search") or 40))

    loc = prefs["locations"]
    job_types = prefs["employment_types"]["value"]
    job_type = job_types[0] if len(job_types) == 1 else None
    runs = []
    for term in prefs["search_terms"]:
        for place in loc["value"]:
            runs.append(dict(search_term=term, location=place,
                             distance=loc.get("radius_miles") or 15, is_remote=False))
        if loc.get("include_remote"):
            # LinkedIn/ZipRecruiter honour is_remote; Indeed ignores it when
            # hours_old is set, so location="Remote" covers Indeed.
            runs.append(dict(search_term=term, location="Remote", is_remote=True))

    jobs: dict[str, dict] = {}
    for r in runs:
        label = f"{r['search_term']} @ {r['location']}"
        log(f"Searching: {label}")
        stats["searches"] += 1
        try:
            df = scrape_jobs(
                site_name=prefs["sites"],
                results_wanted=results_wanted,
                hours_old=int(prefs.get("posted_within_hours") or 24),
                country_indeed="USA",
                job_type=job_type if not r["is_remote"] else None,
                fetch_description=True,
                enforce_annual_salary=True,
                description_format="markdown",
                verbose=0,
                **r,
            )
        except Exception as e:  # one bad site/search shouldn't kill the run
            log(f"  ! {label} failed: {e}")
            stats["failed"].append(f"{label}: {str(e)[:200]}")
            continue
        for row in df.to_dict("records"):
            row = {k: _clean(v) for k, v in row.items()}
            key = row.get("job_url") or row.get("id")
            if key:
                jobs.setdefault(key, row)
        log(f"  {len(df)} results")
        time.sleep(2)
    return list(jobs.values())


# ----------------------------------------------------------------------------
# Missing descriptions (ZipRecruiter during pulls; ZipRecruiter and LinkedIn on request)
# ----------------------------------------------------------------------------
# JobSpy fetches every ZipRecruiter job page back to back; after about 20 requests
# ZipRecruiter starts refusing them, and the rest of the jobs come back with no
# description. So afterwards we fetch the missing ones again, slowly, and only for
# listings we'd actually keep.
ZIP_PAUSE = (4.0, 8.0)        # seconds between job pages
ZIP_BACKOFF = 30.0            # extra wait after a refused request (grows with each in a row)
ZIP_MAX_FAILURES = 3          # give up for this pull after this many refusals in a row
ZIP_MAX_FETCHES = 50          # at most this many per pull, to keep pulls from running long


def _zip_fetcher():
    """A function job_url -> details dict, using JobSpy's own ZipRecruiter page parser."""
    from jobspy.model import DescriptionFormat, ScraperInput
    from jobspy.util import TLSRotating, create_session
    from jobspy.ziprecruiter import ZipRecruiter
    zr = ZipRecruiter()
    zr.scraper_input = ScraperInput(fetch_description=True, description_format=DescriptionFormat.MARKDOWN)
    zr.session = create_session(TLSRotating(None), zr.scraper_input)
    zr.session.impersonate = "safari"
    zr.warn = lambda msg: None            # failures show up as an empty result
    return lambda job: zr._fetch_details(job.get("id"), job["job_url"])


def linkedin_job_id(url) -> str | None:
    """'https://www.linkedin.com/jobs/view/4123456789' (or '.../view/some-title-4123456789/') -> '4123456789'."""
    m = re.search(r"linkedin\.com/jobs/view/(?:[\w%-]*-)?(\d{5,})", str(url or ""))
    return m.group(1) if m else None


def _linkedin_fetcher():
    """A function listing -> details dict, using JobSpy's own LinkedIn job-page parser."""
    from jobspy.linkedin import LinkedIn
    from jobspy.linkedin.constant import headers
    from jobspy.model import DescriptionFormat, ScraperInput
    from jobspy.util import RequestsRotating, create_session
    li = LinkedIn()
    li.scraper_input = ScraperInput(fetch_description=True, description_format=DescriptionFormat.MARKDOWN)
    li.session = create_session(RequestsRotating(None, clear_cookies=True), li.scraper_input, headers)
    li.warn = lambda msg: None            # failures show up as an empty result

    def fetch(job):
        job_id = linkedin_job_id(job.get("job_url"))
        return li._fetch_details(job_id) if job_id else {}
    return fetch


LINKEDIN_PAUSE = (3.0, 7.0)   # seconds between LinkedIn job pages (JobSpy's own pace)
BACKFILL_MAX_PER_SITE = 100   # "Fetch missing descriptions": at most this many pages per board per run


def _description_sites() -> dict:
    """Boards whose job pages can be fetched again for a missing description. Looked up
    when called, so tests can swap the fetchers and pauses."""
    return {
        "zip_recruiter": {"label": "ZipRecruiter", "make_fetcher": _zip_fetcher, "pause": ZIP_PAUSE},
        "linkedin": {"label": "LinkedIn", "make_fetcher": _linkedin_fetcher, "pause": LINKEDIN_PAUSE},
    }


def fill_descriptions(jobs: list[dict], label: str, make_fetcher, pause, limit: int,
                      log=print, fetch=None, sleep=time.sleep) -> dict:
    """Fetch missing descriptions for these listings from one board, a few seconds apart,
    backing off when the board refuses and stopping after ZIP_MAX_FAILURES refusals in a
    row. Updates the listings in place. Returns {"tried", "fetched", "stopped_early"}
    (plus "error" if the fetcher couldn't be set up)."""
    jobs = jobs[:limit]
    stats = {"tried": 0, "fetched": 0, "stopped_early": False}
    if not jobs:
        return stats
    if fetch is None:
        try:
            fetch = make_fetcher()
        except Exception as e:  # JobSpy internals changed: skip this step, don't fail the run
            log(f"  ! Couldn't set up {label} description fetching ({type(e).__name__}: {e}); skipped")
            stats["error"] = f"{type(e).__name__}: {e}"
            return stats
    log(f"Fetching {len(jobs)} missing {label} description(s), a few seconds apart...")
    streak = 0
    for i, job in enumerate(jobs):
        if i:
            sleep(random.uniform(*pause))
        stats["tried"] += 1
        try:
            details = fetch(job) or {}
        except Exception:
            details = {}
        if details.get("description"):
            for k, v in details.items():
                if v is not None and not job.get(k):
                    job[k] = v
            stats["fetched"] += 1
            streak = 0
            continue
        streak += 1
        if streak >= ZIP_MAX_FAILURES:
            stats["stopped_early"] = True
            log(f"  ! {label} kept refusing; stopped after {stats['tried']} (try again later)")
            break
        sleep(ZIP_BACKOFF * streak)
    log(f"  got {stats['fetched']} of {stats['tried']} {label} description(s)")
    return stats


def fill_zip_descriptions(jobs: list[dict], log=print, fetch=None, sleep=time.sleep) -> dict:
    """fill_descriptions for ZipRecruiter listings during a pull (at most ZIP_MAX_FETCHES)."""
    return fill_descriptions(jobs, "ZipRecruiter", _zip_fetcher, ZIP_PAUSE, ZIP_MAX_FETCHES,
                             log=log, fetch=fetch, sleep=sleep)


def missing_description_where(open_only: bool) -> str:
    """SQL condition for roles with no description; open_only leaves out Not a fit and archived roles."""
    sql = "(description IS NULL OR TRIM(description)='')"
    if open_only:
        sql += " AND COALESCE(archived,0)=0 AND status!='not_a_fit'"
    return sql


def missing_descriptions(conn, open_only: bool) -> dict:
    """How many roles have no description: {"total", "fetchable", "by_site": {site: n}}.
    Fetchable = on a board whose job page can be fetched again (see _description_sites)."""
    sites = _description_sites()
    by_site = {r["site"] or "manual": r["n"] for r in conn.execute(
        f"SELECT site, COUNT(*) n FROM roles WHERE {missing_description_where(open_only)} GROUP BY site")}
    fetchable = conn.execute(
        f"SELECT COUNT(*) FROM roles WHERE {missing_description_where(open_only)} AND job_url IS NOT NULL "
        f"AND site IN ({', '.join('?' * len(sites))})", list(sites)).fetchone()[0]
    return {"total": sum(by_site.values()), "fetchable": fetchable, "by_site": by_site}


def backfill_descriptions(use_ai=True, open_only=True, trigger="cli", log=print) -> dict:
    """Fetch descriptions for roles already in the tracker that have none, from the boards
    that allow it (ZipRecruiter, LinkedIn), then, with use_ai, AI-screen every role that got
    one (a role the AI had marked Not a fit goes back to Not applied if it now fits).
    open_only leaves out Not a fit and archived roles. Recorded in the run history as a
    "descriptions" run. Raises db.RunInProgress if a pull or re-screen is running."""
    with db.run_lock():
        conn = db.connect()
        run_id = db.start_run(conn, kind="descriptions", trigger=trigger)
        try:
            sites = _description_sites()
            rows = conn.execute(f"SELECT id, site, job_url FROM roles WHERE {missing_description_where(open_only)} "
                                "ORDER BY id DESC").fetchall()
            by_site: dict[str, list[dict]] = {}
            for r in rows:
                if r["site"] in sites and r["job_url"]:
                    by_site.setdefault(r["site"], []).append(
                        {"role_id": r["id"], "id": f"role {r['id']}", "job_url": r["job_url"]})
            fetchable = sum(len(v) for v in by_site.values())
            log(f"{len(rows)} {'open ' if open_only else ''}role(s) without a description; "
                f"{fetchable} can be fetched again from the job board "
                f"({', '.join(cfg['label'] for cfg in sites.values())}).")
            per_site, got = {}, []
            for site, listings in by_site.items():
                cfg = sites[site]
                per_site[site] = fill_descriptions(listings, cfg["label"], cfg["make_fetcher"], cfg["pause"],
                                                   BACKFILL_MAX_PER_SITE, log=log)
                for job in listings:
                    desc = job.get("description")
                    if isinstance(desc, str) and desc.strip() and \
                            "description" in db.fill_blanks(conn, job["role_id"], {"description": desc},
                                                 note=f"Description fetched from the {cfg['label']} job page"):
                        got.append(job["role_id"])
                conn.commit()   # keep each board's descriptions even if a later step fails
            ai = None
            if got and use_ai:
                import ai_screen
                restore = {rid for rid in got if db.last_change_by_ai(conn, rid)}
                ai = ai_screen.screen_roles(conn, got, log=log, restore_ids=restore, run_id=run_id)
            elif got:
                log("AI assessment is off for this run, so the fit of these roles wasn't updated.")
            tried = sum(s["tried"] for s in per_site.values())
            fetched = sum(s["fetched"] for s in per_site.values())
            db.finish_run(conn, run_id, status="partial" if ai and ai["errors"] else "ok",
                          scraped=len(rows), kept=tried, added=len(got),
                          **({"ai_screened": ai["screened"], "ai_rejected": ai["rejected"], "ai_unsure": ai["unsure"],
                              "ai_input_tokens": ai["input_tokens"], "ai_output_tokens": ai["output_tokens"],
                              "ai_cost": ai["cost"]} if ai else {}))
            return {"missing": len(rows), "fetchable": fetchable, "unsupported": len(rows) - fetchable,
                    "tried": tried, "fetched": fetched, "sites": per_site, "described": got, "ai": ai,
                    "open_only": open_only, "use_ai": use_ai,
                    "stopped_early": any(s["stopped_early"] for s in per_site.values())}
        except Exception as e:
            conn.rollback()
            db.finish_run(conn, run_id, status="error", log=str(e))
            raise
        finally:
            conn.close()


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------
INTERVAL_MULT = {"yearly": 1, "monthly": 12, "weekly": 52, "daily": 260, "hourly": 2080}
_AMT = r"(\d{2,3}(?:,\d{3})+(?:\.\d{2})?|\d{2,3}(?:\.\d+)?\s?[kK])"
SAL_RE = re.compile(r"\$\s?" + _AMT + r"\s*(?:usd)?\s*(?:/\s*(?:yr|year))?\s*"
                    r"(?:-|–|—|to)\s*\$?\s?" + _AMT, re.I)
HOURLY_RE = re.compile(r"\$\s?(\d{2,3}(?:\.\d+)?)\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:\.\d+)?)\s*"
                       r"(?:/|per)\s*(?:hr|hour)", re.I)
YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*\+?)?\s*years?"
    r"(?:\s+of)?(?:\s+[\w/-]+){0,6}?\s+(?:experience|exp\b|building|working|"
    r"developing|designing|shipping|writing|professional|industry|hands-on)", re.I)
# "Experience Required: 8+ years", "Experience: >10 YEARS"
YEARS_LABEL_RE = re.compile(
    r"experience(?:\s+required)?\s*:\s*(?:>|≥|over|minimum of)?\s*(\d{1,2})\s*\+?\s*"
    r"(?:(?:-|–|to)\s*(\d{1,2})\s*)?years", re.I)


def _money(s: str) -> float:
    s = s.replace(",", "").replace(" ", "")
    return float(s[:-1]) * 1000 if s[-1] in "kK" else float(s)


def salary_range(job: dict) -> tuple[float, float] | None:
    """Annualised (min, max) or None if unknown."""
    lo, hi = job.get("min_amount"), job.get("max_amount")
    if lo or hi:
        mult = INTERVAL_MULT.get((job.get("interval") or "yearly").lower(), 1)
        return float(lo or hi) * mult, float(hi or lo) * mult
    desc = (job.get("description") or "").replace("\\", "")
    m = SAL_RE.search(desc)
    if m:
        a, b = _money(m.group(1)), _money(m.group(2))
        if 20_000 <= a <= b <= 2_000_000:
            return a, b
    m = HOURLY_RE.search(desc)
    if m:
        return float(m.group(1)) * 2080, float(m.group(2)) * 2080
    return None


def years_required(job: dict) -> tuple[int, int | None] | None:
    """(min_years, max_years_or_None) or None if not stated."""
    desc = (job.get("description") or "").replace("\\", "")
    hits = [(int(m.group(1)), int(m.group(2)) if m.group(2) else None)
            for rx in (YEARS_RE, YEARS_LABEL_RE) for m in rx.finditer(desc)]
    hits = [h for h in hits if h[0] <= 20]
    # The smallest minimum is usually the real requirement ("nice to have" is bigger).
    return min(hits, key=lambda h: h[0]) if hits else None


def _parse_place(s: str) -> tuple[str, str]:
    parts = [p.strip().lower() for p in (s or "").split(",")]
    city = parts[0] if parts else ""
    state = parts[1] if len(parts) > 1 else ""
    return city, US_STATES.get(state, state)


def location_ok(job: dict, prefs: dict) -> tuple[bool, str]:
    loc = job.get("location") or ""
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if job.get("is_remote") or "remote" in loc.lower():
        us = (not parts or any(p.lower() in ("usa", "us", "united states", "remote") for p in parts)
              or (len(parts) > 1 and parts[1].lower() in US_STATES))
        where = "Remote" + (f" ({loc})" if loc and "remote" not in loc.lower() else "")
        return bool(prefs["locations"].get("include_remote")) and us, where
    city, state = _parse_place(loc)
    for place in prefs["locations"]["value"]:
        pcity, pstate = _parse_place(place)
        same_state = not pstate or not state or state == pstate
        if pcity in NYC_PLACES and city in NYC_PLACES and same_state:
            return True, f"{parts[0]}, NY"
        if city == pcity and same_state:
            return True, loc.replace(", USA", "").replace(", United States", "")
    return False, loc


def posted_at(job: dict) -> dt.date | None:
    d = job.get("date_posted")
    if isinstance(d, dt.datetime):
        return d.date()
    if isinstance(d, dt.date):
        return d
    if isinstance(d, str):
        try:
            return dt.date.fromisoformat(d[:10])
        except ValueError:
            return None
    return None


# ----------------------------------------------------------------------------
# Rule filters (mandatory preferences only)
# ----------------------------------------------------------------------------
def evaluate(job: dict, today: dt.date, prefs: dict) -> tuple[bool, str, dict]:
    title = job.get("title") or ""
    company = job.get("company") or ""
    info: dict = {"posted": posted_at(job), "years": years_required(job),
                  "salary": salary_range(job)}
    ok, info["where"] = location_ok(job, prefs)
    info["ai"] = bool(AI_TERMS.search(title)) or bool(AI_TERMS.search((job.get("description") or "")[:2000]))

    max_days = max(1, math.ceil((prefs.get("posted_within_hours") or 24) / 24))
    if info["posted"] and (today - info["posted"]).days > max_days:
        return False, "older than the posted-within window", info

    ti = prefs["title_include"]
    rx = words_re(ti["value"])
    if ti["mandatory"] and rx and not rx.search(title):
        return False, "title missing required words", info
    te = prefs["title_exclude"]
    rx = words_re(te["value"])
    if te["mandatory"] and ((rx and rx.search(title)) or LEVEL_NUM_RE.search(title)):
        return False, "title has excluded words / level", info

    # Blocked companies always apply (a block isn't a "nice to have").
    if db.company_blocked(company, prefs["companies_exclude"]["value"]):
        return False, "blocked company", info

    if prefs["locations"]["mandatory"] and not ok:
        return False, "location", info
    wp = prefs["workplace"]
    if wp["mandatory"] and wp["value"] == ["remote"] and "Remote" not in info["where"]:
        return False, "not remote", info

    et = prefs["employment_types"]
    jt = JOB_TYPE_ALIASES.get(str(job.get("job_type") or "").lower().split(",")[0].strip())
    if et["mandatory"] and jt and et["value"] and jt not in et["value"]:
        return False, f"job type ({jt})", info
    if et["mandatory"] and "internship" not in et["value"] and \
            (job.get("job_level") or "").lower() == "internship":
        return False, "internship", info

    exp = prefs["experience"]
    yrs = info["years"]
    if exp["mandatory"]:
        if yrs:
            lo, hi = yrs
            if lo > exp["max"]:
                return False, f"{lo}+ years required", info
            if hi is not None and hi < exp["min"]:
                return False, "entry level by years", info
            if lo == 0 and (hi is None or hi <= 1) and exp["min"] >= 1:
                return False, "entry level by years", info
        elif (job.get("job_level") or "").lower() == "entry level" and exp["min"] >= 1:
            # LinkedIn tags many 1-3 yr roles "entry level"; only drop when the
            # description doesn't state a real experience requirement.
            return False, "entry level (LinkedIn tag, no years stated)", info

    sal = prefs["salary"]
    s = info["salary"]
    if sal["mandatory"]:
        if s and sal.get("min_top") and s[1] < sal["min_top"]:
            return False, "pay too low", info
        if s and sal.get("min_floor") and s[0] < sal["min_floor"]:
            return False, "pay floor too low", info
        if not s and not sal.get("include_unlisted", True):
            return False, "pay not listed", info
    return True, "", info


def to_record(job: dict, info: dict) -> dict:
    """Shape a scraped job + filter info into a DB row."""
    sal = info.get("salary") or (None, None)
    yrs = info.get("years") or (None, None)
    posted = info.get("posted")
    return {
        "title": job.get("title"),
        "company": job.get("company"),
        "location": info.get("where") or job.get("location"),
        "is_remote": bool(job.get("is_remote")) or "remote" in str(info.get("where", "")).lower(),
        "site": job.get("site"),
        "job_url": job.get("job_url"),
        "job_url_direct": job.get("job_url_direct"),
        "date_posted": posted.isoformat() if posted else None,
        "pay_min": sal[0], "pay_max": sal[1],
        "years_min": yrs[0], "years_max": yrs[1],
        "is_ai": info.get("ai"),
        "job_level": job.get("job_level"),
        "job_type": job.get("job_type"),
        "description": job.get("description"),
        "metadata": {k: v for k, v in job.items() if k != "description"},
        "source": f"scrape ({job.get('site')})",
    }


# ----------------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------------
def run(reuse=False, ignore_seen=False, use_ai=True, trigger="cli", log=print) -> dict:
    """Scrape, filter, add new matches to jobs.db as 'not_applied', then AI-screen them.
    Raises db.RunInProgress if another pull or re-screen is running."""
    with db.run_lock():
        try:
            db.backup_if_due()
        except Exception as e:  # a failed backup shouldn't block the pull
            log(f"  ! Daily backup failed: {e}")
        return _run(reuse, ignore_seen, use_ai, trigger, log)


def _run(reuse, ignore_seen, use_ai, trigger, log) -> dict:
    today = dt.date.today()
    results_dir = HERE / "results"
    results_dir.mkdir(exist_ok=True)
    conn = db.connect()
    prefs = db.get_prefs(conn)
    run_id = db.start_run(conn, kind="pull", trigger=trigger)
    result = {"scraped": 0, "kept": 0, "added": 0, "rejected": {}, "ai": None}
    fetch_stats: dict = {}
    try:
        raw_file = results_dir / f"raw_{today:%Y-%m-%d}.json"
        if reuse and raw_file.exists():
            log(f"Re-using cached scrape {raw_file.name}")
            jobs = json.loads(raw_file.read_text(encoding="utf-8"))
        else:
            log(f"{len(prefs['search_terms'])} search term(s) x "
                f"{len(prefs['locations']['value']) + bool(prefs['locations'].get('include_remote'))} "
                f"location(s) on {', '.join(prefs['sites'])}")
            jobs = fetch_jobs(prefs, log, fetch_stats)

        # ZipRecruiter listings that came back without a description (see
        # fill_zip_descriptions): fetch them again, slowly, but only the ones we'd keep:
        # new listings that pass the filters, or roles we already have with no description,
        # and not when another board's copy of the same posting has a description.
        described = {(db._norm(j.get("title")), db.company_core(j.get("company")))
                     for j in jobs if (j.get("description") or "").strip()}
        need_zip = []
        for job in jobs:
            if (job.get("site") != "zip_recruiter" or (job.get("description") or "").strip()
                    or not job.get("job_url")
                    or (db._norm(job.get("title")), db.company_core(job.get("company"))) in described):
                continue
            existing = db.find_existing(conn, job.get("title"), job.get("company"), job.get("job_url"),
                                        location=job.get("location"), job_url_direct=job.get("job_url_direct"))
            if existing is not None:
                if not existing["description"]:
                    need_zip.append(job)
            elif (ignore_seen or not db.is_seen(conn, job.get("job_url") or job.get("id"))) \
                    and evaluate(job, today, prefs)[0]:
                need_zip.append(job)
        zip_stats = fill_zip_descriptions(need_zip, log)
        if not reuse:
            raw_file.write_text(json.dumps(jobs, default=str), encoding="utf-8")

        # Each posting is filtered on its own, so a role listed in several cities
        # is kept if any listing passes. Postings that fail the filters are NOT
        # marked seen: if you loosen your preferences they're checked again.
        kept, rejected, keys = [], {}, []
        filled_ids: dict[int, list[str]] = {}   # roles already in the DB that got new details
        for job in jobs:
            key = job.get("job_url") or job.get("id")
            existing = db.find_existing(conn, job.get("title"), job.get("company"), job.get("job_url"),
                                        location=job.get("location"), job_url_direct=job.get("job_url_direct"))
            if existing is not None:
                # Same posting as a role we already have (same URL, or same title and
                # company on another board within 30 days). Don't add it again, but fill
                # in anything the first copy was missing, e.g. a description LinkedIn
                # didn't return that ZipRecruiter does.
                _, _, info = evaluate(job, today, prefs)
                added = db.fill_blanks(conn, existing["id"], to_record(job, info))
                if added:
                    filled_ids.setdefault(existing["id"], []).extend(added)
                rejected["already in DB / seen before"] = rejected.get("already in DB / seen before", 0) + 1
                keys.append(key)
                continue
            if not ignore_seen and db.is_seen(conn, key):
                rejected["already in DB / seen before"] = rejected.get("already in DB / seen before", 0) + 1
                keys.append(key)
                continue
            ok, reason, info = evaluate(job, today, prefs)
            if ok:
                kept.append((job, info))
                keys.append(key)
            else:
                rejected[reason] = rejected.get(reason, 0) + 1

        # Passing listings of the same posting (same title + company, e.g. on two
        # boards) merge into one role, filling in each other's blanks.
        new_ids = []
        for job, info in kept:
            rid, created = db.upsert_role(conn, to_record(job, info))
            if created:
                new_ids.append(rid)
        db.mark_seen(conn, keys)
        conn.commit()
        # Roles that only now have a description: screen them again if the AI had
        # nothing to go on (never screened, "unknown", or it marked them Not a fit).
        described = [rid for rid, fields in filled_ids.items() if "description" in fields]
        rescreen_ids, restore_ids = [], set()
        for rid in described:
            r = conn.execute("SELECT status, ai_match FROM roles WHERE id=?", (rid,)).fetchone()
            if r["status"] == "not_applied" and r["ai_match"] in (None, "unknown"):
                rescreen_ids.append(rid)
            elif db.last_change_by_ai(conn, rid):
                rescreen_ids.append(rid)
                restore_ids.add(rid)
        log(f"Scraped {len(jobs)} postings, {len(kept)} listings passed rule filters, "
            f"{len(new_ids)} new roles added to DB.")
        if filled_ids:
            log(f"Filled in missing details on {len(filled_ids)} role(s) already in the DB "
                f"({len(described)} got a description).")
        for reason, n in sorted(rejected.items(), key=lambda x: -x[1]):
            log(f"  filtered: {reason}: {n}")
        sites: dict[str, int] = {}
        for job in jobs:
            sites[job.get("site") or "unknown"] = sites.get(job.get("site") or "unknown", 0) + 1
        result.update(scraped=len(jobs), kept=len(kept), added=len(new_ids), rejected=rejected,
                      sites=sites, details_filled=len(filled_ids), descriptions_added=len(described),
                      searches=fetch_stats.get("searches", 0), failed_searches=fetch_stats.get("failed", []),
                      zip_descriptions=zip_stats)

        ai = {"screened": 0, "rejected": 0, "unsure": 0, "input_tokens": 0,
              "output_tokens": 0, "cost": 0.0, "errors": 0, "skipped": None}
        to_screen = new_ids + rescreen_ids
        if use_ai and to_screen:
            import ai_screen
            ai = ai_screen.screen_roles(conn, to_screen, prefs, log=log, run_id=run_id,
                                        restore_ids=restore_ids)
        result["ai"] = ai
        status = "partial" if ai.get("errors") else "ok"
        db.finish_run(conn, run_id, status=status, scraped=len(jobs), kept=len(kept),
                      added=len(new_ids), ai_screened=ai["screened"], ai_rejected=ai["rejected"],
                      ai_unsure=ai["unsure"], ai_input_tokens=ai["input_tokens"],
                      ai_output_tokens=ai["output_tokens"], ai_cost=ai["cost"])
    except Exception as e:
        conn.rollback()   # don't keep half-written roles from the failed pull
        db.finish_run(conn, run_id, status="error", log=str(e))
        raise
    finally:
        conn.close()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="re-check postings seen before (DB still prevents duplicates)")
    ap.add_argument("--reuse", action="store_true",
                    help="re-filter today's cached scrape instead of scraping again")
    ap.add_argument("--no-ai", action="store_true", help="skip the AI screen")
    ap.add_argument("--trigger", default="cli")
    args = ap.parse_args()
    if sys.stdout is None:  # pythonw (e.g. the scheduled task): no console, log to a file
        sys.stdout = sys.stderr = open(HERE / "run_log.txt", "a", encoding="utf-8", buffering=1)
        print(f"\n=== {dt.datetime.now():%Y-%m-%d %H:%M} ({args.trigger}) ===")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        run(reuse=args.reuse, ignore_seen=args.all, use_ai=not args.no_ai, trigger=args.trigger)
    except db.RunInProgress as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
