"""The repository-tracked ledger, exercised against real git operations.

These are the tests that justify the design. Storing cost in a tracked file
instead of the commit message is only worth doing if it genuinely survives the
operations that destroyed the trailers -- squash above all -- so those are
verified by actually running git, not by reasoning about it.
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

from tkus import repoledger
from tkus.pricing import RateTable

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RepoLedgerTestCase(unittest.TestCase):
    """A real repo with tkus hooks installed and a synthetic Claude transcript."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects)
        self.counter = 0

        self.env = dict(os.environ)
        self.env.update({
            "PYTHONPATH": PROJECT,
            "TKUS_CLAUDE_PROJECTS": self.projects,
            "TKUS_COPILOT_HOME": os.path.join(self.tmp, "no-copilot"),
            "TKUS_REPO_LEDGER": "1",
            "GIT_AUTHOR_NAME": "Tester", "GIT_COMMITTER_NAME": "Tester",
            "GIT_AUTHOR_EMAIL": "t@e.st", "GIT_COMMITTER_EMAIL": "t@e.st",
        })
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Tester")
        self.git("config", "user.email", "t@e.st")
        subprocess.run([sys.executable, "-m", "tkus", "install"], cwd=self.repo,
                       env=self.env, stdout=subprocess.DEVNULL, check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args, **kw):
        out = subprocess.run(["git"] + list(args), cwd=kw.pop("cwd", self.repo),
                             env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        if kw.get("check", True) and out.returncode != 0:
            raise AssertionError("git %s failed: %s"
                                 % (" ".join(args), out.stderr.decode()))
        return out.stdout.decode()

    def inject_usage(self, output_tokens=1000):
        """One synthetic Claude request attributable to this repo."""
        self.counter += 1
        encoded = os.path.realpath(self.repo).replace(os.sep, "-")
        directory = os.path.join(self.projects, encoded)
        os.makedirs(directory, exist_ok=True)
        # Stamped now, not in the past: the previous commit advanced the cursor
        # to roughly now, so a backdated record would fall outside the window.
        stamp = datetime.now(timezone.utc)
        with open(os.path.join(directory, "s.jsonl"), "a") as fh:
            fh.write(json.dumps({
                "type": "assistant", "requestId": "r%d" % self.counter,
                "cwd": os.path.realpath(self.repo),
                "timestamp": stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "message": {"model": "claude-opus-5", "usage": {
                    "input_tokens": 0, "output_tokens": output_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                       "ephemeral_5m_input_tokens": 0},
                    "speed": "standard", "service_tier": "standard"}},
            }) + "\n")

    def commit(self, message, content=None):
        with open(os.path.join(self.repo, "f.txt"), "a") as fh:
            fh.write(content or (message + "\n"))
        self.git("add", "f.txt")
        self.git("commit", "-q", "-m", message)

    def entries(self, ref="HEAD"):
        """Every ledger entry present in a given tree."""
        out = []
        listing = self.git("ls-tree", "-r", "--name-only", ref)
        for path in listing.split("\n"):
            if path.strip().startswith(repoledger.LEDGER_DIR + "/"):
                blob = self.git("show", "%s:%s" % (ref, path.strip()))
                out.extend(json.loads(l) for l in blob.split("\n") if l.strip())
        return out


class TestCommitMessagesAreUntouched(RepoLedgerTestCase):
    def test_message_is_byte_identical_to_one_without_tkus(self):
        """The headline property: tkus no longer writes to commit messages."""
        self.inject_usage()
        self.commit("A perfectly ordinary message")
        self.assertEqual(self.git("log", "-1", "--format=%B"),
                         "A perfectly ordinary message\n\n")

    def test_ledger_lands_in_the_same_commit(self):
        """Staging in pre-commit is what puts the file in *this* commit."""
        self.inject_usage()
        self.commit("first")
        files = self.git("show", "--stat", "--format=", "HEAD")
        self.assertIn(repoledger.LEDGER_DIR, files)
        self.assertEqual(len(self.entries()), 1)

    def test_worktree_is_clean_afterwards(self):
        self.inject_usage()
        self.commit("first")
        self.assertEqual(self.git("status", "--porcelain").strip(), "")


class TestSquash(RepoLedgerTestCase):
    def test_entries_survive_a_squash_merge(self):
        """The entire reason for this design. Squash discards commit messages
        but preserves the tree, so the ledger comes through intact."""
        self.inject_usage(1000)
        self.commit("base")
        self.git("checkout", "-q", "-b", "feature")
        self.inject_usage(2000)
        self.commit("branch one")
        self.inject_usage(3000)
        self.commit("branch two")
        branch_total = sum(e["usd"] for e in self.entries())

        self.git("checkout", "-q", "main")
        self.git("merge", "--squash", "feature")
        self.env["GIT_EDITOR"] = "true"
        self.git("commit", "-q")

        squashed = self.entries()
        self.assertEqual(len(self.git("log", "--format=%H").split()), 2,
                         "history should be squashed to two commits")
        self.assertAlmostEqual(sum(e["usd"] for e in squashed), branch_total, places=9)

    def test_squashed_message_needs_no_trailers(self):
        self.inject_usage()
        self.commit("base")
        self.git("checkout", "-q", "-b", "feature")
        self.inject_usage()
        self.commit("work")
        self.git("checkout", "-q", "main")
        self.git("merge", "--squash", "feature")
        self.env["GIT_EDITOR"] = "true"
        self.git("commit", "-q")
        self.assertNotIn("AI-Cost", self.git("log", "-1", "--format=%B"))
        self.assertTrue(self.entries())


