"""
Quick command-line updates for the job tracker database.

  python jobs.py applied "globex"                      mark a role as applied (today, newest resume)
  python jobs.py applied initech --resume path\\to.pdf  ...with a specific resume
  python jobs.py status "initech" interviewing --note "Phone screen Tue"
  python jobs.py add "Software Engineer" "Acme" --url https://... --status applied
  python jobs.py archive "acme"                         hide a role (add --undo to restore)
  python jobs.py find acme                             search roles
  python jobs.py list --status applied                 list roles by status
  python jobs.py scrape                                pull new roles (same as daily_jobs.py)

Statuses: not_applied, interested, applied, interviewing, offer, rejected, withdrawn, closed, not_a_fit
(closed = no longer accepting applications)
Use python from .venv:  .venv\\Scripts\\python.exe jobs.py ...
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import db

STATUS_ALIASES = {
    "new": "not_applied", "not-applied": "not_applied", "todo": "not_applied",
    "interview": "interviewing", "interviews": "interviewing",
    "reject": "rejected", "rejection": "rejected", "declined": "rejected",
    "withdraw": "withdrawn", "expired": "closed", "filled": "closed",
    "no-longer-accepting": "closed", "no_longer_accepting": "closed", "skip": "not_a_fit", "not-a-fit": "not_a_fit", "nope": "not_a_fit",
    "saved": "interested", "save": "interested",
}


def norm_status(s: str) -> str:
    s = s.lower().strip().replace(" ", "_")
    s = STATUS_ALIASES.get(s, s)
    if s not in db.STATUSES:
        sys.exit(f"Unknown status '{s}'. Use one of: {', '.join(db.STATUSES)}")
    return s


def newest_resume() -> str | None:
    """The most recently modified resume (the same list the web app's picker shows)."""
    files = db.resume_files()
    return str(files[0]) if files else None


def fmt(r) -> str:
    pay = ""
    if r["pay_max"]:
        pay = f"  ${(r['pay_min'] or r['pay_max'])/1000:.0f}k-${r['pay_max']/1000:.0f}k"
    applied = f"  applied {r['applied_at']}" if r["applied_at"] else ""
    return (f"  #{r['id']:<4} [{db.STATUSES[r['status']]:<12}] {r['title']} @ {r['company']}"
            f"  ({r['location'] or ''}){pay}{applied}")


def pick(c, query: str, include_archived=False):
    """Find one role by id or search text; ask if several match."""
    if query.lstrip("#").isdigit():
        r = c.execute("SELECT * FROM roles WHERE id=?", (int(query.lstrip("#")),)).fetchone()
        if not r:
            sys.exit(f"No role with id {query}")
        return r
    rows, _ = db.list_roles(c, q=query, sort="first_seen", include_archived=include_archived)
    # Prefer company/title matches over description matches.
    ql = query.lower()
    strong = [r for r in rows if ql in (r["company"] or "").lower() or ql in (r["title"] or "").lower()]
    rows = strong or rows
    if not rows:
        sys.exit(f"No roles match '{query}'. Add it with:  python jobs.py add \"<title>\" \"<company>\"")
    if len(rows) == 1:
        return c.execute("SELECT * FROM roles WHERE id=?", (rows[0]["id"],)).fetchone()
    print(f"{len(rows)} roles match '{query}':")
    for r in rows[:25]:
        print(fmt(r))
    choice = input("Enter the # of the role (blank to cancel): ").strip().lstrip("#")
    if not choice:
        sys.exit("Cancelled.")
    r = c.execute("SELECT * FROM roles WHERE id=?", (int(choice),)).fetchone()
    if not r:
        sys.exit("No such id.")
    return r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("applied", help="mark a role as applied")
    a.add_argument("query", help="role id or search text (company/title)")
    a.add_argument("--resume", help="resume file used (default: newest resume in the job applications folder)")
    a.add_argument("--date", help="date applied, YYYY-MM-DD (default today)")
    a.add_argument("--note")

    s = sub.add_parser("status", help="set a role's status")
    s.add_argument("query")
    s.add_argument("status")
    s.add_argument("--note")

    ad = sub.add_parser("add", help="add a role that wasn't scraped")
    ad.add_argument("title")
    ad.add_argument("company")
    ad.add_argument("--url")
    ad.add_argument("--location")
    ad.add_argument("--remote", action="store_true")
    ad.add_argument("--pay-min", type=float)
    ad.add_argument("--pay-max", type=float)
    ad.add_argument("--status", default="not_applied")
    ad.add_argument("--resume")
    ad.add_argument("--note")

    ar = sub.add_parser("archive", help="hide a role from default views (use --undo to restore)")
    ar.add_argument("query")
    ar.add_argument("--undo", action="store_true")

    f = sub.add_parser("find", help="search roles")
    f.add_argument("query", nargs="?", default="")

    l = sub.add_parser("list", help="list roles")
    l.add_argument("--status", help="comma-separated statuses")

    sub.add_parser("scrape", help="pull new roles now")

    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    c = db.connect()

    if args.cmd == "applied":
        r = pick(c, args.query)
        resume = args.resume or r["resume_path"] or newest_resume()
        if resume:
            resume = str(Path(resume).expanduser().resolve())
        db.update_role(c, r["id"], {"status": "applied", "resume_path": resume,
                                    "applied_at": args.date or dt.date.today().isoformat()},
                       note=args.note)
        c.commit()
        print(f"✓ Applied: {r['title']} @ {r['company']}  (#{r['id']})\n  resume: {resume}")

    elif args.cmd == "status":
        r = pick(c, args.query)
        st = norm_status(args.status)
        db.update_role(c, r["id"], {"status": st}, note=args.note)
        c.commit()
        print(f"✓ {r['title']} @ {r['company']} → {db.STATUSES[st]}")

    elif args.cmd == "add":
        st = norm_status(args.status)
        existing = db.find_existing(c, args.title, args.company, args.url)
        if existing:
            print("Already in the database:")
            print(fmt(existing))
            return
        rid, _ = db.upsert_role(c, {
            "title": args.title, "company": args.company, "job_url": args.url,
            "location": args.location, "is_remote": args.remote, "site": "manual",
            "pay_min": args.pay_min, "pay_max": args.pay_max, "source": "manual",
        }, status=st, note="Added from command line")
        extra = {"notes": args.note}
        if st == "applied":
            extra["resume_path"] = args.resume or newest_resume()
            extra["applied_at"] = dt.date.today().isoformat()
        elif args.resume:
            extra["resume_path"] = args.resume
        db.update_role(c, rid, {k: v for k, v in extra.items() if v})
        c.commit()
        print(f"✓ Added #{rid}: {args.title} @ {args.company} [{db.STATUSES[st]}]")

    elif args.cmd == "archive":
        r = pick(c, args.query, include_archived=True)
        db.set_archived(c, r["id"], not args.undo)
        c.commit()
        print(f"✓ {'Unarchived' if args.undo else 'Archived'}: {r['title']} @ {r['company']}")

    elif args.cmd == "find":
        rows, _ = db.list_roles(c, q=args.query)
        print(f"{len(rows)} roles")
        for r in rows:
            print(fmt(r))

    elif args.cmd == "list":
        sts = [norm_status(x) for x in args.status.split(",")] if args.status else None
        rows, counts = db.list_roles(c, statuses=sts, sort="status", order="asc")
        print("  ".join(f"{db.STATUSES[k]}: {v}" for k, v in counts.items() if v))
        for r in rows:
            print(fmt(r))

    elif args.cmd == "scrape":
        import daily_jobs
        try:
            daily_jobs.run()
        except db.RunInProgress as e:
            sys.exit(str(e))


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:   # e.g. a bad --date; nothing was committed
        sys.exit(f"Not saved: {e}")
