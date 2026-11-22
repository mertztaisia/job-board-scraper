"""
Local web UI for the job tracker.

    .venv\\Scripts\\python.exe app.py      then open http://localhost:5055
"""

from __future__ import annotations

import datetime as dt
import threading
import traceback
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import db
import settings

HERE = Path(__file__).resolve().parent
PORT = settings.PORT

app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="/static")
app.json.sort_keys = False          # keep status order as defined in db.STATUSES


def _hostname(value: str) -> str:
    """'localhost:5055' / 'http://[::1]:5055' -> 'localhost' / '::1'."""
    try:
        return (urlsplit(value if "//" in value else "//" + value).hostname or "").lower()
    except ValueError:
        return ""


@app.before_request
def same_origin_only():
    """Only answer requests meant for this local app.

    - Host must be localhost (or an allowed name): blocks DNS-rebinding pages.
    - Writes from a browser must come from this app's own page (same host AND
      port): blocks other websites, and other apps on localhost, from posting
      forms or fetches to the API (CSRF). Scripts and curl send no Origin
      header and are allowed."""
    if _hostname(request.host) not in settings.ALLOWED_HOSTS:
        abort(403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin is not None and _origin_netloc(origin) != request.host.lower():
            abort(403)
        # "same-site" includes other ports on localhost, so only same-origin passes.
        if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            abort(403)


def _origin_netloc(origin: str) -> str:
    """'http://localhost:5055' -> 'localhost:5055' ('null' and junk -> '')."""
    try:
        return urlsplit(origin).netloc.lower()
    except ValueError:
        return ""


def body() -> dict:
    """The JSON request body, or {} when there is none."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@app.after_request
def no_cache(resp):
    # Always serve the latest UI files after an update (no stale cached JS/CSS).
    if request.path == "/" or request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@contextmanager
def conn():
    """A connection that commits on success, rolls back on error, and is always closed."""
    c = db.connect()
    try:
        with c:
            yield c
    finally:
        c.close()


# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(HERE / "static", "index.html")


STARTED_AT = dt.datetime.now().isoformat(timespec="seconds")


@app.get("/api/health")
def health():
    """Liveness check used by service.ps1 status."""
    try:
        with conn() as c:
            roles_n = c.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
        ok = True
    except Exception as e:  # report, don't crash
        roles_n, ok = str(e), False
    return jsonify(ok=ok, version=settings.VERSION, started_at=STARTED_AT,
                   db=str(db.DB_PATH), roles=roles_n), (200 if ok else 500)


@app.get("/api")
def api_index():
    """Lists every API endpoint (handy when scripting against the tracker)."""
    routes = sorted(
        {(r.rule, ",".join(sorted(m for m in r.methods if m in {"GET", "POST", "PUT", "PATCH", "DELETE"})),
          ((app.view_functions[r.endpoint].__doc__ or "").strip().splitlines() or [""])[0])
         for r in app.url_map.iter_rules() if r.rule.startswith("/api")})
    return jsonify(version=settings.VERSION,
                   endpoints=[{"path": p, "methods": m, "description": d} for p, m, d in routes])


@app.get("/api/meta")
def meta():
    import ai_screen
    with conn() as c:
        prefs = db.get_prefs(c)
    return jsonify(statuses=db.STATUSES, base_dir=str(db.BASE_DIR), db_path=str(db.DB_PATH),
                   interests=prefs.get("interests", []), ai=ai_screen.key_status(),
                   ai_enabled=prefs["ai"].get("enabled", True), model=prefs["ai"].get("model"),
                   round_kinds=db.ROUND_KINDS, round_statuses=db.ROUND_STATUSES,
                   match_levels=db.MATCH_LEVELS,
                   blocked_companies=prefs["companies_exclude"]["value"],
                   sites=sorted(r["site"] for r in _sites_in_db()),
                   places=_common_places())


def _common_places():
    with conn() as c:
        return db.common_places(c)


def _sites_in_db():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT DISTINCT COALESCE(site, 'manual') AS site FROM roles")]


@app.post("/api/blocked-companies")
def add_blocked_company():
    """Block a company: its roles are skipped by every pull (Preferences → Blocked companies)."""
    try:
        with conn() as c:
            return jsonify(blocked=db.block_company(c, body().get("company", "")))
    except ValueError as e:
        return jsonify(error=str(e)), 400


# ----------------------------------------------------------------------------
# Roles
# ----------------------------------------------------------------------------
ADDED_WINDOWS = {"h1": dt.timedelta(hours=1), "1": dt.timedelta(days=1),
                 "7": dt.timedelta(days=7), "30": dt.timedelta(days=30)}


@app.get("/api/roles")
def roles():
    a = request.args
    statuses = [s for s in a.get("status", "").split(",") if s in db.STATUSES]
    tags = [t for t in a.get("tags", "").split("|") if t]
    since = None
    if a.get("added") in ADDED_WINDOWS:
        since = (dt.datetime.now() - ADDED_WINDOWS[a["added"]]).isoformat(timespec="seconds")
    posted_since = None
    if a.get("posted") in ADDED_WINDOWS and a["posted"] != "h1":
        posted_since = (dt.date.today() - ADDED_WINDOWS[a["posted"]]).isoformat()
    # Paging: ?limit=50&offset=0  (limit capped at 500; omit limit for everything)
    try:
        limit = min(max(int(a.get("limit", 0)), 0), 500) or None
        offset = max(int(a.get("offset", 0)), 0)
        min_pay = float(a["min_pay"]) if a.get("min_pay") else None
        max_years = int(a["max_years"]) if a.get("max_years") else None
    except ValueError:
        return jsonify(error="limit, offset, min_pay and max_years must be numbers"), 400
    with conn() as c:
        rows, counts, matching = db.list_roles(
            c, q=a.get("q", ""), statuses=statuses, tags=tags,
            remote=a.get("remote") == "1", paid=a.get("paid") == "1", added_since=since,
            sort=a.get("sort", "status"), order=a.get("order", "asc"),
            include_archived=a.get("archived") == "1",
            limit=limit, offset=offset, with_total=True,
            fits=[f for f in a.get("fit", "").split(",") if f], min_pay=min_pay, max_years=max_years,
            posted_since=posted_since, sites=[s for s in a.get("sites", "").split(",") if s],
            has_description=a.get("described") == "1", step_todo=a.get("todo") == "1",
            locations=[l for l in a.get("loc", "").split("|") if l.strip()])
        total = c.execute("SELECT COUNT(*) FROM roles WHERE COALESCE(archived,0)=0").fetchone()[0]
        archived = c.execute("SELECT COUNT(*) FROM roles WHERE archived=1").fetchone()[0]
        all_counts = {k: 0 for k in db.STATUSES}
        for r in c.execute("SELECT status, COUNT(*) n FROM roles WHERE COALESCE(archived,0)=0 "
                           "GROUP BY status"):
            all_counts[r["status"]] = r["n"]
    return jsonify(roles=rows, counts=counts, all_counts=all_counts,
                   total=total + (archived if a.get("archived") == "1" else 0),
                   archived_count=archived, matching=matching,
                   limit=limit, offset=offset)


@app.get("/api/roles/<int:rid>")
def role(rid):
    with conn() as c:
        r = db.get_role(c, rid)
    return jsonify(r) if r else abort(404)


@app.patch("/api/roles/<int:rid>")
def patch_role(rid):
    data = body()
    note = data.pop("note", None)
    try:
        with conn() as c:
            r = db.update_role(c, rid, data, note=note)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(r) if r else abort(404)


@app.post("/api/roles")
def create_role():
    data = body()
    if not (data.get("title") and data.get("company")):
        return jsonify(error="Title and company are required."), 400
    # Check every field before writing anything, so a bad value saves nothing.
    try:
        data = {**data, **db.check_role_changes(
            {k: v for k, v in data.items() if k in db.EDITABLE_FIELDS and k != "status"})}
    except ValueError as e:
        return jsonify(error=str(e)), 400
    with conn() as c:
        existing = db.find_existing(c, data["title"], data["company"], data.get("job_url"),
                                     location=data.get("location"))
        if existing:
            return jsonify(error="This role is already in the database.",
                           id=existing["id"]), 409
        status = data.get("status") or "not_applied"
        rec = {k: data.get(k) for k in db.ROLE_FIELDS}
        rec["site"] = rec.get("site") or "manual"
        rec["source"] = "manual"
        try:
            rid, _ = db.upsert_role(c, rec, status=status, note="Added manually")
        except ValueError as e:
            return jsonify(error=str(e)), 400
        extra = {k: data.get(k) for k in ("resume_path", "notes", "applied_at") if data.get(k)}
        if status == "applied" and "applied_at" not in extra:
            extra["applied_at"] = dt.date.today().isoformat()
        if extra:
            db.update_role(c, rid, extra)
        r = db.get_role(c, rid)
    return jsonify(r), 201


# ----------------------------------------------------------------------------
# Status transitions, archive, interview rounds, Q&A
# ----------------------------------------------------------------------------
def _role_or_404(c, rid):
    r = db.get_role(c, rid)
    return jsonify(r) if r else abort(404)


@app.post("/api/roles/<int:rid>/transition")
def transition(rid):
    data = body()
    if data.get("status") and data["status"] not in db.STATUSES:
        return jsonify(error=f"Unknown status {data['status']}"), 400
    try:
        with conn() as c:   # an error rolls back everything this status change wrote
            r = db.transition(c, rid, data)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(r) if r else abort(404)


@app.post("/api/roles/<int:rid>/screen")
def screen_role(rid):
    """AI-screen one role now (e.g. after adding its description yourself)."""
    import ai_screen
    try:
        t = ai_screen.screen_one_role(rid, trigger="ui")
    except db.RunInProgress as e:
        return jsonify(error=str(e)), 409
    if t is None:
        abort(404)
    if t["skipped"]:
        return jsonify(error=t["skipped"]), 400
    if t["errors"]:
        detail = next((m.strip(" !") for m in t["messages"] if "AI error" in m), "")
        return jsonify(error=f"The AI screen failed. {detail}".strip()), 502
    with conn() as c:
        return jsonify(role=db.get_role(c, rid), ai=t)


@app.post("/api/roles/<int:rid>/archive")
def archive(rid):
    data = body()
    with conn() as c:
        r = db.set_archived(c, rid, bool(data.get("archived", True)))
    return jsonify(r) if r else abort(404)


@app.post("/api/roles/<int:rid>/rounds")
def add_round(rid):
    with conn() as c:
        if not db.role_exists(c, rid):
            abort(404)
        db.add_round(c, rid, body())
        return _role_or_404(c, rid)


@app.patch("/api/rounds/<int:round_id>")
def patch_round(round_id):
    with conn() as c:
        rid = db.update_round(c, round_id, body())
        return _role_or_404(c, rid) if rid else abort(404)


@app.delete("/api/rounds/<int:round_id>")
def del_round(round_id):
    with conn() as c:
        rid = db.delete_round(c, round_id)
        return _role_or_404(c, rid) if rid else abort(404)


@app.post("/api/roles/<int:rid>/qa")
def add_qa(rid):
    data = body()
    items = data.get("items") or [data]
    with conn() as c:
        if not db.role_exists(c, rid):
            abort(404)
        for q in items:
            db.add_qa(c, rid, q.get("question"), q.get("answer"), q.get("round_id"))
        return _role_or_404(c, rid)


@app.patch("/api/qa/<int:qa_id>")
def patch_qa(qa_id):
    with conn() as c:
        rid = db.update_qa(c, qa_id, body())
        return _role_or_404(c, rid) if rid else abort(404)


@app.delete("/api/qa/<int:qa_id>")
def del_qa(qa_id):
    with conn() as c:
        rid = db.delete_qa(c, qa_id)
        return _role_or_404(c, rid) if rid else abort(404)


@app.get("/api/check")
def check_duplicates():
    """Roles at the same company (or same URL), shown while adding a role."""
    company = (request.args.get("company") or "").strip()
    url = (request.args.get("url") or "").strip()
    out = []
    with conn() as c:
        if company:
            out += [dict(r) for r in c.execute(
                "SELECT id, title, company, status, applied_at, archived FROM roles "
                "WHERE LOWER(company) LIKE ? ORDER BY first_seen DESC LIMIT 10",
                (f"%{company.lower()}%",))]
        if url:
            out += [dict(r) for r in c.execute(
                "SELECT id, title, company, status, applied_at, archived FROM roles "
                "WHERE job_url=? OR job_url_direct=?", (url, url))]
    seen, uniq = set(), []
    for r in out:
        if r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    return jsonify(matches=uniq)


# ----------------------------------------------------------------------------
# Preferences, AI status, run history
# ----------------------------------------------------------------------------
@app.get("/api/preferences")
def get_preferences():
    with conn() as c:
        return jsonify(prefs=db.get_prefs(c), defaults=db.DEFAULT_PREFS)


@app.put("/api/preferences")
def put_preferences():
    """Save search preferences. Invalid values (e.g. a blank years-of-experience box)
    are rejected with a 400 that lists every problem, so they can't break a pull."""
    data = body()
    terms = data.get("search_terms")
    if isinstance(terms, list):
        data["search_terms"] = [t.strip() for t in terms if isinstance(t, str) and t.strip()]
    try:
        with conn() as c:
            prefs = db.save_prefs(c, data)
            archived = db.auto_archive_not_a_fit(c, prefs)   # a shorter period applies right away
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(prefs=prefs, auto_archived=archived)


@app.get("/api/insights")
def get_insights():
    """Applying stats: applications, new roles and AI spend per week, review funnel,
    match levels and progress toward your targets."""
    import insights
    try:
        weeks = min(max(int(request.args.get("weeks", 12)), 4), 52)
    except ValueError:
        return jsonify(error="weeks must be an integer"), 400
    with conn() as c:
        return jsonify(insights.build(c, weeks))


@app.put("/api/targets")
def put_targets():
    """Set targets: applications_per_week, inbox_max."""
    import insights
    try:
        with conn() as c:
            return jsonify(targets=insights.save_targets(c, body()))
    except (ValueError, TypeError) as e:
        return jsonify(error=f"Invalid target: {e}"), 400


@app.get("/api/ai/status")
def ai_status():
    import ai_screen
    return jsonify(ai_screen.key_status())


@app.post("/api/ai/test")
def ai_test():
    """One tiny request to confirm the API key works (costs a fraction of a cent)."""
    import ai_screen
    if not ai_screen.key_status()["configured"]:
        return jsonify(ok=False, error="No ANTHROPIC_API_KEY in job_search/.env"), 400
    try:
        with conn() as c:
            model = db.get_prefs(c)["ai"].get("model") or "claude-haiku-5-5"
        r = ai_screen.client().messages.create(
            model=model, max_tokens=64, output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with the single word: ready"}])
        text = next((b.text for b in r.content if b.type == "text"), "")
        return jsonify(ok=True, model=model, reply=text.strip())
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 400


@app.get("/api/runs")
def runs():
    with conn() as c:
        rows = db.list_runs(c, 40)
        month = dt.date.today().replace(day=1).isoformat()
        spend = c.execute("SELECT COALESCE(SUM(ai_cost),0), COUNT(*) FROM scrape_runs "
                          "WHERE started_at >= ?", (month,)).fetchone()
        total = c.execute("SELECT COALESCE(SUM(ai_cost),0) FROM scrape_runs").fetchone()[0]
    return jsonify(runs=rows, month_cost=spend[0], month_runs=spend[1], total_cost=total)


# ----------------------------------------------------------------------------
# Background tasks: pulls and AI re-screens
# ----------------------------------------------------------------------------
TASK = {"running": False, "kind": None, "log": [], "result": None, "started_at": None,
        "finished_at": None, "error": None}
TASK_LOCK = threading.Lock()


def _log(msg):
    for line in str(msg).splitlines() or [""]:
        TASK["log"].append(f"{dt.datetime.now():%H:%M:%S}  {line}")


def _worker(kind, opts):
    try:
        if kind == "pull":
            import daily_jobs
            TASK["result"] = daily_jobs.run(trigger="ui", log=_log)
        else:
            import ai_screen
            TASK["result"] = {"ai": ai_screen.rescreen(scope=opts.get("scope", "unscreened"),
                                                       trigger="ui", log=_log)}
    except db.RunInProgress as e:
        TASK["error"] = str(e)
        _log(str(e))
    except Exception as e:  # surface the error in the UI
        TASK["error"] = str(e)
        _log("ERROR: " + str(e))
        _log(traceback.format_exc())
    finally:
        TASK["running"] = False
        TASK["finished_at"] = db.now_iso()


def _start(kind, opts=None):
    with TASK_LOCK:
        if TASK["running"]:
            return jsonify(TASK), 409
        TASK.update(running=True, kind=kind, log=[], result=None, error=None,
                    started_at=db.now_iso(), finished_at=None)
        threading.Thread(target=_worker, args=(kind, opts or {}), daemon=True).start()
    return jsonify(TASK), 202


@app.post("/api/scrape")
def start_scrape():
    return _start("pull")


@app.post("/api/ai/rescreen")
def start_rescreen():
    data = body()
    return _start("rescreen", {"scope": data.get("scope", "unscreened")})


@app.get("/api/scrape")
def task_status():
    last = None
    with conn() as c:
        row = c.execute("SELECT started_at, finished_at, scraped, kept, added, status, ai_cost, "
                        "ai_rejected FROM scrape_runs WHERE kind='pull' AND status!='running' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            last = dict(row)
    return jsonify({**TASK, "last_run": last})


# ----------------------------------------------------------------------------
# Resumes
# ----------------------------------------------------------------------------
@app.get("/api/resumes")
def resumes():
    files = [{"path": str(p), "name": str(p.relative_to(db.BASE_DIR)),
              "modified": dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")}
             for p in db.resume_files()]
    return jsonify(files=files, base_dir=str(db.BASE_DIR))


@app.get("/api/file")
def open_file():
    """Open a resume file. Only files listed by /api/resumes are served, so this
    can't be used to read .env, the database or anything else on disk."""
    p = Path(request.args.get("path", "")).resolve()
    if p not in {f.resolve() for f in db.resume_files()}:
        abort(403)
    return send_file(p)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--db", help="use a different database file (e.g. a test copy)")
    args = ap.parse_args()
    if args.db:
        db.DB_PATH = Path(args.db)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)
