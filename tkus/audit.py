"""Reconciling transcripts and telemetry against what was billed.

Three figures for the same requests:

  transcripts  every Claude Code request found on disk, priced by tkus
  telemetry    the same requests as Claude Code reported them, plus the ones
               the transcripts never record (compaction, WebFetch/WebSearch
               sub-requests, helpers) at the price Claude Code gave them
  billed       the month-to-date spend `/usage` shows

Telemetry only exists from when the receiver was set up, so its figure covers
its own window, never the month before it.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional

from .pricing import RateTable, compute_cost
from .providers.base import UsageRecord


def _tokens(record: UsageRecord) -> tuple:
    return (record.input_tokens, record.output_tokens, record.cache_read,
            record.cache_write_1h + record.cache_write_5m)


def _event_tokens(event: dict) -> tuple:
    return tuple(int(event.get(key) or 0) for key in (
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_creation_tokens"))


def reconcile(records, events, table, since, until):
    # type: (List[UsageRecord], List[dict], RateTable, datetime, datetime) -> OrderedDict
    """Compare transcript records and telemetry events within (since, until].

    Requests are matched by request id. A transcript request with no event
    came from a session that was not sending telemetry (started before setup,
    or while the receiver was down); its count says how far to trust the
    telemetry figure. An event with no transcript request is one the
    transcripts missed.
    """
    window = [r for r in records if since < r.timestamp <= until]
    requests = [e for e in events if e.get("event") == "api_request"
                and since < e["at"] <= until]
    errors = [e for e in events if e.get("event") == "api_error"
              and since < e["at"] <= until]
    by_id = {e.get("request_id"): e for e in requests if e.get("request_id")}
    record_ids = {r.request_id for r in window}

    matched = [r for r in window if r.request_id in by_id]
    unreported = [r for r in window if r.request_id not in by_id]
    hidden = [e for e in requests if e.get("request_id") not in record_ids]
    differ = [r.request_id for r in matched
              if _tokens(r) != _event_tokens(by_id[r.request_id])]

    transcripts_usd = compute_cost(window, table, when=None).total
    matched_usd = compute_cost(matched, table, when=None).total
    hidden_usd = sum(int(e.get("cost_usd_micros") or 0) for e in hidden) / 1e6
    # Claude Code's own price for the requests both sources saw: a second
    # opinion on tkus's rate table, independent of the bill.
    claude_matched_usd = sum(int(by_id[r.request_id].get("cost_usd_micros") or 0)
                             for r in matched) / 1e6

    groups = {}  # type: Dict[tuple, List[dict]]
    for event in hidden:
        key = (str(event.get("query_source") or "unknown"),
               str(event.get("model") or "unknown"))
        groups.setdefault(key, []).append(event)
    breakdown = [
        OrderedDict([("query_source", source), ("model", model),
                     ("calls", len(group)),
                     ("usd", round(sum(int(e.get("cost_usd_micros") or 0)
                                       for e in group) / 1e6, 6))])
        for (source, model), group in groups.items()
    ]
    breakdown.sort(key=lambda row: -row["usd"])

    return OrderedDict([
        ("since", since),
        ("until", until),
        ("transcripts_usd", round(transcripts_usd, 6)),
        ("telemetry_usd", round(matched_usd + hidden_usd, 6)),
        ("matched", len(matched)),
        ("matched_usd", round(matched_usd, 6)),
        ("claude_priced_matched_usd", round(claude_matched_usd, 6)),
        ("hidden", len(hidden)),
        ("hidden_usd", round(hidden_usd, 6)),
        ("hidden_by_source", breakdown),
        ("unreported", len(unreported)),
        ("unreported_usd", round(compute_cost(unreported, table, when=None).total, 6)),
        ("token_mismatches", len(differ)),
        ("errors", len(errors)),
    ])


def coverage_gap(starts, since, until, up_now):
    # type: (List[datetime], datetime, datetime, bool) -> Optional[str]
    """Why telemetry cannot be trusted for (since, until], or None if it can."""
    if not starts or starts[0] > since:
        return "the receiver was not running at %s" % since.isoformat()
    restarted = [s for s in starts if since < s <= until]
    if restarted:
        return "the receiver restarted at %s, so it was down before that" % (
            restarted[0].isoformat())
    if not up_now:
        return "the receiver is not running now"
    return None


def previous_run(history, month, before):
    # type: (List[dict], str, str) -> Optional[dict]
    """The latest earlier audit of the same billing month, for an interval."""
    earlier = [h for h in history
               if h.get("month") == month and h.get("at", "") < before
               and isinstance(h.get("billed"), (int, float))]
    return max(earlier, key=lambda h: h["at"]) if earlier else None
