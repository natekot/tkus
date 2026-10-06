"""`tkus tag` -- usage that belongs to no branch.

Every token since the last commit is claimed by the next commit, on whatever
branch that is. So an hour of planning on `main` with nothing to commit there
used to be billed to the first commit of the next feature branch. A tag marks
where that work ended; the next commit, on any branch, files it separately.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tkus import hooks, repoledger

from .test_rollup import RollupHarness

TAGS = "%s/Tester/%s/" % (repoledger.LEDGER_DIR, repoledger.TAGS_DIR)


class TagHarness(RollupHarness):
    def setUp(self):
        super().setUp()
        # A first commit, so the cursor exists and windows start after it.
        self.inject_usage(1)
        self.commit("base")

    def tkus(self, *args):
        return subprocess.run([sys.executable, "-m", "tkus"] + list(args),
                              cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)

    def tag(self, *args):
        out = self.tkus("tag", *args)
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        return out.stdout.decode()

    def tag_files(self, name=""):
        return [p for p in self.ledger_files() if p.startswith(TAGS + name)]

    def file_entries(self, rel, ref="HEAD"):
        blob = self.git("show", "%s:%s" % (ref, rel))
        return [json.loads(l) for l in blob.split("\n") if l.strip()]

    def output_tokens(self, entries):
        return sum(row.get("out", 0) for e in entries for row in e["providers"])

    def abandon_commit(self):
        with open(os.path.join(self.repo, "f.txt"), "a") as fh:
            fh.write("x")
        self.git("add", "f.txt")
        self.env["GIT_EDITOR"] = "false"
        self.git("commit", check=False)
        self.env["GIT_EDITOR"] = "true"


class TestTaggedUsageLeavesTheBranch(TagHarness):
    def test_the_next_commit_files_it_apart_from_its_own_branch(self):
        self.inject_usage(2000)
        self.tag("strategy")
        self.git("checkout", "-q", "-b", "feature")
        self.inject_usage(3000)
        self.commit("feature work")

        [tagged] = self.tag_files("strategy/")
        self.assertIn(tagged, self.git("show", "--name-only", "--format=", "HEAD"),
                      "the tag lands in the commit that carried it")
        self.assertEqual(self.output_tokens(self.file_entries(tagged)), 2000)
        self.assertEqual(self.output_tokens(
            self.file_entries(".tkus/Tester/feature.jsonl")), 3000)

    def test_the_windows_meet_without_overlapping(self):
        self.inject_usage(2000)
        self.tag("strategy")
        self.inject_usage(3000)
        self.commit("work")

        base = self.file_entries(".tkus/Tester/main.jsonl")[0]
        [tagged] = self.file_entries(self.tag_files()[0])
        mine = self.file_entries(".tkus/Tester/main.jsonl")[-1]
        self.assertEqual(tagged["since"], base["until"])
        self.assertEqual(mine["since"], tagged["until"])

    def test_all_tagged_usage_leaves_the_branch_nothing(self):
        self.inject_usage(2000)
        self.tag("strategy")
        self.git("checkout", "-q", "-b", "feature")
        self.commit("feature work")

        self.assertEqual(len(self.tag_files("strategy/")), 1)
        self.assertNotIn(".tkus/Tester/feature.jsonl", self.ledger_files())

    def test_log_for_the_carrying_branch_leaves_it_out(self):
        self.inject_usage(2000)
        self.tag("strategy")
        self.git("checkout", "-q", "-b", "feature")
        self.inject_usage(3000)
        self.commit("feature work")

        log = json.loads(self.tkus("log", "--json").stdout.decode())
        feature = [g for g in self.as_json()["groups"] if g["name"] == "feature"]
        self.assertAlmostEqual(log["total"], feature[0]["usd"], places=9)


class TestOutput(TagHarness):
    def test_a_window_within_one_day_names_the_date_once(self):
        self.inject_usage(2000)
        out = self.tag("strategy")
        self.assertRegex(out, r"\(\d{4}-\d\d-\d\d \d\d:\d\d -> \d\d:\d\d UTC\)")


class TestReusedNames(TagHarness):
    def test_one_name_on_two_branches_merges_without_conflict(self):
        """Tag names are buckets people reuse. One shared file per name would
        conflict as soon as the second of two open PRs merged."""
        for branch in ("one", "two"):
            self.git("checkout", "-q", "main")
            self.inject_usage(2000)
            self.tag("strategy")
            self.git("checkout", "-q", "-b", branch)
            # Its own file, so the only thing the two branches share is .tkus/.
            with open(os.path.join(self.repo, branch + ".txt"), "w") as fh:
                fh.write(branch)
            self.git("add", branch + ".txt")
            self.git("commit", "-q", "-m", "on %s" % branch)

        self.git("checkout", "-q", "main")
        self.env["GIT_EDITOR"] = "true"
        for branch in ("one", "two"):
            self.git("merge", "--squash", branch)
            self.git("commit", "-q")

        self.assertEqual(len(self.tag_files("strategy/")), 2)
        [row] = [g for g in self.as_json()["groups"] if g["name"] == "tag:strategy"]
        self.assertEqual(row["entries"], 2)

    def test_tagging_the_newest_name_again_extends_it(self):
        self.inject_usage(2000)
        self.tag("strategy")
        self.inject_usage(500)
        self.tag("strategy")
        self.commit("work")

        [tagged] = self.tag_files()
        self.assertEqual(self.output_tokens(self.file_entries(tagged)), 2500)

    def test_different_names_get_their_own_files(self):
        self.inject_usage(2000)
        self.tag("strategy")
        self.inject_usage(500)
        self.tag("research")
        self.commit("work")

        self.assertEqual(len(self.tag_files("strategy/")), 1)
        self.assertEqual(len(self.tag_files("research/")), 1)
        [research] = self.tag_files("research/")
        self.assertEqual(self.output_tokens(self.file_entries(research)), 500)


class TestPendingUntilCommitted(TagHarness):
    def test_listed_while_waiting_for_a_commit(self):
        self.inject_usage(2000)
        self.tag("strategy")
        self.assertIn("strategy", self.tag())

    def test_nothing_listed_once_committed(self):
        self.inject_usage(2000)
        self.tag("strategy")
        self.commit("work")
        self.assertNotIn("strategy", self.tag())

    def test_an_abandoned_commit_neither_loses_nor_doubles_it(self):
        self.inject_usage(2000)
        self.tag("strategy")
        self.abandon_commit()
        self.assertIn("strategy", self.tag())

        self.git("commit", "-q", "-m", "for real this time")
        [tagged] = self.tag_files()
        self.assertEqual(len(self.file_entries(tagged)), 1)
        self.assertEqual(self.git("status", "--porcelain").strip(), "")

    def test_no_verify_leaves_it_for_the_next_commit(self):
        self.inject_usage(2000)
        self.tag("strategy")
        with open(os.path.join(self.repo, "f.txt"), "a") as fh:
            fh.write("x")
        self.git("add", "f.txt")
        self.git("commit", "-q", "--no-verify", "-m", "skipped hooks")
        self.assertEqual(self.tag_files(), [])
        self.assertIn("strategy", self.tag())

        self.commit("hooks again")
        self.assertEqual(len(self.tag_files("strategy/")), 1)

    def test_a_file_already_committed_is_not_written_twice(self):
        """If post-commit never ran, the marker is still pending -- but HEAD
        already holds its file, and adding the entry again would double it."""
        post = os.path.join(hooks.hooks_path(self.repo), "post-commit")
        self.inject_usage(2000)
        self.tag("strategy")
        os.rename(post, post + ".off")
        self.commit("post-commit missing")
        os.rename(post + ".off", post)
        self.commit("post-commit back")

        [tagged] = self.tag_files()
        self.assertEqual(len(self.file_entries(tagged)), 1)


class TestUndo(TagHarness):
    def test_the_usage_goes_back_to_the_branch(self):
        self.inject_usage(2000)
        self.tag("stratgey")
        self.tag("--undo")
        self.commit("work")

        self.assertEqual(self.tag_files(), [])
        self.assertEqual(self.output_tokens(
            self.file_entries(".tkus/Tester/main.jsonl")[1:]), 2000)

    def test_only_the_newest_is_dropped(self):
        self.inject_usage(2000)
        self.tag("strategy")
        self.inject_usage(500)
        self.tag("research")
        self.tag("--undo")
        self.commit("work")

        self.assertEqual(len(self.tag_files("strategy/")), 1)
        self.assertEqual(self.tag_files("research/"), [])

    def test_with_nothing_pending_it_refuses(self):
        self.assertEqual(self.tkus("tag", "--undo").returncode, 1)


class TestRefusals(TagHarness):
    def assertRefused(self, *args):
        out = self.tkus("tag", *args)
        self.assertEqual(out.returncode, 1, out.stdout.decode())
        self.assertTrue(out.stderr.decode().strip())
        self.assertNotIn("strategy", self.tag())

    def test_nothing_to_tag(self):
        self.assertRefused("strategy")

    def test_nothing_since_the_last_tag(self):
        self.inject_usage(2000)
        self.tag("research")
        self.assertRefused("strategy")

    def test_without_hooks_nothing_would_ever_commit_it(self):
        self.tkus("uninstall")
        self.inject_usage(2000)
        self.assertRefused("strategy")

    def test_local_only_has_no_tracked_ledger_to_put_it_in(self):
        self.env["TKUS_REPO_LEDGER"] = "0"
        self.inject_usage(2000)
        self.assertRefused("strategy")

    def test_an_ignored_ledger_has_nowhere_to_put_it(self):
        self.git("rm", "-q", "-r", "--cached", ".tkus")
        with open(os.path.join(self.repo, ".gitignore"), "w") as fh:
            fh.write(".tkus/\n")
        self.inject_usage(2000)
        self.assertRefused("strategy")


class TestReport(TagHarness):
    def test_tagged_usage_is_listed_apart_from_what_the_branch_will_claim(self):
        self.inject_usage(2000)
        self.tag("strategy")
        self.inject_usage(3000)

        out = self.tkus("report").stdout.decode()
        self.assertIn("strategy", out)
        rows = [l for l in out.split("\n") if l.startswith("claude-code")]
        self.assertEqual(len(rows), 1, out)
        self.assertEqual(rows[0].split()[3], "3000",
                         "the table is what the next commit's branch will claim")


class TestRollup(TagHarness):
    def setUp(self):
        super().setUp()
        self.inject_usage(2000)
        self.tag("strategy")
        self.git("checkout", "-q", "-b", "feature")
        self.inject_usage(3000)
        self.commit("feature work")

    def test_a_tag_is_its_own_row_after_the_branches(self):
        groups = self.as_json()["groups"]
        self.assertEqual([(g["name"], g["kind"]) for g in groups],
                         [("feature", "branch"), ("main", "branch"),
                          ("tag:strategy", "tag")])
        self.assertIn("tag:strategy", self.stdout())

    def test_every_grouping_is_still_the_same_money(self):
        totals = [self.as_json("--by", by)["total"]
                  for by in ("branch", "identity", "date")]
        self.assertAlmostEqual(totals[0], totals[1], places=9)
        self.assertAlmostEqual(totals[0], totals[2], places=9)
        self.assertAlmostEqual(totals[0], sum(e["usd"] for e in self.entries()),
                               places=9)

    def test_rename_does_not_offer_it_as_a_deleted_branch(self):
        out = self.tkus("rename").stdout.decode()
        self.assertNotIn("tags", out)
        self.assertNotIn("strategy", out)


class TestNames(TagHarness):
    def test_a_slash_nests_like_a_branch_name(self):
        self.inject_usage(2000)
        self.tag("planning/q4")
        self.commit("work")

        self.assertEqual(len(self.tag_files("planning/q4/")), 1)
        names = [g["name"] for g in self.as_json()["groups"]]
        self.assertIn("tag:planning/q4", names)

    def test_unsafe_characters_are_replaced(self):
        self.inject_usage(2000)
        self.tag("a:b")
        self.commit("work")

        self.assertEqual(len(self.tag_files("a-b/")), 1)


if __name__ == "__main__":
    unittest.main()
