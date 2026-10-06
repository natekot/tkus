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
from typing import List, Optional

from .providers.base import format_timestamp, parse_timestamp

STATE_DIR = "tkus"
CURSOR_FILE = "cursor.json"
PENDING_FILE = "pending.json"
TAGS_FILE = "tags.json"


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


def write_pending(repo_root, window_end, amending=False, detail=None, tags=None):
    # type: (str, datetime, bool, Optional[dict], Optional[List[dict]]) -> None
    """Record the in-flight window, plus the detail post-commit will file.

    `detail` rides along because the commit SHA does not exist yet: only
    post-commit can key a ledger entry to it. `tags` are the markers this
    commit filed, for post-commit to clear once the commit exists.
    """
    payload = {"window_end": format_timestamp(window_end), "amending": bool(amending)}
    if detail is not None:
        payload["detail"] = detail
    if tags:
        payload["tags"] = tags
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
    clear_tags(repo_root, pending.get("tags") or [])
    try:
        os.remove(pending_path)
    except OSError:
        pass
    return parse_timestamp(window_end)


def read_tags(repo_root: str) -> List[dict]:
    """Pending `tkus tag` markers, oldest first: {"name", "until"} each.

    A marker is not an entry. It only says where a named stretch of usage
    ended; the next commit splits its window there. Keeping it that way means
    the cursor still advances only once a commit exists, and undoing a tag is
    a deletion.
    """
    data = _read(os.path.join(state_dir(repo_root, create=False), TAGS_FILE))
    tags = data.get("tags")
    if not isinstance(tags, list):
        return []
    return [t for t in tags if isinstance(t, dict) and t.get("name")
            and parse_timestamp(t.get("until"))]


def _write_tags(repo_root: str, tags: List[dict]) -> None:
    path = os.path.join(state_dir(repo_root), TAGS_FILE)
    if tags:
        _write(path, {"tags": tags})
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def add_tag(repo_root: str, name: str, until: datetime) -> bool:
    """Mark usage up to `until` as `name`. True when it extended the newest
    marker instead -- tagging the same work twice is one stretch, not two."""
    tags = read_tags(repo_root)
    extended = bool(tags) and tags[-1]["name"] == name
    if extended:
        tags[-1]["until"] = format_timestamp(until)
    else:
        tags.append({"name": name, "until": format_timestamp(until)})
    _write_tags(repo_root, tags)
    return extended


def drop_last_tag(repo_root: str) -> Optional[dict]:
    tags = read_tags(repo_root)
    if not tags:
        return None
    dropped = tags.pop()
    _write_tags(repo_root, tags)
    return dropped


def clear_tags(repo_root: str, filed: List[dict]) -> None:
    """Drop exactly the markers a commit filed, and no others."""
    done = {(t.get("name"), t.get("until")) for t in filed if isinstance(t, dict)}
    if not done:
        return
    tags = read_tags(repo_root)
    keep = [t for t in tags if (t["name"], t["until"]) not in done]
    if len(keep) != len(tags):
        _write_tags(repo_root, keep)


def reset(repo_root: str) -> None:
    directory = state_dir(repo_root, create=False)
    for name in (CURSOR_FILE, PENDING_FILE, TAGS_FILE):
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass
