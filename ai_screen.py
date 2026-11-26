"""
AI second-pass screening with Claude.

For each role, Claude reads the full posting plus your preferences and returns a
match level (strong / good / partial / weak / not_a_fit / unknown, see db.MATCH_LEVELS),
a one-line reason, which interests match, which must-have requirements are violated,
and the experience/pay it found.

  * not_a_fit  -> role is auto-marked "Not a fit" (reason saved in history)
  * unknown    -> left as "Not applied" (e.g. no description was scraped)
  * otherwise  -> left as "Not applied", with the match level + tags

API key: put ANTHROPIC_API_KEY=sk-ant-... in job_search/.env
"""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import db

HERE = Path(__file__).resolve().parent

# $ per million tokens (input, output). Haiku 5.5 rates for prompts up to 100K tokens.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
MAX_WORKERS = 4

MATCH_TO_VERDICT = {"strong": "fit", "good": "fit", "partial": "fit", "weak": "fit",
                    "not_a_fit": "not_a_fit", "unknown": "unsure"}

SCHEMA = {
    "type": "object",
    "properties": {
        "match": {"type": "string", "enum": list(db.MATCH_LEVELS)},
        "reason": {"type": "string", "description": "One sentence, max ~25 words"},
        "violated_requirements": {"type": "array", "items": {"type": "string"}},
        "interests_matched": {"type": "array", "items": {"type": "string"}},
        "years_min": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "years_max": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "pay_min": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "pay_max": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    },
    "required": ["match", "reason", "violated_requirements",
                 "interests_matched", "years_min", "years_max", "pay_min", "pay_max"],
    "additionalProperties": False,
}


# ----------------------------------------------------------------------------
# Key handling
# ----------------------------------------------------------------------------
def load_env():
    import settings
    settings.load_env()


def key_status() -> dict:
    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return {"configured": bool(key),
            "hint": f"…{key[-4:]}" if len(key) > 8 else "",
            "env_file": str(HERE / ".env")}


def client():
    load_env()
    import anthropic
    return anthropic.Anthropic()


def cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, PRICES["claude-haiku-5-5"])
    return input_tokens * pin / 1e6 + output_tokens * pout / 1e6


# ----------------------------------------------------------------------------
# Prompt
# ----------------------------------------------------------------------------
def _req(label, value, mandatory):
    return f"- {label}: {value}  [{'MUST HAVE' if mandatory else 'nice to have'}]"


def build_system_prompt(p: dict) -> str:
    loc = p["locations"]
    if loc["value"]:
        locs = ", ".join(loc["value"])
        if loc.get("include_remote"):
            locs += " — or fully remote within the US"
    else:
        locs = "fully remote within the US" if loc.get("include_remote") else "anywhere"
    sal = p["salary"]
    # The top-of-range box is optional (blank = no minimum).
    sal_txt = (f"top of the posted range at least ${sal['min_top']:,.0f}/yr" if sal.get("min_top")
               else "no minimum on the top of the range")
    if sal.get("min_floor"):
        sal_txt += f"; bottom of range at least ${sal['min_floor']:,.0f}"
    if sal.get("include_unlisted"):
        sal_txt += " (postings without pay are acceptable)"
    exp = p["experience"]
    lines = [
        _req("Location", locs, loc["mandatory"]),
        _req("Workplace type", ", ".join(p["workplace"]["value"]) or "any", p["workplace"]["mandatory"]),
        _req("Experience", f"roles asking for {exp['min']}-{exp['max']} years. Reject roles whose "
             f"minimum requirement is above {exp['max']} years, or that are entry level / new grad "
             f"(under {exp['min']} year)", exp["mandatory"]),
        _req("Pay", sal_txt, sal["mandatory"]),
        _req("Employment type", ", ".join(p["employment_types"]["value"]) or "any",
             p["employment_types"]["mandatory"]),
        _req("Seniority/titles to avoid", ", ".join(p["title_exclude"]["value"]) or "none",
             p["title_exclude"]["mandatory"]),
    ]
    if p["companies_exclude"]["value"]:
        lines.append(_req("Companies to exclude", ", ".join(p["companies_exclude"]["value"]), True))
    if p["industries"]["value"]:
        lines.append(_req("Industries", ", ".join(p["industries"]["value"]), p["industries"]["mandatory"]))
    if p["company_size"]["value"]:
        lines.append(_req("Company size", ", ".join(p["company_size"]["value"]),
                          p["company_size"]["mandatory"]))
    if p["visa_sponsorship"]["value"]:
        lines.append(_req("Visa sponsorship", "candidate needs sponsorship; reject roles that say they "
                          "cannot sponsor", p["visa_sponsorship"]["mandatory"]))
    interests = ", ".join(p["interests"]) or "none"
    LEVELS = "\n".join(f"- {k}: {v['desc']}" for k, v in db.MATCH_LEVELS.items())
    return f"""You screen job postings for one job seeker. Read the posting and decide whether it fits their requirements.

Requirements:
{chr(10).join(lines)}

Interests (never a reason to reject; they push the match level up): {interests}

In the job seeker's own words:
{p.get('prompt') or '(none)'}

How to decide - pick exactly one match level:
{LEVELS}

Rules:
- "not_a_fit" ONLY when the posting clearly violates a MUST HAVE requirement. Put each violated requirement in violated_requirements (short, e.g. "requires 6+ years"). For every other level, violated_requirements is empty.
- "nice to have" items never cause not_a_fit; they move the level between strong / good / partial / weak.
- If the posting simply doesn't mention something (no years stated, no pay listed, no workplace type), that is NOT a violation; it is a gap that can lower the level, and should be mentioned in the reason.
- "unknown" ONLY when no description was scraped AND the title/company/location alone can't tell you whether it fits.
- Experience: judge by the REQUIRED minimum (not "nice to have"). "3-5 years" has minimum 3. Reject only when that minimum is GREATER than {exp['max']}; a minimum equal to {exp['max']} is acceptable.
- Title words to avoid mean the job's seniority or role (e.g. a "Staff Engineer" or "Solutions Architect" title), not domain words in the team name (e.g. "IAM Architecture", "Platform Leadership team").
- interests_matched: only items from the Interests list that genuinely apply to the role's work, spelled exactly as listed.
- years_min/years_max and pay_min/pay_max: what the posting states (annual USD; convert hourly x2080), else null.
- reason: one plain sentence a person can scan."""