class TestAbandonedCommit(RepoLedgerTestCase):
    def test_abandoned_commit_does_not_double_count(self):
        """pre-commit runs even when the commit is abandoned, leaving a staged
        entry. Rebuilding from HEAD makes the next commit overwrite it."""
        self.inject_usage(5000)
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write("x")
        self.git("add", "f.txt")
        self.env["GIT_EDITOR"] = "false"
        self.git("commit", check=False)           # abandoned in the editor
        self.env["GIT_EDITOR"] = "true"

        self.git("commit", "-q", "-m", "for real this time")
        entries = self.entries()
        self.assertEqual(len(entries), 1, "the abandoned entry must be replaced")

    def test_cursor_does_not_advance_on_abandonment(self):
        from tkus import cursor
        self.inject_usage()
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write("x")
        self.git("add", "f.txt")
        self.env["GIT_EDITOR"] = "false"
        self.git("commit", check=False)
        self.assertIsNone(cursor.cursor_since(self.repo))


class TestAmend(RepoLedgerTestCase):
    def test_amend_keeps_the_original_entry_without_duplicating(self):
        """No amend detection required: the entry is already in the index."""
        self.inject_usage(4000)
        self.commit("original")
        before = self.entries()
        self.assertEqual(len(before), 1)

        self.env["GIT_EDITOR"] = "true"
        self.git("commit", "-q", "--amend", "-m", "reworded")
        after = self.entries()
        self.assertEqual(len(after), 1)
        self.assertAlmostEqual(after[0]["usd"], before[0]["usd"], places=9)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "reworded")


class TestBranchSwitching(RepoLedgerTestCase):
    def test_switching_branches_does_not_re_collect(self):
        """The window comes from the cursor in .git/, not from the ledger file.
        A new branch has no ledger file of its own, so deriving the window from
        it would re-attribute work already recorded on another branch."""
        self.inject_usage(1000)
        self.commit("on main")
        main_entries = self.entries()
        self.assertEqual(len(main_entries), 1)

        # New branch, no new usage: nothing further should be attributed.
        self.git("checkout", "-q", "-b", "side")
        self.commit("on side, no new usage")
        self.assertEqual(len(self.entries()), 1,
                         "already-attributed usage must not be recorded again")

        # New usage on the branch is attributed once, to the branch.
        self.inject_usage(7000)
        self.commit("on side, with usage")
        self.assertEqual(len(self.entries()), 2)

    def test_separate_branches_use_separate_files(self):
        self.inject_usage()
        self.commit("on main")
        self.git("checkout", "-q", "-b", "feature/thing")
        self.inject_usage()
        self.commit("on branch")
        paths = [p for p in self.git("ls-files").split("\n")
                 if p.startswith(repoledger.LEDGER_DIR)]
        self.assertEqual(len(paths), 2, paths)
        self.assertTrue(any("feature/thing" in p for p in paths),
                        "a branch name with a slash becomes a nested path")


