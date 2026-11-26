"""SQLite storage for the job tracker (jobs.db next to this file)."""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

import settings

HERE = Path(__file__).resolve().parent
DB_PATH = settings.DB_PATH
BASE_DIR = settings.RESUME_DIR  # resumes live here (job_search/resumes unless JOBS_RESUME_DIR is set)
log = logging.getLogger("job-tracker.db")

# A scraped posting with the same title + company as a role first seen within this
# many days is treated as the same posting (e.g. cross-posted on another board).
# Older matches are a repost or a new opening and get their own row.
DUPLICATE_WINDOW_DAYS = 30

STATUSES = {
    "not_applied": "Not applied",
    "interested": "Interested",
    "applied": "Applied",
    "interviewing": "Interviewing",
    "offer": "Offer",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn",
    "closed": "No longer accepting applications",
    "not_a_fit": "Not a fit",
}
# AI match levels: an ordered scale the model picks from (no fake numeric scores).
MATCH_LEVELS = {
    "strong":    {"label": "Strong",    "desc": "Meets every must-have, matches most nice-to-haves, and the work lines up "
                                                "with your interests and description. Apply soon."},
    "good":      {"label": "Good",      "desc": "Meets every must-have and several nice-to-haves; a solid fit with a gap "
                                                "or two."},
    "partial":   {"label": "Partial",   "desc": "Doesn't break any must-have, but has notable gaps: few nice-to-haves, "
                                                "loose link to your interests, or key details unstated."},
    "weak":      {"label": "Weak",      "desc": "Technically allowed, but a poor match for your nice-to-haves and "
                                                "interests. Low priority."},
    "not_a_fit": {"label": "Not a fit", "desc": "Clearly breaks at least one must-have. Auto-marked Not a fit."},
    "unknown":   {"label": "Unknown",   "desc": "Not enough information to judge (usually no description was scraped)."},
}
MATCH_ORDER = list(MATCH_LEVELS)

# Statuses that mean you haven't applied (moving here clears the applied date).
PRE_APPLY_STATUSES = {"not_applied", "interested"}

# Order used when sorting by status: open work first, closed/dismissed last.
STATUS_SORT = ["not_applied", "interested", "interviewing", "offer", "applied",
               "rejected", "withdrawn", "closed", "not_a_fit"]

# ----------------------------------------------------------------------------
# Default search preferences: a neutral starting point for a new install. Your own
# preferences are set on the Preferences page and stored in the database (in full),
# so changing these defaults never changes preferences you've saved.
# ----------------------------------------------------------------------------
DEFAULT_PREFS = {
    "search_terms": ["software engineer"],
    "sites": ["linkedin", "indeed", "zip_recruiter"],
    "results_per_search": 40,
    "results_all": False,        # True: no limit per search (see daily_jobs.NO_LIMIT_RESULTS)
    "posted_within_hours": 24,
    # No cities yet: remote roles only until you add a location.
    "locations": {"value": [], "radius_miles": 25,
                  "include_remote": True, "mandatory": True},
    "workplace": {"value": ["onsite", "hybrid", "remote"], "mandatory": False},
    "experience": {"min": 0, "max": 30, "mandatory": False},
    "salary": {"min_top": None, "min_floor": None, "include_unlisted": True,
               "mandatory": False},
    "employment_types": {"value": ["fulltime"], "mandatory": True},
    "title_include": {"value": [], "mandatory": False},
    "title_exclude": {"value": [], "mandatory": False},
    "companies_exclude": {"value": [], "mandatory": True},   # "Blocked companies"; always applied
    "industries": {"value": [], "mandatory": False},
    "company_size": {"value": [], "mandatory": False},
    "visa_sponsorship": {"value": False, "mandatory": False},
    "interests": [],
    "prompt": "",
    "ai": {"enabled": True, "model": "claude-haiku-5-5", "unsure_action": "keep"},
    # Roles that have been "Not a fit" for this many days are archived (hidden) automatically.
    "auto_archive": {"enabled": True, "days": 7},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS roles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signature       TEXT NOT NULL,             -- normalised "title||company" (not unique)
    title           TEXT,
    company         TEXT,
    location        TEXT,
    is_remote       INTEGER,
    site            TEXT,                      -- linkedin / indeed / zip_recruiter / manual
    job_url         TEXT,
    job_url_direct  TEXT,
    date_posted     TEXT,
    pay_min         REAL,
    pay_max         REAL,
    years_min       INTEGER,
    years_max       INTEGER,
    is_ai           INTEGER,
    job_level       TEXT,
    job_type        TEXT,
    description     TEXT,
    metadata        TEXT,                      -- full scraped record as JSON
    source          TEXT,
    status          TEXT NOT NULL DEFAULT 'not_applied' CHECK (status IN (__STATUSES__)),
    applied_at      TEXT,
    resume_path     TEXT,
    notes           TEXT,
    first_seen      TEXT,
    updated_at      TEXT,
    ai_verdict      TEXT,                      -- fit / not_a_fit / unsure (derived from ai_match)
    ai_reason       TEXT,
    ai_tags         TEXT,                      -- JSON list of matched interests
    ai_flags        TEXT,                      -- JSON list of violated requirements
    ai_screened_at  TEXT,
    ai_model        TEXT,
    ai_match        TEXT,                      -- strong / good / partial / weak / not_a_fit / unknown
    archived        INTEGER DEFAULT 0,
    archived_at     TEXT,
    offer_deadline  TEXT,
    offer_salary    REAL,
    offer_details   TEXT
);

CREATE TABLE IF NOT EXISTS status_history (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id  INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    status   TEXT,
    note     TEXT,
    at       TEXT
);

-- Every posting URL / signature ever scraped (kept or filtered out),
-- so the scraper never processes the same posting twice.
CREATE TABLE IF NOT EXISTS seen_postings (
    key         TEXT PRIMARY KEY,
    first_seen  TEXT
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT,
    finished_at  TEXT,
    scraped      INTEGER,
    kept         INTEGER,
    added        INTEGER,
    status       TEXT,
    log          TEXT
);

CREATE TABLE IF NOT EXISTS interview_rounds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id       INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    round_number  INTEGER,
    kind          TEXT,                      -- recruiter / hiring_manager / technical / ...
    scheduled_on  TEXT,                      -- date of the interview
    status        TEXT,                      -- scheduled / waiting / passed / rejected / cancelled
    notes         TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_rounds_role ON interview_rounds(role_id);

