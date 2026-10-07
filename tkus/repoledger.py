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

Usage that belongs to no branch is filed by `tkus tag` instead, one file per
occurrence for the same reason:

    .tkus/<identity>/.tags/<name>/<until>.jsonl

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
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import ledger

LEDGER_DIR = ".tkus"

# Where `tkus tag` files usage that belongs to no branch. A leading dot is what
# makes it safe: git rejects a branch name with any component starting with
# one, and _sanitize strips it from identities, so neither can ever land here.
TAGS_DIR = ".tags"

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
    return _branch_path(value.strip())


def _branch_path(name: str) -> str:
    # Slashes are kept: `feature/x` becomes a nested directory, which git and
    # every supported filesystem handle.
    parts = [_sanitize(p, "-") for p in name.split("/")]
    return "/".join(p for p in parts if p) or "detached"


_RENAMED = re.compile(r"^Branch: renamed refs/heads/(.+) to refs/heads/(.+)$")


ALIAS_KEY = "tkusRenamedFrom"


def renamed_from(repo_root: str) -> List[str]:
    """Every name the current branch had before a `git branch -m`, oldest first,
    then any recorded with `tkus rename`.

    Read from the branch's reflog, which git carries across a rename -- so a
    chain a -> b -> c yields both a and b from c's log. The reflog is local, so
    a rename made in another clone is invisible here, as is a branch cut from
    the old one before deleting it; `tkus rename` records those by hand.
    """
    ref = _git(repo_root, ["symbolic-ref", "HEAD"])
    if not ref or not ref.strip():
        return []
    text = _git(repo_root, ["reflog", "show", "--format=%gs", ref.strip(), "--"])
    names = []
    for line in reversed((text or "").split("\n")):
        match = _RENAMED.match(line.strip())
        if match and match.group(1) not in names:
            names.append(match.group(1))
    short = ref.strip()[len("refs/heads/"):]
    return names + [n for n in aliases(repo_root, short) if n not in names]


def aliases(repo_root: str, branch: str) -> List[str]:
    """Names recorded by `tkus rename` as this branch's former ones.

    Kept in the branch's own config section because git maintains it for us:
    `git branch -m` carries it to the new name and `git branch -D` deletes it,
    so an alias can neither strand itself nor outlive its branch.
    """
    text = _git(repo_root, ["config", "--get-all",
                            "branch.%s.%s" % (branch, ALIAS_KEY)])
    return [line.strip() for line in (text or "").split("\n") if line.strip()]


def add_alias(repo_root: str, branch: str, old: str) -> None:
    if old not in aliases(repo_root, branch):
        _git(repo_root, ["config", "--add", "branch.%s.%s" % (branch, ALIAS_KEY), old])


def branch_exists(repo_root: str, name: str) -> bool:
    return bool(_git(repo_root, ["rev-parse", "--verify", "-q", "refs/heads/" + name]))


def entry_key(entry: dict) -> Optional[tuple]:
    """What identifies an entry across rewrites of the line that holds it.

    Excludes the priced fields, so a re-priced line is still the same entry.
    None for an entry with no timestamp, which cannot be told apart from
    another one and so must never be merged with it.
    """
    stamp = entry.get("until") or entry.get("at")
    if not stamp:
        return None
    return (stamp, entry.get("since"), entry.get("parent"))


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


def write_entry(repo_root, entry, rel_path=None, table=None):
    # type: (str, Optional[dict], Optional[str], object) -> str
    """Rebuild the ledger from HEAD plus this entry, and stage it.

    Staging is what puts the file in *this* commit rather than the next one.
    With no entry it only folds in a renamed branch's file (see pending_fold).

    Given the rate table, rows recorded before their model had a rate are
    priced on the way through. That is not optional for `tkus reprice --fill`:
    HEAD still holds them unpriced, so a rebuild without it would quietly undo
    a fill staged for this commit.
    """
    rel = rel_path or relative_path(repo_root)
    folded, stale = _renamed_entries(repo_root, rel)
    entries = _merge(folded, read_committed(repo_root, rel))
    if table is not None:
        entries = [ledger.fill_unpriced(e, table) or e for e in entries]
    if entry is not None:
        entries.append(entry)

    rewrite(repo_root, rel, entries)
    for old in stale:
        # Only once the new file holds its entries. Staged here for the same
        # reason the new file is: so the move lands in *this* commit.
        _git(repo_root, ["rm", "-q", "--cached", "--ignore-unmatch", "--", old])
        try:
            os.remove(absolute_path(repo_root, old))
        except OSError:
            pass
    return rel


