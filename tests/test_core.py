"""
Tests for the filters, AI double-check, duplicate handling and API safety checks.

    .venv\\Scripts\\python.exe -m unittest discover -s tests -t .

Every test uses a throwaway database in a temp folder; jobs.db is never touched.
"""

from __future__ import annotations

import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

import ai_screen
import daily_jobs
import db
import settings

# A sample search the filter tests run against (the code's defaults are neutral):
# one city plus remote, 1-4 years, a pay floor, engineering titles without seniority.
TEST_PREFS = db._deep_merge(db.DEFAULT_PREFS, {
    "locations": {"value": ["New York, NY"], "radius_miles": 15, "include_remote": True, "mandatory": True},
    "experience": {"min": 1, "max": 4, "mandatory": True},
    "salary": {"min_top": 120000, "mandatory": True},
    "title_include": {"value": ["engineer", "developer"], "mandatory": True},
    "title_exclude": {"value": ["senior", "sr", "staff", "principal", "lead", "manager",
                                "intern", "internship", "new grad"], "mandatory": True},
})


class TempDB(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (db.DB_PATH, settings.BACKUP_DIR, db.BASE_DIR, daily_jobs.HERE)
        db.DB_PATH = self.tmp / "test.db"
        settings.BACKUP_DIR = self.tmp / "backups"
        db.BASE_DIR = self.tmp / "resumes"
        db.BASE_DIR.mkdir()
        daily_jobs.HERE = self.tmp            # raw scrape cache goes here, not results/
        db._initialized.clear()
        self.conn = db.connect()
        db.save_prefs(self.conn, TEST_PREFS)
        self.prefs = db.get_prefs(self.conn)

    def tearDown(self):
        self.conn.close()
        db.DB_PATH, settings.BACKUP_DIR, db.BASE_DIR, daily_jobs.HERE = self._saved
        db._initialized.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)


def job(**kw):
    base = {"title": "Software Engineer", "company": "Acme", "location": "New York, NY, US",
            "is_remote": False, "site": "linkedin", "job_url": "https://example.com/1",
            "date_posted": dt.date.today().isoformat(), "job_type": "fulltime",
            "description": "We want 2+ years of experience building backend services."}
    base.update(kw)
    return base


class ParsingTests(unittest.TestCase):
    def test_years(self):
        self.assertEqual(daily_jobs.years_required({"description": "3-5 years of experience in Python"}), (3, 5))
        self.assertEqual(daily_jobs.years_required({"description": "5+ years experience"}), (5, None))
        self.assertIsNone(daily_jobs.years_required({"description": "a great team"}))

    def test_salary(self):
        self.assertEqual(daily_jobs.salary_range({"description": "Pay: $150,000 - $200,000 a year"}),
                         (150000, 200000))
        self.assertEqual(daily_jobs.salary_range({"description": "$60 - $80 per hour"}), (60 * 2080, 80 * 2080))
        self.assertEqual(daily_jobs.salary_range({"min_amount": 100, "max_amount": 120, "interval": "hourly"}),
                         (208000, 249600))

    def test_location(self):
        prefs = {"locations": dict(TEST_PREFS["locations"])}
        self.assertTrue(daily_jobs.location_ok({"location": "Brooklyn, NY"}, prefs)[0])
        self.assertTrue(daily_jobs.location_ok({"location": "Austin, TX", "is_remote": True}, prefs)[0])
        self.assertFalse(daily_jobs.location_ok({"location": "London, UK", "is_remote": True}, prefs)[0])
        self.assertFalse(daily_jobs.location_ok({"location": "San Francisco, CA"}, prefs)[0])

    def test_evaluate_title_and_years(self):
        today = dt.date.today()
        self.assertTrue(daily_jobs.evaluate(job(), today, TEST_PREFS)[0])
        self.assertFalse(daily_jobs.evaluate(job(title="Senior Software Engineer"), today, TEST_PREFS)[0])
        self.assertFalse(daily_jobs.evaluate(job(description="8+ years of experience"), today, TEST_PREFS)[0])


class VerdictCheckTests(unittest.TestCase):
    def check(self, flags, years_min=None, role=None):
        result = {"verdict": "not_a_fit", "match": "not_a_fit", "reason": "x",
                  "violated_requirements": flags, "years_min": years_min}
        return ai_screen.check_experience_verdict(result, TEST_PREFS, role or {})

    def test_title_seniority_is_kept(self):
        r = self.check(["Senior-level title"])
        self.assertEqual(r["verdict"], "not_a_fit")
        r = self.check(["Title is Staff level, requires senior experience"])
        self.assertEqual(r["verdict"], "not_a_fit")

    def test_years_within_range_is_dropped(self):
        r = self.check(["requires 4+ years"], years_min=4)
        self.assertEqual((r["verdict"], r["match"]), ("fit", "partial"))

    def test_years_above_range_is_kept(self):
        self.assertEqual(self.check(["requires 6+ years"], years_min=6)["verdict"], "not_a_fit")

    def test_remote(self):
        role = {"is_remote": True}
        self.assertEqual(self.check(["not remote-friendly location"], role=role)["verdict"], "fit")
        self.assertEqual(self.check(["remote, but only outside the US"], role=role)["verdict"], "not_a_fit")


class DuplicateTests(TempDB):
    def test_reposted_role_after_window_gets_its_own_row(self):
        rid, created = db.upsert_role(self.conn, job())
        self.assertTrue(created)
        old = (dt.datetime.now() - dt.timedelta(days=db.DUPLICATE_WINDOW_DAYS + 5)).isoformat()
        self.conn.execute("UPDATE roles SET first_seen=? WHERE id=?", (old, rid))
        rid2, created2 = db.upsert_role(self.conn, job(job_url="https://example.com/2"))
        self.assertTrue(created2)
        self.assertNotEqual(rid, rid2)

    def test_cross_posted_role_merges(self):
        rid, _ = db.upsert_role(self.conn, job(description=None))
        rid2, created = db.upsert_role(self.conn, job(job_url="https://indeed.com/9", description="full text"))
        self.assertFalse(created)
        self.assertEqual(rid, rid2)
        self.assertEqual(db.get_role(self.conn, rid)["description"], "full text")

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            db.upsert_role(self.conn, job(), status="bogus")

    def test_rename_onto_existing_role_is_allowed(self):
        a, _ = db.upsert_role(self.conn, job())
        b, _ = db.upsert_role(self.conn, job(title="Backend Engineer", job_url="https://example.com/b"))
        db.update_role(self.conn, b, {"title": "Software Engineer"})   # used to raise IntegrityError
        self.assertEqual(db.get_role(self.conn, b)["title"], "Software Engineer")

    def test_pull_keeps_nyc_listing_when_another_city_comes_first(self):
        listings = [job(location="San Francisco, CA", job_url="https://example.com/sf"),
                    job(location="New York, NY", job_url="https://example.com/ny")]
        orig = daily_jobs.fetch_jobs
        daily_jobs.fetch_jobs = lambda prefs, log, stats=None: listings
        try:
            res = daily_jobs.run(use_ai=False, log=lambda *_: None)
        finally:
            daily_jobs.fetch_jobs = orig
        self.assertEqual(res["added"], 1)
        row = self.conn.execute("SELECT job_url FROM roles").fetchone()
        self.assertEqual(row["job_url"], "https://example.com/ny")
        # the filtered-out SF listing isn't marked seen, so it's re-checked next time
        self.assertFalse(db.is_seen(self.conn, "https://example.com/sf"))


class RunLockTests(TempDB):
    def test_only_one_run_at_a_time(self):
        with db.run_lock():
            with self.assertRaises(db.RunInProgress):
                with db.run_lock():
                    pass
        with db.run_lock():   # released again
            pass

    def test_stale_running_rows_are_marked_interrupted(self):
        old = db.start_run(self.conn)
        new = db.start_run(self.conn)
        st = dict(self.conn.execute("SELECT id, status FROM scrape_runs").fetchall())
        self.assertEqual((st[old], st[new]), ("interrupted", "running"))


