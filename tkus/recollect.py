"""`tkus reprice --recollect` -- ledger entries re-read from the transcripts.

Entries recorded before two collection fixes are low, by an amount that varies
per commit: subagent transcripts were never read, and each request was counted
from its first line, written while the response was still streaming. No flat
correction is right, but the transcripts are still on disk for a while, and
every entry records the window it claimed.

What an entry does not record is where it was committed from. A commit claims
only the sessions inside its own worktree, and worktrees' windows overlap, so
re-reading a window for the wrong one would claim another branch's work. The
old collection is therefore replayed for every place the commit could have
run, and an entry is rewritten only when the places that reproduce it to the
token agree on the new answer. Reproducing it also proves its transcripts are
all still there: Claude Code prunes them, and a partial window would lower the
entry rather than correct it.
"""

from __future__ import annotations

import bisect
import copy
import subprocess
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import ledger, paths
from .pricing import RateTable, compute_cost
from .providers.base import UsageRecord, aggregate_by_model, parse_timestamp
from .providers.claude_code import (ClaudeCodeProvider, _iter_records,
                                    candidate_dirs, complete_requests,
                                    split_window)

PROVIDER = ClaudeCodeProvider.name
COUNTS = [name for name, _ in ledger._FIELDS]

# Why an entry was left as recorded.
ALREADY = "already re-read"
UNREPRODUCED = "not reproduced (transcripts pruned, or an older tkus)"
AMBIGUOUS = "ambiguous (more than one worktree fits)"
NO_WINDOW = "no window recorded"


def worktrees(repo_root):
    # type: (str) -> List[str]
    """Every worktree of the repository, the main one first."""
    try:
        out = subprocess.run(["git", "worktree", "list", "--porcelain"],
                             cwd=repo_root, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return [repo_root]
    found = [line[len("worktree "):].strip()
             for line in out.stdout.decode("utf-8", "replace").splitlines()
             if line.startswith("worktree ")]
    return found or [repo_root]


def signature(rows):
    # type: (List[dict]) -> tuple
    """An entry's Claude Code token counts, for comparing two of them."""
    return tuple(sorted(
        (row.get("model"),) + tuple(int(row.get(name) or 0) for name in COUNTS)
        for row in rows if row.get("provider", PROVIDER) == PROVIDER))


def _rows(records):
    # type: (List[UsageRecord]) -> List[dict]
    out = []
    for totals in aggregate_by_model(records).values():
        row = {"provider": totals.provider, "model": totals.model}
        for name, attr in ledger._FIELDS:
            row[name] = getattr(totals, attr, 0)
        out.append(row)
    return out


class Transcripts:
    """Every Claude Code line for a repository and its worktrees, read once."""

    def __init__(self, roots):
        # type: (List[str]) -> None
        provider = ClaudeCodeProvider()
        directories = []  # type: List[str]
        for root in roots:
            for directory in candidate_dirs(root):
                if directory not in directories:
                    directories.append(directory)
        # (cwd, record, is_subagent), in the order collection reads them.
        self.lines = []  # type: List[Tuple[str, UsageRecord, bool]]
        for directory in directories:
            for path, is_subagent in provider.files(directory):
                for obj in _iter_records(path):
                    record = provider._to_record(obj, None)
                    if record is not None:
                        self.lines.append((obj.get("cwd") or "", record, is_subagent))
        self._within = {}  # type: Dict[Tuple[str, str], bool]
        self._by_root = {}  # type: Dict[str, tuple]

        # Places a commit can have run: each worktree, and each directory a
        # session ran in -- a deleted worktree is gone from `git worktree
        # list`, but its sessions still name it. Not only directories inside
        # a worktree: `C:\git\dts-x` beside `C:\git\dts` is a common layout,
        # and its project directory matches the prefix glob. An unrelated
        # repository that matches too is harmless, because a place counts
        # only if it reproduces the entry to the token.
        self.candidates = []  # type: List[str]
        seen = set()
        for place in list(roots) + sorted({cwd for cwd, _, _ in self.lines if cwd}):
            key = paths.normalize(place)
            if key in seen:
                continue
            seen.add(key)
            self.candidates.append(place)

    def within(self, cwd, root):
        # type: (str, str) -> bool
        key = (cwd, root)
        if key not in self._within:
            self._within[key] = bool(cwd) and paths.is_within(cwd, root)
        return self._within[key]

    def _root(self, root):
        # type: (str) -> tuple
        """This root's session lines sorted by time, and its requests."""
        if root not in self._by_root:
            mine = [(i, line) for i, line in enumerate(self.lines)
                    if self.within(line[0], root)]
            sessions = sorted(((line[1].timestamp, i, line[1])
                               for i, line in mine if not line[2]),
                              key=lambda item: (item[0], item[1]))
            # complete_requests re-dates records, and the replay needs each
            # line's own time, so it works on copies.
            requests = complete_requests(
                [(copy.copy(line[1]), line[2]) for _, line in mine])
            self._by_root[root] = ([t for t, _, _ in sessions], sessions, requests)
        return self._by_root[root]

    def as_recorded(self, root, since, until):
        # type: (str, Optional[datetime], datetime) -> List[UsageRecord]
        """What collection before the fixes claimed for this window: session
        files only, each request from its first line inside the window."""
        times, sessions, _ = self._root(root)
        lo = 0 if since is None else bisect.bisect_right(times, since)
        hi = bisect.bisect_right(times, until)
        out, seen = [], set()
        for _, _, record in sorted(sessions[lo:hi], key=lambda item: item[1]):
            if record.request_id not in seen:
                seen.add(record.request_id)
                out.append(record)
        return out

    def now(self, root, since, until):
        # type: (str, Optional[datetime], datetime) -> List[UsageRecord]
        sessions, subagents = split_window(self._root(root)[2], since, until)
        return sessions + subagents


def reread(entry, transcripts, table):
    # type: (dict, Transcripts, RateTable) -> Tuple[Optional[dict], Optional[str]]
    """(the entry re-read, None) or (None, why it was left alone).

    Both are None when re-reading changes nothing.
    """
    if entry.get("recollected"):
        return None, ALREADY
    until = parse_timestamp(entry.get("until") or "")
    since = parse_timestamp(entry.get("since") or "") if entry.get("since") else None
    if until is None or (entry.get("since") and since is None):
        return None, NO_WINDOW

    recorded = signature(entry.get("providers") or [])
    answers = {}  # type: Dict[tuple, List[UsageRecord]]
    for root in transcripts.candidates:
        if signature(_rows(transcripts.as_recorded(root, since, until))) != recorded:
            continue
        records = transcripts.now(root, since, until)
        answers[signature(_rows(records))] = records
    if not answers:
        return None, UNREPRODUCED
    if len(answers) > 1:
        return None, AMBIGUOUS
    [(new, records)] = answers.items()
    if new == recorded:
        return None, None

    cost = compute_cost(records, table, when=None)
    fresh = ledger.build_entry(aggregate_by_model(records), cost)["providers"]
    others = [row for row in entry.get("providers") or []
              if row.get("provider", PROVIDER) != PROVIDER]
    usd = cost.total + sum(float(row.get("usd") or 0.0) for row in others)
    return dict(entry, providers=fresh + others, usd=round(usd, 10),
                rates_version=table.version, recollected=True), None
