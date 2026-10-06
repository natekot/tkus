"""tkus -- attribute AI token usage and cost to git commits.

Cost is recorded in a repository-tracked ledger, never in the commit message.
See tkus/repoledger.py for why: squash+merge discards commit messages but
preserves the tree.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import __version__
from . import cursor as cursor_mod
from . import catalog, hooks, ledger, repoledger
from .pricing import (RateTable, RateTableError, compute_cost,
                      compute_cost_from_totals)
from .providers import claude_code  # noqa: F401  (registers the provider)
from .providers import copilot  # noqa: F401  (registers the provider)
from .providers.base import aggregate_by_model, collect_all, format_timestamp



def repo_root(start: Optional[str] = None) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start or os.getcwd(),
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, OSError):
        sys.exit("tkus: not inside a git repository")
    return out.decode().strip()


def _repo_root_or_none() -> Optional[str]:
    """repo_root() without the exit: rates are global, and are worth reading
    outside a repository even though a repository can override them."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=os.getcwd(), stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError):
        return None
    return out.decode().strip() or None


def _rates_when(args) -> datetime:
    value = getattr(args, "at", None)
    if not value:
        return now()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        sys.exit("tkus: --at expects YYYY-MM-DD, got %r" % value)
    return parsed.replace(tzinfo=timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


def head_sha(root: str) -> Optional[str]:
    """HEAD's sha, or None before the first commit exists.

    The return code has to be checked: on an unborn branch `git rev-parse HEAD`
    exits 128 but still prints the literal string "HEAD" on stdout, which would
    otherwise be recorded as an entry's parent and match no commit ever.
    """
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode().strip() or None


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------

def hook_pre_commit(root: str, argv: List[str]) -> int:
    """Record the window into the tracked ledger and stage it.

    Runs in pre-commit because staging here puts the file in *this* commit
    rather than the next one. Nothing is written to the commit message.
    """
    table = RateTable.load(root)
    window_end = now()
    since = cursor_mod.cursor_since(root)
    records = collect_all(root, since, window_end)
    totals = aggregate_by_model(records)
    if not totals:
        if repoledger.enabled(table):
            rel = repoledger.relative_path(root)
            if repoledger.pending_fold(root, rel) and (
                    repoledger.is_tracked(root, rel)
                    or not repoledger.is_ignored(root, rel)):
                repoledger.write_entry(root, None, rel)
        cursor_mod.write_pending(root, window_end)
        return 0

    cost = compute_cost(records, table, when=None)
    entry = ledger.build_entry(totals, cost, at=window_end)
    entry["since"] = format_timestamp(since) if since else None
    entry["until"] = format_timestamp(window_end)
    # The new commit's SHA does not exist yet; its parent does, and identifies
    # the commit on linear history.
    entry["parent"] = head_sha(root)
    entry.pop("sha", None)

    # The local ledger is always written (via post-commit, which knows the SHA).
    # Only committing it to the repository is optional -- and .gitignore counts
    # as saying no, provided the file is not already tracked.
    if repoledger.enabled(table):
        rel = repoledger.relative_path(root)
        if repoledger.is_tracked(root, rel) or not repoledger.is_ignored(root, rel):
            repoledger.write_entry(root, entry, rel)
    cursor_mod.write_pending(root, window_end, detail=entry)
    return 0


def hook_post_commit(root: str, argv: List[str]) -> int:
    """Advance the cursor, and file the per-SHA detail locally."""
    detail = (cursor_mod.read_pending(root) or {}).get("detail")
    cursor_mod.promote_pending(root)
    if detail:
        sha = head_sha(root)
        if sha:
            detail = dict(detail)
            detail["sha"] = sha
            ledger.append(root, detail)
    return 0


HOOK_DISPATCH = {
    "pre-commit": hook_pre_commit,
    "post-commit": hook_post_commit,
}


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

REPO_CONFIG = ".tkus.json"


def cmd_install(args) -> int:
    root = repo_root()
    if getattr(args, "local_only", False):
        path = os.path.join(root, REPO_CONFIG)
        data = {}
        if os.path.isfile(path):
            try:
                with open(path) as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
        data["repo_ledger"] = False
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")

    for note in hooks.install(root):
        print(note)
    print("tkus installed in %s" % root)

    if repoledger.enabled(RateTable.load(root)):
        print("cost will be committed to %s/ -- visible to everyone with access "
              "to this repository. Use `tkus install --local-only` to keep it in "
              ".git/ instead." % repoledger.LEDGER_DIR)
    else:
        print("local-only: cost is recorded in .git/ and never committed. "
              "It is lost if .git is deleted, and shared with nobody.")
    return 0


def cmd_uninstall(args) -> int:
    for note in hooks.uninstall(repo_root()):
        print(note)
    return 0


def _warn_if_not_installed(root: str) -> None:
    """`report` reads agent transcripts directly, so it works with no hooks at
    all. Say so, or its "since the beginning" window reads like a backlog
    waiting to be attributed when nothing is recording and nothing ever will."""
    if hooks.is_installed(root):
        return
    print()
    print("note: tkus is not installed in this repository, so this usage will "
          "never be attributed to a commit.")
    print("      run `tkus install` to start recording.")


def _print_table(totals, cost) -> None:
    if not totals:
        print("no AI usage found for this window")
        return
    rows = sorted(totals.values(),
                  key=lambda t: (t.provider, -cost.per_model.get(t.key, 0.0), t.model))
    pwidth = max(len(t.provider) for t in rows)
    width = max(len(t.model) for t in rows)
    header = "%-*s %-*s %6s %12s %12s %14s %10s" % (
        pwidth, "provider", width, "model", "reqs", "output",
        "cache-wr", "cache-rd", "cost")
    print(header)
    print("-" * len(header))
    for t in rows:
        print("%-*s %-*s %6d %12d %12d %14d %10s" % (
            pwidth, t.provider, width, t.model, t.requests, t.output_tokens,
            t.cache_write_1h + t.cache_write_5m, t.cache_read,
            "%.2f" % cost.per_model.get(t.key, 0.0)))
    print("-" * len(header))
    print("%-*s %-*s %6s %12s %12s %14s %10s" % (
        pwidth, "TOTAL", width, "", "", "", "", "", "%.2f" % cost.total))
    print()
    print("%s, rates@%s (%s)" % (cost.currency, cost.rates_version,
                                 "override" if cost.overridden else "list"))
    for provider, aiu in sorted(cost.aiu_per_provider.items()):
        print("%s: %.6f AI Units" % (provider, aiu))
    unbilled = [t.model for t in rows
                if not cost.per_model.get(t.key) and t.provider != "claude-code"]
    if unbilled:
        print("not billed by the provider (self-hosted endpoint): %s"
              % ", ".join(sorted(set(unbilled))))
    if cost.is_partial:
        print("warning: no rate for %s -- tokens counted, cost excluded"
              % ", ".join(cost.unpriced_models))


def cmd_report(args) -> int:
    root = repo_root()
    since = None if args.all else cursor_mod.cursor_since(root)
    records = collect_all(root, since, now())
    cost = compute_cost(records, RateTable.load(root), when=None)
    if args.all:
        print("All recorded usage for %s\n" % root)
    else:
        print("Unattributed usage since %s\n"
              % (since.isoformat() if since else "the beginning"))
    _print_table(aggregate_by_model(records), cost)
    _warn_if_not_installed(root)
    return 0


def _ledger_scope(rel: str) -> tuple:
    """Split `.tkus/<identity>/<branch>.jsonl` into (identity, branch).

    The branch keeps its slashes: `repoledger.branch_name` writes `feature/x`
    as a nested directory, so everything after the identity is the branch name.
    """
    parts = rel.split("/")
    if len(parts) <= 2:
        return "unknown", rel
    return parts[1], "/".join(parts[2:]).rsplit(".jsonl", 1)[0]


def _grouped(root: str, key: str):
    """Ledger entries grouped for reporting."""
    groups = OrderedDict()  # type: Dict[str, list]
    for rel, entries in sorted(repoledger.read_all(root).items()):
        who, branch = _ledger_scope(rel)
        for entry in entries:
            if key == "identity":
                name = who
            elif key == "date":
                name = (entry.get("until") or entry.get("at") or "")[:10] or "unknown"
            else:
                name = branch
            groups.setdefault(name, []).append(entry)
    return groups


def cmd_rollup(args) -> int:
    """Totals from the repository ledger."""
    root = repo_root()
    groups = _grouped(root, args.by)
    if not groups:
        # stdout stays machine-readable, so the note goes to stderr whenever
        # something is parsing us.
        out = sys.stderr if args.json else sys.stdout
        out.write("no committed ledger entries under %s/\n" % repoledger.LEDGER_DIR)
        if repoledger.is_ignored(root):
            out.write("(%s is in .gitignore, so cost is recorded locally only -- "
                      "see `tkus show`)\n" % repoledger.LEDGER_DIR)
        elif not repoledger.enabled(RateTable.load(root)):
            out.write("(running --local-only, so cost is recorded in .git/ only -- "
                      "see `tkus show`)\n")
        if not args.json:
            return 0

    rows = []
    grand = 0.0
    currency = "USD"
    for name in sorted(groups):
        entries = groups[name]
        total = sum(float(e.get("usd") or 0.0) for e in entries)
        currency = entries[0].get("currency", currency)
        grand += total
        rows.append({"name": name, "entries": len(entries), "usd": total})

    if args.json:
        print(json.dumps({"by": args.by, "currency": currency, "total": grand,
                          "groups": rows}, indent=2, sort_keys=True))
        return 0

    width = max(len(row["name"]) for row in rows)
    print("%-*s %8s %10s" % (width, args.by, "entries", "cost"))
    print("-" * (width + 20))
    for row in rows:
        print("%-*s %8d %10.2f" % (width, row["name"], row["entries"], row["usd"]))
    print("-" * (width + 20))
    print("%-*s %8s %10.2f  %s" % (width, "TOTAL", "", grand, currency))
    return 0


def _commit_index(root: str, branch: str):
    """`branch`'s history, indexed by first parent.

    An entry records the sha of HEAD *before* its commit, because pre-commit
    runs while the new sha does not yet exist (see hook_pre_commit). Getting
    back to the commit therefore means inverting that: the commit whose first
    parent is the recorded one. The root commit has no parent and is filed
    under "ROOT", which is what an entry written before the first commit
    carries as `parent: null`.

    --first-parent because it is the chain of commits actually *made* on this
    branch, which is the same set the branch's ledger file records. It is also
    linear, so no two commits can share a first parent and the index is
    unambiguous.

    Returns (index, by_sha, commit_count). `by_sha` is the same commits keyed
    by their own sha, for entries attributed by `_introduced_by` instead. The
    count comes free from this walk and is what the coverage line reports
    against.
    """
    try:
        out = subprocess.run(
            ["git", "log", "--first-parent", "--format=%H%x00%P%x00%cI%x00%s",
             branch],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return {}, {}, 0
    if out.returncode != 0:
        return {}, {}, 0        # no such branch, or no commits yet

    index = {}
    by_sha = {}
    count = 0
    for line in out.stdout.decode("utf-8", "replace").split("\n"):
        if not line.strip():
            continue
        fields = line.split("\0")
        if len(fields) < 4:
            continue
        sha, parents, when, subject = fields[0], fields[1], fields[2], fields[3]
        key = parents.split()[0] if parents.strip() else "ROOT"
        # `git log` walks newest-first, so `count` doubles as a position: a
        # higher number is older, which is what puts the table in order.
        index[key] = by_sha[sha] = (sha, subject, when, count)
        count += 1
    return index, by_sha, count


def _introduced_by(root: str, branch: str) -> Dict[tuple, str]:
    """Which commit on `branch` first added each ledger entry, by entry key.

    This is what makes attribution survive history rewriting. The `parent`
    pointer is fixed at write time and an amend or rebase invalidates it -- and
    pre-commit cannot tell it is amending, so it cannot write a better one. But
    the amended commit's diff against its real parent adds every entry it
    carries, and a rebased commit re-adds exactly its own. It is also history,
    not reflog, so it gives the same answer in any clone.

    The oldest adding commit wins, so a line that moved between files -- a
    folded rename, a `git mv` -- stays with the commit that first recorded it.
    `-m` rather than `--diff-merges=first-parent`, which needs git 2.31.
    """
    try:
        out = subprocess.run(
            ["git", "-c", "core.quotepath=false", "log", "--first-parent", "-m",
             "-p", "--no-renames", "--format=%x00%H", branch, "--",
             repoledger.LEDGER_DIR],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}

    added = {}  # type: Dict[tuple, str]
    sha = None
    for line in out.stdout.decode("utf-8", "replace").split("\n"):
        if line.startswith("\0"):
            sha = line[1:].strip()
        elif sha and line.startswith("+{"):
            try:
                entry = json.loads(line[1:])
            except ValueError:
                continue
            key = repoledger.entry_key(entry) if isinstance(entry, dict) else None
            if key:
                added[key] = sha        # newest-first, so the oldest lands last
    return added


def _coverage(recorded: int, commits: int, orphans: int) -> str:
    """How much of the branch the breakdown below actually accounts for.

    Without this the table is silently partial in two directions at once --
    commits with no entry are omitted, and orphaned entries name no commit --
    and a list of shas and subjects reads as though it were the whole story.
    """
    text = "%d of %d commit%s %s recorded usage" % (
        recorded, commits, "" if commits == 1 else "s",
        "has" if recorded == 1 else "have")
    if orphans:
        text += "; %d entr%s orphaned by rebase or amend" % (
            orphans, "y" if orphans == 1 else "ies")
    return text


def cmd_log(args) -> int:
    """Per-commit cost for one branch, from the tracked ledger.

    Unlike `tkus show`, this reads the ledger committed to the repository, so
    it works in any clone -- not only on the machine that made the commits.
    """
    root = repo_root()
    branch = args.branch or repoledger.branch_name(root)

    entries = []
    for rel, items in sorted(repoledger.read_all(root).items()):
        # Every identity's file for this branch: `rollup --by branch` sums
        # across people, and a branch two people worked on is one branch.
        if _ledger_scope(rel)[1] == branch:
            entries.extend(items)

    index, by_sha, commit_count = _commit_index(root, branch)
    introduced = _introduced_by(root, branch) if entries else {}

    commits = {}
    orphaned = []
    currency = "USD"
    for entry in entries:
        usd = float(entry.get("usd") or 0.0)
        currency = entry.get("currency", currency)
        found = by_sha.get(introduced.get(repoledger.entry_key(entry)))
        if not found:
            # No commit added it -- it is only in the worktree, such as the
            # staged ledger of an abandoned commit. The parent pointer is all
            # there is. "HEAD" appears in ledgers written before head_sha
            # checked git's exit code: on an unborn branch it recorded that
            # string instead of null. It meant the root commit then and it
            # means the root commit now.
            parent = entry.get("parent")
            found = index.get("ROOT" if parent in (None, "HEAD") else parent)
        if not found:
            orphaned.append({"parent": entry.get("parent"),
                             "at": entry.get("until") or entry.get("at"),
                             "usd": usd})
            continue
        sha, subject, when, order = found
        row = commits.get(sha)
        if row:
            row["usd"] += usd       # two entries can land on one commit
        else:
            commits[sha] = {"sha": sha, "subject": subject, "at": when,
                            "usd": usd, "order": order}

    ordered = sorted(commits.values(), key=lambda r: -r["order"])
    orphan_total = sum(o["usd"] for o in orphaned)
    total = sum(r["usd"] for r in ordered) + orphan_total

    if not entries:
        # stdout stays machine-readable, so the note goes to stderr whenever
        # something is parsing us.
        out = sys.stderr if (args.total or args.json) else sys.stdout
        out.write("no committed ledger entries for %s under %s/\n"
                  % (branch, repoledger.LEDGER_DIR))
        if repoledger.is_ignored(root):
            out.write("(%s is in .gitignore, so cost is recorded locally only "
                      "-- see `tkus show`)\n" % repoledger.LEDGER_DIR)
        elif not repoledger.enabled(RateTable.load(root)):
            out.write("(running --local-only, so cost is recorded in .git/ only "
                      "-- see `tkus show`)\n")
        if not (args.total or args.json):
            return 0

    if args.json:
        print(json.dumps({
            "branch": branch,
            "currency": currency,
            "total": total,
            "commits": [{"sha": r["sha"], "subject": r["subject"],
                         "at": r["at"], "usd": r["usd"]} for r in ordered],
            "orphaned": orphaned,
        }, indent=2, sort_keys=True))
        return 0

    if args.total:
        print(_money(total))
        return 0

    print("branch %s" % branch)
    print(_coverage(len(ordered), commit_count, len(orphaned)))
    print()

    rule = min(shutil.get_terminal_size((80, 24)).columns, 100)
    room = max(20, rule - 23)
    print("%-9s %10s  %s" % ("commit", "cost", "subject"))
    print("-" * rule)
    for row in ordered:
        subject = row["subject"]
        if len(subject) > room:
            subject = subject[:room - 3] + "..."
        print("%-9s %10.2f  %s" % (row["sha"][:9], row["usd"], subject))
    if orphaned:
        print("%-9s %10.2f  (orphaned: no commit on this branch claims these)"
              % ("--", orphan_total))
    print("-" * rule)
    print("%-9s %10.2f  %s" % ("TOTAL", total, currency))
    return 0


def _usd(entries: List[dict]) -> float:
    return sum(float(e.get("usd") or 0.0) for e in entries)


def cmd_rename(args) -> int:
    """Carry a branch's ledger over to the name it continued under.

    `git branch -m` is folded automatically, but only because the reflog says
    so. A branch cut from the old one before deleting it, or renamed on GitHub
    or in another clone, leaves nothing to find -- so this records the old name
    and the next commit folds it the same way.

    It records rather than moves because pre-commit rebuilds the ledger from
    HEAD: a moved-and-staged file would be overwritten by the next commit,
    while the staged removal of the old one still landed.
    """
    root = repo_root()
    if not args.old:
        return _list_rename_candidates(root)

    new = args.new or repoledger.branch_name(root)
    old_rel = repoledger.branch_file(root, args.old)
    new_rel = repoledger.branch_file(root, new)
    if old_rel == new_rel:
        sys.stderr.write("tkus: %s and %s share one ledger file\n" % (args.old, new))
        return 1
    found = repoledger.read_committed(root, old_rel)
    if not found:
        sys.stderr.write("tkus: no committed ledger at %s -- run `tkus rename` "
                         "with no arguments to list candidates\n" % old_rel)
        return 1
    if repoledger.branch_exists(root, args.old):
        # The fold leaves a live branch's file alone, so recording this would
        # promise a move that never happens.
        sys.stderr.write("tkus: branch %s still exists, so its ledger is still "
                         "its own; delete it first (git branch -D %s)\n"
                         % (args.old, args.old))
        return 1

    repoledger.add_alias(root, new, args.old)
    print("%d entr%s (%s %s) from %s will move into %s at your next commit"
          % (len(found), "y" if len(found) == 1 else "ies", _money(_usd(found)),
             found[0].get("currency", "USD"), old_rel, new_rel))
    return 0


def _list_rename_candidates(root: str) -> int:
    """This identity's ledgers whose branch no longer exists here."""
    mine = repoledger.identity(root)
    current = repoledger.branch_name(root)
    rows = []
    for rel in repoledger.all_files(root):
        who, branch = _ledger_scope(rel)
        if who != mine or branch == current or repoledger.branch_exists(root, branch):
            continue
        entries = repoledger.read_committed(root, rel)
        if entries:
            rows.append((branch, len(entries), _usd(entries)))
    if not rows:
        print("no ledgers here belong to a deleted branch")
        return 0
    width = max(len(r[0]) for r in rows)
    print("ledgers whose branch no longer exists (tkus rename <old>):")
    for branch, count, usd in sorted(rows):
        print("  %-*s %4d entr%s %10s" % (width, branch, count,
                                          "y  " if count == 1 else "ies",
                                          _money(usd)))
    return 0


def cmd_reprice(args) -> int:
    """Re-price the repository ledger with the current rate table.

    Possible because the ledger stores raw token counts, not just a figure.
    """
    root = repo_root()
    table = RateTable.load(root)
    from .providers.base import parse_timestamp

    grand_old = grand_new = 0.0
    rows = 0
    for rel, entries in sorted(repoledger.read_all(root).items()):
        for entry in entries:
            totals = ledger.totals_from_entry(entry)
            if not totals:
                continue
            when = parse_timestamp(entry.get("until") or entry.get("at") or "") or now()
            cost = compute_cost_from_totals(totals, table, when)
            grand_old += float(entry.get("usd") or 0.0)
            grand_new += cost.total
            rows += 1
    if not rows:
        print("no ledger entries to re-price")
        return 0
    print("entries      %d" % rows)
    print("as recorded  %.2f" % grand_old)
    print("re-priced    %.2f  (rates@%s)" % (grand_new, table.version))
    delta = grand_new - grand_old
    if abs(delta) >= 0.005:
        print("difference   %+.2f" % delta)
    return 0


def cmd_show(args) -> int:
    """Per-commit detail from the local ledger in .git/."""
    root = repo_root()
    sha = args.commit or "HEAD"
    try:
        resolved = subprocess.check_output(
            ["git", "rev-parse", sha], cwd=root, stderr=subprocess.DEVNULL
        ).decode().strip()
    except (subprocess.CalledProcessError, OSError):
        sys.exit("tkus: no such commit: %s" % sha)

    entry = ledger.lookup(root, resolved)
    if not entry:
        print("no local detail for %s" % resolved[:9])
        print("(the per-commit ledger lives in .git and covers only commits made "
              "on this machine; try `tkus rollup` for the tracked ledger)")
        return 0

    print("commit %s   %s   rates@%s" % (
        resolved[:9], entry.get("at", "?"), entry.get("rates_version", "?")))
    print()
    rows = entry.get("providers") or []
    width = max([len(r.get("model", "")) for r in rows] + [5])
    for row in rows:
        fields = " ".join("%s=%s" % (k, v) for k, v in sorted(row.items())
                          if k not in ("provider", "model", "usd"))
        print("  %-12s %-*s %8.4f  %s" % (
            row.get("provider", "?"), width, row.get("model", "?"),
            row.get("usd", 0.0), fields))
    print()
    print("  total %s %.4f" % (entry.get("currency", "USD"), entry.get("usd", 0.0)))
    return 0


def cmd_hook(args) -> int:
    handler = HOOK_DISPATCH.get(args.name)
    if handler is None:
        return 0
    try:
        return handler(repo_root(), args.args)
    except Exception as exc:  # noqa: BLE001
        # Recording cost must never block a commit.
        sys.stderr.write("tkus: %s hook failed: %s\n" % (args.name, exc))
        return 0


def _money(value: float) -> str:
    """Two decimals when that is exact, more when it is not."""
    for places in (2, 4, 6):
        text = "%.*f" % (places, value)
        if abs(float(text) - value) < 1e-12:
            return text
    return "%.6f" % value


def _rate_rows(table, when):
    """Effective per-MTok prices for every model, cache class expanded.

    The table stores cache prices as multipliers on the input rate, which is
    the one thing a reader cannot do in their head. What matters at the point of
    use is that a cache read costs $0.50/MTok -- not that it is 0.1x something
    else -- so the multipliers are resolved into money here.
    """
    rows = []
    for model, entry in (table.data.get("models") or {}).items():
        for speed in entry:
            rate = table.rate_for(model, speed, when)
            if not rate:
                continue          # priced only outside this date
            inp, out = rate
            row = {"model": model, "speed": speed, "input": inp, "output": out}
            cache = table.cache_rates(model, speed, when)
            for (name, _default), value in zip(table.CACHE_FIELDS, cache):
                # Rounded because 3.0 * 0.1 is 0.30000000000000004, which is
                # correct and unreadable. Ten places is far below the precision
                # of any published rate, so nothing real is lost.
                row[name] = round(value, 10)
            rows.append(row)
    return rows


def _scheduled_changes(table, when):
    """Price windows that begin after `when`, so a change cannot arrive unseen."""
    day = when.strftime("%Y-%m-%d")
    out = []
    for model, entry in (table.data.get("models") or {}).items():
        for speed, windows in entry.items():
            current = table.rate_for(model, speed, when)
            for window in windows:
                start = window.get("from")
                if not start or start <= day:
                    continue
                out.append({
                    "model": model, "speed": speed, "from": start,
                    "input": float(window["input"]),
                    "output": float(window["output"]),
                    "from_input": current[0] if current else None,
                    "from_output": current[1] if current else None,
                    "note": window.get("note"),
                })
    return sorted(out, key=lambda r: (r["from"], r["model"]))


def _active_window(table, model, speed, when):
    """The rate window in force, so its `until` can be inspected.

    `RateTable.rate_for` returns only the prices; a drift check also has to know
    whether the window it would be contradicting is a deliberate, dated one.
    """
    entry = table.data.get("models", {}).get(table.canonical(model))
    if not entry:
        return None
    day = when.strftime("%Y-%m-%d")
    for window in entry.get(speed) or entry.get("standard") or []:
        start, end = window.get("from"), window.get("until")
        if start and day < start:
            continue
        if end and day > end:
            continue
        return window
    return None


def _rate_drift(table, data, when):
    """Bundled table vs the installed Claude Code catalog.

    Only `standard` is compared: the catalog carries no speed dimension, so a
    fast-mode rate has nothing upstream to disagree with.
    """
    changed, missing = [], []
    for entry in data["models"]:
        model = entry["id"]
        upstream = data["tiers"][entry["tier"]]
        current = table.rate_for(model, "standard", when)
        if current is None:
            missing.append({"model": model, "input": upstream["input"],
                            "output": upstream["output"], "tier": entry["tier"]})
            continue
        window = _active_window(table, model, "standard", when) or {}
        for index, field in ((0, "input"), (1, "output")):
            if abs(current[index] - upstream[field]) > 1e-9:
                changed.append({
                    "model": model, "field": field,
                    "bundled": current[index], "upstream": upstream[field],
                    # A window with an explicit end is a deliberate statement the
                    # catalog cannot make -- it has no dates. Overwriting it
                    # would discard information rather than refresh it.
                    "dated": bool(window.get("until") or window.get("from")),
                    "until": window.get("until"),
                    # A pinned rate has been checked against published pricing,
                    # which outranks the catalog: Claude Code's own table can be
                    # stale (it still carries Sonnet 5's cancelled increase).
                    "pinned": window.get("pinned") or None,
                })
    known = set(table.data.get("aliases", {}))
    alias_gaps = {dated: canonical
                  for dated, canonical in catalog.aliases(data).items()
                  if dated not in known}
    return {
        "changed": changed, "missing": missing, "alias_gaps": alias_gaps,
        "multiplier_conflicts": catalog.multiplier_conflicts(data, table),
    }


def _print_drift(drift, data, table) -> None:
    print("compared against Claude Code %s\n" % data["version"])
    changed, missing = drift["changed"], drift["missing"]

    if changed:
        width = max(len(c["model"]) for c in changed)
        header = "%-*s %-7s %12s -> %-12s" % (width, "model", "field",
                                              "bundled", "claude-code")
        print(header)
        print("-" * len(header))
        for c in changed:
            if c["pinned"]:
                note = "  [pinned]"
            elif c["dated"]:
                note = "  [dated window%s]" % (
                    " ending %s" % c["until"] if c["until"] else "")
            else:
                note = ""
            print("%-*s %-7s %12s -> %-12s%s" % (
                width, c["model"], c["field"], _money(c["bundled"]),
                _money(c["upstream"]), note))
        print()

    if missing:
        print("absent from the bundled table (priced as zero today):")
        for m in missing:
            print("  %-22s %s / %s" % (m["model"], _money(m["input"]),
                                       _money(m["output"])))
        print()

    for conflict in drift["multiplier_conflicts"]:
        print("warning: %s %s is %s upstream, but the multipliers imply %s -- "
              "cache pricing can no longer be expressed as a multiple of input."
              % (conflict["tier"], conflict["field"], _money(conflict["actual"]),
                 _money(conflict["expected"])))

    if drift["alias_gaps"]:
        print("dated model IDs with no alias (%d):" % len(drift["alias_gaps"]))
        for dated, canonical in sorted(drift["alias_gaps"].items()):
            print("  %s -> %s" % (dated, canonical))
        print()

    for change in changed:
        if change["pinned"]:
            print("pinned: %s %s -- %s"
                  % (change["model"], change["field"], change["pinned"]))
    dated = [c for c in changed if c["dated"] and not c["pinned"]]
    if dated:
        print("%d change(s) fall inside a dated window and are not "
              "auto-resolvable: the catalog carries no dates, so it cannot "
              "express introductory or scheduled pricing." % len(dated))
    if not changed and not missing:
        print("bundled table agrees with the catalog.")


def _override_path() -> str:
    from .pricing import _global_config_dir
    return os.path.join(_global_config_dir(), "rates.json")


def _explicit_cache(table, tier_prices, rate_in):
    """Cache prices this input rate cannot derive, as an override.

    Only the ones that actually deviate: writing out a number the multiplier
    already produces would repeat three fields on every model to say nothing.
    Without this, adding a model on a tier like tier_10_50_cache_read_0_25
    would quietly price its cache reads at four times the real rate.
    """
    out = OrderedDict()
    for name, default in table.CACHE_FIELDS:
        upstream = tier_prices.get(name)
        if upstream is None:
            continue
        if abs(rate_in * table.multiplier(name, default) - upstream) > 1e-9:
            out[name] = upstream
    return out


def _build_update(table, data, drift, when, existing):
    """The override to write, plus everything deliberately left alone.

    Three refusals, each because the catalog cannot express what it would be
    overwriting:

    * A model the user has already overridden locally is almost certainly a
      negotiated rate. Replacing it with a list price would quietly undo the
      thing the override exists for.
    * A dated window is a statement the catalog cannot make -- it carries no
      dates, so it cannot distinguish "the price changed" from "introductory
      pricing is still running".
    * Fast-mode pricing has no counterpart upstream at all.
    """
    from datetime import timedelta

    existing_models = (existing.get("models") or {})
    today = when.strftime("%Y-%m-%d")
    yesterday = (when - timedelta(days=1)).strftime("%Y-%m-%d")

    models, skipped = OrderedDict(), []

    for entry in drift["missing"]:
        name = entry["model"]
        if name in existing_models:
            skipped.append((name, "already overridden locally"))
            continue
        window = OrderedDict([("from", None), ("until", None),
                              ("input", entry["input"]),
                              ("output", entry["output"])])
        cache = _explicit_cache(table, data["tiers"].get(entry["tier"]) or {},
                                entry["input"])
        if cache:
            window["cache"] = cache
        models[name] = {"standard": [window]}

    by_model = OrderedDict()
    for change in drift["changed"]:
        by_model.setdefault(change["model"], []).append(change)

    for name, changes in by_model.items():
        if name in existing_models:
            skipped.append((name, "already overridden locally"))
            continue
        pinned = next((c["pinned"] for c in changes if c["pinned"]), None)
        if pinned:
            skipped.append((name, "pinned to published pricing"))
            continue
        if any(c["dated"] for c in changes):
            skipped.append((name, "inside a dated window the catalog cannot express"))
            continue
        upstream = catalog.rate_for(data, name)
        if not upstream:
            continue
        current = table.data.get("models", {}).get(table.canonical(name), {})
        windows = [dict(w) for w in (current.get("standard") or [])]
        active = _active_window(table, name, "standard", when)
        for window in windows:
            # Close the window rather than editing it, so `tkus reprice` keeps
            # historical commits at the rates that actually applied to them.
            if active is not None and window.get("from") == active.get("from") \
                    and window.get("until") == active.get("until"):
                window["until"] = yesterday
        windows.append({"from": today, "until": None,
                        "input": upstream["input"], "output": upstream["output"]})
        # Only `standard` is replaced; a `fast` block on the same model is left
        # untouched by the deep merge.
        models[name] = {"standard": windows}

    proposal = {}
    if models:
        proposal["models"] = models
    aliases = {k: v for k, v in drift["alias_gaps"].items()
               if k not in (existing.get("aliases") or {})}
    if aliases:
        proposal["aliases"] = aliases
    return proposal, skipped


def cmd_rates_update(args) -> int:
    table = RateTable.load(_repo_root_or_none())
    when = _rates_when(args)
    data = catalog.extract()
    if data is None:
        print("could not read a pricing catalog from Claude Code; nothing to "
              "update. The bundled table is unchanged and still in use.")
        return 0

    path = _override_path()
    existing = {}
    if os.path.isfile(path):
        try:
            with open(path) as handle:
                loaded = json.load(handle)
            existing = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            sys.exit("tkus: %s exists but is not readable JSON; refusing to "
                     "overwrite it." % path)

    drift = _rate_drift(table, data, when)
    proposal, skipped = _build_update(table, data, drift, when, existing)

    for name, why in skipped:
        print("left alone: %s -- %s" % (name, why))
    if not proposal:
        print("\nnothing to write." if skipped else "bundled table agrees with "
              "the catalog; nothing to write.")
        return 0

    merged = dict(existing)
    for key, value in proposal.items():
        combined = dict(merged.get(key) or {})
        combined.update(value)
        merged[key] = combined

    print("\nwould write %s:\n" % path)
    print(json.dumps(proposal, indent=2, sort_keys=True))

    if not getattr(args, "yes", False):
        print("\ndry run. Re-run with --yes to write it.")
        return 0

    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(merged, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    print("\nwrote %s" % path)
    print("rates are now marked as overridden; `tkus rates` will say so.")
    return 0


def cmd_rates_check(args) -> int:
    table = RateTable.load(_repo_root_or_none())
    when = _rates_when(args)
    data = catalog.extract()
    if data is None:
        # Absence is not drift. A machine without Claude Code, or a release that
        # moved the catalog, must not look like a pricing change.
        message = ("could not read a pricing catalog from Claude Code; "
                   "the bundled table is unchanged and still in use.")
        if getattr(args, "json", False):
            json.dump({"available": False, "reason": message}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            print(message)
        return 0

    drift = _rate_drift(table, data, when)
    if getattr(args, "json", False):
        json.dump(dict(drift, available=True, claude_code=data["version"],
                       source=data["path"]),
                  sys.stdout, indent=2, sort_keys=True, default=str)
        sys.stdout.write("\n")
    else:
        _print_drift(drift, data, table)
    return 1 if (drift["changed"] or drift["missing"]) else 0


def cmd_rates(args) -> int:
    if getattr(args, "check", False):
        return cmd_rates_check(args)
    if getattr(args, "update", False):
        return cmd_rates_update(args)
    root = _repo_root_or_none()
    table = RateTable.load(root)
    when = _rates_when(args)
    rows = _rate_rows(table, when)
    changes = _scheduled_changes(table, when)

    if getattr(args, "json", False):
        json.dump({
            "version": table.version, "currency": table.currency,
            "effective": when.strftime("%Y-%m-%d"),
            "unit": "per 1000000 tokens",
            "overridden": table.is_overridden,
            "usd_per_aiu": table.data.get("usd_per_aiu"),
            "tier_multipliers": table.data.get("tier_multipliers", {}),
            "models": rows, "scheduled_changes": changes,
            "sources": table.sources,
        }, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    print("Rate table %s (%s), effective %s\n"
          % (table.version, table.currency, when.strftime("%Y-%m-%d")))

    if rows:
        print("%s per 1M tokens" % table.currency)
        mwidth = max(max(len(r["model"]) for r in rows), len("model"))
        swidth = max(max(len(r["speed"]) for r in rows), len("speed"))
        header = "%-*s %-*s %9s %9s %12s %12s %10s" % (
            mwidth, "model", swidth, "speed", "input", "output",
            "cache-wr-1h", "cache-wr-5m", "cache-rd")
        print(header)
        print("-" * len(header))
        for r in rows:
            # input/output are authored at 2dp, but the cache columns are
            # products of a multiplier and can land anywhere -- a negotiated
            # 3.50 makes cache-read 0.175, and rounding that to 0.18 is a 3%
            # error on the largest token class most sessions have.
            print("%-*s %-*s %9.2f %9.2f %12s %12s %10s" % (
                mwidth, r["model"], swidth, r["speed"], r["input"], r["output"],
                _money(r["cache_write_1h"]), _money(r["cache_write_5m"]),
                _money(r["cache_read"])))
        print()

    tiers = table.data.get("tier_multipliers") or {}
    non_standard = ["%s x%g" % (k, v) for k, v in sorted(tiers.items()) if v != 1.0]
    if non_standard:
        print("Tiers, applied to every rate above: %s" % ", ".join(non_standard))

    aiu = table.data.get("usd_per_aiu")
    if aiu:
        print("Copilot bills in AI Units, recorded per request: "
              "1 AIU = %s %.4f" % (table.currency, float(aiu)))

    tools = table.data.get("server_tools") or {}
    for name, price in sorted(tools.items()):
        print("Server tool %s: %s %.2f" % (name, table.currency, price))

    if changes:
        print("\nScheduled changes")
        for c in changes:
            detail = "input %.2f -> %.2f, output %.2f -> %.2f" % (
                c["from_input"], c["input"], c["from_output"], c["output"]
            ) if c["from_input"] is not None else (
                "input %.2f, output %.2f" % (c["input"], c["output"]))
            print("  %s  %s (%s)  %s%s" % (
                c["from"], c["model"], c["speed"], detail,
                " -- %s" % c["note"] if c.get("note") else ""))

    print("\nSources")
    for path in table.sources:
        print("  %s" % path)
    print("%s prices" % ("overridden" if table.is_overridden else "list"))
    if not root:
        print("note: not inside a git repository, so any repository-level "
              "%s was not applied." % REPO_CONFIG)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tkus",
        description="Attribute AI token usage and cost to git commits.")
    parser.add_argument("--version", action="version", version="tkus " + __version__)
    sub = parser.add_subparsers(dest="command")

    install = sub.add_parser("install", help="install git hooks in this repo")
    install.add_argument("--local-only", action="store_true",
                         help="record only in .git/, never commit cost to the repo")
    install.set_defaults(func=cmd_install)

    sub.add_parser("uninstall", help="remove tkus git hooks").set_defaults(
        func=cmd_uninstall)

    report = sub.add_parser("report", help="usage not yet attributed to a commit")
    report.add_argument("--all", action="store_true",
                        help="ignore the cursor and report all recorded usage")
    report.set_defaults(func=cmd_report)

    rollup = sub.add_parser("rollup", help="totals from the tracked ledger")
    rollup.add_argument("--by", choices=("branch", "identity", "date"),
                        default="branch")
    rollup.add_argument("--json", action="store_true",
                        help="emit the totals as JSON")
    rollup.set_defaults(func=cmd_rollup)

    log = sub.add_parser("log", help="per-commit cost for one branch")
    log.add_argument("--branch", metavar="NAME",
                     help="branch to report on (default: the current one)")
    log.add_argument("--total", action="store_true",
                     help="print only the total, for scripting")
    log.add_argument("--json", action="store_true",
                     help="machine-readable output")
    log.set_defaults(func=cmd_log)

    rename = sub.add_parser(
        "rename", help="carry a branch's ledger over after its name changed")
    rename.add_argument("old", nargs="?",
                        help="the former branch name (omit to list candidates)")
    rename.add_argument("new", nargs="?",
                        help="the branch it continued as (default: the current one)")
    rename.set_defaults(func=cmd_rename)

    reprice = sub.add_parser("reprice",
                             help="re-price the ledger with the current rates")
    reprice.set_defaults(func=cmd_reprice)

    show = sub.add_parser("show", help="per-commit detail from the local ledger")
    show.add_argument("commit", nargs="?", help="commit-ish (default HEAD)")
    show.set_defaults(func=cmd_show)

    rates = sub.add_parser("rates", help="show the rate table used for pricing")
    rates.add_argument("--at", metavar="YYYY-MM-DD",
                       help="rates in effect on this date (default today)")
    rates.add_argument("--check", action="store_true",
                       help="compare against the installed Claude Code catalog; "
                            "exit 1 if they disagree. Writes nothing.")
    rates.add_argument("--update", action="store_true",
                       help="write refreshed rates to the global override file; "
                            "a dry run unless --yes is given")
    rates.add_argument("--yes", action="store_true",
                       help="with --update, actually write the file")
    rates.add_argument("--json", action="store_true",
                       help="machine-readable output")
    rates.set_defaults(func=cmd_rates)

    hook = sub.add_parser("hook", help=argparse.SUPPRESS)
    hook.add_argument("name")
    hook.add_argument("args", nargs=argparse.REMAINDER)
    hook.set_defaults(func=cmd_hook)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 1
    try:
        return args.func(args)
    except RateTableError as exc:
        # A traceback here would be noise: the fault is in a config file the
        # user owns, and the fix is to correct or remove it.
        sys.stderr.write("tkus: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