CREATE TABLE IF NOT EXISTS role_qa (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id     INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    round_id    INTEGER REFERENCES interview_rounds(id) ON DELETE SET NULL,
    stage       TEXT,                        -- application / interview
    question    TEXT,
    answer      TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_qa_role ON role_qa(role_id);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""
ROLES_TABLE_SQL = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS roles"):SCHEMA.index(");") + 2]
ROLES_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_roles_status    ON roles(status);
CREATE INDEX IF NOT EXISTS idx_roles_company   ON roles(company);
CREATE INDEX IF NOT EXISTS idx_roles_url       ON roles(job_url);
CREATE INDEX IF NOT EXISTS idx_roles_url_dir   ON roles(job_url_direct);
CREATE INDEX IF NOT EXISTS idx_roles_signature ON roles(signature);
"""
# ----------------------------------------------------------------------------
# Database versions (migrations)
#
# The database's schema version is stored in it (PRAGMA user_version). SCHEMA above is
# version 2, the baseline every release from 1.0.0 to 1.7.0 uses. Each later change to
# the structure is a numbered migration below, and SCHEMA_VERSION is the newest number.
# Every database, new or old, is brought to SCHEMA_VERSION the same way: the baseline,
# then each migration it hasn't had yet, in order, once, each in its own transaction (a
# failed migration changes nothing). A backup is saved before any upgrade. A database
# with a newer schema than the code knows is refused (DatabaseTooNew) instead of being
# touched; to go back to an older version, restore the backup from before the upgrade.
#
# To change the schema:
#   1. add a function, above MIGRATIONS, that makes the change, e.g.
#          def _m3_role_tags(conn):
#              conn.execute("ALTER TABLE roles ADD COLUMN tags TEXT")
#      (use conn.execute only; executescript would commit half way);
#   2. register it as the next number in MIGRATIONS and raise SCHEMA_VERSION to match;
#   3. leave SCHEMA as it is (it stays the version-2 baseline) and add a test.
# launch.py reads SCHEMA_VERSION from each release's db.py, so keep it a plain number.
# ----------------------------------------------------------------------------
BASELINE_VERSION = 2
SCHEMA_VERSION = 2

MIGRATIONS: dict = {
    # 3: ("add tags to roles", _m3_role_tags),
}

# Columns added during the 1.x baseline (before numbered migrations), kept so databases
# from before them still get them: (table, column, type).
BASELINE_COLUMNS = [
    ("scrape_runs", "kind", "TEXT"),          # pull / rescreen / descriptions / import
    ("scrape_runs", "trigger", "TEXT"),       # ui / cli / schedule
    ("scrape_runs", "ai_screened", "INTEGER"),
    ("scrape_runs", "ai_rejected", "INTEGER"),
    ("scrape_runs", "ai_unsure", "INTEGER"),
    ("scrape_runs", "ai_input_tokens", "INTEGER"),
    ("scrape_runs", "ai_output_tokens", "INTEGER"),
    ("scrape_runs", "ai_cost", "REAL"),
    ("status_history", "source", "TEXT"),     # 'ai' = made by the AI screen; NULL = you / a pull
]
# One-time data fixes that run right after a column above is added.
BASELINE_BACKFILLS = {
    # Before this column existed, AI changes were only recognisable by their note.
    ("status_history", "source"): "UPDATE status_history SET source='ai' "
                                  "WHERE note LIKE 'AI screen:%' OR note LIKE 'AI re-screen:%'",
}

ROLE_FIELDS = [
    "title", "company", "location", "is_remote", "site", "job_url",
    "job_url_direct", "date_posted", "pay_min", "pay_max", "years_min",
    "years_max", "is_ai", "job_level", "job_type", "description", "metadata",
    "source",
]
EDITABLE_FIELDS = set(ROLE_FIELDS) - {"metadata", "signature"} | {
    "status", "applied_at", "resume_path", "notes", "offer_deadline", "offer_salary",
    "offer_details"}

# Steps of the hiring process after you apply (stored in interview_rounds). The
# role's status stays broad (Applied, Interviewing, Offer...); each step has a
# type and its own state, so "technical screen to do" vs "done" lives on the step.
ROUND_KINDS = {
    "recruiter": "Recruiter screen", "technical_screen": "Technical screen",
    "hiring_manager": "Hiring manager", "technical": "Technical interview",
    "coding": "Coding", "system_design": "System design", "behavioral": "Behavioral",
    "take_home": "Take-home", "onsite": "Onsite / final", "other": "Other",
}
# Keys are kept from the first version (stored data); "scheduled" now reads "To do".
ROUND_STATUSES = {
    "scheduled": "To do", "waiting": "Done, waiting to hear back",
    "passed": "Passed", "rejected": "Rejected",
    "cancelled": "Cancelled",
}
# Adding a step to a role in one of these statuses moves it to Interviewing.
PRE_INTERVIEW_STATUSES = {"not_applied", "interested", "applied"}

SORTS = {
    "title": "LOWER(title)", "company": "LOWER(company)", "pay_max": "pay_max",
    "date_posted": "date_posted", "first_seen": "first_seen",
    "applied_at": "applied_at", "updated_at": "updated_at",
    "years_min": "years_min", "location": "LOWER(location)",
    "fit": "CASE ai_match " + " ".join(f"WHEN '{k}' THEN {i}" for i, k in enumerate(MATCH_ORDER)) + " END",
    "status": "CASE status " + " ".join(
        f"WHEN '{k}' THEN {i}" for i, k in enumerate(STATUS_SORT)) + " END",
}


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").lower()).strip()


def signature(title, company) -> str:
    return f"{_norm(title)}||{_norm(company)}"


def _regexp(pattern, value):
    if value is None:
        return False
    try:
        return re.search(pattern, value, re.I) is not None
    except re.error:
        return False


_initialized: set[str] = set()   # database files whose schema is up to date in this process


def _open(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.create_function("REGEXP", 2, _regexp, deterministic=True)
    # Same employer under any of its names (see companies_match), for the company filter.
    conn.create_function("COMPANY_MATCH", 2, lambda a, b: companies_match(a, b), deterministic=True)
    return conn


def connect() -> sqlite3.Connection:
    """Open the database. Schema setup and migrations run once per process, not
    on every connection. Callers should close the connection when done."""
    conn = _open(DB_PATH)
    key = str(Path(DB_PATH).resolve())
    if key not in _initialized:
        try:
            _init_schema(conn)
        except Exception:
            conn.close()
            raise
        _initialized.add(key)
    return conn


class DatabaseError(RuntimeError):
    """The database can't be used by this version of the tracker (see the message)."""


class DatabaseTooNew(DatabaseError):
    """The database's schema is newer than this version of the tracker knows."""


class MigrationFailed(DatabaseError):
    """A migration failed; it was rolled back, so the database is unchanged."""


def _setting(conn, key) -> str | None:
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:   # no settings table yet
        return None
    return row[0] if row else None


def _last_app_version(conn) -> str | None:
    """The newest tracker version that has used this database (recorded since 1.8.0)."""
    return _setting(conn, "app_version")


def schema_history(conn) -> list[dict]:
    """Migrations applied to this database: [{"version", "name", "at", "app_version"}]."""
    try:
        return json.loads(_setting(conn, "schema_history") or "[]")
    except ValueError:
        return []


def _record(conn, key, value):
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _init_schema(conn):
    """Bring the database to SCHEMA_VERSION (see "Database versions" above)."""
    import versions
    fresh = conn.execute("SELECT 1 FROM sqlite_master WHERE name='roles'").fetchone() is None
    current = 0 if fresh else conn.execute("PRAGMA user_version").fetchone()[0]
    last_app = None if fresh else _last_app_version(conn)
    if current > SCHEMA_VERSION:
        raise DatabaseTooNew(
            f"This database ({DB_PATH}) has schema version {current}"
            + (f" (last used by version {last_app})" if last_app else "")
            + f", but this version of the tracker ({settings.LAUNCHED_AS}) only knows up to "
            f"{SCHEMA_VERSION}, so it won't touch it. Start a newer version (the latest release is the "
            f"default), or to go back, restore the copy saved before the upgrade (in {settings.BACKUP_DIR}) "
            f"and point JOBS_DB at it.")
    statuses_changed = not fresh and not _status_check_current(conn)
    newer_app = not fresh and (last_app is None or versions.newer(settings.VERSION, last_app))
    if not fresh and (current < SCHEMA_VERSION or statuses_changed or newer_app):
        path = backup(conn, reason=f"before-{settings.VERSION}")
        log.info("Upgrading the database (schema %s -> %s, app %s -> %s); backup at %s",
                 current, SCHEMA_VERSION, last_app or "1.7.0 or earlier", settings.VERSION, path)
    conn.execute("PRAGMA journal_mode=WAL")
    if current <= BASELINE_VERSION:
        # New databases and 1.x databases: the version-2 baseline.
        conn.executescript(SCHEMA.replace("__STATUSES__", _status_list_sql()))
        _migrate(conn)
        if not fresh and current < BASELINE_VERSION:
            _rebuild_roles(conn, clean_seen=True)          # from before the 1.0.0 baseline
            statuses_changed = False
        conn.executescript(ROLES_INDEXES)
        conn.execute(f"PRAGMA user_version={BASELINE_VERSION}")
        conn.commit()
        current = BASELINE_VERSION
    if statuses_changed:
        _rebuild_roles(conn)                               # the status list changed in code
    _apply_migrations(conn, current)
    if fresh or newer_app:
        _record(conn, "app_version", settings.VERSION)     # the newest version that has used it
    conn.commit()
    _migrate_seen_json(conn)


def _apply_migrations(conn, current: int):
    """Run migrations current+1 .. SCHEMA_VERSION, each once and in its own transaction."""
    for version in range(current + 1, SCHEMA_VERSION + 1):
        name, fn = MIGRATIONS[version]
        log.info("Database migration %d: %s", version, name)
        conn.commit()
        conn.execute("BEGIN")
        try:
            fn(conn)
            conn.execute(f"PRAGMA user_version={version}")
            history = schema_history(conn) + [{"version": version, "name": name, "at": now_iso(),
                                               "app_version": settings.VERSION}]
            _record(conn, "schema_history", json.dumps(history))
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise MigrationFailed(
                f"Database migration {version} ({name}) failed and was undone, so the database is "
                f"unchanged at schema version {version - 1}: {type(e).__name__}: {e}") from e


def _status_list_sql() -> str:
    return ", ".join(f"'{s}'" for s in STATUSES)


