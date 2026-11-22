"""
Production entry point: serves the API + web UI with Waitress (a production
WSGI server) on http://127.0.0.1:5055 by default.

    .venv\\Scripts\\python.exe serve.py          # run in this window (logs to console + file)
    .venv\\Scripts\\pythonw.exe serve.py         # run with no window (how the service runs)

Manage the background service with service.ps1 (install / start / stop / status / logs).
"""

from __future__ import annotations

import logging
import logging.handlers
import socket
import sys
import threading
import time

import settings


def setup_logging():
    settings.LOG_DIR.mkdir(exist_ok=True)
    log_file = settings.LOG_DIR / "server.log"
    handlers = [logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3,
                                                     encoding="utf-8")]
    if sys.stdout is not None:            # pythonw has no console
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        handlers.append(logging.StreamHandler(sys.stdout))
    else:                                  # send stray print() output to the log too
        f = open(log_file, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def backup_loop(log):
    """Back up the database once a day while the server runs (see settings.BACKUP_DIR)."""
    import db
    while True:
        try:
            path = db.backup_if_due()
            if path:
                log.info("Daily backup: %s", path)
        except Exception:
            log.exception("Daily backup failed")
        time.sleep(3600)


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) == 0


def main():
    setup_logging()
    log = logging.getLogger("job-tracker")
    if port_in_use(settings.HOST, settings.PORT):
        log.error("Port %s is already in use. Is the tracker already running? "
                  "(service.ps1 status)", settings.PORT)
        sys.exit(1)
    from waitress import serve
    from app import app
    import db
    log.info("Job Tracker %s on http://%s:%s  (db: %s)", settings.VERSION, settings.HOST,
             settings.PORT, settings.DB_PATH)
    if db.in_synced_folder(settings.DB_PATH):
        log.warning("The database is in a cloud-synced folder (%s). Syncing a live SQLite file "
                    "can corrupt it; set JOBS_DB in .env to a local folder (see README).",
                    settings.DB_PATH.parent)
    db.connect().close()   # run any schema upgrade now, before serving requests
    db.mark_stale_runs()   # runs left 'running' by a previous crash or restart
    threading.Thread(target=backup_loop, args=(log,), daemon=True, name="daily-backup").start()
    serve(app, host=settings.HOST, port=settings.PORT, threads=settings.THREADS,
          ident="job-tracker")


if __name__ == "__main__":
    main()
