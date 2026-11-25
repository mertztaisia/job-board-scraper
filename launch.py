"""
Start the tracker at a chosen version (the latest release unless you say otherwise).

    .venv\\Scripts\\python.exe launch.py                          latest release (default)
    .venv\\Scripts\\python.exe launch.py --version 1.6.0          a specific release (v1.6.0 works too)
    .venv\\Scripts\\python.exe launch.py --version local          the code in this folder as it is now,
                                                                 unreleased changes included
    .venv\\Scripts\\python.exe launch.py --list                   list the releases
    .venv\\Scripts\\python.exe launch.py --version 1.6.0 jobs.py list
                                                     run another script (default serve.py) at that version

Releases are the git tags vX.Y.Z (see CHANGELOG.md). A release other than the code checked
out here is unpacked once into JOBS_VERSIONS_DIR (default %LOCALAPPDATA%\\JobTracker\\versions)
and run from there with this folder's settings (.env, database, resumes, backups) and
virtual environment. service.ps1 and start_tracker.bat start the tracker through this.

Your data only moves forward: each release knows the newest database schema it can use
(db.SCHEMA_VERSION), and one whose schema is older than your database's refuses to start
instead of touching it (restore the backup saved before the upgrade to go back; see
"Database versions" in db.py).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402  (also loads .env into the environment)
import versions  # noqa: E402

LOCAL = "local"
SCRIPTS = ("serve.py", "app.py", "jobs.py", "daily_jobs.py")


class LaunchError(Exception):
    pass


def say(msg: str):
    """Print, or with pythonw (no console, e.g. the background service) append to the log."""
    if sys.stdout is not None:
        print(msg, flush=True)
        return
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(settings.LOG_DIR / "launch.log", "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def git(*args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(HERE), *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        raise LaunchError(f"Couldn't run git ({e}). Install Git, or use --version local.") from None
    if out.returncode:
        raise LaunchError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def releases() -> list[str]:
    """Release versions (git tags vX.Y.Z), newest first."""
    return versions.newest_first(git("tag", "--list", "v*").split())


def checked_out_release() -> str | None:
    """The release this folder has checked out, if it's exactly a release with no changes."""
    try:
        tags = git("tag", "--points-at", "HEAD").split()
        dirty = git("status", "--porcelain", "--untracked-files=no").strip()
    except LaunchError:
        return None
    found = versions.newest_first(tags)
    return found[0] if found and not dirty else None


def resolve(requested: str | None) -> tuple[str, Path]:
    """(version label, folder to run from) for --version: None/"latest", "local" or "X.Y.Z"."""
    req = (requested or "latest").strip().lower()
    if req in (LOCAL, "dev"):
        return LOCAL, HERE
    try:
        rel = releases()
    except LaunchError:
        if req == "latest":
            say("Git isn't available, so the code in this folder is used (--version local).")
            return LOCAL, HERE
        raise
    if req == "latest":
        if not rel:
            say("There are no releases yet, so the code in this folder is used (--version local).")
            return LOCAL, HERE
        version = rel[0]
    else:
        if not versions.parse(req):
            raise LaunchError(f"'{requested}' isn't a version. Use a release like 1.6.0, 'latest' or 'local'.")
        version = ".".join(map(str, versions.parse(req)))
        if version not in rel:
            raise LaunchError(f"There's no release {version}. Releases: {', '.join(rel) or 'none'}.")
    if checked_out_release() == version:
        return version, HERE
    return version, unpack(version)


def unpack(version: str) -> Path:
    """The release's files in VERSIONS_DIR/<version> (unpacked from git the first time)."""
    root = settings.VERSIONS_DIR
    dest = root / version
    if (dest / "serve.py").exists():
        return dest
    root.mkdir(parents=True, exist_ok=True)
    partial = root / f"{version}.partial"
    if partial.exists():
        shutil.rmtree(partial)          # a previous unpack that didn't finish (inside VERSIONS_DIR)
    partial.mkdir()
    archive = root / f"{version}.tar"
    say(f"Unpacking release {version} into {dest} ...")
    git("archive", "--format=tar", "-o", str(archive), f"v{version}")
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(partial, filter="data")
    finally:
        archive.unlink(missing_ok=True)
    partial.rename(dest)
    return dest


