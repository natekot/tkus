"""Date-ranged rate table and cost computation.

Rates change. A commit made in August must stay priced with August's rates, so
every entry carries a validity window and each commit is priced with the rates
in effect on its own date. The table version is stamped into the commit message
so a stale table is visible rather than silent.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from .providers.base import UsageRecord

BUNDLED_RATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rates.json")
REPO_OVERRIDE = ".tkus.json"


class RateTableError(Exception):
    """A rate override exists but cannot be read.

    Raised rather than ignored: a malformed override must not silently fall
    back to list prices, because the reason to have one is usually a negotiated
    rate, and quietly billing at list would be worse than failing.
    """


def _global_config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "tkus")


def _global_overrides():
    # rates.json came first and keeps working; config.json is the better home
    # for settings that are not rates, such as the trailer format.
    directory = _global_config_dir()
    return [os.path.join(directory, "rates.json"),
            os.path.join(directory, "config.json")]


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class RateTable:
    """Bundled list prices, with user overrides merged on top."""

    # Keys whose presence in an override actually changes a price. A config file
    # that only sets something like `trailer_format` must not make the cost line
    # claim the rates were overridden -- that would misrepresent a list-price
    # figure as a negotiated one.
    RATE_KEYS = frozenset([
        "models", "multipliers", "tier_multipliers", "server_tools",
        "usd_per_aiu", "aliases", "currency",
    ])

    def __init__(self, data, sources=None, rates_overridden=False):
        # type: (dict, Optional[List[str]], bool) -> None
        self.data = data
        self.sources = sources or []
        self._rates_overridden = rates_overridden

    @property
    def version(self) -> str:
        return str(self.data.get("version", "unknown"))

    @property
    def currency(self) -> str:
        return str(self.data.get("currency", "USD"))

    @property
    def is_overridden(self) -> bool:
        """True only when a user override actually changed a price."""
        return self._rates_overridden

    @classmethod
    def load(cls, repo_root: Optional[str] = None) -> "RateTable":
        with open(BUNDLED_RATES, "r") as fh:
            data = json.load(fh)
        sources = [BUNDLED_RATES]
        candidates = list(_global_overrides())
        candidates.append(os.path.join(repo_root or "", REPO_OVERRIDE))
        rates_overridden = False
        for path in candidates:
            if repo_root is None and path.endswith(REPO_OVERRIDE):
                continue
            if os.path.isfile(path):
                try:
                    with open(path, "r") as fh:
                        override = json.load(fh)
                except (OSError, ValueError) as exc:
                    raise RateTableError("%s: %s" % (path, exc))
                if not isinstance(override, dict):
                    raise RateTableError("%s: expected a JSON object" % path)
                if any(key in cls.RATE_KEYS for key in override):
                    rates_overridden = True
                data = _deep_merge(data, override)
                sources.append(path)
        return cls(data, sources, rates_overridden)

    def canonical(self, model: str) -> str:
        return self.data.get("aliases", {}).get(model, model)

    def rate_for(self, model: str, speed: str, when: datetime):
        # type: (...) -> Optional[Tuple[float, float]]
        """(input, output) USD per MTok for a model at a point in time."""
        entry = self.data.get("models", {}).get(self.canonical(model))
        if not entry:
            return None
        # Fast mode has its own price list; fall back to standard if absent.
        windows = entry.get(speed) or entry.get("standard") or []
        day = when.strftime("%Y-%m-%d")
        for window in windows:
            start, end = window.get("from"), window.get("until")
            if start and day < start:
                continue
            if end and day > end:
                continue
            return (float(window["input"]), float(window["output"]))
        return None

    def multiplier(self, name: str, default: float) -> float:
        return float(self.data.get("multipliers", {}).get(name, default))

    def tier_multiplier(self, tier: str) -> float:
        return float(self.data.get("tier_multipliers", {}).get(tier, 1.0))

    def web_search_per_1k(self) -> float:
        return float(
            self.data.get("server_tools", {}).get("web_search_per_1k_requests", 0.0)
        )

    def usd_per_aiu(self) -> float:
        """Dollar value of one GitHub AI Unit. GitHub documents 1 credit = $0.01."""
        return float(self.data.get("usd_per_aiu", 0.01))


@dataclass
class Cost:
    total: float = 0.0
    currency: str = "USD"
    rates_version: str = "unknown"
    overridden: bool = False
    unpriced_models: List[str] = field(default_factory=list)
    # Keyed by (provider, model), matching aggregate_by_model.
    per_model: Dict[tuple, float] = field(default_factory=dict)
    per_provider: Dict[str, float] = field(default_factory=dict)
    # AI Units per provider, for providers that report cost natively.
    aiu_per_provider: Dict[str, float] = field(default_factory=dict)

    @property
    def is_partial(self) -> bool:
        return bool(self.unpriced_models)

    def for_provider(self, provider: str) -> "Cost":
        """A view of this Cost restricted to one provider, for its own trailer."""
        return Cost(
            total=self.per_provider.get(provider, 0.0),
            currency=self.currency,
            rates_version=self.rates_version,
            overridden=self.overridden,
            unpriced_models=list(self.unpriced_models),
            per_model={k: v for k, v in self.per_model.items() if k[0] == provider},
            per_provider={provider: self.per_provider.get(provider, 0.0)},
            aiu_per_provider={provider: self.aiu_per_provider.get(provider, 0.0)},
        )


def compute_cost(records, table, when=None):
    # type: (Iterable[UsageRecord], RateTable, Optional[datetime]) -> Cost
    """Price records, bucketing by (model, speed, tier) so mixed modes stay correct.

    A commit can mix standard and fast requests on the same model, which price
    differently -- grouping only by model would flatten that distinction.
    """
    records = list(records)
    cost = Cost(
        currency=table.currency,
        rates_version=table.version,
        overridden=table.is_overridden,
    )

    buckets = {}  # type: Dict[tuple, List[UsageRecord]]
    for record in records:
        buckets.setdefault((record.provider,) + record.rate_key, []).append(record)

    cw1h = table.multiplier("cache_write_1h", 2.0)
    cw5m = table.multiplier("cache_write_5m", 1.25)
    cread = table.multiplier("cache_read", 0.1)
    search_rate = table.web_search_per_1k()
    aiu_rate = table.usd_per_aiu()

    unpriced = set()
    for (provider, model, speed, tier), group in buckets.items():
        searches = sum(r.web_search_requests for r in group)
        subtotal = searches / 1000.0 * search_rate
        aiu_total = 0.0

        # A rate lookup is only needed for records that do not carry their own
        # cost and are not on an unbilled endpoint. Looking it up regardless
        # would flag a model as "unpriced" even when nothing needed pricing.
        needs_rate = any(r.nano_aiu is None and not r.unbilled for r in group)
        rate = None
        if needs_rate:
            # Price each bucket at the rates in effect for its own window.
            stamp = when or max(r.timestamp for r in group)
            rate = table.rate_for(model, speed, stamp)
            if rate is None:
                # Never price an unknown model as zero -- that silently undercounts.
                unpriced.add(model)

        tier_mult = table.tier_multiplier(tier)
        for r in group:
            if r.nano_aiu is not None:
                # The provider recorded the exact price it charged.
                aiu = r.nano_aiu / 1e9
                aiu_total += aiu
                subtotal += aiu * aiu_rate
            elif r.unbilled:
                # Self-hosted endpoint: real compute, but nothing is billed.
                continue
            elif rate is not None:
                rate_in, rate_out = rate
                subtotal += (
                    r.input_tokens * rate_in
                    + r.output_tokens * rate_out
                    + r.cache_write_1h * rate_in * cw1h
                    + r.cache_write_5m * rate_in * cw5m
                    + r.cache_read * rate_in * cread
                ) / 1_000_000.0 * tier_mult

        key = (provider, model)
        cost.per_model[key] = cost.per_model.get(key, 0.0) + subtotal
        cost.per_provider[provider] = cost.per_provider.get(provider, 0.0) + subtotal
        if aiu_total:
            cost.aiu_per_provider[provider] = (
                cost.aiu_per_provider.get(provider, 0.0) + aiu_total)
        cost.total += subtotal

    cost.unpriced_models = sorted(unpriced)
    return cost


def compute_cost_from_totals(totals_by_model, table, when):
    # type: (dict, RateTable, datetime) -> Cost
    """Price already-aggregated per-model totals.

    Used when re-pricing history and when merging squashed commits, where the
    original records are gone and only trailer token counts survive. Those
    counts carry no speed or tier, so everything is priced as standard --
    a fast-mode request folded into a squash is priced at standard rates.
    """
    cost = Cost(
        currency=table.currency,
        rates_version=table.version,
        overridden=table.is_overridden,
    )
    cw1h = table.multiplier("cache_write_1h", 2.0)
    cw5m = table.multiplier("cache_write_5m", 1.25)
    cread = table.multiplier("cache_read", 0.1)
    search_rate = table.web_search_per_1k()
    aiu_rate = table.usd_per_aiu()

    unpriced = set()
    for t in totals_by_model.values():
        subtotal = t.web_search_requests / 1000.0 * search_rate

        if t.nano_aiu:
            # Provider-reported cost survives the trailer round-trip, so a
            # re-price uses the recorded figure rather than guessing a rate.
            aiu = t.nano_aiu / 1e9
            subtotal += aiu * aiu_rate
            cost.aiu_per_provider[t.provider] = (
                cost.aiu_per_provider.get(t.provider, 0.0) + aiu)
        elif t.provider == "claude-code":
            rate = table.rate_for(t.model, "standard", when)
            if rate is None:
                unpriced.add(t.model)
            else:
                rate_in, rate_out = rate
                subtotal += (
                    t.input_tokens * rate_in
                    + t.output_tokens * rate_out
                    + t.cache_write_1h * rate_in * cw1h
                    + t.cache_write_5m * rate_in * cw5m
                    + t.cache_read * rate_in * cread
                ) / 1_000_000.0
        # Other providers with no recorded AIU are unbilled endpoints: zero.

        cost.per_model[t.key] = cost.per_model.get(t.key, 0.0) + subtotal
        cost.per_provider[t.provider] = cost.per_provider.get(t.provider, 0.0) + subtotal
        cost.total += subtotal

    cost.unpriced_models = sorted(unpriced)
    return cost
