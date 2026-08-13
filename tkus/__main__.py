"""tkus -- attribute AI token usage and cost to git commits.

Cost is recorded in a repository-tracked ledger, never in the commit message.
See tkus/repoledger.py for why: squash+merge discards commit messages but
preserves the tree.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import cursor as cursor_mod
from . import hooks, ledger, repoledger
from .pricing import RateTable, compute_cost, compute_cost_from_totals
from .providers import claude_code  # noqa: F401  (registers the provider)
from .providers import copilot  # noqa: F401  (registers the provider)
from .providers.base import aggregate_by_model, collect_all, format_timestamp

__version__ = "0.2.0"


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


def now() -> datetime:
    return datetime.now(timezone.utc)


def head_sha(root: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return out.stdout.decode().strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


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
    return 0


def _grouped(root: str, key: str):
    """Ledger entries grouped for reporting."""
    groups = OrderedDict()  # type: Dict[str, list]
    for rel, entries in sorted(repoledger.read_all(root).items()):
        parts = rel.split("/")
        who = parts[1] if len(parts) > 2 else "unknown"
        branch = "/".join(parts[2:]).rsplit(".jsonl", 1)[0] if len(parts) > 2 else rel
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
        print("no committed ledger entries under %s/" % repoledger.LEDGER_DIR)
        if repoledger.is_ignored(root):
            print("(%s is in .gitignore, so cost is recorded locally only -- "
                  "see `tkus show`)" % repoledger.LEDGER_DIR)
        elif not repoledger.enabled(RateTable.load(root)):
            print("(running --local-only, so cost is recorded in .git/ only -- "
                  "see `tkus show`)")
        return 0

    width = max(len(name) for name in groups)
    grand = 0.0
    currency = "USD"
    print("%-*s %8s %10s" % (width, args.by, "entries", "cost"))
    print("-" * (width + 20))
    for name in sorted(groups):
        entries = groups[name]
        total = sum(float(e.get("usd") or 0.0) for e in entries)
        currency = entries[0].get("currency", currency)
        grand += total
        print("%-*s %8d %10.2f" % (width, name, len(entries), total))
    print("-" * (width + 20))
    print("%-*s %8s %10.2f  %s" % (width, "TOTAL", "", grand, currency))
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
    rollup.set_defaults(func=cmd_rollup)

    reprice = sub.add_parser("reprice",
                             help="re-price the ledger with the current rates")
    reprice.set_defaults(func=cmd_reprice)

    show = sub.add_parser("show", help="per-commit detail from the local ledger")
    show.add_argument("commit", nargs="?", help="commit-ish (default HEAD)")
    show.set_defaults(func=cmd_show)

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
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