def _status_check_current(conn) -> bool:
    """True if the roles table's CHECK constraint allows exactly the statuses in
    STATUSES. Adding or removing a status in code makes this False, and the table
    is rebuilt with the new list (with a backup first)."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='roles'").fetchone()[0]
    return f"CHECK (status IN ({_status_list_sql()}))" in sql


def _migrate(conn):
    for table, col, typ in BASELINE_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            if (table, col) in BASELINE_BACKFILLS:
                conn.execute(BASELINE_BACKFILLS[(table, col)])
    conn.commit()


def _rebuild_roles(conn, clean_seen=False):
    """One-time upgrade of an old roles table (SQLite can't drop a UNIQUE constraint
    or add a CHECK in place): signature is no longer unique, status is checked,
    the unused ai_score column is dropped (old score-only rows get a match level).
    Also run when the status list changes, to update the CHECK. clean_seen (only on
    the first upgrade) clears seen-postings entries for postings that were only
    filtered out, so they can be re-checked after preference changes. A backup is
    made first."""
    path = backup(conn, reason="before-upgrade")
    log.info("Upgrading roles table; backup at %s", path)
    old_cols = {r["name"] for r in conn.execute("PRAGMA table_info(roles)")}
    if clean_seen:   # a table from before the 1.0.0 baseline: the baseline definition
        tmp = ROLES_TABLE_SQL.replace("CREATE TABLE IF NOT EXISTS roles", "CREATE TABLE roles_new") \
            .replace("__STATUSES__", _status_list_sql())
    else:            # the table as it is now, with the new status list in its CHECK
        current_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='roles'").fetchone()[0]
        tmp = re.sub(r"CHECK\s*\(\s*status\s+IN\s*\([^)]*\)\s*\)", f"CHECK (status IN ({_status_list_sql()}))",
                     current_sql, count=1)
        tmp = re.sub(r'^CREATE TABLE\s+"?roles"?', "CREATE TABLE roles_new", tmp, count=1)
    indexes = [r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE type='index' "
                                          "AND tbl_name='roles' AND sql IS NOT NULL")]
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS roles_new")
        conn.execute(tmp)
        new_cols = [r["name"] for r in conn.execute("PRAGMA table_info(roles_new)")]
        unknown = conn.execute(f"SELECT COUNT(*) FROM roles WHERE status NOT IN ({_status_list_sql()})"
                               ).fetchone()[0]
        if unknown:
            log.warning("%d role(s) had a status that no longer exists; set to Not applied "
                        "(their old status is still in the backup and the status history)", unknown)
        exprs = []
        for c in new_cols:
            if c == "status":
                exprs.append(f"CASE WHEN status IN ({_status_list_sql()}) THEN status ELSE 'not_applied' END")
            elif c == "ai_match" and "ai_score" in old_cols:
                exprs.append(
                    "COALESCE(ai_match, CASE ai_verdict WHEN 'unsure' THEN 'unknown' "
                    "WHEN 'not_a_fit' THEN 'not_a_fit' WHEN 'fit' THEN CASE "
                    "WHEN ai_score >= 80 THEN 'strong' WHEN ai_score >= 65 THEN 'good' "
                    "WHEN ai_score >= 50 THEN 'partial' ELSE 'weak' END END)")
            elif c in old_cols:
                exprs.append(c)
            else:
                exprs.append("NULL")
        conn.execute(f"INSERT INTO roles_new({', '.join(new_cols)}) SELECT {', '.join(exprs)} FROM roles")
        conn.execute("DROP TABLE roles")
        conn.execute("ALTER TABLE roles_new RENAME TO roles")
        for sql in indexes:                                # dropping the table dropped its indexes
            conn.execute(sql.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
                         if "IF NOT EXISTS" not in sql else sql)
        if clean_seen:
            conn.execute("DELETE FROM seen_postings WHERE key NOT IN "
                         "(SELECT job_url FROM roles WHERE job_url IS NOT NULL "
                         " UNION SELECT job_url_direct FROM roles WHERE job_url_direct IS NOT NULL)")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise sqlite3.IntegrityError(f"foreign key check failed after upgrade: {len(bad)} rows")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


# ----------------------------------------------------------------------------
# Backups (kept outside OneDrive by default; see settings.BACKUP_DIR)
# ----------------------------------------------------------------------------
def backup(conn=None, reason: str = "daily") -> Path:
    """Copy the live database (WAL included) to BACKUP_DIR and prune old daily copies."""
    settings.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = settings.BACKUP_DIR / f"{Path(DB_PATH).stem}-{dt.datetime.now():%Y%m%d-%H%M%S}-{reason}.db"
    # Write to a local temp file first and move it in when complete, so a synced
    # backup folder (e.g. OneDrive) never sees a half-written file or a journal.
    fd, tmp_name = tempfile.mkstemp(prefix="jobtracker-backup-", suffix=".db")
    os.close(fd)
    tmp = Path(tmp_name)
    src = conn or _open(DB_PATH)
    try:
        out = sqlite3.connect(tmp)
        try:
            src.backup(out)
        finally:
            out.close()
        shutil.move(str(tmp), str(dest))
    finally:
        tmp.unlink(missing_ok=True)
        if conn is None:
            src.close()
    daily = sorted(settings.BACKUP_DIR.glob(f"{Path(DB_PATH).stem}-*-daily.db"))
    for old in daily[:-settings.BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return dest


def backup_if_due() -> Path | None:
    """Make today's daily backup unless one already exists."""
    if not Path(DB_PATH).exists():
        return None
    today = f"{Path(DB_PATH).stem}-{dt.date.today():%Y%m%d}-"
    if settings.BACKUP_DIR.exists() and any(
            p.name.startswith(today) and p.name.endswith("-daily.db") for p in settings.BACKUP_DIR.iterdir()):
        return None
    return backup(reason="daily")


def in_synced_folder(path) -> bool:
    """True if the path is inside a OneDrive/Dropbox/Google Drive folder, where syncing
    a live SQLite database can corrupt it or create conflict copies."""
    parts = {p.lower() for p in Path(path).resolve().parts}
    return any(p.startswith(("onedrive", "dropbox", "google drive")) for p in parts)


# ----------------------------------------------------------------------------
# Run lock: only one pull or re-screen at a time, across the web app, the
# command line and the scheduled task. The OS releases the lock if the process dies.
# ----------------------------------------------------------------------------
class RunInProgress(RuntimeError):
    pass


@contextlib.contextmanager
def run_lock():
    path = Path(DB_PATH).with_name(Path(DB_PATH).name + ".run.lock")
    f = open(path, "a+b")
    try:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RunInProgress("Another pull or re-screen is already running "
                                "(from the web app, the command line or the daily schedule). "
                                "Try again when it finishes.") from None
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
    finally:
        f.close()


# ----------------------------------------------------------------------------
# Resume files (used by the web app's picker, /api/file and the command line)
# ----------------------------------------------------------------------------
RESUME_EXTS = {".pdf", ".docx", ".doc", ".txt", ".md"}
SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git", "results", "static", "tests", "logs"}


def is_resume_file(path) -> bool:
    """True if path is a file resume_files() would list, checked without walking the folder."""
    try:
        p = Path(path).resolve()
        rel = p.relative_to(Path(BASE_DIR).resolve())
    except (ValueError, OSError):
        return False
    if any(d in SKIP_DIRS or d.startswith(".") for d in rel.parts[:-1]):
        return False
    ext = p.suffix.lower()
    if ext not in RESUME_EXTS:
        return False
    if ext in (".txt", ".md") and "resume" not in p.name.lower() and "cv" not in p.name.lower():
        return False
    return p.is_file()


def resume_files() -> list[Path]:
    """Resume-like files under BASE_DIR, newest first. .txt/.md files count only if
    their name mentions resume or CV. /api/file serves nothing outside this list."""
    out = []
    for root, dirs, names in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in names:
            ext = Path(name).suffix.lower()
            if ext not in RESUME_EXTS:
                continue
            if ext in (".txt", ".md") and "resume" not in name.lower() and "cv" not in name.lower():
                continue
            out.append(Path(root) / name)
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def _migrate_seen_json(conn):
    """One-time import of the old seen_jobs.json history."""
    old = HERE / "seen_jobs.json"
    if not old.exists():
        return
    keys = json.loads(old.read_text(encoding="utf-8"))
    mark_seen(conn, keys)
    conn.commit()
    old.rename(old.with_suffix(".json.migrated"))


# ----------------------------------------------------------------------------
# Preferences
# ----------------------------------------------------------------------------
def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def get_prefs(conn) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key='preferences'").fetchone()
    saved = json.loads(row["value"]) if row else {}
    prefs = _deep_merge(DEFAULT_PREFS, saved)
    # Drop boards that are no longer offered (Google Jobs) from older saved preferences, so
    # pulls skip them and the next save passes validation.
    if isinstance(prefs.get("sites"), list):
        prefs["sites"] = ([s for s in prefs["sites"] if isinstance(s, str) and s in SITES]
                          or list(DEFAULT_PREFS["sites"]))
    return prefs


# Google Jobs isn't offered: Google Search now answers clients that don't run JavaScript
# (JobSpy included) with an "enablejs" redirect page instead of results, so every Google
# search came back empty.
SITES = {"linkedin", "indeed", "zip_recruiter", "glassdoor"}
AI_MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}


def validate_prefs(p: dict) -> list[str]:
    """Problems that would break a pull or the AI screen, as messages for the user.
    An empty list means the preferences are safe to save."""
    errors = []

    def num(path, label, lo, hi, required=True, integer=True):
        node = p
        for k in path.split("."):
            node = node.get(k) if isinstance(node, dict) else None
        if node is None or node == "":
            if required:
                errors.append(f"{label} is required.")
            return None
        if isinstance(node, bool) or not isinstance(node, (int, float)) or (integer and node != int(node)):
            errors.append(f"{label} must be a {'whole ' if integer else ''}number.")
            return None
        if not lo <= node <= hi:
            errors.append(f"{label} must be between {lo:,} and {hi:,}.")
            return None
        return node

    def str_list(value, label, allowed=None, required=False):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            errors.append(f"{label} must be a list.")
            return
        if required and not value:
            errors.append(f"Pick at least one {label.lower()}.")
        if allowed is not None and set(value) - allowed:
            errors.append(f"Unknown {label.lower()}: {', '.join(sorted(set(value) - allowed))}.")

    str_list(p.get("search_terms"), "Search term", required=True)
    str_list(p.get("sites"), "Job site", allowed=SITES, required=True)
    num("results_per_search", "Results per search", 5, 200)
    if not isinstance(p.get("results_all", False), bool):
        errors.append("“Limit results per search” must be on or off.")
    num("posted_within_hours", "Posted within (hours)", 1, 24 * 30)
    num("locations.radius_miles", "Radius (miles)", 0, 100)
    loc = p.get("locations") or {}
    if not loc.get("value") and not loc.get("include_remote"):
        errors.append("Add a location or turn on remote roles, or a pull has nothing to search.")
    lo = num("experience.min", "Minimum years of experience", 0, 30)
    hi = num("experience.max", "Maximum years of experience", 0, 30)
    if lo is not None and hi is not None and lo > hi:
        errors.append("Minimum years of experience can't be more than the maximum.")
    num("salary.min_top", "Top of pay range", 0, 10_000_000, required=False, integer=False)
    num("salary.min_floor", "Bottom of pay range", 0, 10_000_000, required=False, integer=False)
    for key, label in (("locations", "Locations"), ("workplace", "Workplace type"),
                       ("employment_types", "Employment type"), ("title_include", "Title words"),
                       ("title_exclude", "Excluded title words"), ("companies_exclude", "Excluded companies"),
                       ("industries", "Industries"), ("company_size", "Company size")):
        str_list((p.get(key) or {}).get("value"), label)
    str_list(p.get("interests"), "Interests")
    if not isinstance((p.get("auto_archive") or {}).get("enabled", True), bool):
        errors.append("“Archive Not a fit roles automatically” must be on or off.")
    num("auto_archive.days", "Days before archiving Not a fit roles", 1, 365)
    if (p.get("ai") or {}).get("model") not in AI_MODELS:
        errors.append(f"Unknown AI model. Use one of: {', '.join(sorted(AI_MODELS))}.")
    return errors