def build_user_message(role: dict) -> str:
    meta = role.get("metadata") or {}
    fields = {
        "title": role.get("title"), "company": role.get("company"),
        "location": role.get("location"), "remote": bool(role.get("is_remote")),
        "posted": role.get("date_posted"), "job_type": role.get("job_type") or meta.get("job_type"),
        "level (from job board)": role.get("job_level") or meta.get("job_level"),
        "pay listed by job board": (f"{role.get('pay_min')}-{role.get('pay_max')}"
                                    if role.get("pay_max") else None),
        "company industry": meta.get("company_industry"),
        "company size": meta.get("company_num_employees"),
    }
    head = "\n".join(f"{k}: {v}" for k, v in fields.items() if v not in (None, "", "None"))
    desc = role.get("description") or "(no description was scraped for this posting)"
    return f"{head}\n\nDescription:\n{desc}"


# ----------------------------------------------------------------------------
# Screening
# ----------------------------------------------------------------------------
def screen_one(cli, model: str, system: str, role: dict) -> tuple[dict, int, int]:
    resp = cli.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": build_user_message(role)}],
    )
    u = resp.usage
    if resp.stop_reason == "refusal":
        return ({"match": "unknown", "verdict": "unsure", "reason": "Model declined to assess this posting.",
                 "violated_requirements": [], "interests_matched": []},
                u.input_tokens, u.output_tokens)
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    result = json.loads(text)
    if result.get("match") not in db.MATCH_LEVELS:
        result["match"] = "unknown"
    result["verdict"] = MATCH_TO_VERDICT[result["match"]]
    return result, u.input_tokens, u.output_tokens


# Which kind of requirement a violation is about. Experience means stated years or
# "experience"; seniority words in a title ("Senior", "Staff", "level") are a title
# violation and are never overridden here.
EXP_RE = re.compile(r"\b(years?|yrs?|experience)\b", re.I)
TITLE_RE = re.compile(r"\btitle\b", re.I)
PAY_WORDS = ("pay", "salary", "compensation", "$")
# A remote objection about the country/region is real even for a remote role.
NON_US_RE = re.compile(r"\b(outside|non-us|non-u\.s\.|international|countr(?:y|ies)|europe|emea|apac|"
                       r"latam|canada|uk|india|mexico|global|worldwide)\b", re.I)


