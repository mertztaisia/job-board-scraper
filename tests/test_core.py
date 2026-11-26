"""
Tests for the filters, AI double-check, duplicate handling and API safety checks.

    .venv\\Scripts\\python.exe -m unittest discover -s tests -t .

Every test uses a throwaway database in a temp folder; jobs.db is never touched.
"""

from __future__ import annotations

import datetime as dt
import json
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
        # Tests never fetch real job pages: LinkedIn and ZipRecruiter pages "fail" (no
        # description) unless a test sets its own fetcher, and nothing waits between pages.
        self._saved_fetch = (daily_jobs._zip_fetcher, daily_jobs._linkedin_fetcher,
                             daily_jobs.ZIP_PAUSE, daily_jobs.LINKEDIN_PAUSE, daily_jobs.ZIP_BACKOFF)
        daily_jobs._zip_fetcher = daily_jobs._linkedin_fetcher = lambda: (lambda job: {})
        daily_jobs.ZIP_PAUSE = daily_jobs.LINKEDIN_PAUSE = (0, 0)
        daily_jobs.ZIP_BACKOFF = 0
        db._initialized.clear()
        self.conn = db.connect()
        db.save_prefs(self.conn, TEST_PREFS)
        self.prefs = db.get_prefs(self.conn)

    def tearDown(self):
        self.conn.close()
        db.DB_PATH, settings.BACKUP_DIR, db.BASE_DIR, daily_jobs.HERE = self._saved
        (daily_jobs._zip_fetcher, daily_jobs._linkedin_fetcher, daily_jobs.ZIP_PAUSE,
         daily_jobs.LINKEDIN_PAUSE, daily_jobs.ZIP_BACKOFF) = self._saved_fetch
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

    def test_google_jobs_is_no_longer_offered(self):
        # Google answers JobSpy with an "enablejs" page, so it's gone from the job sites.
        self.assertNotIn("google", db.SITES)
        self.assertIn("Unknown job site: google.", db.validate_prefs(self.prefs_with(sites=["linkedin", "google"])))
        # Older saved preferences that still list it are cleaned when read, so pulls skip it
        # and saving them again works.
        self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('preferences', ?)",
                          (json.dumps({"sites": ["linkedin", "google"]}),))
        self.assertEqual(db.get_prefs(self.conn)["sites"], ["linkedin"])
        self.assertEqual(db.validate_prefs(db.get_prefs(self.conn)), [])
        self.conn.execute("UPDATE settings SET value=? WHERE key='preferences'", (json.dumps({"sites": ["google"]}),))
        self.assertEqual(db.get_prefs(self.conn)["sites"], db.DEFAULT_PREFS["sites"])

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

    def test_remote_and_places_are_or(self):
        db.update_role(self.conn, self.ids["a"], {"location": "Brooklyn, NY", "is_remote": 0})
        db.update_role(self.conn, self.ids["b"], {"location": "San Francisco, CA", "is_remote": 0})
        db.update_role(self.conn, self.ids["c"], {"location": "Remote (Austin, TX)", "is_remote": 1})
        self.conn.commit()
        self.assertEqual(self.titles(remote="1"), ["C"])
        self.assertEqual(self.titles(loc="Brooklyn"), ["A"])
        self.assertEqual(self.titles(remote="1", loc="Brooklyn"), ["A", "C"])           # remote OR Brooklyn
        self.assertEqual(self.titles(remote="1", loc="Brooklyn", fit="strong"), ["A"])  # other filters still AND

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
        self.assertEqual(db.common_places(self.conn)[0], {"name": "Brooklyn", "count": 2})   # "Brooklyn. NY" too
        db.update_role(self.conn, self.ids["c"], {"location": "Remote (Austin, TX)"})
        self.conn.commit()
        # every city, incl. the one a remote listing names ("Remote (Austin, TX)" -> Austin)
        names = lambda places: [x["name"] for x in places]
        self.assertEqual(names(db.common_places(self.conn)), ["Austin", "Brooklyn", "San Francisco"])
        self.assertEqual(names(self.client.get("/api/meta").get_json()["places"]), ["Austin", "Brooklyn", "San Francisco"])
        db.update_role(self.conn, self.ids["c"], {"location": "Remote (USA)"})   # no city: not listed
        self.conn.commit()
        self.assertEqual(names(db.common_places(self.conn)), ["Brooklyn", "San Francisco"])


class NoLimitTests(TempDB):
    def test_no_limit_asks_boards_for_everything(self):
        import sys, types
        calls = []
        fake = types.ModuleType("jobspy")
        class DF:
            def to_dict(self, _): return []
            def __len__(self): return 0
        fake.scrape_jobs = lambda **kw: calls.append(kw) or DF()
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
        half = len(calls) // 2                 # each search: LinkedIn, and the other boards
        self.assertEqual({c["results_wanted"] for c in calls[:half]}, {25})
        self.assertEqual({c["results_wanted"] for c in calls[half:]}, {daily_jobs.NO_LIMIT_RESULTS})
        # LinkedIn is searched without opening job pages; the other boards as before
        self.assertEqual({(tuple(c["site_name"]), c["fetch_description"]) for c in calls},
                         {(("linkedin",), False), (("indeed", "zip_recruiter"), True)})
        self.assertTrue(db.validate_prefs({**p, "results_all": "yes"}))


class CompanyNameDuplicateTests(TempDB):
    """The same posting under different company names on different boards."""
    TITLE = "Software Development Engineer, Globex Legal Technologies"

    def test_company_names(self):
        self.assertEqual(db.company_core("Globex.com Services LLC"), "globex services")
        self.assertTrue(db.companies_match("Globex", "Globex.com"))
        self.assertTrue(db.companies_match("Globex", "Globex.com Services LLC"))
        self.assertTrue(db.companies_match("Acme, Inc.", "ACME"))
        self.assertFalse(db.companies_match("Hooli", "Hoolibase"))
        self.assertFalse(db.companies_match("Globex", "Initech"))

    def test_pull_merges_three_boards_into_one_role(self):
        res = run_pull([
            job(title=self.TITLE, company="Globex", site="linkedin", job_url="https://li/1", location="New York, NY"),
            job(title=self.TITLE, company="Globex.com", site="indeed", job_url="https://in/2", location="New York, NY, US",
                job_url_direct="https://jobs.globex.example/123"),
            job(title=self.TITLE, company="Globex.com Services LLC", site="glassdoor", job_url="https://gd/3",
                location="New York, NY")])
        self.assertEqual(res["added"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT job_url_direct FROM roles").fetchone()[0], "https://jobs.globex.example/123")

    def test_different_city_or_title_stays_separate(self):
        run_pull([job(title=self.TITLE, company="Globex", job_url="https://li/1", location="New York, NY"),
                  job(title=self.TITLE, company="Globex.com", job_url="https://in/2", location="Seattle, WA",
                      is_remote=False),
                  job(title="Software Engineer II", company="Globex.com", job_url="https://in/3", location="New York, NY")])
        # Seattle is filtered out by the NYC search; the different title is its own role
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0], 2)

    def test_dupes_and_merge(self):
        a, _ = db.upsert_role(self.conn, job(title=self.TITLE, company="Globex", job_url="https://li/1", site="linkedin"))
        # older rows made before the looser matching existed: insert directly
        for company, url, site in (("Globex.com", "https://in/2", "indeed"), ("Globex.com Services LLC", "https://gd/3", "glassdoor")):
            self.conn.execute(
                "INSERT INTO roles(signature, title, company, location, site, job_url, status, first_seen, updated_at) "
                "VALUES(?,?,?,?,?,?, 'not_applied', ?, ?)",
                (db.signature(self.TITLE, company), self.TITLE, company, "New York, NY, US", site, url, db.now_iso(), db.now_iso()))
        ids = [r[0] for r in self.conn.execute("SELECT id FROM roles ORDER BY id")]
        b, c = ids[1], ids[2]
        db.update_role(self.conn, b, {"status": "applied", "applied_at": "2026-11-22", "notes": "applied via site"})
        db.update_role(self.conn, c, {"status": "applied", "applied_at": "2026-11-21", "resume_path": "r.pdf"})
        db.add_round(self.conn, c, {"kind": "recruiter"})
        db.add_qa(self.conn, b, "Why Globex?", "Scale")
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