_COMPANY_SUFFIX_RE = re.compile(r"[^\w\s&]")


def _company_key(name) -> str:
    """'Acme, Inc.' -> 'acme inc'  (lower case, punctuation dropped, spaces collapsed)."""
    return re.sub(r"\s+", " ", _COMPANY_SUFFIX_RE.sub(" ", str(name or "").lower())).strip()


def company_blocked(company, blocked: list) -> bool:
    """True if the company is on the blocked list: the same name, or the blocked name
    followed by more words ("Acme" blocks "Acme Corp" but not "Acmex")."""
    c = _company_key(company)
    if not c:
        return False
    for b in blocked or []:
        b = _company_key(b)
        if b and (c == b or c.startswith(b + " ")):
            return True
    return False


def block_company(conn, company: str) -> list[str]:
    """Add a company to the blocked list (Preferences → Blocked companies), once.
    Returns the updated list."""
    name = (company or "").strip()
    if not name:
        raise ValueError("Company name is empty.")
    prefs = get_prefs(conn)
    blocked = list(prefs["companies_exclude"]["value"])
    if _company_key(name) not in {_company_key(b) for b in blocked}:
        blocked.append(name)
    prefs["companies_exclude"]["value"] = blocked
    save_prefs(conn, prefs)
    return blocked


def save_prefs(conn, prefs: dict) -> dict:
    """Merge with the defaults, validate, and save. Raises ValueError (with every
    problem in the message) instead of saving preferences that would break a pull."""
    merged = _deep_merge(DEFAULT_PREFS, prefs)
    errors = validate_prefs(merged)
    if errors:
        raise ValueError(" ".join(errors))
    conn.execute("INSERT INTO settings(key, value) VALUES('preferences', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps(merged),))
    conn.commit()
    return merged


# ----------------------------------------------------------------------------
# Seen postings
# ----------------------------------------------------------------------------
def is_seen(conn, key) -> bool:
    return bool(key) and conn.execute(
        "SELECT 1 FROM seen_postings WHERE key=?", (key,)).fetchone() is not None


def mark_seen(conn, keys):
    ts = now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO seen_postings(key, first_seen) VALUES(?, ?)",
        [(k, ts) for k in keys if k])


# ----------------------------------------------------------------------------
# Runs (pulls and AI re-screens), with AI token usage and cost
# ----------------------------------------------------------------------------
def _mark_interrupted(conn):
    conn.execute("UPDATE scrape_runs SET status='interrupted', finished_at=COALESCE(finished_at, ?) "
                 "WHERE status='running'", (now_iso(),))


def mark_stale_runs():
    """At server start: if no pull or re-screen holds the run lock, any run still
    marked 'running' died without finishing (e.g. the server restarted mid-run)."""
    try:
        with run_lock():
            conn = connect()
            try:
                _mark_interrupted(conn)
                conn.commit()
            finally:
                conn.close()
    except RunInProgress:
        pass   # a real run (schedule / command line) is in progress; leave it alone


def start_run(conn, kind="pull", trigger="cli") -> int:
    """Record a new run. Call while holding run_lock(): any run still marked
    'running' at that point died without finishing (e.g. the service restarted)."""
    _mark_interrupted(conn)
    cur = conn.execute("INSERT INTO scrape_runs(started_at, kind, trigger, status) "
                       "VALUES(?,?,?, 'running')", (now_iso(), kind, trigger))
    conn.commit()
    return cur.lastrowid


