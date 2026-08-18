"""`tkus rates`: the rate table, resolved into money.

The table stores cache prices as multipliers on the input rate. Those are the
numbers a reader cannot evaluate in their head, and cache reads are usually the
largest token class in a session, so the command's job is to resolve them --
accurately enough to hand-check an invoice against.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

from tkus.__main__ import _money, _rate_rows, _scheduled_changes
from tkus.pricing import RateTable

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AUGUST = datetime(2026, 8, 18, tzinfo=timezone.utc)
SEPTEMBER = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _row(rows, model, speed="standard"):
    for row in rows:
        if row["model"] == model and row["speed"] == speed:
            return row
    return None


class TestMoneyFormatting(unittest.TestCase):
    def test_plain_values_stay_at_two_places(self):
        self.assertEqual(_money(0.5), "0.50")
        self.assertEqual(_money(20.0), "20.00")

    def test_widens_rather_than_rounding_away_precision(self):
        """1.25 x a negotiated 3.50 is 4.375; showing 4.38 misstates the rate."""
        self.assertEqual(_money(4.375), "4.3750")
        self.assertEqual(_money(0.175), "0.1750")

    def test_never_silently_truncates(self):
        for value in (0.1, 0.175, 4.375, 0.0625, 12.5, 1.0):
            self.assertAlmostEqual(float(_money(value)), value, places=6)


class TestRateRows(unittest.TestCase):
    def setUp(self):
        self.table = RateTable.load()

    def test_multipliers_are_resolved_into_money(self):
        row = _row(_rate_rows(self.table, AUGUST), "claude-opus-5")
        self.assertEqual(row["input"], 5.0)
        self.assertEqual(row["cache_write_1h"], 10.0)   # 2.0x
        self.assertEqual(row["cache_write_5m"], 6.25)   # 1.25x
        self.assertEqual(row["cache_read"], 0.5)        # 0.1x

    def test_fast_tier_listed_only_where_declared(self):
        rows = _rate_rows(self.table, AUGUST)
        self.assertIsNotNone(_row(rows, "claude-opus-5", "fast"))
        # haiku declares no fast pricing; rate_for would fall back to standard,
        # which would invent a tier the table does not offer.
        self.assertIsNone(_row(rows, "claude-haiku-4-5", "fast"))

    def test_rates_follow_the_date(self):
        """Every current rate is flat, so the same date logic must return the
        same numbers rather than silently stepping."""
        before = _row(_rate_rows(self.table, AUGUST), "claude-sonnet-5")
        after = _row(_rate_rows(self.table, SEPTEMBER), "claude-sonnet-5")
        self.assertEqual(before["input"], 2.0)
        self.assertEqual(after["input"], 2.0)
        self.assertEqual(before["cache_read"], 0.2)
        self.assertEqual(after["cache_read"], 0.2)


class TestScheduledChanges(unittest.TestCase):
    def setUp(self):
        self.table = RateTable.load()

    def test_a_future_increase_is_surfaced(self):
        """A price rise that arrives silently is the failure mode worth avoiding.

        Exercised on a synthetic table: the bundled one currently schedules no
        increases, since Sonnet 5's was cancelled.
        """
        table = RateTable({
            "version": "test", "currency": "USD",
            "models": {"claude-x": {"standard": [
                {"from": None, "until": "2026-08-31", "input": 2.0, "output": 10.0},
                {"from": "2026-09-01", "until": None, "input": 3.0, "output": 15.0},
            ]}},
        })
        changes = _scheduled_changes(table, AUGUST)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["from"], "2026-09-01")
        self.assertEqual((changes[0]["from_input"], changes[0]["input"]), (2.0, 3.0))

    def test_the_bundled_table_schedules_no_increase(self):
        self.assertEqual(_scheduled_changes(self.table, AUGUST), [])

    def test_nothing_scheduled_once_it_has_taken_effect(self):
        changes = _scheduled_changes(self.table, SEPTEMBER)
        self.assertEqual([c for c in changes if c["model"] == "claude-sonnet-5"], [])


class TestRatesCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = dict(os.environ, PYTHONPATH=PROJECT)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_rates(self, *args, **kw):
        out = subprocess.run(
            [sys.executable, "-m", "tkus", "rates"] + list(args),
            cwd=kw.get("cwd", self.tmp), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return out.returncode, out.stdout.decode()

    def test_runs_outside_a_git_repository(self):
        """Rates are global; refusing to show them outside a repo helps nobody."""
        code, text = self.run_rates()
        self.assertEqual(code, 0)
        self.assertIn("claude-opus-5", text)
        self.assertIn("not inside a git repository", text)

    def test_reports_the_aiu_conversion(self):
        _, text = self.run_rates()
        self.assertIn("1 AIU", text)

    def test_at_selects_the_date(self):
        _, text = self.run_rates("--at", "2026-09-01")
        self.assertIn("effective 2026-09-01", text)

    def test_a_bad_date_fails_loudly(self):
        code, text = self.run_rates("--at", "09-01-2026")
        self.assertEqual(code, 1)
        self.assertIn("YYYY-MM-DD", text)

    def test_json_is_parseable_and_carries_the_unit(self):
        code, text = self.run_rates("--json")
        self.assertEqual(code, 0)
        data = json.loads(text)
        self.assertIn("1000000", data["unit"])
        self.assertFalse(data["overridden"])
        opus = _row(data["models"], "claude-opus-5")
        self.assertEqual(opus["cache_read"], 0.5)

    def test_an_override_is_applied_and_declared(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        with open(os.path.join(repo, ".tkus.json"), "w") as fh:
            json.dump({"models": {"claude-opus-5": {"standard": [
                {"from": None, "until": None, "input": 3.5, "output": 17.5}]}}}, fh)
        code, text = self.run_rates("--json", cwd=repo)
        self.assertEqual(code, 0)
        data = json.loads(text)
        self.assertTrue(data["overridden"])
        opus = _row(data["models"], "claude-opus-5")
        self.assertEqual(opus["input"], 3.5)
        self.assertAlmostEqual(opus["cache_read"], 0.35)

    def test_negotiated_rates_print_without_rounding_error(self):
        repo = os.path.join(self.tmp, "repo2")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        with open(os.path.join(repo, ".tkus.json"), "w") as fh:
            json.dump({"models": {"claude-opus-5": {"standard": [
                {"from": None, "until": None, "input": 3.5, "output": 17.5}]}}}, fh)
        _, text = self.run_rates(cwd=repo)
        line = [l for l in text.split("\n")
                if l.startswith("claude-opus-5") and "standard" in l][0]
        self.assertIn("4.3750", line)      # 1.25 x 3.50, not 4.38
        self.assertNotIn("4.38 ", line)


if __name__ == "__main__":
    unittest.main()
