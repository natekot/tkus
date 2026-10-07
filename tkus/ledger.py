"""Per-commit token detail, kept in `.git/` rather than in the commit message.

This is the *local* per-commit record, keyed by SHA, on the machine that made
the commit. The shared, durable record is the repository-tracked ledger in
tkus/repoledger.py; this one exists because it can key entries by commit SHA,
which the tracked ledger cannot -- the SHA does not exist when the ledger is
written. Deleting `.git` loses this detail; the tracked ledger survives.

Newline-delimited JSON, append-only, last entry wins for a given SHA. An amend
produces a new SHA, so its earlier entry is simply orphaned -- cheaper than
rewriting the file on every commit, and readers skip SHAs that no longer resolve.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .cursor import state_dir
from .pricing import compute_cost_from_totals
from .providers.base import ModelTotals, format_timestamp, parse_timestamp

LEDGER_FILE = "ledger.jsonl"

# Short field name -> ModelTotals attribute. The same names are used in the
# repository ledger, so both files read alike.
_FIELDS = [
    ("reqs", "requests"),
    ("in", "input_tokens"),
    ("out", "output_tokens"),
    ("cw1h", "cache_write_1h"),
    ("cw5m", "cache_write_5m"),
    ("cr", "cache_read"),
    ("ws", "web_search_requests"),
    ("reas", "reasoning_tokens"),
    ("naiu", "nano_aiu"),
]


def path(repo_root: str, create: bool = False) -> str:
    return os.path.join(state_dir(repo_root, create=create), LEDGER_FILE)


def build_entry(totals_by_key, cost, sha=None, at=None) -> dict:
    # type: (Dict[tuple, ModelTotals], object, Optional[str], Optional[datetime]) -> dict
    """A ledger record from a set of totals and their computed cost."""
    rows = []
    for totals in totals_by_key.values():
        if totals.is_empty:
            continue
        row = {"provider": totals.provider, "model": totals.model}
        for name, attr in _FIELDS:
            value = getattr(totals, attr, 0)
            if value:
                row[name] = value
        row["usd"] = round(cost.per_model.get(totals.key, 0.0), 10)
        rows.append(row)
    return {
        "sha": sha,
        "at": format_timestamp(at or datetime.now(timezone.utc)),
        "rates_version": getattr(cost, "rates_version", "unknown"),
        "currency": getattr(cost, "currency", "USD"),
        "usd": round(getattr(cost, "total", 0.0), 10),
        "providers": rows,
    }


def totals_from_entry(entry: dict) -> Dict[tuple, ModelTotals]:
    """Rebuild ModelTotals so a ledger entry can be re-priced like message tokens."""
    out = {}  # type: Dict[tuple, ModelTotals]
    for row in entry.get("providers") or []:
        model = row.get("model")
        if not model:
            continue
        totals = ModelTotals(model=model, provider=row.get("provider", "claude-code"))
        for name, attr in _FIELDS:
            if name in row:
                try:
                    setattr(totals, attr, int(row[name]))
                except (TypeError, ValueError):
                    pass
        out[totals.key] = totals
    return out


# The counters a rate prices. Web searches are priced apart from the model, and
# reasoning tokens are already inside `out`.
PRICED = ("in", "out", "cw1h", "cw5m", "cr")


def fill_unpriced(entry, table):
    # type: (dict, object) -> Optional[dict]
    """The entry with rows recorded before their model had a rate priced, or
    None when it has none that the table can price now.

    Such a row was never priced at zero by mistake -- its commit reported the
    model as unpriced -- but once the rate exists, zero is simply wrong. It is
    recognised by its money: `compute_cost` priced its web searches and nothing
    else, so its `usd` is exactly what those cost.

    The ledger keeps no speed, so a row is priced as standard, as `tkus
    reprice` prices everything. `filled` marks it, and is a constant rather
    than a time so two people filling the same line write the same bytes.
    """
    when = parse_timestamp(entry.get("until") or entry.get("at") or "")
    if when is None:
        return None
    search_rate = table.web_search_per_1k() / 1000.0
    rows, delta, filled = [], 0.0, 0
    for row in entry.get("providers") or []:
        rows.append(row)
        if (row.get("provider", "claude-code") != "claude-code" or "naiu" in row
                or not any(row.get(name) for name in PRICED)):
            continue
        try:
            recorded = float(row.get("usd") or 0.0)
            searches = int(row.get("ws") or 0)
        except (TypeError, ValueError):
            continue
        if abs(recorded - searches * search_rate) > 1e-9:
            continue
        cost = compute_cost_from_totals(totals_from_entry({"providers": [row]}),
                                        table, when)
        if cost.unpriced_models:
            continue
        rows[-1] = dict(row, usd=round(cost.total, 10), filled=True)
        delta += rows[-1]["usd"] - recorded
        filled += 1
    if not filled:
        return None
    return dict(entry, providers=rows,
                usd=round(float(entry.get("usd") or 0.0) + delta, 10))


def append(repo_root: str, entry: dict) -> None:
    """Add one record. Never raises -- a ledger problem must not fail a commit."""
    try:
        with open(path(repo_root, create=True), "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def read_all(repo_root: str) -> Dict[str, dict]:
    """Every record keyed by SHA, last entry winning.

    A truncated or corrupt line is skipped rather than treated as fatal: the
    ledger is a convenience, and losing one record must not break `reprice`.
    """
    out = {}  # type: Dict[str, dict]
    try:
        with open(path(repo_root), "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                sha = entry.get("sha")
                if sha:
                    out[sha] = entry
    except OSError:
        return {}
    return out


def lookup(repo_root: str, sha: str) -> Optional[dict]:
    return read_all(repo_root).get(sha)