def update_run(conn, run_id: int, **fields):
    """Update a run's counters while it's still going (no commit)."""
    conn.execute(f"UPDATE scrape_runs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                 list(fields.values()) + [run_id])


def finish_run(conn, run_id: int, **fields):
    fields["finished_at"] = now_iso()
    conn.execute(f"UPDATE scrape_runs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                 list(fields.values()) + [run_id])
    conn.commit()


def list_runs(conn, limit=30):
    return [dict(r) for r in conn.execute(
        "SELECT id, kind, trigger, started_at, finished_at, scraped, kept, added, status, "
        "ai_screened, ai_rejected, ai_unsure, ai_input_tokens, ai_output_tokens, ai_cost "
        "FROM scrape_runs ORDER BY id DESC LIMIT ?", (limit,))]


# ----------------------------------------------------------------------------
# Roles
# ----------------------------------------------------------------------------
def _clean(rec: dict) -> dict:
    out = {}
    for f in ROLE_FIELDS:
        v = rec.get(f)
        if isinstance(v, str) and not v.strip():
            v = None
        if isinstance(v, bool):
            v = int(v)
        out[f] = v
    if isinstance(out.get("metadata"), (dict, list)):
        out["metadata"] = json.dumps(out["metadata"], default=str)
    return out


# Legal and web endings that boards add to the same employer's name:
# "Globex", "Globex.com", "Globex.com Services LLC".
_COMPANY_ENDINGS = {"inc", "incorporated", "llc", "l l c", "ltd", "limited", "corp", "corporation",
                    "co", "company", "plc", "gmbh", "lp", "llp", "pbc", "sa", "ag", "bv", "nv"}


def company_core(name) -> str:
    """A company name reduced for duplicate matching: lower case, '.com' and punctuation
    dropped, legal endings removed. 'Globex.com Services LLC' -> 'globex services'."""
    s = re.sub(r"\.(com|io|ai|net|org|co)\b", " ", str(name or "").lower())
    words = re.sub(r"[^\w&]+", " ", s).split()
    while words and words[-1] in _COMPANY_ENDINGS:
        words.pop()
    return " ".join(words)


def companies_match(a, b) -> bool:
    """Same employer as different boards name it: equal after company_core, or one name
    is the other plus more words ('globex' / 'globex services'), never part of a word."""
    x, y = company_core(a), company_core(b)
    if not x or not y:
        return False
    return x == y or x.startswith(y + " ") or y.startswith(x + " ")


def _place_key(location) -> str:
    """'New York, NY, US' -> 'new york'; 'Remote (Austin, TX)' -> 'remote'."""
    s = str(location or "").strip().lower()
    if s.startswith("remote"):
        return "remote"
    return re.split(r",|\.\s", s)[0].strip().rstrip(".")


def find_existing(conn, title=None, company=None, job_url=None,
                  within_days: int | None = DUPLICATE_WINDOW_DAYS, location=None, job_url_direct=None):
    """The role this posting duplicates, if any:
    - same job URL (or the employer's own link), at any age; else
    - same title and company first seen within `within_days` days (None = any age); else
    - same title, the same employer under another name ("Globex" / "Globex.com Services
      LLC") and the same city, within `within_days` days. Needs `location`."""
    for url in (job_url, job_url_direct):
        if url:
            row = conn.execute("SELECT * FROM roles WHERE job_url=? OR job_url_direct=? "
                               "ORDER BY id DESC LIMIT 1", (url, url)).fetchone()
            if row:
                return row
    if not (title or company):
        return None
    since = None
    if within_days is not None:
        since = (dt.datetime.now() - dt.timedelta(days=within_days)).isoformat(timespec="seconds")
    sql, args = "SELECT * FROM roles WHERE signature=?", [signature(title, company)]
    if since:
        sql += " AND first_seen >= ?"
        args.append(since)
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    if row or not (title and company and location):
        return row
    # Same title (exactly, ignoring case and spacing), employer named differently, same place.
    prefix = _norm(title) + "||"
    sql, args = "SELECT * FROM roles WHERE substr(signature, 1, ?) = ?", [len(prefix), prefix]
    if since:
        sql += " AND first_seen >= ?"
        args.append(since)
    place = _place_key(location)
    for cand in conn.execute(sql + " ORDER BY id DESC", args):
        if companies_match(company, cand["company"]) and place and _place_key(cand["location"]) == place:
            return cand
    return None


def fill_blanks(conn, role_id: int, rec: dict, note: str | None = None) -> list[str]:
    """Copy fields from another listing of the same posting into a role, only where the
    role has nothing yet (never overwrites). Returns the names of the fields filled."""
    row = conn.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
    if not row:
        return []
    new = _clean(rec)
    filled = {f: new[f] for f in ROLE_FIELDS
              if row[f] is None and new.get(f) is not None and f not in ("title", "company")}
    if filled:
        filled["updated_at"] = now_iso()
        conn.execute(f"UPDATE roles SET {', '.join(f'{k}=?' for k in filled)} WHERE id=?",
                     list(filled.values()) + [role_id])
        names = [k for k in filled if k != "updated_at"]
        if "description" in names:
            add_history(conn, role_id, row["status"],
                        note or f"Description added from another listing ({new.get('site') or 'scrape'})")
        return names
    return []


def find_duplicate_groups(conn) -> list[list[dict]]:
    """Roles already in the database that the duplicate rule (see find_existing) treats
    as one posting: same title, same employer under any of its names, same city, first
    seen within DUPLICATE_WINDOW_DAYS of each other. Read-only; oldest first in each group."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, title, company, location, site, status, first_seen, job_url FROM roles ORDER BY id")]
    by_title: dict[str, list[dict]] = {}
    for r in rows:
        by_title.setdefault(_norm(r["title"]), []).append(r)
    groups, window = [], dt.timedelta(days=DUPLICATE_WINDOW_DAYS)
    for same_title in by_title.values():
        used = set()
        for i, a in enumerate(same_title):
            if a["id"] in used:
                continue
            group = [a]
            for b in same_title[i + 1:]:
                if b["id"] in used:
                    continue
                near = abs(dt.datetime.fromisoformat(a["first_seen"]) - dt.datetime.fromisoformat(b["first_seen"])) <= window
                if near and companies_match(a["company"], b["company"]) and \
                        _place_key(a["location"]) and _place_key(a["location"]) == _place_key(b["location"]):
                    group.append(b)
            if len(group) > 1:
                used.update(r["id"] for r in group)
                groups.append(group)
    return groups


# How far along a status is, for keeping the furthest one when merging duplicates.
_STATUS_PROGRESS = {"not_applied": 0, "interested": 1, "not_a_fit": 1, "closed": 1, "applied": 2,
                    "withdrawn": 3, "rejected": 3, "interviewing": 4, "offer": 5}


def merge_roles(conn, keep_id: int, other_ids: list[int]) -> dict | None:
    """Merge duplicate roles into one. The kept role gets:
    - every history entry, step and question of the others (nothing is lost);
    - the others' values for any field it's missing, and their notes appended;
    - the earliest applied date, and the furthest status (e.g. Applied over Not applied);
    - their URLs remembered as seen, so a later pull doesn't add them again.
    The other roles are then deleted. Returns the merged role, or None if keep_id is missing."""
    keep = conn.execute("SELECT * FROM roles WHERE id=?", (keep_id,)).fetchone()
    if not keep:
        return None
    others = [conn.execute("SELECT * FROM roles WHERE id=?", (i,)).fetchone() for i in other_ids if i != keep_id]
    others = [o for o in others if o]
    if not others:
        return get_role(conn, keep_id)
    fill_fields = ROLE_FIELDS + ["resume_path", "offer_deadline", "offer_salary", "offer_details",
                                 "ai_verdict", "ai_reason", "ai_tags", "ai_flags", "ai_screened_at",
                                 "ai_model", "ai_match"]
    changes, notes = {}, [keep["notes"]] if keep["notes"] else []
    status = keep["status"]
    applied = [r["applied_at"] for r in [keep, *others] if r["applied_at"]]
    for o in others:
        for f in fill_fields:
            if keep[f] is None and changes.get(f) is None and o[f] is not None:
                changes[f] = o[f]
        if o["notes"] and o["notes"] not in notes:
            notes.append(o["notes"])
        if _STATUS_PROGRESS.get(o["status"], 0) > _STATUS_PROGRESS.get(status, 0):
            status = o["status"]
    if applied:
        changes["applied_at"] = min(applied)
    if len(notes) > 1:
        changes["notes"] = "\n\n".join(notes)
    if status != keep["status"]:
        changes["status"] = status
    ids = [o["id"] for o in others]
    marks = ", ".join("?" * len(ids))
    for table in ("status_history", "interview_rounds", "role_qa"):
        conn.execute(f"UPDATE {table} SET role_id=? WHERE role_id IN ({marks})", [keep_id, *ids])
    mark_seen(conn, [u for o in others for u in (o["job_url"], o["job_url_direct"]) if u])
    conn.execute(f"DELETE FROM roles WHERE id IN ({marks})", ids)
    if changes:
        changes["updated_at"] = now_iso()
        conn.execute(f"UPDATE roles SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?",
                     list(changes.values()) + [keep_id])
    desc = "; ".join(f"#{o['id']} ({o['site'] or 'manual'}: {o['company']})" for o in others)
    add_history(conn, keep_id, status, f"Merged duplicate role(s) into this one: {desc}")
    return get_role(conn, keep_id)


def role_exists(conn, role_id: int) -> bool:
    return conn.execute("SELECT 1 FROM roles WHERE id=?", (role_id,)).fetchone() is not None


def add_history(conn, role_id, status, note=None, source=None):
    """source: who made the change. 'ai' for the AI screen; None means you
    (web app, command line) or a pull adding the role."""
    conn.execute("INSERT INTO status_history(role_id, status, note, at, source) VALUES(?,?,?,?,?)",
                 (role_id, status, note, now_iso(), source))


def current_status(conn, role_id: int) -> str | None:
    row = conn.execute("SELECT status FROM roles WHERE id=?", (role_id,)).fetchone()
    return row["status"] if row else None


def last_change_by_ai(conn, role_id: int) -> bool:
    """True if the role is Not a fit because the AI screen put it there."""
    return conn.execute(f"SELECT 1 FROM roles WHERE id=? AND {AI_REJECTED_SQL}",
                        (role_id,)).fetchone() is not None


def upsert_role(conn, rec: dict, status="not_applied", note=None):
    """Insert a role. If it duplicates an existing one (see find_existing), only fill
    in that role's blank fields; never changes status. Returns (id, created)."""
    if status not in STATUSES:
        raise ValueError(f"Unknown status '{status}'. Use one of: {', '.join(STATUSES)}")
    rec_c = _clean(rec)
    existing = find_existing(conn, rec_c["title"], rec_c["company"], rec_c["job_url"],
                             location=rec_c["location"], job_url_direct=rec_c["job_url_direct"])
    ts = now_iso()
    if existing:
        sets = ", ".join(f"{f}=COALESCE({f}, ?)" for f in ROLE_FIELDS)
        conn.execute(f"UPDATE roles SET {sets} WHERE id=?",
                     [rec_c[f] for f in ROLE_FIELDS] + [existing["id"]])
        return existing["id"], False
    cols = ROLE_FIELDS + ["signature", "status", "first_seen", "updated_at"]
    vals = [rec_c[f] for f in ROLE_FIELDS] + [
        signature(rec_c["title"], rec_c["company"]), status, ts, ts]
    cur = conn.execute(
        f"INSERT INTO roles({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", vals)
    add_history(conn, cur.lastrowid, status,
                note or f"Added from {rec_c.get('source') or 'scrape'}")
    return cur.lastrowid, True


REQUIRED_TEXT_FIELDS = {"title": "Title", "company": "Company"}
NUMBER_FIELDS = {"pay_min": "Pay min", "pay_max": "Pay max", "offer_salary": "Offer salary"}
WHOLE_NUMBER_FIELDS = {"years_min": "Years min", "years_max": "Years max"}
URL_FIELDS = {"job_url": "Posting URL", "job_url_direct": "Company page URL"}
DATE_FIELDS = {"applied_at": "Applied date", "offer_deadline": "Offer deadline", "date_posted": "Posted date"}
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:\d{2}|Z)?)?")


def check_role_changes(changes: dict) -> dict:
    """Validate and normalise edits to a role: blank text becomes None, title and
    company can't be cleared, numbers must be numbers (numeric text is converted),
    dates must be real YYYY-MM-DD dates. Raises ValueError listing every problem."""
    out, errors = {}, []
    for k, v in changes.items():
        if isinstance(v, str):
            v = v.strip() or None
        if k in REQUIRED_TEXT_FIELDS and v is None:
            errors.append(f"{REQUIRED_TEXT_FIELDS[k]} can't be blank.")
        elif v is not None and (k in NUMBER_FIELDS or k in WHOLE_NUMBER_FIELDS):
            label = NUMBER_FIELDS.get(k) or WHOLE_NUMBER_FIELDS[k]
            try:
                if isinstance(v, bool):
                    raise ValueError
                num = float(v)
                if not math.isfinite(num) or num < 0:   # NaN, infinity or negative
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                errors.append(f"{label} must be a number (0 or more).")
                continue
            top = 100 if k in WHOLE_NUMBER_FIELDS else 100_000_000   # beyond any real years / pay
            if num > top:
                errors.append(f"{label} must be at most {top:,}.")
                continue
            if k in WHOLE_NUMBER_FIELDS:
                if num != int(num):
                    errors.append(f"{label} must be a whole number.")
                    continue
                v = int(num)
            else:
                v = num
        elif v is not None and k in DATE_FIELDS:
            # A YYYY-MM-DD date, optionally a full ISO date-time; anything else
            # (trailing text, compact 20261121, 2026-1-5) is rejected.
            s = str(v)
            try:
                if not DATE_RE.fullmatch(s):
                    raise ValueError
                v = (dt.date.fromisoformat(s) if len(s) == 10 else dt.datetime.fromisoformat(s).date()).isoformat()
            except ValueError:
                errors.append(f"{DATE_FIELDS[k]} must be a date like 2026-11-21.")
                continue
        elif v is not None and k in URL_FIELDS:
            # Only web links: a javascript: or data: link would run when its button is clicked.
            if not isinstance(v, str) or not re.match(r"https?://[^\s/]+", v, re.I):
                errors.append(f"{URL_FIELDS[k]} must be a web address starting with http:// or https://.")
                continue
        elif k == "is_remote" and v is not None:
            flag = str(v).strip().lower() if not isinstance(v, bool) else str(int(v))
            if flag in ("1", "true"):
                v = 1
            elif flag in ("0", "false"):
                v = 0
            else:
                errors.append("Remote must be true or false.")
                continue
        out[k] = v
    if errors:
        raise ValueError(" ".join(errors))
    return out


