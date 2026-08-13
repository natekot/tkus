"""Regression against a snapshot of a real Copilot CLI database.

`tests/fixtures/copilot_sample.db` holds 787 real usage rows from 21 sessions.
Everything identifying is replaced: repository names become `acme/project-*`,
branches become `topic-N`, session IDs become sequential, and the self-hosted
endpoint's model name becomes `internal-model` (its real name would reveal
private infrastructure, and the adapter identifies it by the absent rate card
rather than by name). The prose-bearing tables are simply not present.

The numbers below were taken from the source database before redaction.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone

from tkus.pricing import RateTable, compute_cost, compute_cost_from_totals
from tkus.providers.base import aggregate_by_model
from tkus.providers.copilot import CopilotProvider
from tkus import ledger

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "copilot_sample.db")

EXPECTED_ROWS = 787
BILLED_MODEL = "claude-sonnet-4.6"
UNBILLED_MODEL = "internal-model"

EXPECTED_BILLED = {
    "requests": 568,
    "input_tokens": 23377,        # uncached: raw input minus cache read + write
    "output_tokens": 160798,
    "cache_read": 37899857,
    "cache_write_5m": 504385,
}
EXPECTED_UNBILLED = {"requests": 219, "input_tokens": 7989991, "output_tokens": 62194}

EXPECTED_AIU = 1574.350185
EXPECTED_USD = EXPECTED_AIU * 0.01   # GitHub documents 1 AI credit = $0.01


class TestCopilotRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        home = os.path.join(self.tmp, "copilot")
        os.makedirs(home)
        shutil.copy(FIXTURE, os.path.join(home, "session-store.db"))
        os.environ["TKUS_COPILOT_HOME"] = home

        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        # The fixture's sessions span three repositories; this matches one of them.
        subprocess.run(
            ["git", "remote", "add", "origin",
             "https://github.com/acme/project-a.git"], cwd=self.repo, check=True)

        self.all_records = self._collect_all()

    def tearDown(self):
        os.environ.pop("TKUS_COPILOT_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect_all(self):
        """Every row, ignoring repo scoping, by matching each repository in turn."""
        records = []
        seen = set()
        for slug in ("acme/project-a", "acme/project-b", "acme/project-c"):
            subprocess.run(["git", "remote", "set-url", "origin",
                            "https://github.com/%s.git" % slug],
                           cwd=self.repo, check=True)
            for r in CopilotProvider().collect(
                    self.repo, None, datetime(2099, 1, 1, tzinfo=timezone.utc)):
                if r.request_id not in seen:
                    seen.add(r.request_id)
                    records.append(r)
        return records

    def test_row_count(self):
        self.assertEqual(len(self.all_records), EXPECTED_ROWS)

    def test_billed_model_totals(self):
        totals = aggregate_by_model(self.all_records)[("copilot", BILLED_MODEL)]
        for field, expected in EXPECTED_BILLED.items():
            self.assertEqual(getattr(totals, field), expected, field)

    def test_unbilled_model_totals(self):
        totals = aggregate_by_model(self.all_records)[("copilot", UNBILLED_MODEL)]
        for field, expected in EXPECTED_UNBILLED.items():
            self.assertEqual(getattr(totals, field), expected, field)

    def test_cost_from_recorded_aiu(self):
        cost = compute_cost(self.all_records, RateTable.load(None))
        self.assertAlmostEqual(cost.aiu_per_provider["copilot"], EXPECTED_AIU, places=6)
        self.assertAlmostEqual(cost.total, EXPECTED_USD, places=6)

    def test_unbilled_endpoint_costs_nothing_and_is_not_flagged_unpriced(self):
        """Zero here means "not charged", not "we lack a rate" -- flagging it
        would be a false alarm on every commit."""
        unbilled = [r for r in self.all_records if r.unbilled]
        self.assertEqual(len(unbilled), 219)
        cost = compute_cost(unbilled, RateTable.load(None))
        self.assertEqual(cost.total, 0.0)
        self.assertFalse(cost.is_partial)
        self.assertEqual(cost.unpriced_models, [])

    def test_no_rate_table_needed_for_copilot(self):
        """Copilot reports its own price, so an empty rate table changes nothing."""
        empty = RateTable({"version": "test", "currency": "USD", "usd_per_aiu": 0.01})
        cost = compute_cost(self.all_records, empty)
        self.assertAlmostEqual(cost.total, EXPECTED_USD, places=6)
        self.assertFalse(cost.is_partial)

    def test_uncached_input_never_negative(self):
        self.assertTrue(all(r.input_tokens >= 0 for r in self.all_records))

    def test_recorded_cost_matches_embedded_rate_card(self):
        """The property that pins the pricing interpretation: Copilot's own
        total equals the sum over its embedded rate card. Verified here on
        every row of real data that carries one."""
        conn = sqlite3.connect(FIXTURE)
        checked = 0
        for total, details in conn.execute(
                "select total_nano_aiu, token_details_json from assistant_usage_events "
                "where token_details_json is not null and token_details_json != ''"):
            calc = sum(i["tokenCount"] * i["costPerBatch"] // i["batchSize"]
                       for i in json.loads(details))
            self.assertEqual(calc, total)
            checked += 1
        conn.close()
        self.assertEqual(checked, 568)

    def test_repo_scoping_partitions_the_rows(self):
        """Each row belongs to exactly one repository."""
        counts = []
        for slug in ("acme/project-a", "acme/project-b", "acme/project-c"):
            subprocess.run(["git", "remote", "set-url", "origin",
                            "https://github.com/%s.git" % slug],
                           cwd=self.repo, check=True)
            counts.append(len(CopilotProvider().collect(
                self.repo, None, datetime(2099, 1, 1, tzinfo=timezone.utc))))
        self.assertEqual(sum(counts), EXPECTED_ROWS)
        self.assertTrue(all(c > 0 for c in counts))

    def test_ledger_round_trip_preserves_cost(self):
        """Written to the ledger and read back, the cost must survive -- this is
        what makes `tkus reprice` trustworthy for Copilot."""
        table = RateTable.load(None)
        totals = aggregate_by_model(self.all_records)
        cost = compute_cost(self.all_records, table)

        restored = ledger.totals_from_entry(ledger.build_entry(totals, cost, sha="x"))
        reparsed = compute_cost_from_totals(
            restored, table, datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.assertAlmostEqual(reparsed.total, cost.total, places=6)

    def test_fixture_contains_no_prose_tables(self):
        conn = sqlite3.connect(FIXTURE)
        tables = {r[0] for r in conn.execute(
            "select name from sqlite_master where type='table'")}
        conn.close()
        self.assertEqual(tables, {"sessions", "assistant_usage_events"})


if __name__ == "__main__":
    unittest.main()
