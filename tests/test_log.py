"""`tkus log` -- per-commit cost for one branch.

Exercised against real git rather than a fixture, because attribution rests on
git history -- which commit's diff added each entry -- and the operations that
rewrite that history -- amend, rebase, rename -- are real git operations.
Reasoning about them is not evidence that the attribution survives them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tkus import repoledger

from .test_repoledger import RepoLedgerTestCase


class LogHarness(RepoLedgerTestCase):
    """The repo harness plus a way to run `tkus log` and read its output."""

    def log(self, *args, **kw):
        out = subprocess.run(
            [sys.executable, "-m", "tkus", "log"] + list(args),
            cwd=kw.pop("cwd", self.repo), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return out

    def stdout(self, *args):
        out = self.log(*args)
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        return out.stdout.decode()

    def as_json(self, *args):
        return json.loads(self.stdout("--json", *args))


class TestPerCommitBreakdown(LogHarness):
    def test_every_recorded_commit_is_listed_with_its_subject(self):
        self.inject_usage(1000)
        self.commit("first thing")
        self.inject_usage(2000)
        self.commit("second thing")

        data = self.as_json()
        subjects = [c["subject"] for c in data["commits"]]
        self.assertEqual(subjects, ["first thing", "second thing"])
        self.assertEqual(data["orphaned"], [])

        text = self.stdout()
        for sha in (c["sha"][:9] for c in data["commits"]):
            self.assertIn(sha, text)
        self.assertIn("first thing", text)

    def test_total_is_the_sum_of_the_commits(self):
        self.inject_usage(1000)
        self.commit("one")
        self.inject_usage(5000)
        self.commit("two")

        data = self.as_json()
        self.assertAlmostEqual(data["total"],
                               sum(c["usd"] for c in data["commits"]), places=9)

    def test_the_first_commit_has_no_parent_and_still_maps(self):
        """`head_sha` returns None before any commit exists, so the very first
        entry records `parent: null` -- it belongs to the root commit."""
        self.inject_usage(3000)
        self.commit("the root commit")

        raw = self.entries()
        self.assertEqual(len(raw), 1)
        self.assertIsNone(raw[0]["parent"])

        data = self.as_json()
        self.assertEqual(len(data["commits"]), 1)
        self.assertEqual(data["commits"][0]["subject"], "the root commit")
        self.assertEqual(data["orphaned"], [])

    def test_ordering_is_oldest_first(self):
        for n, msg in enumerate(("a", "b", "c"), start=1):
            self.inject_usage(1000 * n)
            self.commit(msg)
        data = self.as_json()
        self.assertEqual([c["subject"] for c in data["commits"]], ["a", "b", "c"])


class TestAgreesWithRollup(LogHarness):
    def test_total_matches_the_branch_row_in_rollup(self):
        """The one invariant that must never break: whatever `log` does with
        attribution, its total is the same money `rollup` reports."""
        self.inject_usage(1000)
        self.commit("one")
        self.inject_usage(9000)
        self.commit("two")

        rollup = subprocess.run(
            [sys.executable, "-m", "tkus", "rollup"], cwd=self.repo,
            env=self.env, stdout=subprocess.PIPE).stdout.decode()
        row = [l for l in rollup.split("\n") if l.startswith("main ")]
        self.assertEqual(len(row), 1, rollup)
        rollup_total = float(row[0].split()[-1])

        self.assertAlmostEqual(self.as_json()["total"], rollup_total, places=2)


class TestTotalFlag(LogHarness):
    def test_total_prints_one_parseable_number_and_nothing_else(self):
        self.inject_usage(4000)
        self.commit("only commit")
        text = self.stdout("--total")
        self.assertEqual(len(text.strip().split("\n")), 1, text)
        float(text.strip())          # raises if it is not a bare number


class TestAmendKeepsAttribution(LogHarness):
    """Amending with new usage in the window used to strand an entry.

    Pre-commit runs again during the amend and, if there is fresh usage, writes
    a second entry whose `parent` is the pre-amend commit -- which the amend then
    makes unreachable. Pre-commit cannot know it is amending, so the pointer
    cannot be fixed at write time. `log` instead credits each entry to the commit
    whose diff added it, and the amended commit's diff adds every entry.
    """

    def amend(self, usage):
        self.inject_usage(usage)
        self.env["GIT_EDITOR"] = "true"
        self.git("commit", "-q", "--amend", "-m", "reworded")

    def test_amending_after_new_usage_keeps_it_on_the_amended_commit(self):
        self.inject_usage(4000)
        self.commit("original")
        self.amend(2000)

        data = self.as_json()
        self.assertEqual(data["orphaned"], [])
        self.assertEqual([c["subject"] for c in data["commits"]], ["reworded"])
        self.assertAlmostEqual(
            data["commits"][0]["usd"], sum(e["usd"] for e in self.entries()),
            places=9)

    def test_repeated_amends_keep_everything_on_one_commit(self):
        """The reported case: every amend used to strand the one before it."""
        self.commit("base")
        self.inject_usage(100000)
        self.commit("first")
        self.amend(1000)
        self.amend(2000)

        self.assertEqual(len(self.entries()), 3)
        data = self.as_json()
        self.assertEqual(data["orphaned"], [])
        self.assertEqual([c["subject"] for c in data["commits"]], ["reworded"])
        self.assertAlmostEqual(data["commits"][0]["usd"], data["total"], places=9)

    def test_attribution_survives_into_a_fresh_clone(self):
        """No reflog, no .git/tkus state: history alone has to be enough."""
        self.inject_usage(4000)
        self.commit("original")
        self.amend(2000)

        clone = os.path.join(self.tmp, "clone")
        self.git("clone", "-q", self.repo, clone)
        out = self.log("--json", cwd=clone)
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        data = json.loads(out.stdout.decode())
        self.assertEqual(data["orphaned"], [])
        self.assertEqual([c["subject"] for c in data["commits"]], ["reworded"])

    def test_totals_still_agree_with_rollup(self):
        self.inject_usage(4000)
        self.commit("original")
        self.amend(2000)
        rollup = subprocess.run(
            [sys.executable, "-m", "tkus", "rollup"], cwd=self.repo,
            env=self.env, stdout=subprocess.PIPE).stdout.decode()
        row = [l for l in rollup.split("\n") if l.startswith("main ")][0]
        self.assertAlmostEqual(self.as_json()["total"],
                               float(row.split()[-1]), places=2)

    def test_amending_without_new_usage_keeps_the_entry_attributed(self):
        """No new usage, no second entry, nothing stranded."""
        self.inject_usage(4000)
        self.commit("original")
        self.env["GIT_EDITOR"] = "true"
        self.git("commit", "-q", "--amend", "-m", "reworded")

        data = self.as_json()
        self.assertEqual(data["orphaned"], [])
        self.assertEqual([c["subject"] for c in data["commits"]], ["reworded"])


class TestRebaseKeepsAttribution(LogHarness):
    def test_each_rebased_commit_keeps_its_own_cost(self):
        """A rebase gives every commit a new parent, so the recorded pointers
        all name commits that are no longer on the branch."""
        self.commit("base")
        self.git("checkout", "-q", "-b", "feature")
        self.inject_usage(1000)
        self.commit("a", content="a\n")
        self.inject_usage(9000)
        self.commit("b", content="b\n")
        before = {c["subject"]: c["usd"] for c in self.as_json()["commits"]}

        self.git("checkout", "-q", "main")
        with open(os.path.join(self.repo, "g.txt"), "w") as fh:
            fh.write("moved\n")
        self.git("add", "g.txt")
        self.git("commit", "-q", "-m", "main moves on")
        self.git("checkout", "-q", "feature")
        self.git("rebase", "-q", "main")

        data = self.as_json()
        self.assertEqual(data["orphaned"], [])
        after = {c["subject"]: c["usd"] for c in data["commits"]}
        self.assertEqual(set(after), {"a", "b"})
        for subject in ("a", "b"):
            self.assertAlmostEqual(after[subject], before[subject], places=9)


class TestOrphanedEntries(LogHarness):
    """What is still orphaned: an entry in the worktree that no commit added,
    such as the staged ledger an abandoned commit leaves behind."""

    def strand_one(self):
        self.inject_usage(4000)
        self.commit("recorded")
        path = repoledger.absolute_path(self.repo)
        with open(path, "a", newline="\n") as fh:
            fh.write(json.dumps({"usd": 0.5, "currency": "USD",
                                 "parent": self.git("rev-parse", "HEAD").strip(),
                                 "until": "2099-01-01T00:00:00.000Z"}) + "\n")

    def test_the_money_survives_even_though_no_commit_claims_it(self):
        self.strand_one()
        data = self.as_json()
        self.assertEqual(len(data["orphaned"]), 1)
        self.assertEqual([c["subject"] for c in data["commits"]], ["recorded"])
        self.assertAlmostEqual(
            data["total"], sum(c["usd"] for c in data["commits"]) + 0.5, places=9,
            msg="an orphaned entry is still real money and must be in TOTAL")
        self.assertIn("orphaned", self.stdout())


class TestCoverageLine(LogHarness):
    def test_it_counts_commits_without_recorded_usage(self):
        self.inject_usage(1000)
        self.commit("recorded")
        self.commit("no usage at all")
        self.inject_usage(2000)
        self.commit("recorded again")

        text = self.stdout()
        self.assertIn("2 of 3 commits have recorded usage", text)
        self.assertNotIn("orphaned", text)

    def test_it_agrees_with_the_verb(self):
        self.inject_usage(1000)
        self.commit("only one")
        self.assertIn("1 of 1 commit has recorded usage", self.stdout())

    def test_the_orphan_clause_appears_only_once_there_are_orphans(self):
        self.inject_usage(1000)
        self.commit("recorded")
        self.assertNotIn("orphaned", self.stdout())

        with open(repoledger.absolute_path(self.repo), "a", newline="\n") as fh:
            fh.write(json.dumps({"usd": 0.5, "currency": "USD", "parent": "f" * 40,
                                 "until": "2099-01-01T00:00:00.000Z"}) + "\n")
        self.assertIn("1 entry orphaned", self.stdout())


class TestBranchScope(LogHarness):
    def test_another_branch_is_not_included(self):
        self.inject_usage(1000)
        self.commit("on main")
        self.git("checkout", "-q", "-b", "feature/thing")
        self.inject_usage(8000)
        self.commit("on the branch")

        branch = self.as_json()
        self.assertEqual([c["subject"] for c in branch["commits"]],
                         ["on the branch"])

        main = self.as_json("--branch", "main")
        self.assertEqual([c["subject"] for c in main["commits"]], ["on main"])
        self.assertNotAlmostEqual(branch["total"], main["total"], places=6)

    def test_branch_names_with_slashes_resolve(self):
        self.git("checkout", "-q", "-b", "feature/thing")
        self.inject_usage(1000)
        self.commit("on the branch")
        self.assertEqual(self.as_json("--branch", "feature/thing")["branch"],
                         "feature/thing")
        self.assertEqual(len(self.as_json("--branch", "feature/thing")["commits"]), 1)

    def test_every_identity_on_the_branch_is_counted(self):
        """`rollup --by branch` sums across identities; so does this."""
        self.inject_usage(1000)
        self.commit("mine")
        mine = self.as_json()["total"]

        # A second person's ledger file for the same branch.
        other = repoledger.absolute_path(self.repo, ".tkus/Someone Else/main.jsonl")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        with open(other, "w") as fh:
            fh.write(json.dumps({"usd": 1.25, "currency": "USD",
                                 "parent": None, "at": "2026-08-01T00:00:00Z"}) + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "add a colleague's ledger")

        self.assertAlmostEqual(self.as_json()["total"], mine + 1.25, places=6)


class TestNothingRecorded(LogHarness):
    def test_an_empty_ledger_is_not_an_error(self):
        self.commit("no usage")
        out = self.log()
        self.assertEqual(out.returncode, 0, out.stderr.decode())

    def test_total_still_prints_a_number_with_the_note_on_stderr(self):
        """A scripting flag must always emit one parseable number on stdout."""
        self.commit("no usage")
        out = self.log("--total")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.decode().strip(), "0.00")
        self.assertTrue(out.stderr.decode().strip(),
                        "the 'nothing recorded' note belongs on stderr")


if __name__ == "__main__":
    unittest.main()
