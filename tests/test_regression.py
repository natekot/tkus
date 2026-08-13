"""Regression against a snapshot of real Claude Code transcripts.

`tests/fixtures/real_sample.jsonl` holds the token counts of 442 real requests.
Everything else is stripped or synthetic: no prompt or response text, request
IDs replaced with sequential placeholders, and timestamps rewritten to a fixed
synthetic sequence -- real ones are account-linked metadata that would reveal
working hours in a public repository, and the regression only needs the counts.

It is checked in so the regression is reproducible on any machine. Pointing the
test at live transcripts would fail everywhere but the machine that recorded
them, and the numbers move while a session is open.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from tkus.pricing import RateTable, compute_cost, compute_cost_from_totals
from tkus.providers.base import aggregate_by_model
from tkus.providers.claude_code import ClaudeCodeProvider
from tkus import ledger

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "real_sample.jsonl")

# Verified against the source transcripts at snapshot time.
EXPECTED_REQUESTS = 442
EXPECTED_MODEL = "claude-opus-5"
EXPECTED_TOKENS = {
    "input_tokens": 823,
    "output_tokens": 358557,
    "cache_write_1h": 1887162,
    "cache_write_5m": 0,
    "cache_read": 94789255,
}
EXPECTED_COST = 75.2342875


class TestRealDataRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        projects = os.path.join(self.tmp, "projects", "-repo")
        os.makedirs(projects)
        shutil.copy(FIXTURE, os.path.join(projects, "session.jsonl"))
        os.environ["TKUS_CLAUDE_PROJECTS"] = os.path.join(self.tmp, "projects")
        self.records = ClaudeCodeProvider().collect(
            "/repo", None, datetime(2099, 1, 1, tzinfo=timezone.utc))

    def tearDown(self):
        os.environ.pop("TKUS_CLAUDE_PROJECTS", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_request_count(self):
        self.assertEqual(len(self.records), EXPECTED_REQUESTS)

    def test_request_ids_unique(self):
        ids = [r.request_id for r in self.records]
        self.assertEqual(len(ids), len(set(ids)))

    def test_token_totals(self):
        totals = aggregate_by_model(self.records)
        self.assertEqual(list(totals), [("claude-code", EXPECTED_MODEL)])
        model = totals[("claude-code", EXPECTED_MODEL)]
        for field, expected in EXPECTED_TOKENS.items():
            self.assertEqual(getattr(model, field), expected, field)

    def test_total_cost(self):
        cost = compute_cost(self.records, RateTable.load(None), when=None)
        self.assertAlmostEqual(cost.total, EXPECTED_COST, places=6)
        self.assertFalse(cost.is_partial)

    def test_cache_reads_dominate(self):
        """The finding the whole tool rests on: pricing only input and output
        would report a small fraction of the real number."""
        cost = compute_cost(self.records, RateTable.load(None), when=None)
        naive = (EXPECTED_TOKENS["input_tokens"] * 5
                 + EXPECTED_TOKENS["output_tokens"] * 25) / 1_000_000.0
        self.assertLess(naive / cost.total, 0.15)

    def test_round_trip_through_the_ledger_preserves_cost(self):
        """Tokens written to the ledger must re-price to the same figure, which
        is what makes `tkus reprice` trustworthy."""
        table = RateTable.load(None)
        totals = aggregate_by_model(self.records)
        cost = compute_cost(self.records, table, when=None)

        entry = ledger.build_entry(totals, cost, sha="x")
        restored = ledger.totals_from_entry(entry)

        when = max(r.timestamp for r in self.records)
        reparsed_cost = compute_cost_from_totals(restored, table, when)
        self.assertAlmostEqual(reparsed_cost.total, cost.total, places=6)

    def test_fixture_contains_no_prose(self):
        """The fixture doubles as evidence for the privacy claim: only usage
        fields are ever read out of a transcript."""
        with open(FIXTURE, "r") as fh:
            body = fh.read()
        for banned in ('"text"', '"content"', '"thinking"', '"input"'):
            self.assertNotIn(banned, body)


if __name__ == "__main__":
    unittest.main()
