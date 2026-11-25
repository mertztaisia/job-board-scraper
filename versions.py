"""
Version numbers for the tracker.

Releases follow semantic versioning (MAJOR.MINOR.PATCH) and are git tags named vX.Y.Z;
CHANGELOG.md says what changed in each. settings.VERSION is the version of the code in
this folder. See launch.py for running a particular version.
"""

from __future__ import annotations

import re

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse(text) -> tuple[int, int, int] | None:
    """'1.6.0' or 'v1.6.0' -> (1, 6, 0); anything else -> None."""
    m = _VERSION_RE.match(str(text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(a, b) -> bool:
    """True if version a is newer than version b (both like '1.6.0')."""
    pa, pb = parse(a), parse(b)
    return pa is not None and pb is not None and pa > pb


def newest_first(versions) -> list[str]:
    """Valid version numbers ('v' dropped), newest first, without duplicates."""
    found = {".".join(map(str, p)) for p in map(parse, versions) if p}
    return sorted(found, key=parse, reverse=True)