class AutoArchiveTests(TempDB):
    def not_a_fit_days_ago(self, days, **kw):
        rid, _ = db.upsert_role(self.conn, job(job_url=f"https://example.com/{days}-{len(kw)}", **kw))
        db.update_role(self.conn, rid, {"status": "not_a_fit"}, note="not for me")
        # backdate its whole history: added the day before, Not a fit `days` ago
        when = lambda d: (dt.datetime.now() - dt.timedelta(days=d)).isoformat(timespec="seconds")
        self.conn.execute("UPDATE status_history SET at=? WHERE role_id=? AND status!='not_a_fit'", (when(days + 1), rid))
        self.conn.execute("UPDATE status_history SET at=? WHERE role_id=? AND status='not_a_fit'", (when(days), rid))
        self.conn.commit()
        return rid

    def archived(self, rid):
        return bool(self.conn.execute("SELECT archived FROM roles WHERE id=?", (rid,)).fetchone()[0])

    def test_default_is_7_days(self):
        self.assertEqual(db.DEFAULT_PREFS["auto_archive"], {"enabled": True, "days": 7})
        old = self.not_a_fit_days_ago(8, title="Old Engineer")
        new = self.not_a_fit_days_ago(3, title="New Engineer")
        self.assertEqual(db.auto_archive_not_a_fit(self.conn), 1)
        self.assertTrue(self.archived(old))
        self.assertFalse(self.archived(new))
        self.assertIn("Archived automatically", db.get_role(self.conn, old)["history"][0]["note"])

    def test_later_notes_dont_reset_the_clock(self):
        rid = self.not_a_fit_days_ago(10)
        db.update_role(self.conn, rid, {}, note="a note added today")
        self.assertEqual(db.auto_archive_not_a_fit(self.conn), 1)

    def test_unarchived_by_you_stays_unarchived(self):
        rid = self.not_a_fit_days_ago(10)
        db.auto_archive_not_a_fit(self.conn)
        db.set_archived(self.conn, rid, False)
        self.assertEqual(db.auto_archive_not_a_fit(self.conn), 0)
        self.assertFalse(self.archived(rid))

    def test_off_and_validation(self):
        rid = self.not_a_fit_days_ago(30)
        off = db._deep_merge(self.prefs, {"auto_archive": {"enabled": False}})
        self.assertEqual(db.auto_archive_not_a_fit(self.conn, off), 0)
        self.assertTrue(db.validate_prefs(db._deep_merge(self.prefs, {"auto_archive": {"days": 0}})))
        self.assertTrue(db.validate_prefs(db._deep_merge(self.prefs, {"auto_archive": {"days": None}})))
        # saving a shorter period archives right away
        import app
        r = app.app.test_client().put("/api/preferences", json=db._deep_merge(self.prefs, {"auto_archive": {"days": 2}}))
        self.assertEqual(r.get_json()["auto_archived"], 1)
        self.assertTrue(self.archived(rid))


class ZipRecruiterDescriptionTests(TempDB):
    """Missing ZipRecruiter descriptions are fetched again, slowly (no network here)."""

    def test_paced_and_gives_up_after_repeated_refusals(self):
        daily_jobs.ZIP_PAUSE, daily_jobs.ZIP_BACKOFF = (4.0, 8.0), 30.0       # real pacing (restored after)
        sleeps, answers = [], iter([{"description": "one"}, {}, {"description": "three"}, {}, {}, {}, {"description": "never"}])
        jobs = [{"job_url": f"https://zip/{i}"} for i in range(7)]
        st = daily_jobs.fill_zip_descriptions(jobs, log=lambda *_: None, fetch=lambda j: next(answers), sleep=sleeps.append)
        self.assertEqual(st, {"tried": 6, "fetched": 2, "stopped_early": True})
        self.assertEqual([j.get("description") for j in jobs[:3]], ["one", None, "three"])
        self.assertIsNone(jobs[6].get("description"))
        self.assertTrue(all(x >= daily_jobs.ZIP_PAUSE[0] for x in sleeps))         # never back to back
        self.assertIn(daily_jobs.ZIP_BACKOFF * 2, sleeps)                           # waits longer after refusals

    def test_pull_fetches_only_listings_it_would_keep(self):
        fetched = []
        saved = daily_jobs._zip_fetcher
        daily_jobs._zip_fetcher = lambda: (lambda j: fetched.append(j["job_url"]) or {"description": "Full text. 2+ years of experience."})
        daily_jobs.ZIP_PAUSE, pause = (0, 0), daily_jobs.ZIP_PAUSE
        try:
            res = run_pull([
                job(site="zip_recruiter", job_url="https://zip/keep", description=None),
                job(site="zip_recruiter", job_url="https://zip/senior", title="Senior Software Engineer", description=None),
                job(site="zip_recruiter", job_url="https://zip/dup", title="Backend Engineer", company="Initech", description=None),
                job(site="linkedin", job_url="https://li/dup", title="Backend Engineer", company="Initech")])
        finally:
            daily_jobs._zip_fetcher, daily_jobs.ZIP_PAUSE = saved, pause
        self.assertEqual(fetched, ["https://zip/keep"])        # not the filtered-out one, not the one LinkedIn covered
        self.assertEqual(res["zip_descriptions"]["fetched"], 1)
        d = self.conn.execute("SELECT description FROM roles WHERE job_url='https://zip/keep'").fetchone()[0]
        self.assertEqual(d, "Full text. 2+ years of experience.")

    def test_a_board_that_refuses_every_search_is_reported_blocked(self):
        import logging
        import sys
        import types
        calls = {"linkedin": 0}

        class DF:
            def __init__(self, rows): self.rows = rows
            def to_dict(self, _): return self.rows
            def __len__(self): return len(self.rows)

        def scrape_jobs(site_name, **kw):
            rows = []
            for site in site_name:
                if site == "zip_recruiter":                         # refused every time, like the 307s
                    logging.getLogger("jobspy.ziprecruiter").error("response status code 307")
                elif site == "linkedin":                            # answers once, then gets blocked
                    calls["linkedin"] += 1
                    if calls["linkedin"] == 1:
                        rows.append({"site": "linkedin", "job_url": "https://li/1", "title": "x"})
                    else:
                        logging.getLogger("jobspy.linkedin").error("response status code 429 (blocked: use a proxy)")
                else:
                    rows.append({"site": site, "job_url": f"https://{site}/{len(rows)}{kw['search_term']}", "title": "x"})
            return DF(rows)
        fake = types.ModuleType("jobspy")
        fake.scrape_jobs = scrape_jobs
        saved, sleep = sys.modules.get("jobspy"), daily_jobs.time.sleep
        sys.modules["jobspy"], daily_jobs.time.sleep = fake, (lambda s: None)
        lines = []
        try:
            stats = {}
            daily_jobs.fetch_jobs(TEST_PREFS, lines.append, stats)
        finally:
            daily_jobs.time.sleep = sleep
            if saved is not None:
                sys.modules["jobspy"] = saved
            else:
                del sys.modules["jobspy"]
        self.assertEqual(stats["blocked"], {"zip_recruiter": "response status code 307"})   # not LinkedIn, not Indeed
        self.assertIn("  ! ZipRecruiter blocked your searches (response status code 307)", lines)

    def test_ziprecruiter_search_follows_redirects(self):
        # ZipRecruiter answers searches with a 307 to the same URL (+ zrbc=); JobSpy's
        # session doesn't follow redirects, so every search came back empty.
        import jobspy.ziprecruiter as zr
        from jobspy.model import ScraperInput
        from jobspy.util import TLSRotating
        saved = zr.create_session
        self.addCleanup(setattr, zr, "create_session", saved)
        self.assertFalse(saved(TLSRotating(None), ScraperInput(site_name=["zip_recruiter"])).allow_redirects)
        daily_jobs._zip_follow_redirects()
        daily_jobs._zip_follow_redirects()                                    # only wrapped once
        self.assertTrue(zr.create_session._follows_redirects)
        self.assertIs(zr.create_session.__closure__[0].cell_contents, saved)
        session = zr.create_session(TLSRotating(None), ScraperInput(site_name=["zip_recruiter"]))
        self.assertTrue(session.allow_redirects)

    def test_linkedin_pages_only_for_listings_worth_keeping(self):
        from jobspy.model import CompensationInterval, JobType
        opened = []
        pages = {
            "https://www.linkedin.com/jobs/view/1000001": "Build APIs. 2+ years of experience required.",
            "https://www.linkedin.com/jobs/view/1000004": "Lead our platform. 8+ years of experience required.",
        }

        def fetch(job):
            opened.append(job["job_url"])
            return {"description": pages.get(job["job_url"], "Text."), "job_type": [JobType.FULL_TIME],
                    "interval": CompensationInterval.YEARLY, "job_level": "mid-senior level"}
        daily_jobs._linkedin_fetcher = lambda: fetch
        li = lambda n, **kw: job(**{"site": "linkedin", "job_url": f"https://www.linkedin.com/jobs/view/{n}",
                                    "description": None, **kw})
        have, _ = db.upsert_role(self.conn, li(1000002, title="Platform Engineer", description="Already here."))
        db.mark_seen(self.conn, ["https://www.linkedin.com/jobs/view/1000003"])
        self.conn.commit()
        res = run_pull([
            li(1000001),                                                   # new and fits: opened
            li(1000002, title="Platform Engineer"),                        # already in the tracker
            li(1000003, title="Data Engineer"),                            # seen in an earlier pull
            li(1000004, title="Backend Engineer"),                         # fits so far; its page says 8+ years
            li(1000005, title="Senior Software Engineer"),                 # ruled out by title words
            li(1000006, title="Software Engineer", location="Denver, CO"),  # ruled out by location
        ])
        self.assertEqual(opened, ["https://www.linkedin.com/jobs/view/1000001",
                                  "https://www.linkedin.com/jobs/view/1000004"])
        self.assertEqual(res["descriptions"]["linkedin"]["fetched"], 2)
        added = {r["job_url"]: r for r in map(dict, self.conn.execute("SELECT job_url, description, job_type FROM roles"))}
        self.assertEqual(added["https://www.linkedin.com/jobs/view/1000001"]["job_type"], "fulltime")   # plain values
        self.assertIn("2+ years", added["https://www.linkedin.com/jobs/view/1000001"]["description"])
        self.assertNotIn("https://www.linkedin.com/jobs/view/1000004", added)                    # 8+ years: filtered out
        self.assertEqual(res["added"], 1)

    def test_backfill_existing_roles(self):
        rid, _ = db.upsert_role(self.conn, job(site="zip_recruiter", job_url="https://zip/old", description=None))
        self.conn.commit()
        saved, pause = daily_jobs._zip_fetcher, daily_jobs.ZIP_PAUSE
        daily_jobs._zip_fetcher = lambda: (lambda j: {"description": "Backfilled"})
        daily_jobs.ZIP_PAUSE = (0, 0)
        try:
            r = daily_jobs.backfill_descriptions(use_ai=False, log=lambda *_: None)
        finally:
            daily_jobs._zip_fetcher, daily_jobs.ZIP_PAUSE = saved, pause
        self.assertEqual((r["missing"], r["described"]), (1, [rid]))
        self.assertEqual(db.get_role(self.conn, rid)["description"], "Backfilled")


class ReviewFixTests(TempDB):
    """Fixes from the pre-merge review."""

    def setUp(self):
        super().setUp()
        import app
        self.client = app.app.test_client()

    def test_remote_listing_without_a_location_is_kept(self):
        prefs = {"locations": dict(TEST_PREFS["locations"])}
        self.assertEqual(daily_jobs.location_ok({"is_remote": True, "location": ""}, prefs), (True, "Remote"))
        self.assertTrue(daily_jobs.location_ok({"is_remote": True, "location": None}, prefs)[0])

    def test_pay_with_only_a_minimum_is_annualised_once(self):
        self.assertEqual(daily_jobs.salary_range({"min_amount": 50, "max_amount": None, "interval": "hourly"}),
                         (104000, 104000))

    def test_infinite_or_nan_numbers_are_rejected(self):
        rid, _ = db.upsert_role(self.conn, job())
        self.conn.commit()
        for field, value in (("pay_min", "inf"), ("pay_max", "1e999"), ("offer_salary", "nan"), ("years_min", "inf")):
            r = self.client.patch(f"/api/roles/{rid}", json={field: value})
            self.assertEqual(r.status_code, 400, (field, value))
        self.assertIsNone(db.get_role(self.conn, rid)["pay_min"])

    def test_failed_pull_keeps_nothing_it_half_wrote(self):
        orig = db.mark_seen

        def boom(*a, **k):
            raise RuntimeError("disk full")
        db.mark_seen = boom
        try:
            with self.assertRaises(RuntimeError):
                run_pull([job()])
        finally:
            db.mark_seen = orig
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM scrape_runs ORDER BY id DESC").fetchone()[0], "error")

    def test_zip_setup_failure_skips_only_that_step(self):
        saved = daily_jobs._zip_fetcher

        def broken():
            raise AttributeError("JobSpy changed")
        daily_jobs._zip_fetcher = broken
        try:
            res = run_pull([job(site="zip_recruiter", job_url="https://zip/keep", description=None)])
        finally:
            daily_jobs._zip_fetcher = saved
        self.assertEqual(res["added"], 1)                       # the pull still finished
        self.assertEqual(res["zip_descriptions"]["tried"], 0)
        self.assertIn("JobSpy changed", res["zip_descriptions"]["error"])

    def test_bad_step_input_is_400_and_saves_nothing(self):
        rid, _ = db.upsert_role(self.conn, job())
        db.update_role(self.conn, rid, {"status": "applied"})
        self.conn.commit()
        for bad in ({"kind": "lunch"}, {"round_number": "two"}, {"round_number": 0},
                    {"status": "maybe"}, {"scheduled_on": "next week"}, {"notes": 5}):
            r = self.client.post(f"/api/roles/{rid}/rounds", json=bad)
            self.assertEqual(r.status_code, 400, bad)
        role = db.get_role(self.conn, rid)
        self.assertEqual((role["rounds"], role["status"]), ([], "applied"))
        # the UI's blank fields still work
        r = self.client.post(f"/api/roles/{rid}/rounds", json={"kind": "", "scheduled_on": "", "status": "scheduled"})
        self.assertEqual(r.status_code, 200)
        step = r.get_json()["rounds"][0]
        self.assertEqual(self.client.patch(f"/api/rounds/{step['id']}", json={"status": ""}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/rounds/{step['id']}", json={"status": "waiting"}).status_code, 200)
        r = self.client.patch(f"/api/rounds/{step['id']}", json={"status": "passed", "create_next": True, "next": "x"})
        self.assertEqual(r.status_code, 400)

    def test_steps_and_questions_of_another_role_are_refused(self):
        a, _ = db.upsert_role(self.conn, job())
        b, _ = db.upsert_role(self.conn, job(title="Backend Engineer", job_url="https://example.com/b"))
        other_step = db.add_round(self.conn, b, {"kind": "recruiter"})
        qa_id = db.add_qa(self.conn, a, "Why us?", "Because")
        self.conn.commit()
        r = self.client.post(f"/api/roles/{a}/transition",
                             json={"status": "interviewing", "round": {"id": other_step, "status": "passed"}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(db.current_status(self.conn, a), "not_applied")       # the move was rolled back too
        self.assertEqual(db.list_rounds(self.conn, b)[0]["status"], "scheduled")
        self.assertEqual(self.client.post(f"/api/roles/{a}/qa", json={"question": "Q", "round_id": other_step}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/qa/{qa_id}", json={"round_id": other_step}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/qa/{qa_id}", json={"question": "  "}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/roles/{a}/qa", json={"items": ["not an object"]}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/roles/{a}/transition", json={"qa": [1]}).status_code, 400)

    def test_resume_check_needs_no_folder_walk_and_stays_inside(self):
        (db.BASE_DIR / "cv.pdf").write_bytes(b"%PDF")
        (db.BASE_DIR / ".git").mkdir()
        (db.BASE_DIR / ".git" / "x.pdf").write_bytes(b"%PDF")
        outside = self.tmp / "elsewhere.pdf"
        outside.write_bytes(b"%PDF")
        self.assertTrue(db.is_resume_file(db.BASE_DIR / "cv.pdf"))
        self.assertTrue(db.is_resume_file(db.BASE_DIR / "sub" / ".." / "cv.pdf"))
        self.assertFalse(db.is_resume_file(db.BASE_DIR / ".git" / "x.pdf"))
        self.assertFalse(db.is_resume_file(db.BASE_DIR / ".." / "elsewhere.pdf"))
        self.assertFalse(db.is_resume_file(db.BASE_DIR / "missing.pdf"))
        self.assertEqual(self.client.get("/api/file", query_string={"path": str(outside)}).status_code, 403)
        self.assertEqual(self.client.get("/api/file").status_code, 403)
        self.assertEqual({db.is_resume_file(p) for p in db.resume_files()}, {True})

    def test_archived_applications_still_count_in_insights(self):
        import insights
        rid, _ = db.upsert_role(self.conn, job())
        db.update_role(self.conn, rid, {"status": "rejected", "applied_at": dt.date.today().isoformat()})
        db.set_archived(self.conn, rid, True)
        out = insights.build(self.conn, 8)
        self.assertEqual(out["progress"]["applications_this_week"], 1)
        self.assertEqual(out["applying"]["total"], 1)
        self.assertEqual({s["stage"]: s["count"] for s in out["funnel"]}["Tracked"], 0)


class FetchMissingDescriptionsTests(TempDB):
    """Pull new roles ▾ → Fetch missing descriptions (no network: fetchers are swapped)."""

    def setUp(self):
        super().setUp()
        self.saved = (daily_jobs._zip_fetcher, daily_jobs._linkedin_fetcher, daily_jobs.ZIP_PAUSE, daily_jobs.LINKEDIN_PAUSE)
        self.fetched = []
        fake = lambda: (lambda j: self.fetched.append(j["job_url"]) or {"description": f"Text for {j['job_url']}",
                                                                          "job_type": ["not a column value"]})
        daily_jobs._zip_fetcher = daily_jobs._linkedin_fetcher = fake
        daily_jobs.ZIP_PAUSE = daily_jobs.LINKEDIN_PAUSE = (0, 0)
        add = lambda **kw: db.upsert_role(self.conn, job(description=None, **kw))[0]
        self.open_li = add(site="linkedin", job_url="https://www.linkedin.com/jobs/view/1111111", title="A Engineer")
        self.open_zip = add(site="zip_recruiter", job_url="https://zip/open", title="B Engineer")
        self.indeed = add(site="indeed", job_url="https://indeed.com/9", title="C Engineer")
        self.nafit = add(site="linkedin", job_url="https://www.linkedin.com/jobs/view/2222222", title="D Engineer")
        db.update_role(self.conn, self.nafit, {"status": "not_a_fit"}, note="AI screen: too senior", source="ai")
        self.archived = add(site="zip_recruiter", job_url="https://zip/archived", title="E Engineer")
        db.set_archived(self.conn, self.archived, True)
        self.has_desc = db.upsert_role(self.conn, job(site="linkedin", job_url="https://www.linkedin.com/jobs/view/3333333",
                                                      title="F Engineer"))[0]
        self.conn.commit()

    def tearDown(self):
        daily_jobs._zip_fetcher, daily_jobs._linkedin_fetcher, daily_jobs.ZIP_PAUSE, daily_jobs.LINKEDIN_PAUSE = self.saved
        super().tearDown()

    def desc(self, rid):
        return db.get_role(self.conn, rid)["description"]

    def test_counts(self):
        self.assertEqual(daily_jobs.missing_descriptions(self.conn, True),
                         {"total": 3, "fetchable": 2, "by_site": {"linkedin": 1, "zip_recruiter": 1, "indeed": 1}})
        self.assertEqual(daily_jobs.missing_descriptions(self.conn, False)["fetchable"], 4)

    def test_open_roles_only_by_default_and_ai_off(self):
        r = daily_jobs.backfill_descriptions(use_ai=False, log=lambda *_: None)
        self.assertEqual(sorted(r["described"]), sorted([self.open_li, self.open_zip]))
        self.assertEqual((r["missing"], r["unsupported"], r["ai"]), (3, 1, None))
        self.assertTrue(self.desc(self.open_li).startswith("Text for"))
        self.assertIsNone(self.desc(self.nafit))
        self.assertIsNone(self.desc(self.archived))
        self.assertIsNone(self.desc(self.indeed))
        self.assertNotIn("https://www.linkedin.com/jobs/view/3333333", self.fetched)   # already had one
        run = self.conn.execute("SELECT kind, status, scraped, added FROM scrape_runs ORDER BY id DESC").fetchone()
        self.assertEqual(tuple(run), ("descriptions", "ok", 3, 2))

    def test_all_roles_with_ai(self):
        import ai_screen
        calls = []
        orig = ai_screen.screen_roles
        ai_screen.screen_roles = lambda conn, ids, **kw: calls.append((sorted(ids), kw["restore_ids"])) or {
            "screened": len(ids), "rejected": 0, "unsure": 0, "restored": 0, "errors": 0,
            "input_tokens": 10, "output_tokens": 5, "cost": 0.001, "skipped": None}
        try:
            r = daily_jobs.backfill_descriptions(use_ai=True, open_only=False, log=lambda *_: None)
        finally:
            ai_screen.screen_roles = orig
        everyone = sorted([self.open_li, self.open_zip, self.nafit, self.archived])
        self.assertEqual(sorted(r["described"]), everyone)
        self.assertEqual(calls, [(everyone, {self.nafit})])     # the AI-rejected one may come back
        self.assertEqual(self.conn.execute("SELECT ai_cost FROM scrape_runs ORDER BY id DESC").fetchone()[0], 0.001)

    def test_later_notes_keep_an_ai_rejection_an_ai_rejection(self):
        self.assertTrue(db.last_change_by_ai(self.conn, self.nafit))
        db.fill_blanks(self.conn, self.nafit, {"description": "now it has one"})
        db.set_archived(self.conn, self.nafit, True)
        self.assertTrue(db.last_change_by_ai(self.conn, self.nafit))
        db.update_role(self.conn, self.nafit, {"status": "not_applied"})
        db.update_role(self.conn, self.nafit, {"status": "not_a_fit"}, note="not for me")   # you, this time
        self.assertFalse(db.last_change_by_ai(self.conn, self.nafit))

    def test_history_says_where_the_description_came_from(self):
        daily_jobs.backfill_descriptions(use_ai=False, log=lambda *_: None)
        self.assertEqual(db.get_role(self.conn, self.open_li)["history"][0]["note"],
                         "Description fetched from the LinkedIn job page")

    def test_linkedin_job_id(self):
        self.assertEqual(daily_jobs.linkedin_job_id("https://www.linkedin.com/jobs/view/4123456789/?trk=x"), "4123456789")
        self.assertEqual(daily_jobs.linkedin_job_id("https://www.linkedin.com/jobs/view/dev-at-acme-4123456789"), "4123456789")
        self.assertIsNone(daily_jobs.linkedin_job_id("https://indeed.com/viewjob?jk=1"))

    def test_api(self):
        import app
        client = app.app.test_client()
        d = client.get("/api/descriptions").get_json()
        self.assertEqual((d["open"]["fetchable"], d["all"]["fetchable"]), (2, 4))
        self.assertEqual(client.post("/api/descriptions", json={"use_ai": "yes"}).status_code, 400)
        seen = {}
        orig = daily_jobs.backfill_descriptions
        daily_jobs.backfill_descriptions = lambda **kw: seen.update(kw) or {"described": []}
        try:
            app._worker("descriptions", {"use_ai": False, "open_only": True})
        finally:
            daily_jobs.backfill_descriptions = orig
        self.assertEqual((seen["use_ai"], seen["open_only"], seen["trigger"]), (False, True, "ui"))
        self.assertEqual(app.TASK["result"], {"described": []})


class CompanyTests(TempDB):
    """Company filter in the sidebar, and other roles at the same company in the role panel."""

    def setUp(self):
        super().setUp()
        add = lambda title, company, url, **kw: db.upsert_role(self.conn, job(title=title, company=company, job_url=url, **kw))[0]
        self.a = add("Backend Engineer", "Globex", "https://example.com/g1")
        self.b = add("Data Engineer", "Globex.com Services LLC", "https://example.com/g2", location="Austin, TX")
        self.c = add("ML Engineer", "Globex", "https://example.com/g3")
        self.other = add("Backend Engineer", "Initech", "https://example.com/i1")
        self.lookalike = add("Platform Engineer", "Globexian Labs", "https://example.com/x1")
        db.set_archived(self.conn, self.c, True)
        db.update_role(self.conn, self.b, {"status": "rejected"})
        self.conn.commit()

    def test_other_roles_at_the_same_company(self):
        same = db.get_role(self.conn, self.a)["same_company"]
        self.assertEqual([r["id"] for r in same], [self.b, self.c])    # open first; archived ones included
        self.assertEqual(same[1]["archived"], 1)
        self.assertEqual(db.get_role(self.conn, self.other)["same_company"], [])

    def test_filter(self):
        ids = lambda **kw: sorted(r["id"] for r in db.list_roles(self.conn, **kw)[0])
        self.assertEqual(ids(companies=["globex"]), sorted([self.a, self.b]))          # any name, any case
        self.assertEqual(ids(companies=["Globex"], include_archived=True), sorted([self.a, self.b, self.c]))
        self.assertEqual(ids(companies=["Globex", "Initech"]), sorted([self.a, self.b, self.other]))
        self.assertEqual(ids(companies=["Globexian Labs"]), [self.lookalike])          # not a prefix of a word

    def test_common_companies(self):
        self.assertEqual(db.common_companies(self.conn)[:2],
                         [{"name": "Globex", "count": 2}, {"name": "Globexian Labs", "count": 1}])

    def test_api(self):
        import app
        client = app.app.test_client()
        r = client.get("/api/roles", query_string={"co": "Globex|Initech"}).get_json()
        self.assertEqual(r["matching"], 3)
        self.assertIn({"name": "Globex", "count": 2}, client.get("/api/meta").get_json()["companies"])
        self.assertEqual(len(client.get(f"/api/roles/{self.a}").get_json()["same_company"]), 2)


JOB_PAGE = """<html><head><title>Backend Engineer | Globex</title>
<meta property="og:site_name" content="Globex"><meta property="og:title" content="Backend Engineer | Globex">
<script type="application/ld+json">{"@context": "https://schema.org/", "@type": "JobPosting",
  "title": "Backend Engineer", "datePosted": "2026-11-20", "employmentType": "FULL_TIME",
  "hiringOrganization": {"@type": "Organization", "name": "Globex"},
  "jobLocation": [{"@type": "Place", "address": {"addressCountry": "USA"}},
                  {"@type": "Place", "address": {"addressLocality": "Austin", "addressRegion": "Texas", "addressCountry": "USA"}}],
  "baseSalary": {"@type": "MonetaryAmount", "currency": "USD", "value": {"minValue": 70, "maxValue": 90, "unitText": "HOUR"}},
  "description": "&lt;p&gt;&lt;strong&gt;About us&lt;/strong&gt;&lt;/p&gt;&lt;ul&gt;&lt;li&gt;4+ years of experience building APIs&lt;/li&gt;&lt;/ul&gt;"}
</script></head><body><main><h1>Backend Engineer</h1><p>Some page text.</p></main></body></html>"""

PLAIN_PAGE = """<html><head><title>Data Engineer - Careers</title></head><body>
<nav>Home Jobs About</nav><main><h1>Data Engineer</h1>
<p>We're hiring a data engineer to build our pipelines. You have 3-5 years of experience writing Python.</p>
<p>Pay: $150,000 - $180,000 a year. """ + "Plenty more about the role and the team. " * 50 + """</p>
<p>We offer great benefits.</p><p>Apply now. First name, last name.</p></main>
<footer>Cookie settings</footer></body></html>"""

SPA_SHELL = "<html><head><title>Loading</title></head><body><div id='root'></div><script>app()</script></body></html>"


class JobImportTests(TempDB):
    """Add role → From a link (no network: pages, the browser and the AI are faked)."""

    def run_import(self, pages, use_ai=False, ai=None, render=None, url="https://jobs.example.com/1"):
        import job_import
        fetched = []

        def fetch(u):
            fetched.append(u)
            page = pages.get(u, pages.get("*"))
            if isinstance(page, Exception):
                raise page
            return u, page
        return job_import.import_from_url(url, use_ai=use_ai, fetch=fetch, ai=ai,
                                          render=render or (lambda u: None)), fetched

    def test_job_posting_data(self):
        r, _ = self.run_import({"*": JOB_PAGE})
        f = r["fields"]
        self.assertEqual((f["title"], f["company"], f["location"]), ("Backend Engineer", "Globex", "Austin, TX (+1 more)"))
        self.assertEqual((f["pay_min"], f["pay_max"]), (70 * 2080, 90 * 2080))     # hourly -> yearly
        self.assertEqual((f["job_type"], f["date_posted"], f["years_min"]), ("fulltime", "2026-11-20", 4))
        self.assertIn("**About us**", f["description"])                           # escaped HTML -> Markdown
        self.assertEqual((f["site"], r["sources"]["title"], r["board"]), ("manual", "structured", None))

    def test_plain_page_text(self):
        r, _ = self.run_import({"*": PLAIN_PAGE})
        f = r["fields"]
        self.assertEqual(f["title"], "Data Engineer")                              # the page heading
        self.assertEqual((f["pay_min"], f["pay_max"], f["years_min"], f["years_max"]), (150000, 180000, 3, 5))
        self.assertNotIn("Cookie settings", f["description"])
        self.assertNotIn("company", f)                                             # never guessed from the tab title

    def test_javascript_page_is_rendered_in_a_browser(self):
        rendered = []
        r, _ = self.run_import({"*": SPA_SHELL}, render=lambda u: rendered.append(u) or JOB_PAGE)
        self.assertEqual(rendered, ["https://jobs.example.com/1"])
        self.assertEqual(r["fields"]["company"], "Globex")
        self.assertTrue(any("headless browser" in m for m in r["messages"]))

    def test_refused_page_tries_the_browser_but_not_for_404(self):
        import job_import
        r, _ = self.run_import({"*": job_import.ImportProblem("refused", retry=True)}, render=lambda u: JOB_PAGE)
        self.assertEqual(r["fields"]["title"], "Backend Engineer")
        calls = []
        with self.assertRaises(job_import.ImportProblem) as cm:
            self.run_import({"*": job_import.ImportProblem("The page wasn't found (HTTP 404).")},
                            render=lambda u: calls.append(u) or JOB_PAGE)
        self.assertIn("404", str(cm.exception))
        self.assertEqual(calls, [])

    def test_sign_in_page_is_reported(self):
        import job_import
        page = "<html><head><title>Sign in</title></head><body><main><h1>Sign in to continue</h1>" \
               "<form><input type='password'></form></main></body></html>"
        with self.assertRaises(job_import.ImportProblem) as cm:
            self.run_import({"*": page})
        self.assertIn("sign in", str(cm.exception).lower())

    def test_ai_fills_gaps_and_keeps_structured_values(self):
        import job_import
        seen = {}

        def fake_ai(url, title, text, found, have_description=False):
            seen.update(found=found, have_description=have_description)
            return ({"title": "Wrong Title", "company": "Globex Inc", "is_remote": True, "pay_min": 1},
                    {"model": "claude-haiku-5-5", "input_tokens": 100, "output_tokens": 50, "cost": 0.0001})
        orig = job_import._ai_available
        job_import._ai_available = lambda: (True, None)
        try:
            r, _ = self.run_import({"*": JOB_PAGE}, use_ai=True, ai=fake_ai)
        finally:
            job_import._ai_available = orig
        f = r["fields"]
        self.assertEqual((f["title"], f["company"], f["pay_min"]), ("Backend Engineer", "Globex", 70 * 2080))  # structured kept
        self.assertEqual((f["is_remote"], r["sources"]["is_remote"]), (True, "ai"))                          # gap filled
        self.assertTrue(seen["have_description"])
        run = self.conn.execute("SELECT kind, status, ai_cost FROM scrape_runs").fetchone()
        self.assertEqual(tuple(run), ("import", "ok", 0.0001))                                               # spend recorded

    def test_ai_replaces_weak_page_values(self):
        import job_import
        orig = job_import._ai_available
        job_import._ai_available = lambda: (True, None)
        try:
            r, _ = self.run_import({"*": PLAIN_PAGE}, use_ai=True,
                                   ai=lambda *a, **k: ({"title": "Senior Data Engineer", "company": "Initech"},
                                                       {"model": "m", "input_tokens": 1, "output_tokens": 1, "cost": 0}))
        finally:
            job_import._ai_available = orig
        self.assertEqual((r["fields"]["title"], r["fields"]["company"]), ("Senior Data Engineer", "Initech"))

    def with_ai(self, page, reply):
        import job_import
        orig = job_import._ai_available
        job_import._ai_available = lambda: (True, None)
        try:
            return self.run_import({"*": page}, use_ai=True,
                                   ai=lambda *a, **k: (dict(reply), {"model": "m", "input_tokens": 1, "output_tokens": 1, "cost": 0}))[0]
        finally:
            job_import._ai_available = orig

    def test_ai_never_writes_the_description(self):
        import job_import
        full = self.run_import({"*": PLAIN_PAGE})[0]["fields"]["description"]
        # The AI marks the span; the text comes from the page, character for character.
        r = self.with_ai(PLAIN_PAGE, {"description": "REWRITTEN BY AI",
                                      "_description_bounds": ("We're hiring a data engineer", "We offer great benefits.")})
        d = r["fields"]["description"]
        self.assertNotIn("REWRITTEN", d)
        self.assertTrue(d.startswith("We're hiring a data engineer"))
        self.assertTrue(d.endswith("We offer great benefits."))
        self.assertNotIn("Apply now", d)                        # the page's leftovers are cut off
        self.assertIn(d, full)                                  # an exact piece of the page's own text
        self.assertEqual(r["sources"]["description"], "page")
        # Phrases that aren't on the page: the whole page text is kept.
        r = self.with_ai(PLAIN_PAGE, {"_description_bounds": ("words that aren't there", "We offer great benefits.")})
        self.assertEqual(r["fields"]["description"], full)
        # A description from the page's job data is never trimmed or replaced.
        r = self.with_ai(JOB_PAGE, {"description": "REWRITTEN", "_description_bounds": ("About us", "APIs")})
        self.assertEqual(r["fields"]["description"],
                         self.run_import({"*": JOB_PAGE})[0]["fields"]["description"])
        self.assertEqual(job_import.AI_SCHEMA["properties"].get("description"), None)

    def test_ai_unavailable_is_explained(self):
        import job_import
        orig = job_import._ai_available
        job_import._ai_available = lambda: (False, "No ANTHROPIC_API_KEY")
        try:
            r, _ = self.run_import({"*": JOB_PAGE}, use_ai=True, ai=lambda *a, **k: self.fail("AI called"))
        finally:
            job_import._ai_available = orig
        self.assertIn("No ANTHROPIC_API_KEY", r["messages"])

    def test_only_public_web_links(self):
        import job_import
        for bad in ("http://127.0.0.1:5055/api/meta", "http://localhost/", "http://192.168.1.10/job", "http://[::1]/",
                    "file:///C:/Windows/win.ini", "ftp://example.com/x", "https://user:pw@example.com/job", ""):
            with self.assertRaises(job_import.ImportProblem, msg=bad):
                job_import.fetch_page(job_import.normalize_url(bad))
        self.assertEqual(job_import.normalize_url(" jobs.example.com/1 "), "https://jobs.example.com/1")

    def test_linkedin_page(self):
        page = """<html><body><h1 class="top-card-layout__title">Platform Engineer</h1>
          <a class="topcard__org-name-link">Initech</a><span class="topcard__flavor--bullet">Remote</span>
          <div class="show-more-less-html__markup"><p>Build things. 2+ years of experience.</p></div>
          <li class="description__job-criteria-item"><h3>Employment type</h3><span>Full-time</span></li>
          <div class="compensation__salary">$120,000.00/yr - $140,000.00/yr</div></body></html>"""
        r, fetched = self.run_import({"*": page}, url="https://www.linkedin.com/jobs/search/?currentJobId=4123456789&k=x")
        f = r["fields"]
        self.assertEqual(fetched, ["https://www.linkedin.com/jobs/view/4123456789"])
        self.assertEqual((f["title"], f["company"], f["site"], f["is_remote"], f["job_type"]),
                         ("Platform Engineer", "Initech", "linkedin", True, "fulltime"))
        self.assertEqual((f["pay_min"], f["pay_max"], f["years_min"]), (120000, 140000, 2))

    def test_rule_check_on_imported_fields(self):
        ok = {"title": "Software Engineer", "company": "Acme", "location": "New York, NY (+2 more)",
              "description": "2+ years of experience building APIs."}
        check = lambda **kw: daily_jobs.rule_check({**ok, **kw}, self.prefs)
        self.assertIsNone(check())                                           # "(+2 more)" doesn't confuse it
        self.assertIsNone(check(date_posted="2020-01-01"))                   # age isn't held against it
        self.assertEqual(check(location="San Francisco, CA"), "the location isn't one of your places")
        self.assertIsNone(check(location="", is_remote=True))                # remote roles are allowed
        self.assertEqual(check(years_min=6, description=None), "it asks for 6+ years of experience")
        self.assertEqual(check(pay_min=90000, pay_max=100000), "the top of the pay range is below your minimum")
        self.assertEqual(check(title="Senior Software Engineer"), "the title has an excluded word or a senior level")
        db.block_company(self.conn, "Acme")
        self.assertEqual(daily_jobs.rule_check(ok, db.get_prefs(self.conn)), "the company is on your blocked list")

    def test_import_api_suggests_not_a_fit_and_history_says_why(self):
        import app
        import job_import
        client = app.app.test_client()
        orig = job_import.import_from_url
        job_import.import_from_url = lambda url, use_ai=False: {
            "fields": {"title": "Software Engineer", "company": "Globex", "location": "San Francisco, CA"},
            "sources": {}, "messages": [], "board": None, "ai": None}
        try:
            r = client.post("/api/import", json={"url": "https://x.example/1"}).get_json()
        finally:
            job_import.import_from_url = orig
        self.assertEqual(r["not_a_fit"], "the location isn't one of your places")
        role = client.post("/api/roles", json={"title": "Software Engineer", "company": "Globex", "status": "not_a_fit",
                                               "status_note": "Not a fit by your preferences: " + r["not_a_fit"]}).get_json()
        self.assertEqual((role["status"], role["history"][0]["note"]),
                         ("not_a_fit", "Added manually. Not a fit by your preferences: the location isn't one of your places"))
        self.assertFalse(db.last_change_by_ai(self.conn, role["id"]))         # your call, not the AI's

    def test_board_detection(self):
        import job_import
        self.assertEqual(job_import.board_of("https://www.indeed.com/viewjob?jk=7d53cae1fc6f3c7a"), "indeed")
        self.assertEqual(job_import.board_of("https://www.glassdoor.co.uk/job-listing/x"), "glassdoor")
        self.assertIsNone(job_import.board_of("https://notindeed.com/x"))
        self.assertEqual(job_import.indeed_job_key("https://www.indeed.com/viewjob?jk=7d53cae1fc6f3c7a&from=x"), "7d53cae1fc6f3c7a")
        self.assertEqual(daily_jobs.linkedin_job_id("https://www.linkedin.com/jobs/collections/x/?currentJobId=4123456789"),
                         "4123456789")

    def test_api(self):
        import app
        import job_import
        client = app.app.test_client()
        orig = job_import.import_from_url
        job_import.import_from_url = lambda url, use_ai=False: (_ for _ in ()).throw(job_import.ImportProblem("refused"))
        try:
            r = client.post("/api/import", json={"url": "https://x.example/1"})
            self.assertEqual((r.status_code, r.get_json()["error"]), (422, "refused"))
        finally:
            job_import.import_from_url = orig
        self.assertEqual(client.post("/api/import", json={}).status_code, 400)
        # the filled-in form saves job type, date and site too
        r = client.post("/api/roles", json={"title": "Backend Engineer", "company": "Globex", "site": "linkedin",
                                           "job_type": "fulltime", "date_posted": "2026-11-20", "status": "not_applied"})
        role = r.get_json()
        self.assertEqual((r.status_code, role["site"], role["job_type"], role["date_posted"]),
                         (201, "linkedin", "fulltime", "2026-11-20"))


class VersionTests(TempDB):
    """Versions: comparing them, the database only moving forward, launch.py and release.py."""

    def test_version_numbers(self):
        import versions
        self.assertEqual(versions.parse("v1.6.0"), (1, 6, 0))
        self.assertIsNone(versions.parse("1.6"))
        self.assertTrue(versions.newer("1.10.0", "1.9.9"))                   # numbers, not text
        self.assertFalse(versions.newer("1.8.0", "1.8.0"))
        self.assertEqual(versions.newest_first(["v1.2.0", "v1.10.0", "v1.4.1", "junk", "v1.2.0"]),
                         ["1.10.0", "1.4.1", "1.2.0"])

    def reopen(self):
        self.conn.close()
        db._initialized.clear()
        self.conn = db.connect()

    def set_last_version(self, v):
        self.conn.execute("UPDATE settings SET value=? WHERE key='app_version'", (v,))
        self.conn.commit()

    def test_new_database_records_its_version_without_a_backup(self):
        self.assertEqual(db._last_app_version(self.conn), settings.VERSION)
        self.assertFalse(list(settings.BACKUP_DIR.glob("*before-*")) if settings.BACKUP_DIR.exists() else [])

    def test_upgrade_saves_a_backup_first(self):
        db.upsert_role(self.conn, job())
        self.set_last_version("1.7.0")
        self.reopen()
        self.assertEqual(db._last_app_version(self.conn), settings.VERSION)
        backups = list(settings.BACKUP_DIR.glob(f"*-before-{settings.VERSION}.db"))
        self.assertEqual(len(backups), 1)
        import sqlite3
        old = sqlite3.connect(backups[0])
        self.assertEqual(old.execute("SELECT value FROM settings WHERE key='app_version'").fetchone()[0], "1.7.0")
        old.close()
        self.reopen()                                                          # same version again: no new backup
        self.assertEqual(len(list(settings.BACKUP_DIR.glob("*-before-*.db"))), 1)

    def test_older_app_on_the_same_schema_is_fine(self):
        self.set_last_version("9.0.0")                                         # a newer app used it, same schema
        self.reopen()
        self.assertEqual(db._last_app_version(self.conn), "9.0.0")             # still the newest one recorded
        self.assertFalse(list(settings.BACKUP_DIR.glob("*-before-*.db")) if settings.BACKUP_DIR.exists() else [])

    def test_a_newer_schema_is_refused_and_left_alone(self):
        rid, _ = db.upsert_role(self.conn, job())
        self.conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
        self.conn.commit()
        self.conn.close()
        db._initialized.clear()
        with self.assertRaises(db.DatabaseTooNew) as cm:
            db.connect()
        self.assertIn(f"schema version {db.SCHEMA_VERSION + 1}", str(cm.exception))
        db._initialized.clear()
        raw = db._open(db.DB_PATH)                                             # untouched
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION + 1)
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM roles").fetchone()[0], 1)
        self.conn = raw                                                        # for tearDown

    def with_migrations(self, migrations):
        """Run the test with these extra numbered migrations (and SCHEMA_VERSION raised to match)."""
        saved = (dict(db.MIGRATIONS), db.SCHEMA_VERSION)
        db.MIGRATIONS.update(migrations)
        db.SCHEMA_VERSION = max(db.MIGRATIONS)
        self.addCleanup(lambda: (db.MIGRATIONS.clear(), db.MIGRATIONS.update(saved[0]),
                                 setattr(db, "SCHEMA_VERSION", saved[1])))

    def test_migrations_run_once_in_order_after_a_backup(self):
        rid, _ = db.upsert_role(self.conn, job())
        self.conn.commit()
        ran = []
        base = db.SCHEMA_VERSION

        def add_tags(conn):
            ran.append("tags")
            conn.execute("ALTER TABLE roles ADD COLUMN tags TEXT")
            conn.execute("UPDATE roles SET tags='[]'")

        def add_index(conn):
            ran.append("index")
            conn.execute("CREATE INDEX idx_roles_tags ON roles(tags)")
        self.with_migrations({base + 1: ("add tags to roles", add_tags), base + 2: ("index tags", add_index)})
        self.reopen()
        self.assertEqual(ran, ["tags", "index"])
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], base + 2)
        self.assertEqual(self.conn.execute("SELECT tags FROM roles WHERE id=?", (rid,)).fetchone()[0], "[]")
        self.assertEqual([h["name"] for h in db.schema_history(self.conn)], ["add tags to roles", "index tags"])
        self.assertEqual(len(list(settings.BACKUP_DIR.glob("*-before-*.db"))), 1)   # one backup before upgrading
        self.reopen()
        self.assertEqual(ran, ["tags", "index"])                               # never run twice

    def test_a_new_database_takes_the_same_path(self):
        base = db.SCHEMA_VERSION
        self.with_migrations({base + 1: ("add tags to roles", lambda c: c.execute("ALTER TABLE roles ADD COLUMN tags TEXT"))})
        self.conn.close()
        Path(db.DB_PATH).unlink()
        db._initialized.clear()
        self.conn = db.connect()                                               # a brand-new database
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(roles)")}
        self.assertIn("tags", cols)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], base + 1)
        self.assertFalse(list(settings.BACKUP_DIR.glob("*-before-*.db")) if settings.BACKUP_DIR.exists() else [])

    def test_a_failed_migration_changes_nothing(self):
        rid, _ = db.upsert_role(self.conn, job())
        self.conn.commit()
        base = db.SCHEMA_VERSION

        def half_done(conn):
            conn.execute("ALTER TABLE roles ADD COLUMN broken TEXT")
            conn.execute("UPDATE roles SET title='changed'")
            raise RuntimeError("oops")
        self.with_migrations({base + 1: ("good", lambda c: c.execute("ALTER TABLE roles ADD COLUMN good TEXT")),
                              base + 2: ("bad", half_done)})
        self.conn.close()
        db._initialized.clear()
        with self.assertRaises(db.MigrationFailed) as cm:
            db.connect()
        self.assertIn(f"unchanged at schema version {base + 1}", str(cm.exception))
        db._initialized.clear()
        raw = db._open(db.DB_PATH)
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], base + 1)   # the good one stays
        cols = {r["name"] for r in raw.execute("PRAGMA table_info(roles)")}
        self.assertIn("good", cols)
        self.assertNotIn("broken", cols)
        self.assertEqual(raw.execute("SELECT title FROM roles WHERE id=?", (rid,)).fetchone()[0], "Software Engineer")
        self.conn = raw

    def test_status_rebuild_keeps_what_migrations_added(self):
        rid, _ = db.upsert_role(self.conn, job())
        self.conn.commit()
        base = db.SCHEMA_VERSION

        def add_tags(conn):
            conn.execute("ALTER TABLE roles ADD COLUMN tags TEXT")
            conn.execute("CREATE INDEX idx_roles_tags ON roles(tags)")
            conn.execute("UPDATE roles SET tags='x'")
        self.with_migrations({base + 1: ("tags", add_tags)})
        self.reopen()
        saved = dict(db.STATUSES)
        db.STATUSES["ghosted"] = "Ghosted"                                      # the status list changes in code
        try:
            self.reopen()
            self.assertEqual(self.conn.execute("SELECT tags FROM roles WHERE id=?", (rid,)).fetchone()[0], "x")
            idx = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='roles'")}
            self.assertTrue({"idx_roles_tags", "idx_roles_status"} <= idx)
            db.update_role(self.conn, rid, {"status": "ghosted"})              # the new CHECK allows it
        finally:
            db.STATUSES.clear()
            db.STATUSES.update(saved)

    def test_db_status_command(self):
        import io
        import contextlib
        import types
        import jobs
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = jobs.db_status(types.SimpleNamespace(check=True, backup=False))
        self.assertEqual(code, 0)
        self.assertIn(f"version {db.SCHEMA_VERSION} (up to date)", out.getvalue())
        self.assertIn("Check:         ok", out.getvalue())

    def test_launcher_picks_the_version(self):
        import launch
        saved = (launch.releases, launch.checked_out_release, launch.unpack)
        launch.releases = lambda: ["1.7.0", "1.6.0", "1.0.0"]
        launch.checked_out_release = lambda: "1.7.0"
        launch.unpack = lambda v: Path("versions") / v
        try:
            self.assertEqual(launch.resolve(None), ("1.7.0", launch.HERE))    # latest = checked out here
            self.assertEqual(launch.resolve("latest"), ("1.7.0", launch.HERE))
            self.assertEqual(launch.resolve("v1.6.0"), ("1.6.0", Path("versions") / "1.6.0"))
            self.assertEqual(launch.resolve("local"), ("local", launch.HERE))
            for bad in ("1.5.0", "banana"):
                with self.assertRaises(launch.LaunchError):
                    launch.resolve(bad)
            launch.checked_out_release = lambda: None                          # local changes: latest is unpacked
            self.assertEqual(launch.resolve(None), ("1.7.0", Path("versions") / "1.7.0"))
            launch.releases = lambda: []
            self.assertEqual(launch.resolve(None), ("local", launch.HERE))      # no releases yet
        finally:
            launch.releases, launch.checked_out_release, launch.unpack = saved

    def test_launcher_guards_the_database_and_passes_settings(self):
        import launch
        saved = (launch.settings.DB_PATH, launch.release_schema)
        launch.settings.DB_PATH = db.DB_PATH
        launch.release_schema = lambda v: {"1.0.0": 2, "9.0.0": 5}.get(v)
        try:
            self.assertEqual(launch.database_info()["schema"], db.SCHEMA_VERSION)
            launch.check_database("1.0.0")                                     # same schema: an older release is fine
            launch.check_database("local")
            self.conn.execute("PRAGMA user_version=3")                         # the database moved on
            self.conn.commit()
            with self.assertRaises(launch.LaunchError) as cm:
                launch.check_database("1.0.0")                                 # release 1.0.0 doesn't know schema 3
            self.assertIn("only knows up to 2", str(cm.exception))
            launch.check_database("9.0.0")                                     # a release that knows it: fine
            self.conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION}")
            self.conn.commit()
            env = launch.child_env("1.6.0")
            self.assertEqual((env["JOBS_DB"], env["JOBS_LAUNCHED_AS"]), (str(Path(db.DB_PATH).resolve()), "1.6.0"))
            self.assertTrue(Path(env["JOBS_RESUME_DIR"]).is_absolute())
        finally:
            launch.settings.DB_PATH, launch.release_schema = saved

    def reload_run(self, steps, busy=lambda: False):
        """Run launch.run_with_reload against a temp folder with a fake process. steps: one
        function per tick (it may edit files or change the process); then Ctrl-C."""
        import os
        import launch
        folder = self.tmp / "code"
        folder.mkdir(exist_ok=True)
        (folder / "app.py").write_text("x = 1")
        started = []

        class Proc:
            def __init__(self):
                self.code = None
                self.stopped = False

            def poll(self):
                return self.code

            def terminate(self):
                self.stopped, self.code = True, -1

            def wait(self, timeout=None):
                return self.code

            kill = terminate

        def start():
            started.append(Proc())
            return started[-1]
        ticks = iter(steps)
        tick = [0]

        def sleep(seconds):
            if seconds < 1:          # the short waits inside a restart
                return
            tick[0] += 1
            step = next(ticks, None)
            if step is None:
                raise KeyboardInterrupt
            step(folder, started)
        saved_say = launch.say
        launch.say = lambda msg: None
        self.addCleanup(setattr, launch, "say", saved_say)
        code = launch.run_with_reload(["x"], folder, {}, sleep=sleep, busy=busy, start=start)
        return code, started

    @staticmethod
    def edit(folder, started, _n=[0]):
        import os
        _n[0] += 10
        os.utime(folder / "app.py", (1_000_000 + _n[0], 1_000_000 + _n[0]))

    def test_reload_restarts_on_a_code_change(self):
        nothing = lambda f, s: None
        code, started = self.reload_run([nothing, self.edit, nothing])
        self.assertEqual((code, len(started)), (130, 2))
        self.assertTrue(started[0].stopped)                                    # the old server was stopped

    def test_reload_waits_for_a_running_pull(self):
        busy = [True]
        code, started = self.reload_run([self.edit, lambda f, s: None, lambda f, s: busy.clear()],
                                        busy=lambda: bool(busy))
        self.assertEqual(len(started), 2)                                      # restarted once the pull ended

    def test_reload_after_a_crash_waits_for_the_fix(self):
        def crash(folder, started):
            started[-1].code = 1
        code, started = self.reload_run([crash, lambda f, s: None, lambda f, s: None, self.edit])
        self.assertEqual(len(started), 2)                                      # not restarted until the save

    def test_only_local_code_is_watched(self):
        import launch
        calls = []
        saved = (launch.resolve, launch.check_database, launch.run_with_reload, launch.subprocess.call, launch.say)
        launch.resolve = lambda v: (v or "1.8.0", launch.HERE)
        launch.check_database = lambda v: None
        launch.run_with_reload = lambda *a, **k: calls.append("reload") or 0
        launch.subprocess.call = lambda *a, **k: calls.append("plain") or 0
        launch.say = lambda msg: None
        try:
            launch.main(["--version", "local"])
            launch.main(["--version", "local", "--no-reload"])
            launch.main(["--version", "1.8.0"])
            launch.main(["--version", "local", "jobs.py", "list"])
        finally:
            launch.resolve, launch.check_database, launch.run_with_reload, launch.subprocess.call, launch.say = saved
        self.assertEqual(calls, ["reload", "plain", "plain", "plain"])

    def test_each_release_says_which_schema_it_uses(self):
        import launch
        try:
            rel = launch.releases()
        except launch.LaunchError:
            self.skipTest("git isn't available")
        if "1.0.0" not in rel:
            self.skipTest("this clone has no release tags")
        self.assertEqual(launch.release_schema("1.0.0"), 2)                     # the 1.x baseline

    def test_release_moves_the_unreleased_notes(self):
        import release
        text = "# Changelog\n\n## [Unreleased]\n### Added\n- A thing\n\n## [1.8.0] - 2026-11-25\n- Old\n"
        out = release.release_changelog(text, "1.9.0", "2026-12-01")
        self.assertIn("## [Unreleased]\n\n## [1.9.0] - 2026-12-01\n### Added\n- A thing\n\n## [1.8.0]", out)
        with self.assertRaises(ValueError):
            release.release_changelog("# Changelog\n\n## [Unreleased]\n\n## [1.8.0] - x\n", "1.9.0", "d")
        changelog = (Path(release.__file__).parent / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{settings.VERSION}] - ", changelog)                # the code's version is documented


if __name__ == "__main__":
    unittest.main()