def database_info() -> dict:
    """Your database's schema version and the newest app version that has used it, read
    without changing anything: {"schema": int, "app_version": str|None}, or {} if there's none."""
    path = Path(settings.DB_PATH)
    if not path.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            schema = con.execute("PRAGMA user_version").fetchone()[0]
            try:
                row = con.execute("SELECT value FROM settings WHERE key='app_version'").fetchone()
            except sqlite3.Error:
                row = None
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {"schema": schema, "app_version": row[0] if row else None}


def release_schema(version: str) -> int | None:
    """The newest database schema a release can use (its db.SCHEMA_VERSION)."""
    try:
        source = git("show", f"v{version}:db.py")
    except LaunchError:
        return None
    m = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", source, re.M)
    return int(m.group(1)) if m else None


def check_database(version: str):
    """Refuse a release that doesn't know your database's schema. (Releases from 1.8.0 check
    this themselves too; older ones don't, so the launcher checks for them.)"""
    if version == LOCAL:
        return                      # this folder's code checks for itself when it opens the database
    info, knows = database_info(), release_schema(version)
    if info and knows is not None and info["schema"] > knows:
        raise LaunchError(
            f"Your database has schema version {info['schema']}"
            + (f" (last used by version {info['app_version']})" if info.get("app_version") else "")
            + f", but release {version} only knows up to {knows}, so it won't be started on it. "
            f"To go back to {version}, restore the copy saved before the upgrade (in {settings.BACKUP_DIR}) "
            f"and point JOBS_DB at it, or run a newer release.")


def child_env(label: str) -> dict:
    """This folder's settings, spelled out so a release unpacked elsewhere uses the same
    database, resumes, backups and logs (it has no .env of its own)."""
    env = dict(os.environ)   # includes .env, loaded by settings
    env.update({
        "JOBS_DB": str(Path(settings.DB_PATH).resolve()),
        "JOBS_RESUME_DIR": str(settings.RESUME_DIR),
        "JOBS_BACKUP_DIR": str(settings.BACKUP_DIR),
        "JOBS_LOG_DIR": str(settings.LOG_DIR),
        "JOBS_LAUNCHED_AS": label,
    })
    return env


def requirements_differ(folder: Path) -> bool:
    try:
        return (folder / "requirements.txt").read_text(encoding="utf-8") != \
            (HERE / "requirements.txt").read_text(encoding="utf-8")
    except OSError:
        return False


# ----------------------------------------------------------------------------
# Restart on code changes (local only): your edits show up right away
# ----------------------------------------------------------------------------
RELOAD_POLL = 1.0   # seconds between checks for changed files


def code_snapshot(folder: Path) -> dict:
    """When each of the folder's Python files (and .env) last changed. Page files (static/)
    aren't included: the server sends their newest version on every reload anyway."""
    files = list(folder.glob("*.py")) + [folder / ".env"]
    snap = {}
    for f in files:
        try:
            snap[f.name] = f.stat().st_mtime
        except OSError:
            pass
    return snap


def run_in_progress() -> bool:
    """True while a pull, re-screen or description fetch holds the run lock."""
    import db
    try:
        with db.run_lock():
            return False
    except db.RunInProgress:
        return True