class TestIdentityAndBranchResolution(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def cfg(self, key, value):
        subprocess.run(["git", "config", key, value], cwd=self.repo, check=True)

    def test_branch_resolves_before_the_first_commit(self):
        """`rev-parse --abbrev-ref HEAD` returns the literal 'HEAD' here."""
        self.assertEqual(repoledger.branch_name(self.repo), "main")

    def test_identity_prefers_the_explicit_setting(self):
        self.cfg("user.name", "Some One")
        self.cfg("tkus.identity", "build-bot")
        self.assertEqual(repoledger.identity(self.repo), "build-bot")

    def test_identity_falls_back_to_user_name(self):
        self.cfg("user.name", "Some One")
        self.assertEqual(repoledger.identity(self.repo), "Some One")

    def test_identity_never_uses_the_email(self):
        """Emails in repository paths would be published with the repo."""
        self.cfg("user.email", "someone@example.com")
        self.assertNotIn("@", repoledger.identity(self.repo))

    def test_unsafe_characters_are_replaced(self):
        self.cfg("tkus.identity", 'a:b*c?d"e')
        got = repoledger.identity(self.repo)
        for bad in ':*?"<>|':
            self.assertNotIn(bad, got)

    def test_missing_ledger_in_head_reads_as_empty(self):
        self.assertEqual(repoledger.read_committed(self.repo), [])

    def test_recording_is_on_by_default(self):
        """Recording is the product. A default install that records nothing
        anywhere would make the tool pointless."""
        saved = os.environ.pop("TKUS_REPO_LEDGER", None)
        try:
            self.assertTrue(repoledger.enabled(RateTable.load(self.repo)))
        finally:
            if saved is not None:
                os.environ["TKUS_REPO_LEDGER"] = saved

    def test_local_only_config_turns_off_the_repo_ledger(self):
        with open(os.path.join(self.repo, ".tkus.json"), "w") as fh:
            json.dump({"repo_ledger": False}, fh)
        saved = os.environ.pop("TKUS_REPO_LEDGER", None)
        try:
            self.assertFalse(repoledger.enabled(RateTable.load(self.repo)))
        finally:
            if saved is not None:
                os.environ["TKUS_REPO_LEDGER"] = saved


class TestLocalOnlyStillRecords(RepoLedgerTestCase):
    """Turning off the repo ledger changes *where* cost is recorded, never
    whether. A mode that records nothing at all would be pointless."""

    def setUp(self):
        super().setUp()
        self.env["TKUS_REPO_LEDGER"] = "0"

    def test_nothing_is_committed_to_the_repository(self):
        self.inject_usage()
        self.commit("local only")
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.git("status", "--porcelain").strip(), "")

    def test_but_the_local_ledger_still_records_it(self):
        from tkus import ledger
        self.inject_usage()
        self.commit("local only")
        sha = self.git("rev-parse", "HEAD").strip()
        entry = ledger.lookup(self.repo, sha)
        self.assertIsNotNone(entry, "usage must still be recorded locally")
        self.assertGreater(entry["usd"], 0)


class TestGitignoreOptOut(RepoLedgerTestCase):
    """`.gitignore` is the natural way to say "not in my repository", so it is
    honoured as a first-class opt-out rather than half-working."""

    def test_ignored_ledger_is_not_committed_and_leaves_no_stray_file(self):
        with open(os.path.join(self.repo, ".gitignore"), "w") as fh:
            fh.write(".tkus/\n")
        self.inject_usage()
        self.commit("with the ledger ignored")
        self.assertEqual(self.entries(), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".tkus")),
                         "no stray ignored file should be written")

    def test_ignored_ledger_is_still_recorded_locally(self):
        from tkus import ledger
        with open(os.path.join(self.repo, ".gitignore"), "w") as fh:
            fh.write(".tkus/\n")
        self.inject_usage()
        self.commit("with the ledger ignored")
        sha = self.git("rev-parse", "HEAD").strip()
        self.assertIsNotNone(ledger.lookup(self.repo, sha))

    def test_gitignore_does_not_untrack_an_existing_ledger(self):
        """Standard git behaviour, and the trap: .gitignore is ignored for
        already-tracked files, so an existing ledger keeps being committed
        until `git rm --cached` removes it."""
        self.inject_usage()
        self.commit("ledger tracked")
        self.assertEqual(len(self.entries()), 1)

        with open(os.path.join(self.repo, ".gitignore"), "w") as fh:
            fh.write(".tkus/\n")
        self.inject_usage()
        self.commit("now ignored, but already tracked")
        self.assertEqual(len(self.entries()), 2,
                         "a tracked ledger keeps recording despite .gitignore")

    def test_untracking_it_then_takes_effect(self):
        self.inject_usage()
        self.commit("ledger tracked")
        with open(os.path.join(self.repo, ".gitignore"), "w") as fh:
            fh.write(".tkus/\n")
        self.git("rm", "-r", "-q", "--cached", ".tkus")
        self.inject_usage()
        self.commit("untracked and ignored")
        self.assertEqual(self.entries(), [])


class TestUpgradeFromTrailerVersion(unittest.TestCase):
    """Upgrading must not leave the old prepare-commit-msg hook behind."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.hooks = os.path.join(self.repo, ".git", "hooks")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def _write_legacy(self):
        from tkus.hooks import MARKER
        path = os.path.join(self.hooks, "prepare-commit-msg")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n%s\nexit 0\n" % MARKER)
        return path

    def test_install_removes_the_obsolete_hook(self):
        from tkus import hooks
        legacy = self._write_legacy()
        hooks.install(self.repo)
        self.assertFalse(os.path.exists(legacy))

    def test_uninstall_removes_it_too(self):
        from tkus import hooks
        legacy = self._write_legacy()
        hooks.uninstall(self.repo)
        self.assertFalse(os.path.exists(legacy))

    def test_a_users_own_hook_of_that_name_is_left_alone(self):
        from tkus import hooks
        path = os.path.join(self.hooks, "prepare-commit-msg")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\necho mine\n")
        hooks.install(self.repo)
        self.assertTrue(os.path.exists(path))
        with open(path) as fh:
            self.assertIn("echo mine", fh.read())


if __name__ == "__main__":
    unittest.main()
