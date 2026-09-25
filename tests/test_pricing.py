"""Rate-table and cost tests. Cache handling and date ranges are the risk areas."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from tkus.pricing import RateTable, compute_cost, compute_cost_from_totals
from tkus.providers.base import ModelTotals, UsageRecord

MTOK = 1_000_000


def at(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def record(**kwargs) -> UsageRecord:
    kwargs.setdefault("provider", "test")
    kwargs.setdefault("request_id", "r1")
    kwargs.setdefault("model", "claude-opus-5")
    kwargs.setdefault("timestamp", at("2026-08-09"))
    return UsageRecord(**kwargs)


class TestCachePricing(unittest.TestCase):
    def setUp(self):
        self.table = RateTable.load(None)

    def test_1h_cache_write_is_two_times_input(self):
        """756 of 760 real requests used the 1h TTL. Pricing those at the 5m
        multiplier understates the write line by 60%."""
        cost = compute_cost([record(cache_write_1h=MTOK)], self.table)
        self.assertAlmostEqual(cost.total, 10.0, places=6)  # 5.00 input x 2

    def test_5m_cache_write_is_1_25_times_input(self):
        cost = compute_cost([record(cache_write_5m=MTOK)], self.table)
        self.assertAlmostEqual(cost.total, 6.25, places=6)

    def test_cache_read_is_one_tenth_of_input(self):
        cost = compute_cost([record(cache_read=MTOK)], self.table)
        self.assertAlmostEqual(cost.total, 0.5, places=6)

    def test_ttls_are_not_conflated(self):
        both = compute_cost([record(cache_write_1h=MTOK, cache_write_5m=MTOK)], self.table)
        self.assertAlmostEqual(both.total, 16.25, places=6)

    def test_cache_read_dominates_a_realistic_session(self):
        """The finding that motivated the tool: on real data cache reads were
        58% of cost, and an input+output-only calculation misses them."""
        r = record(input_tokens=190, output_tokens=81772,
                   cache_write_1h=334825, cache_read=13315946)
        full = compute_cost([r], self.table).total
        naive = (190 * 5 + 81772 * 25) / MTOK
        self.assertGreater(full, naive * 5)
        self.assertAlmostEqual(full, 12.05, places=2)


class TestDateRangedRates(unittest.TestCase):
    """The mechanism, exercised on a synthetic table.

    It used to be tested through claude-sonnet-5, whose introductory pricing was
    scheduled to end 2026-08-31. That increase was cancelled, so the real table
    no longer has a dated window -- but date-ranging still has to work, because
    it is what keeps an old commit priced at the rates that applied to it.
    """

    def setUp(self):
        self.table = RateTable({
            "version": "test", "currency": "USD",
            "multipliers": {"cache_write_1h": 2.0, "cache_write_5m": 1.25,
                            "cache_read": 0.1},
            "models": {"claude-opus-5": {"standard": [
                {"from": None, "until": "2026-08-31", "input": 2.0, "output": 10.0},
                {"from": "2026-09-01", "until": None, "input": 3.0, "output": 15.0},
            ]}},
        })

    def test_price_before_the_change(self):
        cost = compute_cost(
            [record(model="claude-opus-5", input_tokens=MTOK,
                    timestamp=at("2026-08-15"))], self.table)
        self.assertAlmostEqual(cost.total, 2.0, places=6)

    def test_price_after_the_change(self):
        cost = compute_cost(
            [record(model="claude-opus-5", input_tokens=MTOK,
                    timestamp=at("2026-09-15"))], self.table)
        self.assertAlmostEqual(cost.total, 3.0, places=6)

    def test_boundary_days(self):
        self.assertEqual(
            self.table.rate_for("claude-opus-5", "standard", at("2026-08-31")),
            (2.0, 10.0))
        self.assertEqual(
            self.table.rate_for("claude-opus-5", "standard", at("2026-09-01")),
            (3.0, 15.0))

    def test_commit_is_priced_at_its_own_date(self):
        """Old commits keep their original cost when the table moves on."""
        old = compute_cost([record(model="claude-opus-5", output_tokens=MTOK,
                                   timestamp=at("2026-08-01"))], self.table)
        new = compute_cost([record(model="claude-opus-5", output_tokens=MTOK,
                                   timestamp=at("2026-10-01"))], self.table)
        self.assertAlmostEqual(old.total, 10.0, places=6)
        self.assertAlmostEqual(new.total, 15.0, places=6)


class TestSonnet5PriceIsFlat(unittest.TestCase):
    """The $2/$10 launch price became the standard price; the increase to
    $3/$15 scheduled for 2026-09-01 was cancelled. tkus carried the schedule,
    so without this it would have started overcharging by 50% on that date."""

    def setUp(self):
        self.table = RateTable.load(None)

    def test_same_price_on_both_sides_of_the_cancelled_date(self):
        for day in ("2026-08-31", "2026-09-01", "2027-06-01"):
            self.assertEqual(
                self.table.rate_for("claude-sonnet-5", "standard", at(day)),
                (2.0, 10.0), "wrong on %s" % day)

    def test_a_commit_after_the_cancelled_date_is_not_overcharged(self):
        cost = compute_cost(
            [record(model="claude-sonnet-5", input_tokens=MTOK,
                    timestamp=at("2026-09-15"))], self.table)
        self.assertAlmostEqual(cost.total, 2.0, places=6)


class TestModifiers(unittest.TestCase):
    def setUp(self):
        self.table = RateTable.load(None)

    def test_fast_mode_prices_higher(self):
        standard = compute_cost([record(output_tokens=MTOK)], self.table)
        fast = compute_cost([record(output_tokens=MTOK, speed="fast")], self.table)
        self.assertAlmostEqual(standard.total, 25.0, places=6)
        self.assertAlmostEqual(fast.total, 50.0, places=6)

    def test_batch_tier_is_half_price(self):
        cost = compute_cost(
            [record(output_tokens=MTOK, service_tier="batch")], self.table)
        self.assertAlmostEqual(cost.total, 12.5, places=6)

    def test_mixed_speed_on_one_model_priced_separately(self):
        """Grouping only by model would flatten fast and standard together."""
        cost = compute_cost(
            [record(request_id="a", output_tokens=MTOK),
             record(request_id="b", output_tokens=MTOK, speed="fast")],
            self.table,
        )
        self.assertAlmostEqual(cost.total, 75.0, places=6)

    def test_web_search_billed_per_thousand_requests(self):
        cost = compute_cost([record(web_search_requests=1000)], self.table)
        self.assertAlmostEqual(cost.total, 10.0, places=6)

    def test_model_without_fast_rates_falls_back_to_standard(self):
        cost = compute_cost(
            [record(model="claude-sonnet-4-6", output_tokens=MTOK, speed="fast")],
            self.table,
        )
        self.assertAlmostEqual(cost.total, 15.0, places=6)


class TestUnknownModels(unittest.TestCase):
    def setUp(self):
        self.table = RateTable.load(None)

    def test_unknown_model_is_flagged_not_zeroed(self):
        """Silently pricing an unrecognised model at zero would undercount a
        real invoice with no visible signal."""
        cost = compute_cost(
            [record(model="claude-future-9", output_tokens=MTOK)], self.table)
        self.assertTrue(cost.is_partial)
        self.assertEqual(cost.unpriced_models, ["claude-future-9"])
        self.assertAlmostEqual(cost.total, 0.0, places=6)

    def test_known_models_still_priced_alongside_unknown(self):
        cost = compute_cost(
            [record(request_id="a", output_tokens=MTOK),
             record(request_id="b", model="claude-future-9", output_tokens=MTOK)],
            self.table,
        )
        self.assertAlmostEqual(cost.total, 25.0, places=6)
        self.assertTrue(cost.is_partial)

    def test_dated_model_id_resolves_through_alias(self):
        cost = compute_cost(
            [record(model="claude-haiku-4-5-20251001", output_tokens=MTOK)], self.table)
        self.assertFalse(cost.is_partial)
        self.assertAlmostEqual(cost.total, 5.0, places=6)


class TestOverrides(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_repo_override_replaces_rate_and_is_flagged(self):
        """Users on Bedrock, Vertex, or a negotiated contract need their own
        rates; the cost line must say it is no longer list price."""
        with open(os.path.join(self.tmp, ".tkus.json"), "w") as fh:
            json.dump({"models": {"claude-opus-5": {
                "standard": [{"from": None, "until": None,
                              "input": 1.0, "output": 2.0}]}}}, fh)

        table = RateTable.load(self.tmp)
        self.assertTrue(table.is_overridden)
        cost = compute_cost([record(output_tokens=MTOK)], table)
        self.assertAlmostEqual(cost.total, 2.0, places=6)
        self.assertTrue(cost.overridden)

    def test_non_rate_config_does_not_claim_rates_were_overridden(self):
        """A config file that only enables the ledger must still report `list`
        pricing -- saying `override` would present a list-price figure as a
        negotiated one."""
        with open(os.path.join(self.tmp, ".tkus.json"), "w") as fh:
            json.dump({"repo_ledger": True}, fh)
        table = RateTable.load(self.tmp)
        self.assertFalse(table.is_overridden)
        self.assertFalse(compute_cost([record(output_tokens=1000)], table).overridden)

    def test_override_merges_rather_than_replaces_whole_table(self):
        with open(os.path.join(self.tmp, ".tkus.json"), "w") as fh:
            json.dump({"models": {"claude-opus-5": {
                "standard": [{"from": None, "until": None,
                              "input": 1.0, "output": 2.0}]}}}, fh)
        table = RateTable.load(self.tmp)
        # An untouched model keeps its bundled rate.
        self.assertEqual(
            table.rate_for("claude-fable-5", "standard", at("2026-08-09")),
            (10.0, 50.0),
        )


class TestTotalsPricing(unittest.TestCase):
    def test_totals_path_matches_record_path(self):
        """`reprice` and squash-merge price from trailer totals; they must
        agree with pricing the original records."""
        table = RateTable.load(None)
        r = record(input_tokens=100, output_tokens=2000,
                   cache_write_1h=5000, cache_read=90000)
        from_records = compute_cost([r], table).total

        totals = {"claude-opus-5": ModelTotals(
            model="claude-opus-5", requests=1, input_tokens=100,
            output_tokens=2000, cache_write_1h=5000, cache_read=90000)}
        from_totals = compute_cost_from_totals(totals, table, at("2026-08-09")).total

        self.assertAlmostEqual(from_records, from_totals, places=9)


if __name__ == "__main__":
    unittest.main()


class TestExplicitCachePrices(unittest.TestCase):
    """A window may state a cache price outright instead of deriving it.

    The multipliers hold for every model but three: Fable 5.1 and Mythos 5.1
    price cache reads at 0.025x input, and Opus 5.5 at 0.05x, where everything
    else is 0.1x. Deriving those would overstate cached reads two- to fourfold,
    on the token category that dominates real agent usage.
    """

    def setUp(self):
        self.table = RateTable({
            "version": "test", "currency": "USD",
            "multipliers": {"cache_write_1h": 2.0, "cache_write_5m": 1.25,
                            "cache_read": 0.1},
            "models": {
                "derived": {"standard": [
                    {"from": None, "until": None, "input": 10.0, "output": 50.0}]},
                "explicit": {"standard": [
                    {"from": None, "until": None, "input": 10.0, "output": 50.0,
                     "cache": {"cache_read": 0.25}}]},
            },
        })

    def test_without_an_override_cache_is_still_a_multiple_of_input(self):
        self.assertEqual(self.table.cache_rates("derived", "standard", at("2026-09-08")),
                         (20.0, 12.5, 1.0))

    def test_an_explicit_price_replaces_the_derived_one(self):
        self.assertEqual(self.table.cache_rates("explicit", "standard", at("2026-09-08")),
                         (20.0, 12.5, 0.25))

    def test_fields_left_out_of_the_override_are_still_derived(self):
        """The override is per field, not all-or-nothing: only cache_read moved."""
        cw1h, cw5m, _ = self.table.cache_rates("explicit", "standard", at("2026-09-08"))
        self.assertEqual((cw1h, cw5m), (20.0, 12.5))

    def test_the_override_reaches_the_price(self):
        cost = compute_cost(
            [record(model="explicit", cache_read=MTOK)], self.table)
        self.assertAlmostEqual(cost.total, 0.25, places=6)

    def test_the_override_reaches_a_reprice_too(self):
        """`tkus reprice` goes through the totals path, which must not diverge."""
        totals = {("test", "explicit"): ModelTotals(
            provider="claude-code", model="explicit", cache_read=MTOK)}
        cost = compute_cost_from_totals(totals, self.table, at("2026-09-08"))
        self.assertAlmostEqual(cost.total, 0.25, places=6)

    def test_deriving_it_would_have_been_four_times_too_much(self):
        """The size of the mistake this exists to prevent."""
        cost = compute_cost([record(model="derived", cache_read=MTOK)], self.table)
        self.assertAlmostEqual(cost.total, 1.0, places=6)


class TestBundledFiveOneModels(unittest.TestCase):
    """The two models that forced the escape hatch, as actually shipped."""

    def setUp(self):
        self.table = RateTable.load(None)

    def test_fable_5_1_prices_cache_reads_at_a_quarter(self):
        cost = compute_cost(
            [record(model="claude-fable-5-1", cache_read=MTOK)], self.table)
        self.assertAlmostEqual(cost.total, 0.25, places=6)

    def test_fable_5_is_unchanged_and_still_derives(self):
        """The point release is a different model, not an alias of the old one."""
        cost = compute_cost(
            [record(model="claude-fable-5", cache_read=MTOK)], self.table)
        self.assertAlmostEqual(cost.total, 1.0, places=6)

    def test_mythos_5_1_prices_cache_reads_at_a_quarter(self):
        cost = compute_cost(
            [record(model="claude-mythos-5-1", cache_read=MTOK)], self.table)
        self.assertAlmostEqual(cost.total, 0.25, places=6)

    def test_input_and_output_are_unaffected(self):
        cost = compute_cost(
            [record(model="claude-fable-5-1", input_tokens=MTOK,
                    output_tokens=MTOK)], self.table)
        self.assertAlmostEqual(cost.total, 60.0, places=6)


class TestBundledOpus55(unittest.TestCase):
    """Opus 5.5 ships on tier_4_20_cache_read_0_20: $4/$20, cache reads at
    $0.20/MTok -- 0.05x input, so deriving them would double the figure."""

    def setUp(self):
        self.table = RateTable.load(None)

    def test_opus_5_5_is_priced(self):
        """Absent from the table, every token it spent was reported unpriced."""
        cost = compute_cost(
            [record(model="claude-opus-5-5", input_tokens=MTOK,
                    output_tokens=MTOK)], self.table)
        self.assertEqual(cost.unpriced_models, [])
        self.assertAlmostEqual(cost.total, 24.0, places=6)

    def test_cache_reads_are_the_stated_price_not_the_derived_one(self):
        cost = compute_cost(
            [record(model="claude-opus-5-5", cache_read=MTOK)], self.table)
        self.assertAlmostEqual(cost.total, 0.2, places=6)

    def test_cache_writes_still_follow_the_multipliers(self):
        self.assertEqual(
            self.table.cache_rates("claude-opus-5-5", "standard", at("2026-09-25")),
            (8.0, 5.0, 0.2))

    def test_opus_5_is_unchanged(self):
        """The point release is a different model, not an alias of the old one."""
        cost = compute_cost(
            [record(model="claude-opus-5", input_tokens=MTOK)], self.table)
        self.assertAlmostEqual(cost.total, 5.0, places=6)