def _stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_with_reload(cmd: list, folder: Path, env: dict, poll: float = RELOAD_POLL,
                    sleep=None, busy=run_in_progress, start=None) -> int:
    """Run cmd, and restart it whenever a .py file (or .env) in folder changes. A pull or
    re-screen that's running is allowed to finish first; if the code fails to start, it's
    started again on the next save instead of giving up."""
    import time
    sleep = sleep or time.sleep
    start = start or (lambda: subprocess.Popen(cmd, cwd=str(folder), env=env))
    say("Watching this folder: the tracker restarts when you save a .py file (or .env).")
    seen = code_snapshot(folder)
    proc = start()
    waiting = crashed = False
    def changed_files(old: dict, new: dict) -> list[str]:
        # Saved (newer time) or new files. A file that briefly can't be read (OneDrive and
        # virus scanners lock files for a moment) is not a change.
        return sorted(name for name, t in new.items() if old.get(name) != t)

    try:
        while True:
            sleep(poll)
            code = proc.poll()
            now = {**seen, **code_snapshot(folder)}
            changed = changed_files(seen, now)
            if not changed:
                if code is not None and not crashed:
                    if code == 0:
                        return 0
                    say(f"The tracker stopped (exit code {code}). It starts again when you save a fix.")
                    crashed = True
                continue
            if code is None and busy():
                if not waiting:
                    say(f"{', '.join(changed)} changed; waiting for the running pull or re-screen to finish "
                        "before restarting.")
                    waiting = True
                continue
            sleep(0.3)                     # let an editor finish saving several files
            seen, waiting, crashed = {**now, **code_snapshot(folder)}, False, False
            if code is None:
                say(f"{', '.join(changed)} changed; restarting the tracker.")
                _stop(proc)
                sleep(0.5)                 # let the port go
            else:
                say(f"{', '.join(changed)} changed; starting the tracker again.")
            proc = start()
    except KeyboardInterrupt:
        _stop(proc)
        return 130


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Start the job tracker at a chosen version.")
    ap.add_argument("--version", "-v", help="a release (e.g. 1.6.0), 'latest' (default) or 'local'")
    ap.add_argument("--list", action="store_true", help="list the releases and exit")
    ap.add_argument("--no-reload", action="store_true",
                    help="with --version local: don't restart when the code changes")
    ap.add_argument("script", nargs="?", default="serve.py", choices=SCRIPTS,
                    help="what to run at that version (default serve.py, the web app)")
    ap.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the script")
    a = ap.parse_args(argv)
    try:
        if a.list:
            rel = releases()
            here = checked_out_release()
            info = database_info()
            say("Releases, newest first (and the database schema each one uses):")
            for v in rel:
                schema = release_schema(v)
                usable = not info or schema is None or schema >= info["schema"]
                say(f"  {v:8} schema {schema if schema is not None else '?'}"
                    + ("" if usable else "   (older than your database: won't start on it)"))
            if not rel:
                say("  none yet")
            import db
            say(f"Code in this folder: {settings.VERSION}, schema {db.SCHEMA_VERSION}"
                + ("" if here else " (plus unreleased changes: --version local)"))
            if info:
                say(f"Your database: schema {info['schema']}"
                    + (f", last used by {info['app_version']}" if info.get("app_version") else "")
                    + f" ({settings.DB_PATH})")
            return 0
        label, folder = resolve(a.version)
        check_database(label)
    except LaunchError as e:
        say(f"Can't start: {e}")
        return 2
    if folder != HERE and requirements_differ(folder):
        say(f"Note: release {label} lists different packages in requirements.txt; it runs with this "
            "folder's virtual environment.")
    say(f"Job Tracker {label} ({folder})")
    cmd = [sys.executable, str(folder / a.script), *a.args]
    # Your own code: restart the web app whenever you save a change (off with --no-reload
    # or JOBS_RELOAD=0). Releases don't change, so they just run.
    reload = (label == LOCAL and a.script in ("serve.py", "app.py") and not a.no_reload
              and os.environ.get("JOBS_RELOAD", "1") != "0")
    if reload:
        return run_with_reload(cmd, folder, child_env(label))
    try:
        return subprocess.call(cmd, cwd=str(folder), env=child_env(label))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
