"""Platform-aware path comparison.

Windows is the awkward case, and the recorded data proves it. A single machine's
Copilot database contains both `C:\\git\\ctrl` and `c:\\git\\ctrl` -- the drive
letter's case varies between sessions -- while `git rev-parse --show-toplevel`
reports `C:/git/ctrl` with forward slashes. A naive comparison matches none of
those against each other, which would silently attribute nothing at all.

Every function takes an explicit `windows` flag defaulting to the host, so
Windows semantics can be exercised from a POSIX test run. Without that the rules
below could only be verified by deploying to Windows.
"""

from __future__ import annotations

import os
from typing import List, Optional


def on_windows() -> bool:
    return os.name == "nt"


def normalize(path: str, windows: Optional[bool] = None) -> str:
    """Canonical form for comparison.

    On Windows: backslashes become forward slashes and case is folded, because
    the filesystem is case-insensitive and the two separators are interchangeable.
    On POSIX nothing is folded -- `/a/B` and `/a/b` are genuinely different files.
    """
    if windows is None:
        windows = on_windows()
    if not path:
        return ""
    text = path.strip()
    if windows:
        text = text.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if len(text) > 1:
        text = text.rstrip("/")
    if windows:
        text = text.lower()
    return text


def resolve(path: str) -> str:
    """Resolve symlinks when the path exists locally, else leave it alone.

    Guarded deliberately: `os.path.realpath("C:\\git\\x")` on POSIX treats the
    string as relative and prefixes the working directory, corrupting a recorded
    Windows path rather than resolving it.
    """
    try:
        if os.path.exists(path):
            return os.path.realpath(path)
    except (OSError, ValueError):
        pass
    return path


def has_windows_shape(path: str) -> bool:
    """True for a drive-letter or UNC path, whatever host we are running on.

    A POSIX path never looks like this, so shape detection lets a record written
    on Windows be matched correctly even when read elsewhere -- which happens
    whenever agent data is copied between machines.
    """
    if not path:
        return False
    return path.startswith("\\\\") or (len(path) >= 2 and path[1] == ":")


def is_within(child: str, parent: str, windows: Optional[bool] = None) -> bool:
    """True when `child` is `parent` or lives beneath it."""
    if windows is None:
        # Windows rules apply when the host uses them, or when either path is
        # plainly a Windows path. Requiring a drive letter or UNC prefix keeps a
        # backslash inside a POSIX filename from being read as a separator.
        windows = (on_windows()
                   or has_windows_shape(child) or has_windows_shape(parent))
    if not child or not parent:
        return False
    c = normalize(resolve(child), windows)
    p = normalize(resolve(parent), windows)
    if not c or not p:
        return False
    return c == p or c.startswith(p.rstrip("/") + "/")


def looks_absolute(path: str) -> bool:
    """Absolute on either platform: `/x`, `C:\\x`, `C:/x`, or a UNC share."""
    if not path:
        return False
    if path.startswith("/") or path.startswith("\\\\"):
        return True
    return len(path) >= 3 and path[1] == ":" and path[2] in "\\/"


def encode_candidates(path: str) -> List[str]:
    """Plausible agent project-directory names for a repository path.

    Claude Code names its per-project directory after the working directory with
    separators replaced. The exact rule for Windows drive letters and colons is
    not documented, so rather than guess one, try the plausible spellings. These
    are only a fast path for narrowing the search: the authoritative check is the
    `cwd` recorded inside each record, and a miss here falls back to scanning
    every project directory.
    """
    if not path:
        return []
    out = []
    for text in (path, path.replace("\\", "/"), path.replace("/", "\\")):
        for encoded in (
            text.replace("\\", "-").replace("/", "-"),
            # Colon dropped, and colon-as-dash: both appear in the wild for
            # drive-letter paths, and neither is documented.
            text.replace(":", "").replace("\\", "-").replace("/", "-"),
            text.replace(":", "-").replace("\\", "-").replace("/", "-"),
        ):
            if encoded and encoded not in out:
                out.append(encoded)
    return out