def update_role(conn, role_id: int, changes: dict, note=None, source=None) -> dict | None:
    """Apply edits to a role. Raises ValueError (nothing saved) for invalid values;
    see check_role_changes."""
    row = conn.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
    if not row:
        return None
    _check_text(note, "Note")
    changes = check_role_changes({k: v for k, v in changes.items() if k in EDITABLE_FIELDS})
    new_status = changes.get("status")
    if new_status and new_status not in STATUSES:
        raise ValueError(f"Unknown status '{new_status}'. Use one of: {', '.join(STATUSES)}")
    if new_status == "applied" and not row["applied_at"] and "applied_at" not in changes:
        changes["applied_at"] = dt.date.today().isoformat()
    # Moving back to a "haven't applied" status undoes the application, so it stops
    # counting in Insights. (Rejected / Withdrawn keep the date: you did apply.)
    if (new_status in PRE_APPLY_STATUSES and new_status != row["status"]
            and row["applied_at"] and "applied_at" not in changes):
        changes["applied_at"] = None
    if "title" in changes or "company" in changes:
        changes["signature"] = signature(changes.get("title", row["title"]),
                                         changes.get("company", row["company"]))
    if not changes:
        return get_role(conn, role_id)
    changes["updated_at"] = now_iso()
    conn.execute(f"UPDATE roles SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?",
                 list(changes.values()) + [role_id])
    if new_status and new_status != row["status"]:
        add_history(conn, role_id, new_status, note, source)
    elif note:
        add_history(conn, role_id, row["status"], note, source)
    return get_role(conn, role_id)


def _sane_number(v, top):
    """v if it's a real number from 0 to top, else None (AI output isn't trusted blindly)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return v if math.isfinite(v) and 0 <= v <= top else None
    except OverflowError:
        return None


def save_ai_result(conn, role_id: int, result: dict, model: str):
    result = {**result, **{k: _sane_number(result.get(k), 100) for k in ("years_min", "years_max")},
              **{k: _sane_number(result.get(k), 100_000_000) for k in ("pay_min", "pay_max")}}
    conn.execute(
        "UPDATE roles SET ai_verdict=?, ai_match=?, ai_reason=?, ai_tags=?, ai_flags=?, "
        "ai_screened_at=?, ai_model=?, "
        "years_min=COALESCE(years_min, ?), years_max=COALESCE(years_max, ?), "
        "pay_min=COALESCE(pay_min, ?), pay_max=COALESCE(pay_max, ?) WHERE id=?",
        (result.get("verdict"), result.get("match"), result.get("reason"),
         json.dumps(result.get("interests_matched") or []),
         json.dumps(result.get("violated_requirements") or []),
         now_iso(), model,
         result.get("years_min"), result.get("years_max"),
         result.get("pay_min"), result.get("pay_max"), role_id))


def _json_or(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, json.JSONDecodeError):
        return default


def get_role(conn, role_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["metadata"] = _json_or(d["metadata"], None)
    d["ai_tags"] = _json_or(d.get("ai_tags"), [])
    d["ai_flags"] = _json_or(d.get("ai_flags"), [])
    d["rounds"] = list_rounds(conn, role_id)
    d["qa"] = [dict(q) for q in conn.execute(
        "SELECT * FROM role_qa WHERE role_id=? ORDER BY created_at, id", (role_id,))]
    d["history"] = [dict(h) for h in conn.execute(
        "SELECT status, note, at FROM status_history WHERE role_id=? ORDER BY at DESC, id DESC",
        (role_id,))]
    d["same_company"] = same_company_roles(conn, role_id, d["company"])
    return d


def same_company_roles(conn, role_id: int, company) -> list[dict]:
    """Your other roles at this employer, under any of its names ("Globex" / "Globex.com
    Services LLC"): open roles first, then newest first; archived ones are included and marked."""
    return [dict(r) for r in conn.execute(
        "SELECT id, title, company, location, is_remote, status, archived, ai_match, first_seen, applied_at "
        "FROM roles WHERE id != ? AND COMPANY_MATCH(company, ?) "
        "ORDER BY COALESCE(archived,0), (status IN ('not_a_fit','rejected','withdrawn','closed')), "
        "first_seen DESC, id DESC", (role_id, company))]


def common_companies(conn) -> list[dict]:
    """Employers of your (non-archived) roles, most roles first, as [{"name", "count"}],
    for the sidebar's company filter. Names of the same employer ("Globex", "Globex.com")
    count together, under the spelling used most."""
    by_core: dict[str, dict[str, int]] = {}
    for (name,) in conn.execute("SELECT company FROM roles WHERE COALESCE(archived,0)=0 AND company IS NOT NULL"):
        key = company_core(name)
        if key:
            spellings = by_core.setdefault(key, {})
            spellings[name.strip()] = spellings.get(name.strip(), 0) + 1
    # Join names the filter treats as one employer ("globex" / "globex services"): shortest
    # first, so each longer name joins the shorter one it extends.
    groups: dict[str, dict[str, int]] = {}
    for key in sorted(by_core, key=len):
        root = next((g for g in groups if key.startswith(g + " ")), key)
        sp = groups.setdefault(root, {})
        for n, c in by_core[key].items():
            sp[n] = sp.get(n, 0) + c
    out = [{"name": max(sp, key=lambda n: (sp[n], -len(n))), "count": sum(sp.values())} for sp in groups.values()]
    return sorted(out, key=lambda c: (-c["count"], c["name"].lower()))


LIST_COLS = ("id, title, company, location, is_remote, site, job_url, job_url_direct, "
             "date_posted, pay_min, pay_max, years_min, years_max, is_ai, status, "
             "applied_at, resume_path, notes, first_seen, updated_at, source, "
             "ai_verdict, ai_match, ai_reason, ai_tags, archived, offer_deadline, offer_salary, "
             # current step: the next one still to do (soonest date first), else the latest
             "(SELECT round_number || '|' || COALESCE(status,'') || '|' || COALESCE(kind,'') "
             "        || '|' || COALESCE(scheduled_on,'') "
             " FROM interview_rounds ir WHERE ir.role_id=roles.id "
             " ORDER BY (status='scheduled') DESC, "
             "          CASE WHEN status='scheduled' THEN COALESCE(scheduled_on,'9999-12-31') END, "
             "          round_number DESC, id DESC LIMIT 1) AS current_step")


def tag_condition(tag: str) -> tuple[str, list]:
    """A role matches an interest tag if the AI tagged it, or (not screened yet)
    the tag appears as a whole word in the title or description."""
    word = r"\b" + re.escape(tag) + r"\b"
    return ("(ai_tags LIKE ? OR (ai_tags IS NULL AND (title REGEXP ? OR description REGEXP ?)))",
            [f'%"{tag}"%', word, word])


# Roles in "Not a fit" because the AI screen put them there (not because you did): the
# entry that moved the role into Not a fit (its first Not a fit entry after the last entry
# with any other status) was recorded with source='ai'. Later notes on the role (a
# description added, archived, a step) are logged with its current status and don't count.
AI_REJECTED_SQL = ("(roles.status='not_a_fit' AND (SELECT h.source FROM status_history h "
                   "WHERE h.role_id=roles.id AND h.status='not_a_fit' AND h.id > COALESCE("
                   "(SELECT MAX(o.id) FROM status_history o WHERE o.role_id=roles.id AND o.status!='not_a_fit'), 0) "
                   "ORDER BY h.id LIMIT 1) IS 'ai')")


def list_roles(conn, q="", statuses=None, remote=False, paid=False, tags=None,
               added_since=None, sort="status", order="asc", include_archived=False,
               limit=None, offset=0, with_total=False, fits=None, min_pay=None,
               max_years=None, posted_since=None, sites=None, has_description=False,
               step_todo=False, locations=None, companies=None):
    """Filtered, sorted roles. limit/offset page through the results;
    with_total=True also returns how many roles match before paging.

    fits: AI match levels to keep ("unscreened" = not screened yet).
    min_pay: top of the posted pay range at least this (roles without pay are left out).
    max_years: required minimum years at most this (roles that don't say are kept).
    posted_since: date_posted on or after this ISO date.
    sites: job boards to keep ("manual" = roles you added yourself).
    has_description: only roles with a description.
    step_todo: only roles with a step still to do.
    locations: place names ("Brooklyn", "CA"); a role matches if its location contains any of them.
    remote together with locations: remote roles OR roles in those places.
    companies: employer names; a role matches if it's at any of them, under any of the
    employer's names (see companies_match)."""
    # Archived roles are excluded at the SQL level unless asked for, so they are
    # never loaded, counted or searched in the default view.
    where, args = ([] if include_archived else ["COALESCE(archived, 0)=0"]), []
    for tok in (q or "").split():
        like = f"%{tok}%"
        where.append("(title LIKE ? OR company LIKE ? OR location LIKE ? "
                     "OR description LIKE ? OR notes LIKE ? OR site LIKE ? "
                     "OR EXISTS (SELECT 1 FROM role_qa qa WHERE qa.role_id=roles.id "
                     "AND (qa.question LIKE ? OR qa.answer LIKE ?)))")
        args += [like] * 8
    if paid:
        where.append("pay_max IS NOT NULL")
    if tags:
        parts = []
        for t in tags:
            sql, a = tag_condition(t)
            parts.append(sql)
            args += a
        where.append("(" + " OR ".join(parts) + ")")
    if added_since:
        where.append("first_seen >= ?")
        args.append(added_since)
    if fits:
        levels = [f for f in fits if f in MATCH_LEVELS]
        parts = []
        if levels:
            parts.append(f"ai_match IN ({', '.join('?' * len(levels))})")
            args += levels
        if "unscreened" in fits:
            parts.append("ai_match IS NULL")
        if parts:
            where.append("(" + " OR ".join(parts) + ")")
    if min_pay:
        where.append("pay_max >= ?")
        args.append(float(min_pay))
    if max_years is not None:
        where.append("(years_min IS NULL OR years_min <= ?)")
        args.append(int(max_years))
    if posted_since:
        where.append("date_posted >= ?")
        args.append(posted_since)
    if sites:
        where.append(f"COALESCE(site, 'manual') IN ({', '.join('?' * len(sites))})")
        args += list(sites)
    if has_description:
        where.append("description IS NOT NULL AND TRIM(description) != ''")
    if step_todo:
        where.append("EXISTS (SELECT 1 FROM interview_rounds ir WHERE ir.role_id=roles.id "
                     "AND ir.status='scheduled')")
    # Location: Remote and places are alternatives (OR): "Remote + Brooklyn" shows remote
    # roles and Brooklyn roles. Places match whole words, any case ("CA" matches
    # "San Jose, CA" but not "Chicago").
    locs = [l.strip() for l in (locations or []) if l and l.strip()]
    loc_parts = ["is_remote=1"] if remote else []
    loc_parts += ["location REGEXP ?" for _ in locs]
    if loc_parts:
        where.append("(" + " OR ".join(loc_parts) + ")")
        args += [r"\b" + re.escape(l).replace(r"\ ", r"[\s,.-]+") + r"\b" for l in locs]
    cos = [c.strip() for c in (companies or []) if c and c.strip()]
    if cos:
        where.append("(" + " OR ".join("COMPANY_MATCH(company, ?)" for _ in cos) + ")")
        args += cos
    base_where = " AND ".join(where) or "1=1"

    counts = {k: 0 for k in STATUSES}
    for r in conn.execute(f"SELECT status, COUNT(*) n FROM roles WHERE {base_where} "
                          "GROUP BY status", args):
        counts[r["status"]] = r["n"]

    status_where, status_args = "", []
    if statuses:
        status_where = f" AND status IN ({', '.join('?' * len(statuses))})"
        status_args = list(statuses)
    sort_sql = SORTS.get(sort, SORTS["status"])
    direction = "ASC" if order == "asc" else "DESC"
    page_sql, page_args = "", []
    if limit:
        page_sql = " LIMIT ? OFFSET ?"
        page_args = [int(limit), max(0, int(offset or 0))]
    rows = conn.execute(
        f"SELECT {LIST_COLS} FROM roles WHERE {base_where}{status_where} "
        f"ORDER BY ({sort_sql}) IS NULL, {sort_sql} {direction}, "
        # tie-breakers: best AI match first, then newest posting
        f"({SORTS['fit']}) IS NULL, {SORTS['fit']}, date_posted DESC, first_seen DESC, id DESC"
        f"{page_sql}",
        args + status_args + page_args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ai_tags"] = _json_or(d.get("ai_tags"), [])
        out.append(d)
    if with_total:
        total = conn.execute(f"SELECT COUNT(*) FROM roles WHERE {base_where}{status_where}",
                             args + status_args).fetchone()[0]
        return out, counts, total
    return out, counts


# ----------------------------------------------------------------------------
# Archive
# ----------------------------------------------------------------------------
_COUNTRY_ONLY = {"usa", "us", "united states", "u.s.", "anywhere"}


def common_places(conn, limit: int | None = None) -> list[dict]:
    """Every city your (non-archived) roles are in, most common first, as
    [{"name": "New York", "count": 44}, ...], for the sidebar's location filter.
    Remote listings count under the city they name ("Remote (Dallas, TX, USA)" -> Dallas);
    a plain "Remote" or "Remote (USA)" has no city (the Remote chip covers those)."""
    counts: dict[str, int] = {}
    names: dict[str, str] = {}          # lower-case key -> first spelling seen
    for (loc,) in conn.execute("SELECT location FROM roles WHERE COALESCE(archived,0)=0 AND location IS NOT NULL"):
        text = loc.strip()
        if text.lower().startswith("remote"):
            inner = re.search(r"\((.*)\)", text)
            text = inner.group(1).strip() if inner else ""
            if text.lower() in _COUNTRY_ONLY:
                text = ""
        # "New York, NY" and the typo'd "New York. NY" are both "New York"
        city = re.split(r",|\.\s", text)[0].strip().rstrip(".")
        if city:
            key = city.lower()
            names.setdefault(key, city)
            counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"name": names[k], "count": n} for k, n in (top[:limit] if limit else top)]