class ApiSafetyTests(TempDB):
    def setUp(self):
        super().setUp()
        import app
        self.client = app.app.test_client()
        (db.BASE_DIR / "resume.pdf").write_bytes(b"%PDF-1.4")
        (db.BASE_DIR / ".env").write_text("ANTHROPIC_API_KEY=secret")

    def test_foreign_host_refused(self):
        self.assertEqual(self.client.get("/api/health", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_cross_site_write_refused(self):
        r = self.client.post("/api/roles", data='{"title":"x","company":"y"}', content_type="text/plain",
                             headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        # another app on localhost (different port) is refused too
        r = self.client.post("/api/roles", json={"title": "x", "company": "y"},
                             headers={"Origin": "http://localhost:3000", "Sec-Fetch-Site": "same-site"})
        self.assertEqual(r.status_code, 403)
        # the tracker's own page (same host and port) is allowed
        r = self.client.post("/api/roles", json={"title": "x", "company": "y"},
                             headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r.status_code, 201)

    def test_file_endpoint_only_serves_resumes(self):
        ok = self.client.get("/api/file", query_string={"path": str(db.BASE_DIR / "resume.pdf")})
        self.assertEqual(ok.status_code, 200)
        ok.close()
        bad = self.client.get("/api/file", query_string={"path": str(db.BASE_DIR / ".env")})
        self.assertEqual(bad.status_code, 403)

    def test_bad_status_is_400(self):
        r = self.client.post("/api/roles", json={"title": "x", "company": "y", "status": "bogus"})
        self.assertEqual(r.status_code, 400)


class InsightsTests(TempDB):
    def test_ai_rejections_are_not_reviewed(self):
        import insights
        rid, _ = db.upsert_role(self.conn, job())
        db.update_role(self.conn, rid, {"status": "not_a_fit"}, note="AI screen: too senior", source="ai")
        rid2, _ = db.upsert_role(self.conn, job(title="Backend Engineer", job_url="https://example.com/b"))
        db.update_role(self.conn, rid2, {"status": "not_a_fit"}, note="not for me")
        funnel = {s["stage"]: s["count"] for s in insights.build(self.conn, 8)["funnel"]}
        self.assertEqual(funnel["Reviewed"], 1)


class AiStatusRaceTests(TempDB):
    def test_status_you_set_during_a_screen_is_kept(self):
        rid, _ = db.upsert_role(self.conn, job())
        self.conn.commit()

        def fake_screen(cli, model, system, role):
            # you mark the role Applied while the AI is still reading it
            other = db.connect()
            db.update_role(other, role["id"], {"status": "applied"})
            other.commit()
            other.close()
            return ({"match": "not_a_fit", "verdict": "not_a_fit", "reason": "too senior",
                     "violated_requirements": ["requires 9+ years"], "interests_matched": [],
                     "years_min": 9}, 10, 10)

        saved = (ai_screen.screen_one, ai_screen.key_status, ai_screen.client)
        ai_screen.screen_one = fake_screen
        ai_screen.key_status = lambda: {"configured": True}
        ai_screen.client = lambda: None
        try:
            totals = ai_screen.screen_roles(self.conn, [rid], self.prefs, log=lambda *_: None)
        finally:
            ai_screen.screen_one, ai_screen.key_status, ai_screen.client = saved
        self.assertEqual(totals["rejected"], 0)
        self.assertEqual(db.current_status(self.conn, rid), "applied")


class SchemaTests(TempDB):
    def test_new_status_triggers_rebuild(self):
        rid, _ = db.upsert_role(self.conn, job())
        self.conn.commit()
        self.conn.close()
        saved = dict(db.STATUSES)
        db.STATUSES["ghosted"] = "Ghosted"
        try:
            db._initialized.clear()
            self.conn = db.connect()
            db.update_role(self.conn, rid, {"status": "ghosted"})   # CHECK would reject it otherwise
            self.conn.commit()
            self.assertEqual(db.current_status(self.conn, rid), "ghosted")
            self.assertTrue(list(settings.BACKUP_DIR.glob("*-before-upgrade.db")))
        finally:
            db.STATUSES.clear()
            db.STATUSES.update(saved)

    def test_old_ai_notes_are_backfilled(self):
        rid, _ = db.upsert_role(self.conn, job())
        self.conn.execute("UPDATE roles SET status='not_a_fit' WHERE id=?", (rid,))
        self.conn.execute("INSERT INTO status_history(role_id, status, note, at) "
                          "VALUES(?, 'not_a_fit', 'AI screen: old style', '2026-01-01')", (rid,))
        self.conn.commit()
        self.conn.execute("ALTER TABLE status_history DROP COLUMN source")
        self.conn.commit()
        db._migrate(self.conn)
        self.assertTrue(db.last_change_by_ai(self.conn, rid))

    def test_backup_leaves_no_partial_files(self):
        db.upsert_role(self.conn, job())
        self.conn.commit()
        dest = db.backup(reason="daily")
        self.assertTrue(dest.exists())
        self.assertEqual([p.name for p in settings.BACKUP_DIR.iterdir()], [dest.name])
        check = db._open(dest)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM roles").fetchone()[0], 1)
        check.close()


class ResumeTests(TempDB):
    def test_cli_and_web_use_the_same_list(self):
        import jobs
        (db.BASE_DIR / "old.pdf").write_bytes(b"x")
        (db.BASE_DIR / "notes.txt").write_text("not a resume")
        (db.BASE_DIR / ".venv").mkdir()
        (db.BASE_DIR / ".venv" / "lib.pdf").write_bytes(b"x")
        import os, time
        new = db.BASE_DIR / "resume_v2.docx"
        new.write_bytes(b"x")
        os.utime(new, (time.time() + 60, time.time() + 60))
        self.assertEqual([p.name for p in db.resume_files()], ["resume_v2.docx", "old.pdf"])
        self.assertEqual(jobs.newest_resume(), str(new))


class Crash(BaseException):
    """Stands in for the process dying mid-run (not caught by 'except Exception')."""


class RunRecordTests(TempDB):
    def test_ai_spend_is_saved_before_a_crash(self):
        r1, _ = db.upsert_role(self.conn, job())
        r2, _ = db.upsert_role(self.conn, job(title="Backend Engineer", job_url="https://example.com/b"))
        self.conn.commit()
        run_id = db.start_run(self.conn, kind="rescreen")
        calls = []

        def fake_screen(cli, model, system, role):
            calls.append(role["id"])
            if len(calls) == 2:
                raise Crash()
            return ({"match": "good", "verdict": "fit", "reason": "ok", "violated_requirements": [],
                     "interests_matched": []}, 1000, 100)

        saved = (ai_screen.screen_one, ai_screen.key_status, ai_screen.client, ai_screen.MAX_WORKERS)
        ai_screen.screen_one, ai_screen.MAX_WORKERS = fake_screen, 1
        ai_screen.key_status = lambda: {"configured": True}
        ai_screen.client = lambda: None
        try:
            with self.assertRaises(Crash):
                ai_screen.screen_roles(self.conn, [r1, r2], self.prefs, log=lambda *_: None, run_id=run_id)
        finally:
            ai_screen.screen_one, ai_screen.key_status, ai_screen.client, ai_screen.MAX_WORKERS = saved
        row = self.conn.execute("SELECT ai_screened, ai_input_tokens, ai_cost FROM scrape_runs WHERE id=?",
                                (run_id,)).fetchone()
        self.assertEqual((row["ai_screened"], row["ai_input_tokens"]), (1, 1000))
        self.assertGreater(row["ai_cost"], 0)

    def test_server_start_marks_dead_runs_interrupted(self):
        run_id = db.start_run(self.conn)          # left 'running', as if the server died
        db.mark_stale_runs()
        st = self.conn.execute("SELECT status FROM scrape_runs WHERE id=?", (run_id,)).fetchone()[0]
        self.assertEqual(st, "interrupted")

    def test_live_run_is_left_alone(self):
        run_id = db.start_run(self.conn)
        with db.run_lock():                       # a real run is in progress elsewhere
            db.mark_stale_runs()
        st = self.conn.execute("SELECT status FROM scrape_runs WHERE id=?", (run_id,)).fetchone()[0]
        self.assertEqual(st, "running")

    def test_status_rebuild_keeps_seen_postings(self):
        db.mark_seen(self.conn, ["https://example.com/merged-crosspost"])
        self.conn.commit()
        self.conn.close()
        saved = dict(db.STATUSES)
        db.STATUSES["ghosted"] = "Ghosted"
        try:
            db._initialized.clear()
            self.conn = db.connect()
            self.assertTrue(db.is_seen(self.conn, "https://example.com/merged-crosspost"))
        finally:
            db.STATUSES.clear()
            db.STATUSES.update(saved)


class PreferenceValidationTests(TempDB):
    def prefs_with(self, **changes):
        import copy
        p = copy.deepcopy(db.DEFAULT_PREFS)
        for path, v in changes.items():
            node = p
            *parents, last = path.split("__")
            for k in parents:
                node = node[k]
            node[last] = v
        return p

    def test_defaults_are_valid_and_neutral(self):
        self.assertEqual(db.validate_prefs(db.DEFAULT_PREFS), [])
        self.assertEqual((db.DEFAULT_PREFS["locations"]["value"], db.DEFAULT_PREFS["prompt"],
                          db.DEFAULT_PREFS["interests"]), ([], "", []))
        self.assertIn("Location: fully remote within the US", ai_screen.build_system_prompt(db.DEFAULT_PREFS))

    def test_needs_a_location_or_remote(self):
        p = self.prefs_with(locations__include_remote=False)
        self.assertTrue(any("Add a location" in e for e in db.validate_prefs(p)))

    def test_blank_experience_is_rejected(self):
        errs = db.validate_prefs(self.prefs_with(experience__max=None))
        self.assertTrue(any("Maximum years" in e for e in errs))
        with self.assertRaises(ValueError):
            db.save_prefs(self.conn, self.prefs_with(experience__max=None))

    def test_min_above_max_is_rejected(self):
        errs = db.validate_prefs(self.prefs_with(experience__min=5, experience__max=2))
        self.assertTrue(any("can't be more than" in e for e in errs))

    def test_bad_values_are_rejected(self):
        self.assertTrue(db.validate_prefs(self.prefs_with(sites=[])))
        self.assertTrue(db.validate_prefs(self.prefs_with(sites=["myspace"])))
        self.assertTrue(db.validate_prefs(self.prefs_with(results_per_search=0)))
        self.assertTrue(db.validate_prefs(self.prefs_with(results_per_search="40")))
        self.assertTrue(db.validate_prefs(self.prefs_with(ai__model="gpt")))

    def test_blank_pay_minimum_is_allowed_and_screen_prompt_works(self):
        p = self.prefs_with(salary__min_top=None)
        self.assertEqual(db.validate_prefs(p), [])
        self.assertIn("no minimum", ai_screen.build_system_prompt(p))

    def test_api_returns_400_and_keeps_old_prefs(self):
        import app
        client = app.app.test_client()
        r = client.put("/api/preferences", json=self.prefs_with(experience__max=None))
        self.assertEqual(r.status_code, 400)
        self.assertIn("Maximum years", r.get_json()["error"])
        self.assertEqual(db.get_prefs(self.conn)["experience"]["max"], TEST_PREFS["experience"]["max"])


class AppliedDateTests(TempDB):
    def test_undoing_applied_clears_the_date(self):
        import insights
        rid, _ = db.upsert_role(self.conn, job())
        db.update_role(self.conn, rid, {"status": "applied"})
        db.update_role(self.conn, rid, {"status": "not_applied"})
        self.assertIsNone(db.get_role(self.conn, rid)["applied_at"])
        self.assertEqual(insights.build(self.conn, 4)["progress"]["applications_this_week"], 0)

    def test_rejected_keeps_the_date(self):
        rid, _ = db.upsert_role(self.conn, job())
        db.update_role(self.conn, rid, {"status": "applied"})
        db.update_role(self.conn, rid, {"status": "rejected"})
        self.assertIsNotNone(db.get_role(self.conn, rid)["applied_at"])


class RoleEditValidationTests(TempDB):
    def setUp(self):
        super().setUp()
        import app
        self.client = app.app.test_client()
        self.rid = self.client.post("/api/roles", json={"title": "SWE", "company": "Acme"}).get_json()["id"]

    def patch(self, **body):
        return self.client.patch(f"/api/roles/{self.rid}", json=body)

    def test_blank_title_or_company_rejected(self):
        r = self.patch(title="", company="  ")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Title can't be blank", r.get_json()["error"])
        self.assertIn("Company can't be blank", r.get_json()["error"])
        self.assertEqual(db.get_role(self.conn, self.rid)["title"], "SWE")

    def test_numbers_checked_and_converted(self):
        self.assertEqual(self.patch(pay_max="lots").status_code, 400)
        self.assertEqual(self.patch(years_min=2.5).status_code, 400)
        self.assertEqual(self.patch(pay_min=-5).status_code, 400)
        r = self.patch(pay_max="185000", years_min="3")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.get_json()["pay_max"], r.get_json()["years_min"]), (185000.0, 3))

    def test_dates_checked(self):
        self.assertEqual(self.patch(applied_at="not-a-date").status_code, 400)
        self.assertEqual(self.patch(applied_at="2026-13-01").status_code, 400)
        self.assertEqual(self.patch(applied_at="2026-11-20").get_json()["applied_at"], "2026-11-20")

    def test_dates_are_strict(self):
        for bad in ["2026-11-21junk", "20261121", "2026-1-5", "21/11/2026"]:
            self.assertEqual(self.patch(applied_at=bad).status_code, 400, bad)
        self.assertEqual(self.patch(applied_at="2026-11-21T09:30:00").get_json()["applied_at"], "2026-11-21")

    def test_remote_flag_is_strict(self):
        self.assertEqual(self.patch(is_remote="yes").status_code, 400)
        self.assertEqual(self.patch(is_remote=True).get_json()["is_remote"], 1)
        self.assertEqual(self.patch(is_remote="false").get_json()["is_remote"], 0)
        self.assertEqual(self.patch(is_remote=1).get_json()["is_remote"], 1)

    def test_bad_transition_saves_nothing(self):
        before = len(db.get_role(self.conn, self.rid)["history"])
        r = self.client.post(f"/api/roles/{self.rid}/transition",
                             json={"status": "offer", "offer_salary": "abc", "note": "x"})
        self.assertEqual(r.status_code, 400)
        role = db.get_role(self.conn, self.rid)
        self.assertEqual((role["status"], len(role["history"])), ("not_applied", before))

    def test_bad_new_role_saves_nothing(self):
        r = self.client.post("/api/roles", json={"title": "ML Eng", "company": "Beta", "status": "applied",
                                                 "applied_at": "yesterday"})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(db.find_existing(self.conn, "ML Eng", "Beta"))

    def test_cli_rejects_bad_date(self):
        import subprocess, sys, os
        env = {**os.environ, "JOBS_DB": str(db.DB_PATH)}
        out = subprocess.run([sys.executable, "jobs.py", "applied", str(self.rid), "--date", "someday"],
                             capture_output=True, text=True, env=env)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("Not saved", out.stderr)
        self.assertIsNone(db.get_role(self.conn, self.rid)["applied_at"])


class ClosedStatusTests(TempDB):
    def test_closed_status_saves_and_sorts_before_not_a_fit(self):
        a, _ = db.upsert_role(self.conn, job())
        b, _ = db.upsert_role(self.conn, job(title="Backend Engineer", job_url="https://example.com/b"))
        c, _ = db.upsert_role(self.conn, job(title="Data Engineer", job_url="https://example.com/c"))
        db.update_role(self.conn, a, {"status": "not_a_fit"})
        db.update_role(self.conn, b, {"status": "closed"})
        db.update_role(self.conn, c, {"status": "withdrawn"})
        rows, counts = db.list_roles(self.conn, sort="status", order="asc")
        self.assertEqual([r["status"] for r in rows], ["withdrawn", "closed", "not_a_fit"])
        self.assertEqual(counts["closed"], 1)

    def test_existing_database_is_upgraded_to_allow_it(self):
        # simulate a database created before "closed" existed
        self.conn.close()
        saved = dict(db.STATUSES)
        del db.STATUSES["closed"]
        db._initialized.clear()
        self.conn = db.connect()
        self.conn.close()
        db.STATUSES.clear()
        db.STATUSES.update(saved)
        db._initialized.clear()
        self.conn = db.connect()               # sees the new status list -> rebuilds
        rid, _ = db.upsert_role(self.conn, job())
        db.update_role(self.conn, rid, {"status": "closed"})
        self.assertEqual(db.current_status(self.conn, rid), "closed")

    def test_cli_alias(self):
        import jobs
        self.assertEqual(jobs.norm_status("no longer accepting"), "closed")
        self.assertEqual(jobs.norm_status("expired"), "closed")


class ManualDescriptionTests(TempDB):
    """Adding a description yourself, then screening that one role."""

    def setUp(self):
        super().setUp()
        import app
        self.client = app.app.test_client()
        self.rid, _ = db.upsert_role(self.conn, job(description=None))
        self.conn.commit()
        self.saved = (ai_screen.screen_one, ai_screen.key_status, ai_screen.client)
        self.seen_text = []

        def fake_screen(cli, model, system, role):
            self.seen_text.append(role.get("description"))
            return ({"match": "good", "verdict": "fit", "reason": "fits", "violated_requirements": [],
                     "interests_matched": [], "years_min": 2, "years_max": 4, "pay_min": None, "pay_max": None},
                    500, 50)

        ai_screen.screen_one = fake_screen
        ai_screen.key_status = lambda: {"configured": True}
        ai_screen.client = lambda: None

    def tearDown(self):
        ai_screen.screen_one, ai_screen.key_status, ai_screen.client = self.saved
        super().tearDown()

    def test_add_description_then_screen(self):
        r = self.client.patch(f"/api/roles/{self.rid}",
                              json={"description": "Build LLM tools. 2-4 years of experience.",
                                    "note": "Description added by you"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/api/roles/{self.rid}/screen")
        self.assertEqual(r.status_code, 200)
        role = r.get_json()["role"]
        self.assertEqual(self.seen_text, ["Build LLM tools. 2-4 years of experience."])
        self.assertEqual((role["ai_match"], role["years_min"]), ("good", 2))
        self.assertIn("Description added by you", [h["note"] for h in role["history"]])
        run = self.conn.execute("SELECT kind, trigger, status, ai_screened FROM scrape_runs").fetchone()
        self.assertEqual(tuple(run), ("rescreen", "ui", "ok", 1))

    def test_role_the_ai_rejected_comes_back_when_it_now_fits(self):
        db.update_role(self.conn, self.rid, {"status": "not_a_fit"}, note="AI screen: no info", source="ai")
        self.conn.commit()
        role = self.client.post(f"/api/roles/{self.rid}/screen").get_json()["role"]
        self.assertEqual(role["status"], "not_applied")

    def test_status_you_set_is_kept(self):
        db.update_role(self.conn, self.rid, {"status": "applied"})
        self.conn.commit()
        role = self.client.post(f"/api/roles/{self.rid}/screen").get_json()["role"]
        self.assertEqual(role["status"], "applied")

    def test_errors(self):
        self.assertEqual(self.client.post("/api/roles/99999/screen").status_code, 404)
        with db.run_lock():
            self.assertEqual(self.client.post(f"/api/roles/{self.rid}/screen").status_code, 409)
        ai_screen.key_status = lambda: {"configured": False}
        r = self.client.post(f"/api/roles/{self.rid}/screen")
        self.assertEqual(r.status_code, 400)
        self.assertIn("ANTHROPIC_API_KEY", r.get_json()["error"])


class StepTests(TempDB):
    """Steps (recruiter call, technical screen, ...) each with a to-do / done state."""

    def current_step(self, rid):
        rows, _ = db.list_roles(self.conn)
        return next(r["current_step"] for r in rows if r["id"] == rid)

    def test_technical_screen_is_a_step_type(self):
        self.assertEqual(db.ROUND_KINDS["technical_screen"], "Technical screen")
        self.assertEqual(db.ROUND_STATUSES["scheduled"], "To do")

    def test_adding_a_step_moves_an_applied_role_to_interviewing(self):
        rid, _ = db.upsert_role(self.conn, job())
        db.update_role(self.conn, rid, {"status": "applied"})
        db.add_round(self.conn, rid, {"kind": "technical_screen", "scheduled_on": "2026-11-25"})
        role = db.get_role(self.conn, rid)
        self.assertEqual(role["status"], "interviewing")
        self.assertEqual(role["history"][0]["note"], "Step 1 (Technical screen): To do")
        self.assertEqual(role["applied_at"], db.get_role(self.conn, rid)["applied_at"])   # applied date kept

    def test_adding_a_step_leaves_later_statuses_alone(self):
        rid, _ = db.upsert_role(self.conn, job())
        db.update_role(self.conn, rid, {"status": "offer"})
        db.add_round(self.conn, rid, {"kind": "other"})
        self.assertEqual(db.current_status(self.conn, rid), "offer")

    def test_status_change_popup_logs_once(self):
        rid, _ = db.upsert_role(self.conn, job())
        db.update_role(self.conn, rid, {"status": "applied"})
        before = len(db.get_role(self.conn, rid)["history"])
        db.transition(self.conn, rid, {"status": "interviewing", "round": {"kind": "recruiter", "status": "scheduled"}})
        role = db.get_role(self.conn, rid)
        self.assertEqual(role["status"], "interviewing")
        self.assertEqual(len(role["history"]) - before, 2)    # the move + the step, not the move twice

    def test_current_step_is_the_next_to_do(self):
        rid, _ = db.upsert_role(self.conn, job())
        db.add_round(self.conn, rid, {"kind": "recruiter", "status": "passed"})
        db.add_round(self.conn, rid, {"kind": "onsite", "scheduled_on": "2026-12-10"})
        db.add_round(self.conn, rid, {"kind": "technical_screen", "scheduled_on": "2026-11-25"})
        n, st, kind, date = self.current_step(rid).split("|")
        self.assertEqual((st, kind, date), ("scheduled", "technical_screen", "2026-11-25"))

    def test_current_step_falls_back_to_latest(self):
        rid, _ = db.upsert_role(self.conn, job())
        r1 = db.add_round(self.conn, rid, {"kind": "recruiter", "status": "passed"})
        r2 = db.add_round(self.conn, rid, {"kind": "technical_screen"})
        db.update_round(self.conn, r2, {"status": "waiting"})     # "Mark done"
        self.assertTrue(self.current_step(rid).startswith("2|waiting|technical_screen"))


def run_pull(listings, use_ai=False):
    """Run a pull against fake scraped listings (no network)."""
    orig = daily_jobs.fetch_jobs
    daily_jobs.fetch_jobs = lambda prefs, log, stats=None: listings
    try:
        return daily_jobs.run(use_ai=use_ai, log=lambda *_: None)
    finally:
        daily_jobs.fetch_jobs = orig


class CrossBoardFillTests(TempDB):
    """A posting seen first without a description gets one from a later listing."""

    def test_later_listing_fills_missing_description(self):
        run_pull([job(site="linkedin", job_url="https://linkedin.example/1", description=None)])
        rid = self.conn.execute("SELECT id FROM roles").fetchone()["id"]
        self.assertIsNone(db.get_role(self.conn, rid)["description"])
        res = run_pull([job(site="zip_recruiter", job_url="https://zip.example/9",
                            description="Build things. 2+ years of experience.")])
        role = db.get_role(self.conn, rid)
        self.assertEqual(role["description"], "Build things. 2+ years of experience.")
        self.assertEqual((res["added"], res["details_filled"], res["descriptions_added"]), (0, 1, 1))
        self.assertTrue(any("Description added from another listing" in (h["note"] or "") for h in role["history"]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0], 1)

    def test_existing_values_are_never_overwritten(self):
        run_pull([job(site="linkedin", job_url="https://linkedin.example/1", description="Original text")])
        run_pull([job(site="zip_recruiter", job_url="https://zip.example/9", description="Other text")])
        self.assertEqual(self.conn.execute("SELECT description FROM roles").fetchone()[0], "Original text")

    def test_role_gets_screened_once_it_has_a_description(self):
        run_pull([job(site="linkedin", job_url="https://linkedin.example/1", description=None)])
        seen = []
        saved = (ai_screen.screen_one, ai_screen.key_status, ai_screen.client)
        ai_screen.screen_one = lambda cli, model, system, role: (seen.append(role["description"]) or (
            {"match": "good", "verdict": "fit", "reason": "ok", "violated_requirements": [], "interests_matched": []}, 1, 1))
        ai_screen.key_status = lambda: {"configured": True}
        ai_screen.client = lambda: None
        try:
            res = run_pull([job(site="zip_recruiter", job_url="https://zip.example/9", description="Real text")],
                           use_ai=True)
        finally:
            ai_screen.screen_one, ai_screen.key_status, ai_screen.client = saved
        self.assertEqual(seen, ["Real text"])
        self.assertEqual(res["ai"]["screened"], 1)


class BlockedCompanyTests(TempDB):
    def test_matching(self):
        blocked = ["Acme", "Foo, Inc."]
        self.assertTrue(db.company_blocked("Acme", blocked))
        self.assertTrue(db.company_blocked("ACME Corp", blocked))
        self.assertTrue(db.company_blocked("Foo Inc", blocked))
        self.assertFalse(db.company_blocked("Acmex", blocked))
        self.assertFalse(db.company_blocked("", blocked))

    def test_block_from_api_and_pull_skips_it(self):
        import app
        client = app.app.test_client()
        r = client.post("/api/blocked-companies", json={"company": "Acme"})
        self.assertEqual(r.get_json()["blocked"], ["Acme"])
        client.post("/api/blocked-companies", json={"company": "acme"})          # no duplicate
        self.assertEqual(db.get_prefs(self.conn)["companies_exclude"]["value"], ["Acme"])
        self.assertEqual(client.get("/api/meta").get_json()["blocked_companies"], ["Acme"])
        res = run_pull([job(company="Acme Corp")])
        self.assertEqual((res["added"], res["rejected"].get("blocked company")), (0, 1))
        self.assertEqual(client.post("/api/blocked-companies", json={"company": " "}).status_code, 400)


class SidebarFilterTests(TempDB):
    def setUp(self):
        super().setUp()
        import app
        self.client = app.app.test_client()
        today = dt.date.today()
        a, _ = db.upsert_role(self.conn, job(title="A Engineer", job_url="u1", pay_max=200000, years_min=2,
                                             description="x", date_posted=today.isoformat()))
        b, _ = db.upsert_role(self.conn, job(title="B Engineer", job_url="u2", pay_max=120000, years_min=6, site="indeed",
                                             description=None, date_posted=(today - dt.timedelta(days=20)).isoformat()))
        c, _ = db.upsert_role(self.conn, job(title="C Engineer", job_url="u3", pay_max=None, years_min=None, site="manual",
                                             description=None))
        db.save_ai_result(self.conn, a, {"verdict": "fit", "match": "strong", "reason": "r"}, "m")
        db.add_round(self.conn, b, {"kind": "technical_screen"})
        self.conn.commit()
        self.ids = {"a": a, "b": b, "c": c}

    def titles(self, **params):
        rows = self.client.get("/api/roles", query_string=params).get_json()["roles"]
        return sorted(r["title"][0] for r in rows)

    def test_filters(self):
        self.assertEqual(self.titles(fit="strong"), ["A"])
        self.assertEqual(self.titles(fit="unscreened"), ["B", "C"])
        self.assertEqual(self.titles(min_pay=150000), ["A"])
        self.assertEqual(self.titles(max_years=4), ["A", "C"])          # C doesn't say: kept
        self.assertEqual(self.titles(posted="7"), ["A", "C"])
        self.assertEqual(self.titles(sites="indeed,manual"), ["B", "C"])
        self.assertEqual(self.titles(described="1"), ["A"])
        self.assertEqual(self.titles(todo="1"), ["B"])
        self.assertEqual(self.client.get("/api/roles", query_string={"min_pay": "lots"}).status_code, 400)

    def test_location_filter_and_common_places(self):
        db.update_role(self.conn, self.ids["a"], {"location": "Brooklyn, NY"})
        db.update_role(self.conn, self.ids["b"], {"location": "San Francisco, CA"})
        db.update_role(self.conn, self.ids["c"], {"location": "Remote (Austin, TX)"})
        self.conn.commit()
        self.assertEqual(self.titles(loc="brooklyn"), ["A"])                 # case-insensitive
        self.assertEqual(self.titles(loc="Brooklyn|, CA"), ["A", "B"])       # any of several
        self.assertEqual(self.titles(loc="Seattle"), [])
        self.assertEqual(self.titles(loc="CA"), ["B"])              # whole word: state code
        db.update_role(self.conn, self.ids["b"], {"location": "Chicago, IL"})
        self.conn.commit()
        self.assertEqual(self.titles(loc="CA"), [])                 # "CA" doesn't match "Chicago"
        self.assertEqual(self.titles(loc="new york"), [])
        db.update_role(self.conn, self.ids["b"], {"location": "San Francisco, CA"})
        db.update_role(self.conn, self.ids["c"], {"location": "Brooklyn. NY"})
        self.conn.commit()
        self.assertEqual(db.common_places(self.conn)[0], "Brooklyn")   # "Brooklyn. NY" counts as Brooklyn
        db.update_role(self.conn, self.ids["c"], {"location": "Remote (Austin, TX)"})
        self.conn.commit()
        self.assertEqual(db.common_places(self.conn), ["Brooklyn", "San Francisco"])   # remote not listed
        self.assertEqual(self.client.get("/api/meta").get_json()["places"], ["Brooklyn", "San Francisco"])


class NoLimitTests(TempDB):
    def test_no_limit_asks_boards_for_everything(self):
        import sys, types
        calls = []
        fake = types.ModuleType("jobspy")
        class DF:
            def to_dict(self, _): return []
            def __len__(self): return 0
        fake.scrape_jobs = lambda **kw: calls.append(kw["results_wanted"]) or DF()
        saved, sleep = sys.modules.get("jobspy"), daily_jobs.time.sleep
        sys.modules["jobspy"], daily_jobs.time.sleep = fake, (lambda s: None)
        try:
            p = db._deep_merge(TEST_PREFS, {"results_per_search": 25})
            daily_jobs.fetch_jobs(p, lambda *_: None)
            daily_jobs.fetch_jobs({**p, "results_all": True}, lambda *_: None)
        finally:
            daily_jobs.time.sleep = sleep
            if saved is not None:
                sys.modules["jobspy"] = saved
            else:
                del sys.modules["jobspy"]
        self.assertEqual(set(calls[:2]), {25})
        self.assertEqual(set(calls[2:]), {daily_jobs.NO_LIMIT_RESULTS})
        self.assertTrue(db.validate_prefs({**p, "results_all": "yes"}))


class CompanyNameDuplicateTests(TempDB):
    """The same posting under different company names on different boards."""
    TITLE = "Software Development Engineer, Amazon Legal Technologies"

    def test_company_names(self):
        self.assertEqual(db.company_core("Amazon.com Services LLC"), "amazon services")
        self.assertTrue(db.companies_match("Amazon", "Amazon.com"))
        self.assertTrue(db.companies_match("Amazon", "Amazon.com Services LLC"))
        self.assertTrue(db.companies_match("Acme, Inc.", "ACME"))
        self.assertFalse(db.companies_match("Meta", "Metabase"))
        self.assertFalse(db.companies_match("Amazon", "Google"))

    def test_pull_merges_three_boards_into_one_role(self):
        res = run_pull([
            job(title=self.TITLE, company="Amazon", site="linkedin", job_url="https://li/1", location="New York, NY"),
            job(title=self.TITLE, company="Amazon.com", site="indeed", job_url="https://in/2", location="New York, NY, US",
                job_url_direct="https://amazon.jobs/10562333"),
            job(title=self.TITLE, company="Amazon.com Services LLC", site="glassdoor", job_url="https://gd/3",
                location="New York, NY")])
        self.assertEqual(res["added"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT job_url_direct FROM roles").fetchone()[0], "https://amazon.jobs/10562333")

    def test_different_city_or_title_stays_separate(self):
        run_pull([job(title=self.TITLE, company="Amazon", job_url="https://li/1", location="New York, NY"),
                  job(title=self.TITLE, company="Amazon.com", job_url="https://in/2", location="Seattle, WA",
                      is_remote=False),
                  job(title="Software Engineer II", company="Amazon.com", job_url="https://in/3", location="New York, NY")])
        # Seattle is filtered out by the NYC search; the different title is its own role
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0], 2)

    def test_dupes_and_merge(self):
        a, _ = db.upsert_role(self.conn, job(title=self.TITLE, company="Amazon", job_url="https://li/1", site="linkedin"))
        # older rows made before the looser matching existed: insert directly
        for company, url, site in (("Amazon.com", "https://in/2", "indeed"), ("Amazon.com Services LLC", "https://gd/3", "glassdoor")):
            self.conn.execute(
                "INSERT INTO roles(signature, title, company, location, site, job_url, status, first_seen, updated_at) "
                "VALUES(?,?,?,?,?,?, 'not_applied', ?, ?)",
                (db.signature(self.TITLE, company), self.TITLE, company, "New York, NY, US", site, url, db.now_iso(), db.now_iso()))
        ids = [r[0] for r in self.conn.execute("SELECT id FROM roles ORDER BY id")]
        b, c = ids[1], ids[2]
        db.update_role(self.conn, b, {"status": "applied", "applied_at": "2026-11-22", "notes": "applied via site"})
        db.update_role(self.conn, c, {"status": "applied", "applied_at": "2026-11-21", "resume_path": "r.pdf"})
        db.add_round(self.conn, c, {"kind": "recruiter"})
        db.add_qa(self.conn, b, "Why Amazon?", "Scale")
        groups = db.find_duplicate_groups(self.conn)
        self.assertEqual([[r["id"] for r in g] for g in groups], [[a, b, c]])
        merged = db.merge_roles(self.conn, b, [a, c])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0], 1)
        self.assertEqual(merged["status"], "interviewing")          # furthest status (c got a step)
        self.assertEqual(merged["applied_at"], "2026-11-21")        # earliest applied date
        self.assertEqual(merged["resume_path"], "r.pdf")            # filled from a duplicate
        self.assertEqual((len(merged["rounds"]), len(merged["qa"])), (1, 1))
        self.assertIn("Merged duplicate role(s)", merged["history"][0]["note"])
        self.assertTrue(db.is_seen(self.conn, "https://li/1"))
        self.assertEqual(db.find_duplicate_groups(self.conn), [])


if __name__ == "__main__":
    unittest.main()
