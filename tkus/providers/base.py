"""Provider seam: every agent adapter yields UsageRecords for a repo + time window."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

# "2026-08-09T02:10:53.829Z" / "...+00:00" / "...53Z". datetime.fromisoformat
# on Python 3.9 rejects the trailing Z and tolerates only 3 or 6 fractional
# digits, so parse explicitly rather than depending on the stdlib version.
_TS = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d+))?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_timestamp(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    m = _TS.match(value.strip())
    if not m:
        return None
    year, mon, day, hour, minute, sec, frac, tz = m.groups()
    micro = int((frac or "")[:6].ljust(6, "0")) if frac else 0
    try:
        dt = datetime(
            int(year), int(mon), int(day), int(hour), int(minute), int(sec), micro,
            tzinfo=timezone.utc,
        )
    except ValueError:
        # Shape matched but the values are out of range (month 13, hour 99).
        # A corrupt transcript line must not take down the commit hook.
        return None
    if tz and tz != "Z":
        # Wall time was parsed as if UTC; shift by the stated offset to get real UTC.
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        offset_sec = (int(digits[:2]) * 60 + int(digits[2:])) * 60
        dt = datetime.fromtimestamp(dt.timestamp() - sign * offset_sec, timezone.utc)
    return dt


def format_timestamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class UsageRecord:
    """One billable request. Deduplicated by (provider, request_id)."""

    provider: str
    request_id: str
    model: str
    timestamp: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_1h: int = 0
    cache_write_5m: int = 0
    cache_read: int = 0
    web_search_requests: int = 0
    reasoning_tokens: int = 0
    # Pricing modifiers. "standard" | "fast" and "standard" | "batch".
    speed: str = "standard"
    service_tier: str = "standard"
    # Cost the provider itself recorded, in billionths of a GitHub AI Unit.
    # When set, it is authoritative and no rate-table lookup is done -- Copilot
    # reports the exact price it charged. None means "price it ourselves".
    nano_aiu: Optional[int] = None
    # True for endpoints the provider does not bill (a self-hosted model).
    # Distinct from nano_aiu being None: this means genuinely free, not unknown.
    unbilled: bool = False

    @property
    def rate_key(self):
        """Records sharing this key can be priced together."""
        return (self.model, self.speed, self.service_tier)


@dataclass
class ModelTotals:
    """Token totals for one model, as rendered into a single trailer line."""

    model: str
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_1h: int = 0
    cache_write_5m: int = 0
    cache_read: int = 0
    web_search_requests: int = 0
    reasoning_tokens: int = 0
    nano_aiu: int = 0
    # Which adapter produced this. Two providers can report the same model name,
    # so totals are keyed by (provider, model) and reported separately.
    provider: str = "claude-code"

    @property
    def key(self):
        return (self.provider, self.model)

    def add(self, other: "ModelTotals") -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_1h += other.cache_write_1h
        self.cache_write_5m += other.cache_write_5m
        self.cache_read += other.cache_read
        self.web_search_requests += other.web_search_requests
        self.reasoning_tokens += other.reasoning_tokens
        self.nano_aiu += other.nano_aiu

    @property
    def is_empty(self) -> bool:
        return not (
            self.input_tokens
            or self.output_tokens
            or self.cache_write_1h
            or self.cache_write_5m
            or self.cache_read
            or self.web_search_requests
            or self.reasoning_tokens
        )


def aggregate_by_model(records: Iterable[UsageRecord]) -> Dict[tuple, ModelTotals]:
    """Sum records per (provider, model), for trailer rendering.

    Keyed by provider as well as model because two providers can report the same
    model name -- Copilot serves `claude-sonnet-4.6` alongside Claude Code's own
    models, and merging them would produce one line attributed to neither.

    Pricing does NOT use this grouping -- a commit can mix standard and fast
    requests on the same model, which price differently. Cost is computed from
    the finer `rate_key` buckets; this coarser view is display only.
    """
    out = {}  # type: Dict[tuple, ModelTotals]
    for r in records:
        key = (r.provider, r.model)
        t = out.setdefault(key, ModelTotals(model=r.model, provider=r.provider))
        t.requests += 1
        t.input_tokens += r.input_tokens
        t.output_tokens += r.output_tokens
        t.cache_write_1h += r.cache_write_1h
        t.cache_write_5m += r.cache_write_5m
        t.cache_read += r.cache_read
        t.web_search_requests += r.web_search_requests
        t.reasoning_tokens += r.reasoning_tokens
        t.nano_aiu += r.nano_aiu or 0
    return out


class Provider:
    """Adapter interface. Implementations read a local transcript store.

    Only Claude Code is implemented. Codex and Copilot would be written against
    formats that could not be verified or tested on this machine, so they are
    deliberately absent rather than speculative.
    """

    name = "base"

    def collect(self, repo_root, since, until):
        # type: (str, Optional[datetime], datetime) -> List[UsageRecord]
        raise NotImplementedError


_REGISTRY = []  # type: List[Provider]


def register(provider: Provider) -> Provider:
    _REGISTRY.append(provider)
    return provider


def all_providers() -> List[Provider]:
    return list(_REGISTRY)


def collect_all(repo_root, since, until):
    # type: (str, Optional[datetime], datetime) -> List[UsageRecord]
    """Collect from every registered provider, deduplicated across providers."""
    seen = set()
    out = []  # type: List[UsageRecord]
    for provider in all_providers():
        for record in provider.collect(repo_root, since, until):
            key = (record.provider, record.request_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(record)
    out.sort(key=lambda r: r.timestamp)
    return out