def check_experience_verdict(result: dict, prefs: dict, role: dict | None = None) -> dict:
    """Double-check a not_a_fit verdict against hard numbers. Small models sometimes
    misread ranges (a 4-year minimum vs a 4-year max), reject vague wording
    ("extensive experience", "Engineer III") with no stated years, or misread listed pay.
    Objections the data contradicts are dropped; if none remain, the role is kept."""
    if result.get("verdict") != "not_a_fit":
        return result
    role = role or {}
    exp, sal = prefs["experience"], prefs["salary"]
    lo = result.get("years_min")
    if lo is None:
        lo = role.get("years_min")
    pay_top = role.get("pay_max") or result.get("pay_max")
    kept, dropped = [], []
    for f in result.get("violated_requirements") or []:
        fl = f.lower()
        if any(w in fl for w in PAY_WORDS) and pay_top and sal.get("min_top") and pay_top >= sal["min_top"]:
            dropped.append(f"listed pay tops out at ${pay_top:,.0f}")
        elif ("remote" in fl and not NON_US_RE.search(f) and role.get("is_remote")
              and prefs["locations"].get("include_remote")):
            dropped.append("it's a remote role and remote US roles are allowed")
        elif EXP_RE.search(f) and not TITLE_RE.search(f) and (lo is None or lo <= exp["max"]):
            dropped.append(f"stated minimum {lo} yrs is within {exp['min']}-{exp['max']}" if lo is not None
                           else "no specific number of years is stated")
        else:
            kept.append(f)
    if dropped and not kept:
        result["verdict"] = "fit"
        result["match"] = "partial"
        result["violated_requirements"] = []
        result["reason"] = f"Kept: {'; '.join(dict.fromkeys(dropped))}. AI note: {result.get('reason', '')}"
    elif dropped:
        result["violated_requirements"] = kept
    return result


def screen_roles(conn, role_ids: list[int], prefs: dict | None = None, log=print,
                 restore_ids: set | None = None, run_id: int | None = None) -> dict:
    """Screen roles with Claude and apply verdicts. Returns usage totals.
    restore_ids: roles the AI previously auto-marked Not a fit; if they now fit,
    they go back to Not applied.
    run_id: the scrape_runs row to keep updated after every role, so the AI spend
    is recorded even if the run crashes or the server restarts part way."""
    restore_ids = restore_ids or set()
    totals = {"screened": 0, "rejected": 0, "unsure": 0, "restored": 0, "errors": 0,
              "input_tokens": 0, "output_tokens": 0, "cost": 0.0, "skipped": None}
    prefs = prefs or db.get_prefs(conn)
    ai = prefs.get("ai", {})
    if not role_ids:
        return totals
    if not ai.get("enabled", True):
        totals["skipped"] = "AI screening is turned off in Preferences."
        log(totals["skipped"])
        return totals
    if not key_status()["configured"]:
        totals["skipped"] = "No ANTHROPIC_API_KEY found in job_search/.env, so AI screening was skipped."
        log(totals["skipped"])
        return totals

    model = ai.get("model") or "claude-haiku-5-5"
    system = build_system_prompt(prefs)
    cli = client()
    interests = {t.lower(): t for t in prefs.get("interests", [])}
    roles = [db.get_role(conn, rid) for rid in role_ids]
    roles = [r for r in roles if r]
    log(f"AI screening {len(roles)} role(s) with {model}...")
    lock = threading.Lock()

    def work(role):
        return role, screen_one(cli, model, system, role)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(work, r) for r in roles]
        for fut in as_completed(futures):
            try:
                role, (result, tin, tout) = fut.result()
            except Exception as e:  # keep going; one failure shouldn't stop the run
                totals["errors"] += 1
                log(f"  ! AI error: {type(e).__name__}: {e}")
                if "authentication" in str(e).lower() or "api key" in str(e).lower():
                    log("  ! Check ANTHROPIC_API_KEY in job_search/.env")
                continue
            with lock:
                # keep only tags that are actually in the interests list
                result = check_experience_verdict(result, prefs, role)
                result["interests_matched"] = [interests[t.lower()] for t in
                                               result.get("interests_matched", [])
                                               if t.lower() in interests]
                db.save_ai_result(conn, role["id"], result, model)
                verdict = result.get("verdict")
                # Re-read the status now, not from when the run started: you may have
                # changed it while the AI was working. save_ai_result above already
                # holds the write lock, so nothing can change it between here and the update.
                status = db.current_status(conn, role["id"])
                if verdict == "not_a_fit" and status == "not_applied":
                    db.update_role(conn, role["id"], {"status": "not_a_fit"},
                                   note=f"AI screen: {result.get('reason')}", source="ai")
                    totals["rejected"] += 1
                elif (role["id"] in restore_ids and status == "not_a_fit"
                      and db.last_change_by_ai(conn, role["id"]) and verdict != "not_a_fit"):
                    db.update_role(conn, role["id"], {"status": "not_applied"},
                                   note=f"AI re-screen: fits your updated preferences. {result.get('reason')}",
                                   source="ai")
                    totals["restored"] += 1
                if verdict == "unsure":
                    totals["unsure"] += 1
                totals["screened"] += 1
                totals["input_tokens"] += tin
                totals["output_tokens"] += tout
                if run_id is not None:
                    db.update_run(conn, run_id, ai_screened=totals["screened"], ai_rejected=totals["rejected"],
                                  ai_unsure=totals["unsure"], ai_input_tokens=totals["input_tokens"],
                                  ai_output_tokens=totals["output_tokens"],
                                  ai_cost=round(cost_of(model, totals["input_tokens"], totals["output_tokens"]), 5))
                conn.commit()
                log(f"  {db.MATCH_LEVELS[result['match']]['label']:<9}  {role['title']} @ {role['company']}"
                    f" - {result.get('reason', '')}")
    totals["cost"] = round(cost_of(model, totals["input_tokens"], totals["output_tokens"]), 5)
    log(f"AI screen done: {totals['screened']} screened, {totals['rejected']} auto-marked Not a fit, "
        f"{totals['unsure']} unsure, {totals['restored']} restored to Not applied. Tokens {totals['input_tokens']:,} in / "
        f"{totals['output_tokens']:,} out ~ ${totals['cost']:.4f}")
    return totals


