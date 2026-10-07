"""`tkus reprice --fill` -- pricing rows recorded before their model had a rate.

A model missing from the rate table is never priced at zero silently: the
commit's report says it is unpriced. But the ledger row is still written, with
only its web searches in `usd`, and adding the rate later changed nothing that
was already committed. These rows are filled in, once, and marked as such.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tkus import ledger, repoledger
from tkus.pricing import RateTable

from .test_rollup import RollupHarness

UNKNOWN = "claude-test-unknown"
RATE = {"standard": [{"from": None, "until": None, "input": 5.0, "output": 25.0}]}


def table(*priced):
    return RateTable({"version": "t", "currency": "USD",
                      "models": {m: RATE for m in priced},
                      "server_tools": {"web_search_per_1k_requests": 10.0}})


def entry(*rows):
    return {"at": "2026-09-25T18:51:38.424Z", "since": "2026-09-25T18:00:00.000Z",
            "until": "2026-09-25T18:51:38.424Z", "parent": "abc", "currency": "USD",
            "rates_version": "t", "usd": round(sum(r["usd"] for r in rows), 10),
            "providers": list(rows)}


def row(model=UNKNOWN, usd=0.0, **counters):
    out = {"provider": "claude-code", "model": model, "usd": usd}
    out.update(counters or {"out": 1000})
    return out


class TestWhichRowsAreFilled(unittest.TestCase):
    def test_a_row_the_table_now_prices_is_filled(self):
        filled = ledger.fill_unpriced(entry(row(out=1000)), table(UNKNOWN))
        [only] = filled["providers"]
        self.assertAlmostEqual(only["usd"], 0.025)
        self.assertIs(only["filled"], True)
        self.assertAlmostEqual(filled["usd"], 0.025)

    def test_the_entry_total_keeps_its_priced_rows(self):
        original = entry(row("claude-known", usd=0.5, out=20000), row(out=1000))
        filled = ledger.fill_unpriced(original, table(UNKNOWN, "claude-known"))
        self.assertAlmostEqual(filled["usd"], 0.525)
        self.assertNotIn("filled", filled["providers"][0])

    def test_a_priced_row_is_left_alone(self):
        self.assertIsNone(ledger.fill_unpriced(
            entry(row(usd=0.025, out=1000)), table(UNKNOWN)))

    def test_a_model_still_without_a_rate_is_left_alone(self):
        self.assertIsNone(ledger.fill_unpriced(entry(row(out=1000)), table()))

    def test_copilot_rows_are_never_filled(self):
        """Copilot records its own cost; one with none is an unbilled endpoint."""
        billed = dict(row(usd=0.0, naiu=0, out=1000), provider="copilot")
        unbilled = dict(row(usd=0.0, out=1000), provider="copilot")
        self.assertIsNone(ledger.fill_unpriced(entry(billed), table(UNKNOWN)))
        self.assertIsNone(ledger.fill_unpriced(entry(unbilled), table(UNKNOWN)))

    def test_a_row_whose_only_cost_was_web_searches_is_filled(self):
        """The search rate applies to any model, so an unpriced row that searched
        was recorded at its search cost, not at zero."""
        searched = row(usd=0.02, out=1000, ws=2)
        [only] = ledger.fill_unpriced(entry(searched), table(UNKNOWN))["providers"]
        self.assertAlmostEqual(only["usd"], 0.045)

    def test_a_row_with_no_tokens_is_left_alone(self):
        self.assertIsNone(ledger.fill_unpriced(
            entry(row(usd=0.02, ws=2)), table(UNKNOWN)))

    def test_the_entry_passed_in_is_not_changed(self):
        original = entry(row(out=1000))
        before = json.dumps(original, sort_keys=True)
        ledger.fill_unpriced(original, table(UNKNOWN))
        self.assertEqual(json.dumps(original, sort_keys=True), before)

    def test_filling_is_idempotent(self):
        once = ledger.fill_unpriced(entry(row(out=1000)), table(UNKNOWN))
        self.assertIsNone(ledger.fill_unpriced(once, table(UNKNOWN)))

    def test_two_fills_write_the_same_bytes(self):
        """Two people filling the same line must not conflict when both merge."""
        a = ledger.fill_unpriced(entry(row(out=1000)), table(UNKNOWN))
        b = ledger.fill_unpriced(entry(row(out=1000)), table(UNKNOWN))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_the_entry_keeps_its_identity(self):
        """`tkus log` finds an entry's commit by its key, across rewrites."""
        original = entry(row(out=1000))
        filled = ledger.fill_unpriced(original, table(UNKNOWN))
        self.assertEqual(repoledger.entry_key(filled), repoledger.entry_key(original))


