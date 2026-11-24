"""
Applying statistics and targets for the Insights page.

Focused on what you control: reviewing roles and sending applications.
All numbers come from jobs.db:
  applications   roles.applied_at (the date you applied)
  new roles      roles.first_seen (when a pull added it)
  AI spend       scrape_runs.ai_cost
"""

from __future__ import annotations

import datetime as dt
import json

import db

DEFAULT_TARGETS = {
    "applications_per_week": 10,
    "inbox_max": 20,          # keep the "Not applied" backlog at or under this
}


def get_targets(conn) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key='targets'").fetchone()
    saved = json.loads(row["value"]) if row else {}
    return {**DEFAULT_TARGETS, **{k: v for k, v in saved.items() if k in DEFAULT_TARGETS}}


def save_targets(conn, data: dict) -> dict:
    t = get_targets(conn)
    for k in DEFAULT_TARGETS:
        if k in data:
            v = data[k]
            t[k] = None if v in (None, "") else max(0, int(v))
    conn.execute("INSERT INTO settings(key, value) VALUES('targets', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(t),))
    conn.commit()
    return t


def _monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _as_date(s) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def build(conn, weeks: int = 12) -> dict:
    today = dt.date.today()
    this_week = _monday(today)
    week_starts = [this_week - dt.timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    index = {w: i for i, w in enumerate(week_starts)}
    applied = [0] * weeks
    new_roles = [0] * weeks
    spend = [0.0] * weeks

    def week_i(d):
        return index.get(_monday(d)) if d else None

    # Archived roles are left out of the backlog, new-role and funnel numbers, matching the
    # rest of the app. Applications still count after you archive the role: you did apply
    # that week, and archiving a rejection shouldn't lower your weekly totals or streak.
    live = "COALESCE(archived,0)=0"
    applied_rows = conn.execute(
        "SELECT applied_at, date_posted, ai_match FROM roles WHERE applied_at IS NOT NULL").fetchall()
    applied_weeks: dict[dt.date, int] = {}   # all history, for the streak
    for r in applied_rows:
        a = _as_date(r["applied_at"])
        if a:
            applied_weeks[_monday(a)] = applied_weeks.get(_monday(a), 0) + 1
    days_to_apply, by_match = [], {k: 0 for k in db.MATCH_LEVELS}
    by_match["unscreened"] = 0
    in_range = 0
    for r in applied_rows:
        a = _as_date(r["applied_at"])
        i = week_i(a)
        if i is None:
            continue
        applied[i] += 1
        in_range += 1
        posted = _as_date(r["date_posted"])
        if posted and a >= posted:
            days_to_apply.append((a - posted).days)
        m = r["ai_match"] or "unscreened"
        by_match[m if m in by_match else "unscreened"] += 1
    first_used = None
    for r in conn.execute(f"SELECT first_seen FROM roles WHERE {live}"):
        d = _as_date(r["first_seen"])
        if d and (first_used is None or d < first_used):
            first_used = d
        i = week_i(d)
        if i is not None:
            new_roles[i] += 1
    for r in conn.execute("SELECT started_at, ai_cost FROM scrape_runs WHERE ai_cost IS NOT NULL"):
        i = week_i(_as_date(r["started_at"]))
        if i is not None:
            spend[i] += r["ai_cost"] or 0

    weekly = [{"week_start": w.isoformat(), "applied": applied[i], "new_roles": new_roles[i],
               "ai_cost": round(spend[i], 4)} for i, w in enumerate(week_starts)]

    q = lambda sql: conn.execute(sql).fetchone()[0]
    funnel = [
        {"stage": "Tracked", "count": q(f"SELECT COUNT(*) FROM roles WHERE {live}")},
        # Reviewed = you moved it on; roles the AI auto-marked Not a fit don't count.
        {"stage": "Reviewed", "count": q(f"SELECT COUNT(*) FROM roles WHERE {live} AND status != 'not_applied' "
                                         f"AND NOT {db.AI_REJECTED_SQL}")},
        {"stage": "Applied", "count": q(f"SELECT COUNT(*) FROM roles WHERE {live} AND "
                                        "(applied_at IS NOT NULL OR status IN ('applied','interviewing','offer'))")},
    ]

    best = max(range(weeks), key=lambda i: applied[i]) if any(applied) else None
    # Average over the weeks you've actually been using the tracker (within the window).
    active_weeks = weeks
    if first_used:
        active_weeks = max(1, min(weeks, (this_week - _monday(first_used)).days // 7 + 1))
    applying = {
        "total": in_range,
        "per_week_avg": round(in_range / active_weeks, 1),
        "best_week": {"week_start": week_starts[best].isoformat(), "count": applied[best]} if best is not None else None,
        "avg_days_posted_to_applied": round(sum(days_to_apply) / len(days_to_apply), 1) if days_to_apply else None,
        "by_match": [{"level": k, "label": (db.MATCH_LEVELS.get(k) or {"label": "Not screened"})["label"], "count": v}
                     for k, v in by_match.items() if v or k in ("strong", "good", "partial", "weak")],
    }

    match_counts = {k: 0 for k in db.MATCH_LEVELS}
    unscreened = 0
    for r in conn.execute(f"SELECT ai_match FROM roles WHERE {live} AND status='not_applied'"):
        m = r["ai_match"]
        if m in match_counts:
            match_counts[m] += 1
        else:
            unscreened += 1
    matches = [{"level": k, "label": v["label"], "count": match_counts[k]}
               for k, v in db.MATCH_LEVELS.items() if k != "not_a_fit"]
    if unscreened:
        matches.append({"level": "unscreened", "label": "Not screened", "count": unscreened})

    targets = get_targets(conn)
    goal = targets["applications_per_week"]
    streak = 0
    if goal:
        # consecutive completed weeks (plus this week if already met) at or above the
        # goal, over all history (not just the chart window)
        wk = this_week if applied_weeks.get(this_week, 0) >= goal else this_week - dt.timedelta(weeks=1)
        while applied_weeks.get(wk, 0) >= goal:
            streak += 1
            wk -= dt.timedelta(weeks=1)
    progress = {
        "applications_this_week": applied[-1],
        "days_left_in_week": 6 - today.weekday(),
        "inbox": q(f"SELECT COUNT(*) FROM roles WHERE {live} AND status='not_applied'"),
        "application_streak_weeks": streak,
    }

    return {"weeks": weeks, "week_starts": [w.isoformat() for w in week_starts], "weekly": weekly,
            "funnel": funnel, "applying": applying, "matches": matches,
            "targets": targets, "progress": progress, "today": today.isoformat()}
