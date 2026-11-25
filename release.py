"""
Make a release: the version number, changelog and git tag in one step.

    .venv\\Scripts\\python.exe release.py 1.9.0

1. Write what changed under "## [Unreleased]" in CHANGELOG.md as you go.
2. Commit your work, then run this with the new version (MAJOR.MINOR.PATCH: a new feature is
   the next minor version, a fix the next patch, a change that breaks how you use it the next major).
3. It checks the working tree is clean and the version is newer than the latest release,
   turns "Unreleased" into "## [1.9.0] - <today>", sets settings.VERSION, commits those two
   files as "Release 1.9.0" and tags it v1.9.0. Nothing is pushed: it prints the command.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import versions  # noqa: E402

CHANGELOG = HERE / "CHANGELOG.md"
SETTINGS = HERE / "settings.py"
UNRELEASED = "## [Unreleased]"


def git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(HERE), *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode:
        sys.exit(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def release_changelog(text: str, version: str, today: str) -> str:
    """CHANGELOG text with the Unreleased notes moved under the new version (and an empty
    Unreleased section left for the next one)."""
    if UNRELEASED not in text:
        raise ValueError(f"CHANGELOG.md has no '{UNRELEASED}' section.")
    head, rest = text.split(UNRELEASED, 1)
    m = re.search(r"^## \[", rest, re.M)
    notes, tail = (rest[:m.start()], rest[m.start():]) if m else (rest, "")
    if not notes.strip():
        raise ValueError("Nothing is listed under 'Unreleased' in CHANGELOG.md. Write what changed first.")
    return f"{head}{UNRELEASED}\n\n## [{version}] - {today}\n{notes.strip()}\n\n{tail}".rstrip() + "\n"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not versions.parse(argv[0]):
        sys.exit("Usage: release.py MAJOR.MINOR.PATCH   (e.g. release.py 1.9.0)")
    version = ".".join(map(str, versions.parse(argv[0])))
    if git("status", "--porcelain", "--untracked-files=no").strip():
        sys.exit("Commit (or stash) your changes first: the release commit should hold only the version bump.")
    released = versions.newest_first(git("tag", "--list", "v*").split())
    if released and not versions.newer(version, released[0]):
        sys.exit(f"{version} isn't newer than the latest release ({released[0]}).")
    try:
        CHANGELOG.write_text(release_changelog(CHANGELOG.read_text(encoding="utf-8"), version,
                                               dt.date.today().isoformat()), encoding="utf-8")
    except ValueError as e:
        sys.exit(str(e))
    s = SETTINGS.read_text(encoding="utf-8")
    s, n = re.subn(r'^VERSION = "[^"]*"', f'VERSION = "{version}"', s, count=1, flags=re.M)
    if not n:
        sys.exit("Couldn't find VERSION in settings.py.")
    SETTINGS.write_text(s, encoding="utf-8")
    git("add", str(CHANGELOG), str(SETTINGS))
    git("commit", "-q", "-m", f"Release {version}")
    git("tag", "-a", f"v{version}", "-m", f"Version {version}")
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    print(f"Released {version} (tag v{version}). To publish it:\n    git push origin {branch} --follow-tags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