class FillHarness(RollupHarness):
    MINE = "%s/Tester/main.jsonl" % repoledger.LEDGER_DIR

    def tkus(self, *args):
        out = subprocess.run([sys.executable, "-m", "tkus"] + list(args),
                             cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        return out.stdout.decode()

    def price_unknown(self):
        """Add the missing rate, as a later release of rates.json would."""
        with open(os.path.join(self.repo, ".tkus.json"), "w") as fh:
            json.dump({"models": {UNKNOWN: RATE}}, fh)

    def record_unpriced(self, output_tokens=1000):
        self.inject_usage(output_tokens, model=UNKNOWN)
        self.commit("used a model with no rate")
        return self.git("rev-parse", "HEAD").strip()

    def rows(self, rel, ref="HEAD"):
        blob = self.git("show", "%s:%s" % (ref, rel))
        return [r for line in blob.split("\n") if line.strip()
                for r in json.loads(line)["providers"] if r["model"] == UNKNOWN]


class TestFillCommand(FillHarness):
    def test_the_row_is_recorded_unpriced_to_begin_with(self):
        self.record_unpriced()
        [only] = self.rows(self.MINE)
        self.assertEqual(only["usd"], 0.0)

    def test_reprice_says_there_is_something_to_fill(self):
        self.record_unpriced()
        self.price_unknown()
        self.assertIn("tkus reprice --fill", self.tkus("reprice"))

    def test_reprice_is_quiet_when_there_is_nothing_to_fill(self):
        self.record_unpriced()
        self.assertNotIn("--fill", self.tkus("reprice"))

    def test_fill_without_yes_is_a_dry_run(self):
        self.record_unpriced()
        self.price_unknown()
        path = repoledger.absolute_path(self.repo, self.MINE)
        with open(path) as fh:
            before = fh.read()

        out = self.tkus("reprice", "--fill")
        self.assertIn(self.MINE, out)
        self.assertIn(UNKNOWN, out)
        self.assertIn("dry run", out)
        with open(path) as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_fill_with_yes_stages_it_for_the_next_commit(self):
        self.record_unpriced()
        self.price_unknown()
        self.tkus("reprice", "--fill", "--yes")
        self.assertIn(self.MINE, self.git("diff", "--cached", "--name-only"))

        self.commit("fill")
        [only] = self.rows(self.MINE)
        self.assertAlmostEqual(only["usd"], 0.025)
        self.assertIs(only["filled"], True)
        self.assertAlmostEqual(self.as_json()["total"], 0.025)

    def test_nothing_to_fill_writes_nothing(self):
        self.inject_usage(1000)
        self.commit("priced")
        self.assertIn("nothing to fill", self.tkus("reprice", "--fill", "--yes"))
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_another_identitys_file_is_filled_too(self):
        rel = "%s/Someone/feature.jsonl" % repoledger.LEDGER_DIR
        self.add_ledger(rel, entry(row(out=1000)))
        self.price_unknown()
        self.tkus("reprice", "--fill", "--yes")
        self.commit("fill")
        [only] = self.rows(rel)
        self.assertAlmostEqual(only["usd"], 0.025)


class TestTheHookKeepsTheFill(FillHarness):
    def test_a_commit_on_the_branch_does_not_undo_a_staged_fill(self):
        """pre-commit rebuilds the branch's file from HEAD, where the row is
        still unpriced -- so without filling there too, it would revert it."""
        self.record_unpriced()
        self.price_unknown()
        self.tkus("reprice", "--fill", "--yes")
        self.inject_usage(500)
        self.commit("more work, carrying the fill")
        [only] = self.rows(self.MINE)
        self.assertAlmostEqual(only["usd"], 0.025)

    def test_a_commit_on_the_branch_fills_its_own_file(self):
        self.record_unpriced()
        self.price_unknown()
        self.inject_usage(500)
        self.commit("more work")
        [only] = self.rows(self.MINE)
        self.assertIs(only["filled"], True)


class TestAttributionSurvivesTheFill(FillHarness):
    def test_log_still_credits_the_commit_that_recorded_it(self):
        recorded = self.record_unpriced()
        self.price_unknown()
        self.tkus("reprice", "--fill", "--yes")
        self.commit("fill")

        data = json.loads(self.tkus("log", "--json"))
        self.assertEqual([(c["sha"], round(c["usd"], 6)) for c in data["commits"]],
                         [(recorded, 0.025)])
        self.assertEqual(data["orphaned"], [])


if __name__ == "__main__":
    unittest.main()