def screen_one_role(role_id: int, trigger="ui") -> dict | None:
    """AI-screen a single role now, e.g. after you pasted its description yourself.
    Same rules as a re-screen: a clear miss on a Not applied role is marked Not a fit;
    a role the AI had marked Not a fit goes back to Not applied if it now fits;
    statuses you set yourself are never changed. Returns the usage totals plus
    "messages" (the screen's log lines), or None if there is no such role.
    Raises db.RunInProgress if a pull or re-screen is running."""
    messages: list[str] = []
    with db.run_lock():
        conn = db.connect()
        try:
            if not db.role_exists(conn, role_id):
                return None
            restore = {role_id} if db.last_change_by_ai(conn, role_id) else set()
            run_id = db.start_run(conn, kind="rescreen", trigger=trigger)
            try:
                t = screen_roles(conn, [role_id], log=messages.append, restore_ids=restore, run_id=run_id)
            except Exception as e:
                conn.rollback()
                db.finish_run(conn, run_id, status="error", log=str(e))
                raise
            db.finish_run(conn, run_id, status="ok" if not t["errors"] else "partial",
                          ai_screened=t["screened"], ai_rejected=t["rejected"], ai_unsure=t["unsure"],
                          ai_input_tokens=t["input_tokens"], ai_output_tokens=t["output_tokens"],
                          ai_cost=t["cost"])
            return {**t, "messages": messages}
        finally:
            conn.close()


def rescreen(scope="unscreened", trigger="ui", log=print) -> dict:
    """Re-run AI on existing 'Not applied' roles (e.g. after changing preferences).
    Raises db.RunInProgress if another pull or re-screen is running."""
    with db.run_lock():
        return _rescreen(scope, trigger, log)


def _rescreen(scope, trigger, log) -> dict:
    conn = db.connect()
    run_id = db.start_run(conn, kind="rescreen", trigger=trigger)
    sql = "SELECT id FROM roles WHERE status='not_applied' AND COALESCE(archived,0)=0"
    if scope == "unscreened":
        sql += " AND ai_screened_at IS NULL"
    ids = [r["id"] for r in conn.execute(sql)]
    restore = set()
    if scope == "all":
        # Roles the AI (not you) marked Not a fit: give them another look.
        restore = {r["id"] for r in conn.execute(
            f"SELECT id FROM roles WHERE COALESCE(archived,0)=0 AND {db.AI_REJECTED_SQL}")}
    log(f"Re-screening {len(ids)} 'Not applied' role(s)"
        + (f" and {len(restore)} role(s) the AI had marked Not a fit" if restore else "") + f" ({scope}).")
    try:
        t = screen_roles(conn, ids + sorted(restore), log=log, restore_ids=restore, run_id=run_id)
    except Exception as e:
        conn.rollback()
        db.finish_run(conn, run_id, status="error", log=str(e))
        conn.close()
        raise
    db.finish_run(conn, run_id, status="ok" if not t["errors"] else "partial",
                  ai_screened=t["screened"], ai_rejected=t["rejected"], ai_unsure=t["unsure"],
                  ai_input_tokens=t["input_tokens"], ai_output_tokens=t["output_tokens"],
                  ai_cost=t["cost"])
    conn.close()
    return t
