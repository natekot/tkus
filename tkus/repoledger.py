"""The repository-tracked ledger.

Cost is recorded as a tracked file rather than in the commit message, because
**squash preserves the tree**. GitHub's squash+merge discards commit messages --
its default keeps only the PR title and a list of commit subjects -- but it keeps
every file change. A tracked file therefore survives squash, rebase, cherry-pick,
and amend, and needs nothing running outside the developer's machine.

    .tkus/<identity>/<branch>.jsonl

Per branch so concurrent pull requests never touch the same file, which is the
merge-conflict problem that rules out a single shared ledger. Per identity so two
people on similarly-named branches stay separate.

The file is **rebuilt from HEAD** on every commit rather than appended to. That
makes an abandoned commit self-correcting: its entry was staged but never
committed, so it is absent from HEAD and the next commit simply overwrites it.
No de-duplication rule, and no way for an abandoned commit to double-count.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Dict, List, Optional

LEDGER_DIR = ".tkus"

# Characters that are illegal in Windows filenames. Git permits some of them in
# branch names, and the primary deployment target is Windows.
_UNSAFE = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def _git(repo_root, args, check=False):
    # type: (str, List[str], bool) -> Optional[str]
    try:
        out = subprocess.run(["git"] + args, cwd=repo_root, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", "replace")


def _sanitize(name: str, fallback: str) -> str:
    name = _UNSAFE.sub("-", (name or "").strip())
    name = name.strip(". ")            # Windows rejects trailing dots and spaces
    return name or fallback


def identity(repo_root: str) -> str:
    """Who to file entries under.

    `user.email` is deliberately not used: it would publish email addresses in
    repository paths.
    """
    for key in ("tkus.identity", "user.name"):
        value = _git(repo_root, ["config", "--get", key])
        if value and value.strip():
            return _sanitize(value.strip().replace(os.sep, "-").replace("/", "-"),
                             "unknown")
    return "unknown"


def branch_name(repo_root: str) -> str:
    """Current branch.

    `symbolic-ref` rather than `rev-parse --abbrev-ref`, which returns the literal
    string "HEAD" (and prints a fatal) before the first commit exists. A detached
    HEAD has no branch, so entries are filed under "detached".
    """
    value = _git(repo_root, ["symbolic-ref", "--short", "HEAD"])
    if not value or not value.strip():
        return "detached"
    # Slashes are kept: `feature/x` becomes a nested directory, which git and
    # every supported filesystem handle.
    parts = [_sanitize(p, "-") for p in value.strip().split("/")]
    return "/".join(p for p in parts if p) or "detached"


def relative_path(repo_root: str) -> str:
    return "%s/%s/%s.jsonl" % (LEDGER_DIR, identity(repo_root), branch_name(repo_root))


def absolute_path(repo_root: str, rel_path: Optional[str] = None) -> str:
    rel = rel_path or relative_path(repo_root)
    return os.path.join(repo_root, *rel.split("/"))


def enabled(table) -> bool:
    """Whether cost is committed to the repository. On by default.

    Recording is the entire point of the tool, so the choice is *where* it is
    recorded, not whether. Turning this off keeps the local per-commit ledger in
    `.git/` -- private, but lost with `.git` and shared with nobody.
    """
    if os.environ.get("TKUS_REPO_LEDGER"):
        return os.environ["TKUS_REPO_LEDGER"].strip().lower() not in ("0", "false", "no")
    return bool(table.data.get("repo_ledger", True))


def is_ignored(repo_root: str, rel_path: Optional[str] = None) -> bool:
    """True when .gitignore excludes the ledger path.

    Reaching for `.gitignore` is the natural way to say "not in my repository",
    so it is honoured as a first-class opt-out. Without this the write would
    still happen, `git add` would fail (silently, since hooks must not break
    commits), and a stray ignored file would be left in the working tree.

    Note this only applies while the file is untracked: git ignores .gitignore
    for anything already tracked, so a ledger that is already committed keeps
    being committed until `git rm --cached` removes it.
    """
    rel = rel_path or relative_path(repo_root)
    try:
        out = subprocess.run(["git", "check-ignore", "-q", "--", rel],
                             cwd=repo_root, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def is_tracked(repo_root: str, rel_path: Optional[str] = None) -> bool:
    rel = rel_path or relative_path(repo_root)
    out = _git(repo_root, ["ls-files", "--error-unmatch", "--", rel])
    return bool(out)


def _parse(text: str) -> List[dict]:
    out = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue          # a corrupt line must not lose the rest
        if isinstance(entry, dict):
            out.append(entry)
    return out


def read_committed(repo_root: str, rel_path: Optional[str] = None) -> List[dict]:
    """Entries as of HEAD, ignoring anything merely staged or in the worktree."""
    rel = rel_path or relative_path(repo_root)
    text = _git(repo_root, ["show", "HEAD:%s" % rel])
    return _parse(text) if text else []


def read_worktree(repo_root: str, rel_path: Optional[str] = None) -> List[dict]:
    path = absolute_path(repo_root, rel_path)
    try:
        with open(path, "r", errors="replace") as fh:
            return _parse(fh.read())
    except OSError:
        return []


def write_entry(repo_root: str, entry: dict, rel_path: Optional[str] = None) -> str:
    """Rebuild the ledger from HEAD plus this entry, and stage it.

    Staging is what puts the file in *this* commit rather than the next one.
    """
    rel = rel_path or relative_path(repo_root)
    entries = read_committed(repo_root, rel) + [entry]

    path = absolute_path(repo_root, rel)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w", newline="\n") as fh:
        for item in entries:
            fh.write(json.dumps(item, sort_keys=True) + "\n")

    _git(repo_root, ["add", "--", rel])
    return rel


def all_files(repo_root: str) -> List[str]:
    """Every ledger file tracked in the working tree, newest-branch-agnostic."""
    text = _git(repo_root, ["ls-files", "--", "%s/**/*.jsonl" % LEDGER_DIR,
                            "%s/*.jsonl" % LEDGER_DIR])
    if not text:
        return []
    return [line.strip() for line in text.split("\n") if line.strip()]


def read_all(repo_root: str) -> Dict[str, List[dict]]:
    """Every tracked ledger file, keyed by its repository-relative path."""
    return {rel: read_worktree(repo_root, rel) for rel in all_files(repo_root)}
