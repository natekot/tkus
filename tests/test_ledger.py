"""The local per-commit ledger in `.git/`.

Commit messages are never written to, so all detail lives in a ledger. This one
is keyed by commit SHA and is local to the machine that made the commit; the
shared, durable record is the repository-tracked ledger (see test_repoledger).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone

from tkus import ledger
from tkus.pricing import RateTable, compute_cost, compute_cost_from_totals
from tkus.providers.base import UsageRecord, aggregate_by_model

WHEN = datetime(2026, 8, 9, tzinfo=timezone.utc)


def records():
    return [
        UsageRecord("claude-code", "a", "claude-opus-5", WHEN,
                    input_tokens=98, output_tokens=37204,
                    cache_write_1h=181042, cache_read=7642311),
        UsageRecord("claude-code", "b", "claude-haiku-4-5", WHEN,
                    output_tokens=800, cache_read=200000),
        UsageRecord("copilot", "1", "claude-sonnet-4.6", WHEN,
                    input_tokens=35, output_tokens=1240, cache_write_5m=18227,
                    cache_read=1509003, reasoning_tokens=41, nano_aiu=6961650000),
        UsageRecord("copilot", "2", "internal-model", WHEN,
                    input_tokens=5000, output_tokens=200,
                    nano_aiu=None, unbilled=True),
    ]


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.table = RateTable.load(None)
        self.records = records()
        self.totals = aggregate_by_model(self.records)
        self.cost = compute_cost(self.records, self.table)

    def test_tokens_survive_the_round_trip(self):
        entry = ledger.build_entry(self.totals, self.cost, sha="deadbeef")
        restored = ledger.totals_from_entry(entry)
        self.assertEqual(set(restored), set(self.totals))
        for key, original in self.totals.items():
            self.assertEqual(restored[key], original, key)

    def test_repricing_from_the_ledger_matches_the_original(self):
        """The property the whole design rests on: storing detail in the ledger
        instead of the commit message must not change what `tkus reprice`
        computes."""
        from_ledger = compute_cost_from_totals(
            ledger.totals_from_entry(
                ledger.build_entry(self.totals, self.cost, sha="x")),
            self.table, WHEN)
        self.assertAlmostEqual(from_ledger.total, self.cost.total, places=9)

    def test_entry_records_the_cost_and_rate_version(self):
        entry = ledger.build_entry(self.totals, self.cost, sha="x")
        self.assertAlmostEqual(entry["usd"], self.cost.total, places=9)
        self.assertEqual(entry["rates_version"], self.table.version)
        self.assertEqual(entry["sha"], "x")

    def test_zero_fields_are_omitted(self):
        """Keeps the file readable; absent means zero on the way back in."""
        entry = ledger.build_entry(self.totals, self.cost, sha="x")
        row = [r for r in entry["providers"] if r["model"] == "internal-model"][0]
        self.assertNotIn("cr", row)
        self.assertEqual(row["in"], 5000)

    def test_unbilled_model_is_recorded_with_zero_cost(self):
        entry = ledger.build_entry(self.totals, self.cost, sha="x")
        row = [r for r in entry["providers"] if r["model"] == "internal-model"][0]
        self.assertEqual(row["usd"], 0.0)
        self.assertEqual(row["out"], 200)


class TestStorage(LedgerTestCase):
    def test_missing_file_reads_as_empty(self):
        self.assertEqual(ledger.read_all(self.repo), {})
        self.assertIsNone(ledger.lookup(self.repo, "abc"))

    def test_append_then_read(self):
        ledger.append(self.repo, {"sha": "abc", "usd": 1.5})
        self.assertAlmostEqual(ledger.lookup(self.repo, "abc")["usd"], 1.5)

    def test_last_entry_wins_for_a_sha(self):
        """An amend rewrites a commit; the newest record is the true one."""
        ledger.append(self.repo, {"sha": "abc", "usd": 1.0})
        ledger.append(self.repo, {"sha": "abc", "usd": 2.0})
        self.assertAlmostEqual(ledger.lookup(self.repo, "abc")["usd"], 2.0)

    def test_corrupt_line_is_skipped_not_fatal(self):
        ledger.append(self.repo, {"sha": "good", "usd": 1.0})
        with open(ledger.path(self.repo), "a") as fh:
            fh.write("{not json\n")
        ledger.append(self.repo, {"sha": "later", "usd": 2.0})
        self.assertEqual(set(ledger.read_all(self.repo)), {"good", "later"})

    def test_lives_inside_git_and_is_never_committed(self):
        ledger.append(self.repo, {"sha": "abc", "usd": 1.0})
        self.assertTrue(ledger.path(self.repo).endswith(
            os.path.join(".git", "tkus", "ledger.jsonl")))
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo,
                                stdout=subprocess.PIPE).stdout.decode()
        self.assertNotIn("tkus", status)

    def test_append_never_raises(self):
        """A ledger problem must never fail a commit."""
        ledger.append(self.repo, {"sha": "x", "bad": {1, 2}})   # not JSON-serialisable
        self.assertEqual(ledger.read_all(self.repo), {})


class TestHookIntegration(LedgerTestCase):
    """The detail rides in pending.json because pre-commit has no SHA yet."""

    def test_detail_is_carried_through_pending(self):
        from tkus import cursor
        cursor.write_pending(self.repo, WHEN, detail={"usd": 3.0, "providers": []})
        self.assertAlmostEqual(cursor.read_pending(self.repo)["detail"]["usd"], 3.0)

    def test_pending_without_detail_is_still_valid(self):
        from tkus import cursor
        cursor.write_pending(self.repo, WHEN)
        self.assertIsNone(cursor.read_pending(self.repo).get("detail"))
        self.assertIsNotNone(cursor.promote_pending(self.repo))


if __name__ == "__main__":
    unittest.main()
