"""`tkus rollup --json` -- the ledger totals, for things that are not people.

The JSON is what a reporting tool built on tkus consumes, so the property that
matters is that it is the same money the table shows: never a second
calculation that could drift from the first.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tkus import repoledger

from .test_repoledger import RepoLedgerTestCase


class RollupHarness(RepoLedgerTestCase):
    """The repo harness plus a way to run `tkus rollup` and read its output."""

    def rollup(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "tkus", "rollup"] + list(args),
            cwd=self.repo, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def stdout(self, *args):
        out = self.rollup(*args)
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        return out.stdout.decode()

    def as_json(self, *args):
        return json.loads(self.stdout("--json", *args))

    def add_ledger(self, rel, *entries):
        """Commit a ledger file as though someone else's hook had written it."""
        path = repoledger.absolute_path(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "add %s" % rel)


class TestJsonAgreesWithTheTable(RollupHarness):
    def test_total_matches_the_table_and_the_groups(self):
        self.inject_usage(1000)
        self.commit("one")
        self.inject_usage(7000)
        self.commit("two")

        data = self.as_json()
        self.assertEqual(data["by"], "branch")
        self.assertEqual(data["currency"], "USD")
        self.assertAlmostEqual(data["total"],
                               sum(g["usd"] for g in data["groups"]), places=9)

        total_line = [l for l in self.stdout().split("\n")
                      if l.startswith("TOTAL")]
        self.assertEqual(len(total_line), 1)
        self.assertAlmostEqual(data["total"], float(total_line[0].split()[1]),
                               places=2)

    def test_groups_carry_name_entry_count_and_cost(self):
        self.inject_usage(1000)
        self.commit("one")
        self.inject_usage(2000)
        self.commit("two")

        groups = self.as_json()["groups"]
        self.assertEqual([g["name"] for g in groups], ["main"])
        self.assertEqual(groups[0]["entries"], 2)
        self.assertGreater(groups[0]["usd"], 0)


class TestGrouping(RollupHarness):
    def setUp(self):
        super().setUp()
        self.inject_usage(1000)
        self.commit("mine")
        self.add_ledger(".tkus/Someone Else/feature/x.jsonl",
                        {"usd": 1.25, "currency": "USD", "parent": None,
                         "at": "2026-08-01T00:00:00Z",
                         "until": "2026-08-01T00:00:00Z"})

    def test_by_branch_keeps_slashes(self):
        names = [g["name"] for g in self.as_json()["groups"]]
        self.assertEqual(names, ["feature/x", "main"])

    def test_by_identity(self):
        data = self.as_json("--by", "identity")
        self.assertEqual(data["by"], "identity")
        self.assertEqual([g["name"] for g in data["groups"]],
                         ["Someone Else", "Tester"])

    def test_by_date(self):
        names = [g["name"] for g in self.as_json("--by", "date")["groups"]]
        self.assertIn("2026-08-01", names)
        self.assertEqual(len(names), 2)

    def test_every_grouping_is_the_same_money(self):
        totals = [self.as_json("--by", by)["total"]
                  for by in ("branch", "identity", "date")]
        self.assertAlmostEqual(totals[0], totals[1], places=9)
        self.assertAlmostEqual(totals[0], totals[2], places=9)


class TestAgreesWithLog(RollupHarness):
    def test_branch_totals_sum_to_the_rollup_total(self):
        """Summing every branch's `log` double-counts nothing, so it must
        equal the rollup -- the invariant a consumer would rely on."""
        self.inject_usage(1000)
        self.commit("on main")
        self.git("checkout", "-q", "-b", "feature/y")
        self.inject_usage(4000)
        self.commit("on feature")

        data = self.as_json()
        from_log = 0.0
        for group in data["groups"]:
            out = subprocess.run(
                [sys.executable, "-m", "tkus", "log", "--json",
                 "--branch", group["name"]],
                cwd=self.repo, env=self.env, stdout=subprocess.PIPE)
            from_log += json.loads(out.stdout.decode())["total"]
        self.assertAlmostEqual(data["total"], from_log, places=9)


class TestNothingRecorded(RollupHarness):
    def test_json_is_still_valid_with_the_note_on_stderr(self):
        self.commit("no usage")
        out = self.rollup("--json")
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        data = json.loads(out.stdout.decode())
        self.assertEqual(data["groups"], [])
        self.assertEqual(data["total"], 0)
        self.assertTrue(out.stderr.decode().strip(),
                        "the 'nothing recorded' note belongs on stderr")

    def test_the_table_keeps_the_note_on_stdout(self):
        self.commit("no usage")
        self.assertIn("no committed ledger entries", self.stdout())


if __name__ == "__main__":
    unittest.main()
