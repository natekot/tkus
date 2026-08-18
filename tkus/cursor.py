"""Watermark cursor: every token is attributed to exactly one commit.

The cursor records the end of the last attributed window. Each commit claims
usage in (cursor, now] and then advances the cursor -- so nothing is counted
twice and nothing falls in a gap.

The cursor advances in `post-commit`, never in `prepare-commit-msg`. The latter
runs before the commit is finalized and the user may still abort in the editor;
advancing there would silently discard that usage.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from typing import Optional

from .providers.base import format_timestamp, parse_timestamp

STATE_DIR = "tkus"
CURSOR_FILE = "cursor.json"
PENDING_FILE = "pending.json"


def git_dir(repo_root: str) -> str:
    """Resolve the git directory.

    Uses rev-parse rather than assuming `<root>/.git`, because in a worktree
    `.git` is a file pointing elsewhere.
    """
    out = subprocess.check_output(
        ["git", "rev-parse", "--absolute-git-dir"], cwd=repo_root
    )
    return out.decode().strip()


def state_dir(repo_root: str, create: bool = True) -> str:
    """Where tkus keeps its per-repo state.

    `create=False` for read paths: a query like `tkus report` must not leave a
    directory behind in a repository it was only asked to look at.
    """
    path = os.path.join(git_dir(repo_root), STATE_DIR)
    if create and not os.path.isdir(path):
        os.makedirs(path)
    return path


def _read(path: str) -> dict:
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def read_cursor(repo_root: str) -> dict:
    return _read(os.path.join(state_dir(repo_root, create=False), CURSOR_FILE))


def cursor_since(repo_root: str, amending: bool = False) -> Optional[datetime]:
    """Window start. When amending, roll back to the previous cursor so the
    amended commit recomputes its own window instead of double-counting.

    A missing `prev_ts` means this was the first promotion, i.e. the commit
    being amended is the first one -- so the window reopens from the beginning.
    Falling back to `last_ts` here would start the amend *after* the window the
    original commit already claimed and silently drop all of it.
    """
    state = read_cursor(repo_root)
    value = state.get("prev_ts" if amending else "last_ts")
    return parse_timestamp(value) if value else None


def write_pending(repo_root, window_end, amending=False, detail=None):
    # type: (str, datetime, bool, Optional[dict]) -> None
    """Record the in-flight window, plus the detail post-commit will file.

    `detail` rides along because the commit SHA does not exist yet: only
    post-commit can key a ledger entry to it.
    """
    payload = {"window_end": format_timestamp(window_end), "amending": bool(amending)}
    if detail is not None:
        payload["detail"] = detail
    _write(os.path.join(state_dir(repo_root), PENDING_FILE), payload)


def read_pending(repo_root: str) -> dict:
    return _read(os.path.join(state_dir(repo_root, create=False), PENDING_FILE))


def promote_pending(repo_root: str) -> Optional[datetime]:
    """Advance the cursor to the pending window end. Called from post-commit.

    Returns the new cursor value, or None if there was nothing pending (for
    example a commit made outside the hook)."""
    directory = state_dir(repo_root)
    pending_path = os.path.join(directory, PENDING_FILE)
    pending = _read(pending_path)
    window_end = pending.get("window_end")
    if not window_end:
        return None

    state = read_cursor(repo_root)
    new_state = {"last_ts": window_end}
    if pending.get("amending"):
        # An amend re-attributes the same window, so the rollback point must
        # stay put. Shifting it would make a second amend start from the first
        # amend's own end and drop everything before it.
        if state.get("prev_ts"):
            new_state["prev_ts"] = state["prev_ts"]
    elif state.get("last_ts"):
        # Retained so `git commit --amend` can recompute from the prior window.
        new_state["prev_ts"] = state["last_ts"]
    elif state.get("prev_ts"):
        new_state["prev_ts"] = state["prev_ts"]

    _write(os.path.join(directory, CURSOR_FILE), new_state)
    try:
        os.remove(pending_path)
    except OSError:
        pass
    return parse_timestamp(window_end)


def reset(repo_root: str) -> None:
    directory = state_dir(repo_root, create=False)
    for name in (CURSOR_FILE, PENDING_FILE):
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass
