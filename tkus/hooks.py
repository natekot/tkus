"""Hook installation.

Never clobbers an existing hook: an unmanaged one is moved aside to
`<name>.local` and chained to, so a repo that already has hooks keeps them.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from typing import List, Tuple

from .cursor import git_dir

MARKER = "# managed by tkus"

# pre-commit is where the ledger is written, because staging there puts the
# file in *this* commit. prepare-commit-msg is gone: commit messages are
# never modified.
HOOKS = ("pre-commit", "post-commit")

# Hooks tkus installed in earlier versions and no longer uses. Without this an
# upgrade would leave an orphaned hook behind that `uninstall` never removes,
# because it is no longer in HOOKS.
LEGACY_HOOKS = ("prepare-commit-msg",)

# Git for Windows runs hooks through its bundled sh, so one POSIX script serves
# every platform -- but the interpreter path must be quoted (Windows paths
# routinely contain spaces, e.g. "C:/Program Files/Python") and written with
# forward slashes, since a backslash is an escape character to sh.
TEMPLATE = """#!/bin/sh
{marker}
# Regenerate with: tkus install
hook_dir=$(dirname "$0")
if [ -f "$hook_dir/{name}.local" ]; then
    # Windows checkouts often carry no executable bit, so fall back to sh
    # rather than silently skipping a hook the user still relies on.
    if [ -x "$hook_dir/{name}.local" ]; then
        "$hook_dir/{name}.local" "$@" || exit $?
    else
        sh "$hook_dir/{name}.local" "$@" || exit $?
    fi
fi
"{python}" -m tkus hook {name} "$@" || true
"""


def _interpreter(python: str) -> str:
    """Interpreter path in a form sh can execute on any platform."""
    return (python or "python3").replace("\\", "/")


def _hooks_dir(repo_root: str) -> str:
    path = os.path.join(git_dir(repo_root), "hooks")
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def _make_executable(path: str) -> None:
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _is_ours(path: str) -> bool:
    try:
        with open(path, "r", errors="replace") as fh:
            return MARKER in fh.read(4096)
    except OSError:
        return False


def _remove_legacy(directory: str) -> List[str]:
    """Drop hooks tkus used to install, restoring anything they displaced."""
    notes = []
    for name in LEGACY_HOOKS:
        path = os.path.join(directory, name)
        if not os.path.exists(path) or not _is_ours(path):
            continue
        os.remove(path)
        notes.append("removed obsolete %s (tkus no longer writes commit messages)"
                     % name)
        backup = path + ".local"
        if os.path.exists(backup):
            os.rename(backup, path)
            notes.append("restored previous %s" % name)
    return notes


def is_installed(repo_root: str) -> bool:
    """True when every hook tkus needs is present and managed by us."""
    directory = os.path.join(git_dir(repo_root), "hooks")
    return all(_is_ours(os.path.join(directory, name)) for name in HOOKS)


def install(repo_root: str, python: str = None) -> List[str]:
    """Install both hooks. Returns human-readable notes about what happened."""
    python = python or sys.executable or "python3"
    directory = _hooks_dir(repo_root)
    notes = _remove_legacy(directory)

    custom_path = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ).stdout.decode().strip()
    if custom_path:
        notes.append(
            "warning: core.hooksPath is set to %r; git will run hooks from there, "
            "not from the directory just written." % custom_path
        )

    for name in HOOKS:
        path = os.path.join(directory, name)
        if os.path.exists(path) and not _is_ours(path):
            backup = path + ".local"
            if os.path.exists(backup):
                notes.append(
                    "skipped %s: both it and %s.local already exist; "
                    "merge them by hand." % (name, name)
                )
                continue
            os.rename(path, backup)
            _make_executable(backup)
            notes.append("moved existing %s to %s.local (still runs first)" % (name, name))

        # newline="\n" is load-bearing on Windows: the default would translate
        # to CRLF, and sh rejects a CRLF script with `bad interpreter: /bin/sh^M`.
        with open(path, "w", newline="\n") as fh:
            fh.write(TEMPLATE.format(
                marker=MARKER, name=name, python=_interpreter(python)))
        _make_executable(path)
        notes.append("installed %s" % name)

    return notes


def uninstall(repo_root: str) -> List[str]:
    directory = _hooks_dir(repo_root)
    notes = _remove_legacy(directory)
    for name in HOOKS:
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            continue
        if not _is_ours(path):
            notes.append("left %s alone (not managed by tkus)" % name)
            continue
        os.remove(path)
        notes.append("removed %s" % name)
        backup = path + ".local"
        if os.path.exists(backup):
            os.rename(backup, path)
            notes.append("restored previous %s" % name)
    return notes