def set_archived(conn, role_id: int, archived: bool):
    row = conn.execute("SELECT status FROM roles WHERE id=?", (role_id,)).fetchone()
    if not row:
        return None
    conn.execute("UPDATE roles SET archived=?, archived_at=?, updated_at=? WHERE id=?",
                 (1 if archived else 0, now_iso() if archived else None, now_iso(), role_id))
    add_history(conn, role_id, row["status"], "Archived" if archived else "Unarchived")
    return get_role(conn, role_id)


def auto_archive_not_a_fit(conn, prefs: dict | None = None) -> int:
    """Archive roles that have been Not a fit for at least the number of days in
    Preferences → Archiving (counted from when they last became Not a fit). A role you
    unarchived yourself after that is left alone. Returns how many were archived."""
    rule = (prefs or get_prefs(conn)).get("auto_archive") or {}
    if not rule.get("enabled", True) or not rule.get("days"):
        return 0
    cutoff = (dt.datetime.now() - dt.timedelta(days=int(rule["days"]))).isoformat(timespec="seconds")
    # "since": when it last became Not a fit = its first Not a fit history entry after the
    # last entry with any other status (later notes on a Not a fit role don't reset it).
    # Entries are ordered by time, then id: a role added and marked Not a fit in the same
    # second has two entries with the same timestamp, and the later one still counts.
    rows = conn.execute(
        "SELECT r.id, (SELECT MIN(h.at) FROM status_history h WHERE h.role_id=r.id AND h.status='not_a_fit' "
        "  AND NOT EXISTS (SELECT 1 FROM status_history o WHERE o.role_id=r.id AND o.status != 'not_a_fit' "
        "                  AND (o.at > h.at OR (o.at = h.at AND o.id > h.id)))) AS since "
        "FROM roles r WHERE r.status='not_a_fit' AND COALESCE(r.archived,0)=0").fetchall()
    n = 0
    for r in rows:
        since = r["since"]
        if not since or since > cutoff:
            continue
        unarchived_later = conn.execute(
            "SELECT 1 FROM status_history WHERE role_id=? AND note='Unarchived' AND at >= ?",
            (r["id"], since)).fetchone()
        if unarchived_later:
            continue
        conn.execute("UPDATE roles SET archived=1, archived_at=?, updated_at=? WHERE id=?",
                     (now_iso(), now_iso(), r["id"]))
        add_history(conn, r["id"], "not_a_fit",
                    f"Archived automatically: Not a fit for {int(rule['days'])}+ days")
        n += 1
    return n


# ----------------------------------------------------------------------------
# Interview rounds
# ----------------------------------------------------------------------------
def list_rounds(conn, role_id: int):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM interview_rounds WHERE role_id=? ORDER BY round_number, id", (role_id,))]


def _round_note(r: dict) -> str:
    kind = ROUND_KINDS.get(r.get("kind") or "", r.get("kind") or "")
    label = ROUND_STATUSES.get(r.get("status") or "", r.get("status") or "")
    return f"Step {r.get('round_number')}{f' ({kind})' if kind else ''}: {label}"


ROUND_FIELDS = ("round_number", "kind", "scheduled_on", "status", "notes")


def check_round(data: dict, new: bool) -> dict:
    """Validate the step fields present in data (blank text becomes None). A new step
    may leave the number and status blank (next number, "To do"); an edit can't clear
    them. Raises ValueError listing every problem."""
    out, errors = {}, []
    for k in ROUND_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if isinstance(v, str):
            v = v.strip() or None
        if v is None:
            if not new and k in ("round_number", "status"):
                errors.append(f"Step {'number' if k == 'round_number' else 'state'} can't be blank.")
                continue
        elif k == "round_number":
            try:
                if isinstance(v, bool) or float(v) != int(float(v)) or int(float(v)) < 1:
                    raise ValueError
                v = int(float(v))
            except (TypeError, ValueError, OverflowError):
                errors.append("Step number must be a whole number (1 or more).")
                continue
        elif k == "kind" and v not in ROUND_KINDS:
            errors.append(f"Unknown step type '{v}'. Use one of: {', '.join(ROUND_KINDS)}.")
            continue
        elif k == "status" and v not in ROUND_STATUSES:
            errors.append(f"Unknown step state '{v}'. Use one of: {', '.join(ROUND_STATUSES)}.")
            continue
        elif k == "scheduled_on":
            try:
                if not isinstance(v, str) or len(v) != 10:
                    raise ValueError
                v = dt.date.fromisoformat(v).isoformat()
            except ValueError:
                errors.append("Step date must be a date like 2026-11-21.")
                continue
        elif k == "notes" and not isinstance(v, str):
            errors.append("Step notes must be text.")
            continue
        out[k] = v
    if errors:
        raise ValueError(" ".join(errors))
    return out


def add_round(conn, role_id: int, data: dict, log_history=True) -> int:
    clean = check_round(data, new=True)
    n = clean.get("round_number")
    if not n:
        n = (conn.execute("SELECT MAX(round_number) FROM interview_rounds WHERE role_id=?",
                          (role_id,)).fetchone()[0] or 0) + 1
    ts = now_iso()
    rec = {"round_number": n, "kind": clean.get("kind"), "scheduled_on": clean.get("scheduled_on"),
           "status": clean.get("status") or "scheduled", "notes": clean.get("notes")}
    cur = conn.execute(
        "INSERT INTO interview_rounds(role_id, round_number, kind, scheduled_on, status, notes, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (role_id, rec["round_number"], rec["kind"], rec["scheduled_on"], rec["status"],
         rec["notes"], ts, ts))
    st = conn.execute("SELECT status FROM roles WHERE id=?", (role_id,)).fetchone()["status"]
    if st in PRE_INTERVIEW_STATUSES:
        # A step means you're in the process: move the role along, so you never
        # have to update the status and the step separately.
        update_role(conn, role_id, {"status": "interviewing"}, note=_round_note(rec))
    elif log_history:
        add_history(conn, role_id, st, _round_note(rec))
    return cur.lastrowid


def _next_step(data: dict) -> dict:
    """The "Got the next step" fields sent with a step update."""
    nxt = data.get("next") or {}
    if not isinstance(nxt, dict):
        raise ValueError("next must be an object.")
    return {k: v for k, v in nxt.items() if k in ROUND_FIELDS}


def update_round(conn, round_id: int, data: dict):
    row = conn.execute("SELECT * FROM interview_rounds WHERE id=?", (round_id,)).fetchone()
    if not row:
        return None
    # Values sent back unchanged (the UI re-sends the step's type and date) aren't
    # re-checked, so a step saved before these checks existed can still be edited.
    fields = check_round({k: v for k, v in data.items() if k not in ROUND_FIELDS or v != row[k]}, new=False)
    if fields:
        fields["updated_at"] = now_iso()
        conn.execute(f"UPDATE interview_rounds SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                     list(fields.values()) + [round_id])
    new = {**dict(row), **fields}
    if "status" in fields and fields["status"] != row["status"]:
        st = conn.execute("SELECT status FROM roles WHERE id=?", (row["role_id"],)).fetchone()["status"]
        add_history(conn, row["role_id"], st, _round_note(new))
    if data.get("create_next") and new.get("status") == "passed":
        add_round(conn, row["role_id"], {**_next_step(data), "status": "scheduled"})
    conn.execute("UPDATE roles SET updated_at=? WHERE id=?", (now_iso(), row["role_id"]))
    return row["role_id"]


def delete_round(conn, round_id: int):
    row = conn.execute("SELECT role_id FROM interview_rounds WHERE id=?", (round_id,)).fetchone()
    if row:
        conn.execute("DELETE FROM interview_rounds WHERE id=?", (round_id,))
    return row["role_id"] if row else None


# ----------------------------------------------------------------------------
# Questions & answers
# ----------------------------------------------------------------------------
QA_STAGES = ("application", "interview")


def _check_text(value, label):
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    return value


def _check_round_of_role(conn, role_id: int, round_id):
    """round_id as an int if it's one of this role's steps; None if blank. Raises ValueError
    for a step that doesn't exist or belongs to another role."""
    if round_id in (None, ""):
        return None
    try:
        if isinstance(round_id, bool):
            raise ValueError
        rid = int(round_id)
    except (TypeError, ValueError):
        raise ValueError("Step id must be a number.") from None
    row = conn.execute("SELECT role_id FROM interview_rounds WHERE id=?", (rid,)).fetchone()
    if not row or row["role_id"] != role_id:
        raise ValueError(f"Step {rid} isn't one of this role's steps.")
    return rid


def add_qa(conn, role_id: int, question: str, answer: str = "", round_id=None, stage=None):
    _check_text(question, "Question")
    _check_text(answer, "Answer")
    if not (question or "").strip():
        return None
    round_id = _check_round_of_role(conn, role_id, round_id)
    if stage is not None and stage not in QA_STAGES:
        raise ValueError(f"Unknown question stage '{stage}'. Use one of: {', '.join(QA_STAGES)}.")
    cur = conn.execute(
        "INSERT INTO role_qa(role_id, round_id, stage, question, answer, created_at) VALUES(?,?,?,?,?,?)",
        (role_id, round_id, stage or ("interview" if round_id else "application"),
         question.strip(), (answer or "").strip() or None, now_iso()))
    return cur.lastrowid


def update_qa(conn, qa_id: int, data: dict):
    row = conn.execute("SELECT role_id FROM role_qa WHERE id=?", (qa_id,)).fetchone()
    if not row:
        return None
    fields = {k: data[k] for k in ("question", "answer", "round_id", "stage") if k in data}
    if "question" in fields:
        _check_text(fields["question"], "Question")
        fields["question"] = (fields["question"] or "").strip()
        if not fields["question"]:
            raise ValueError("Question can't be blank.")
    if "answer" in fields:
        fields["answer"] = (_check_text(fields["answer"], "Answer") or "").strip() or None
    if "round_id" in fields:
        fields["round_id"] = _check_round_of_role(conn, row["role_id"], fields["round_id"])
    if "stage" in fields and fields["stage"] not in QA_STAGES:
        raise ValueError(f"Unknown question stage '{fields['stage']}'. Use one of: {', '.join(QA_STAGES)}.")
    if fields:
        conn.execute(f"UPDATE role_qa SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                     list(fields.values()) + [qa_id])
    return row["role_id"]


def delete_qa(conn, qa_id: int):
    row = conn.execute("SELECT role_id FROM role_qa WHERE id=?", (qa_id,)).fetchone()
    if row:
        conn.execute("DELETE FROM role_qa WHERE id=?", (qa_id,))
    return row["role_id"] if row else None


# ----------------------------------------------------------------------------
# Status transition with stage-specific details (one popup in the UI)
# ----------------------------------------------------------------------------
def transition(conn, role_id: int, data: dict) -> dict | None:
    """Change status and save the extra details collected for that stage:
    applied   -> applied_at, resume_path, Q&A
    interviewing / round update -> an interview round (+ optional next round), Q&A
    offer     -> offer_deadline, offer_salary, offer_details
    any       -> a general note (saved in history)"""
    row = conn.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
    if not row:
        return None
    status = data.get("status") or row["status"]
    note = (_check_text(data.get("note"), "Note") or "").strip() or None
    changes = {"status": status}
    for k in ("applied_at", "resume_path", "offer_deadline", "offer_salary", "offer_details"):
        if k in data and data[k] not in (None, ""):
            changes[k] = data[k]
    update_role(conn, role_id, changes, note=note)

    round_id = None
    rnd = data.get("round")
    if rnd:
        if not isinstance(rnd, dict):
            raise ValueError("round must be an object.")
        if rnd.get("id"):
            round_id = _check_round_of_role(conn, role_id, rnd["id"])
            update_round(conn, round_id, rnd)
        else:
            round_id = add_round(conn, role_id, rnd)
            if rnd.get("create_next") and rnd.get("status") == "passed":
                add_round(conn, role_id, {**_next_step(rnd), "status": "scheduled"})
    qas = data.get("qa") or []
    if not isinstance(qas, list) or not all(isinstance(q, dict) for q in qas):
        raise ValueError("qa must be a list of {question, answer} objects.")
    for qa in qas:
        add_qa(conn, role_id, qa.get("question"), qa.get("answer"),
               round_id=qa.get("round_id") or round_id,
               stage="interview" if (qa.get("round_id") or round_id) else "application")
    return get_role(conn, role_id)