def rewrite(repo_root, rel, entries):
    # type: (str, str, List[dict]) -> None
    """Write a ledger file's entries, one line each, and stage it."""
    path = absolute_path(repo_root, rel)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w", newline="\n") as fh:
        for item in entries:
            fh.write(json.dumps(item, sort_keys=True) + "\n")
    _git(repo_root, ["add", "--", rel])


def _renamed_entries(repo_root, rel):
    # type: (str, str) -> tuple
    """Committed entries from this branch's pre-rename ledger files.

    Without this a `git branch -m old new` starts `new.jsonl` empty, and
    everything before the rename stays in `old.jsonl` -- still in the tree, but
    `tkus log` for `new` never reads it, so a branch's largest entry can vanish
    from its own report. Returns (entries, paths to remove).

    Only for this identity, and only when the old name is gone: a branch
    recreated under the old name owns that file again.
    """
    prefix = "%s/%s/" % (LEDGER_DIR, identity(repo_root))
    if rel != prefix + branch_name(repo_root) + ".jsonl":
        return [], []
    entries, stale = [], []
    for name in renamed_from(repo_root):
        old = prefix + _branch_path(name) + ".jsonl"
        if old == rel or old in stale:
            continue
        if branch_exists(repo_root, name):
            continue
        found = read_committed(repo_root, old)
        if found:
            entries = _merge(entries, found)
            stale.append(old)
    return entries, stale


def pending_fold(repo_root: str, rel: str) -> bool:
    """Whether a renamed branch's file is waiting to be folded into `rel`.

    Checked on commits with no agent usage, which otherwise write nothing -- so
    without it the split would last until some commit happened to use an
    agent, and `tkus log` would be wrong for every commit before that one.
    """
    return bool(_renamed_entries(repo_root, rel)[1])


def branch_file(repo_root: str, branch: str) -> str:
    """This identity's ledger path for a branch other than the current one."""
    return "%s/%s/%s.jsonl" % (LEDGER_DIR, identity(repo_root), _branch_path(branch))


def clean_tag(name: str) -> str:
    """A tag name as its path will spell it: sanitised like a branch name."""
    return _branch_path(name)


def tag_file(repo_root: str, name: str, until: datetime) -> str:
    """This identity's file for one tag's usage up to `until`.

    One file per occurrence rather than per name, because names are reused:
    two open pull requests each adding a line to one shared `strategy.jsonl`
    would conflict as soon as the second merged. That is the same reason
    branches get a file each.
    """
    stamp = until.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")[:-3] + "Z"
    return "%s/%s/%s/%s/%s.jsonl" % (LEDGER_DIR, identity(repo_root), TAGS_DIR,
                                     clean_tag(name), stamp)


def tag_name(rel: str) -> Optional[str]:
    """The tag a ledger path holds, or None for a branch's file."""
    parts = rel.split("/")
    if len(parts) < 5 or parts[0] != LEDGER_DIR or parts[2] != TAGS_DIR:
        return None
    return "/".join(parts[3:-1])


def _merge(first: List[dict], then: List[dict]) -> List[dict]:
    """`first` followed by whatever of `then` it does not already hold -- so an
    old file someone already merged by hand is not counted twice."""
    seen = {entry_key(e) for e in first} - {None}
    return first + [e for e in then if entry_key(e) is None or entry_key(e) not in seen]


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
