"""
Runtime configuration, read from environment variables or job_search/.env.

  ANTHROPIC_API_KEY   key for the AI screen
  JOBS_HOST           interface to listen on (default 127.0.0.1 = this computer only)
  JOBS_PORT           port (default 5055)
  JOBS_DB             path to the SQLite database (default job_search/jobs.db)
  JOBS_RESUME_DIR     folder scanned for resume files (default job_search/resumes; a relative
                      path is taken from the job_search folder, e.g. ".." for the folder above)
  JOBS_THREADS        web server worker threads (default 8)
  JOBS_BACKUP_DIR     folder for daily database backups (default %LOCALAPPDATA%/JobTracker/backups,
                      which is outside OneDrive)
  JOBS_ALLOWED_HOSTS  extra host names the web server answers to, comma-separated
                      (only needed if you open it from another device)
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"


def load_env(path: Path = ENV_FILE):
    """Load KEY=value lines from .env without overriding real environment variables."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and not os.environ.get(k):
            os.environ[k] = v


load_env()

HOST = os.environ.get("JOBS_HOST", "127.0.0.1")
PORT = int(os.environ.get("JOBS_PORT", "5055"))
THREADS = int(os.environ.get("JOBS_THREADS", "8"))
DB_PATH = Path(os.environ.get("JOBS_DB") or HERE / "jobs.db")
# Only this folder (and its subfolders) is offered in the resume picker and served by
# /api/file, so keep it to your resumes rather than a whole home folder.
RESUME_DIR = (HERE / (os.environ.get("JOBS_RESUME_DIR") or "resumes")).resolve()
LOG_DIR = HERE / "logs"
BACKUP_DIR = Path(os.environ.get("JOBS_BACKUP_DIR")
                  or Path(os.environ.get("LOCALAPPDATA") or HERE) / "JobTracker" / "backups")
BACKUP_KEEP = 14  # daily backups to keep

# Host names the server answers to. Requests for any other Host are refused, which
# blocks DNS-rebinding pages from reading the API from your browser.
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"} | {
    h.strip().lower() for h in os.environ.get("JOBS_ALLOWED_HOSTS", "").split(",") if h.strip()}
if HOST not in ("0.0.0.0", "::", ""):
    ALLOWED_HOSTS.add(HOST.lower())
VERSION = "1.0.0"
