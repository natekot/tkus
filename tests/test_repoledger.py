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

from tkus import cursor, hooks, repoledger
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

    def ledger_files(self):
        return sorted(p for p in self.git("ls-files").split("\n")
                      if p.startswith(repoledger.LEDGER_DIR + "/"))


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


class TestLinkedWorktree(RepoLedgerTestCase):
    """Git runs hooks from the common git dir, so one install covers every
    worktree. tkus used to look in the per-worktree git dir instead."""

    def setUp(self):
        super().setUp()
        self.commit("base")
        self.main = self.repo
        self.repo = os.path.join(self.tmp, "wt")
        self.git("worktree", "add", "-q", "-b", "feature", self.repo,
                 cwd=self.main)

    def test_counts_as_installed_without_reinstalling(self):
        self.assertTrue(hooks.is_installed(self.repo))

    def test_commit_records_without_reinstalling(self):
        self.inject_usage()
        self.commit("in worktree")
        self.assertEqual(len(self.entries()), 1)
        self.assertTrue(os.path.exists(
            os.path.join(cursor.git_dir(self.repo), "tkus", "cursor.json")))

    def test_install_writes_the_hooks_git_runs(self):
        hooks.install(self.repo)
        for name in hooks.HOOKS:
            self.assertFalse(os.path.exists(
                os.path.join(cursor.git_dir(self.repo), "hooks", name)))
            self.assertTrue(os.path.exists(
                os.path.join(self.main, ".git", "hooks", name)))

    def test_install_removes_hooks_an_earlier_version_left_in_the_worktree(self):
        stray = os.path.join(cursor.git_dir(self.repo), "hooks")
        os.makedirs(stray)
        for name in hooks.HOOKS:
            with open(os.path.join(stray, name), "w") as fh:
                fh.write("#!/bin/sh\n%s\n" % hooks.MARKER)
        hooks.install(self.repo)
        self.assertEqual(os.listdir(stray), [])

    def test_uninstall_removes_the_shared_hooks(self):
        hooks.uninstall(self.repo)
        self.assertFalse(hooks.is_installed(self.main))


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


class TestBranchRename(RepoLedgerTestCase):
    """The ledger file is named after the branch, so `git branch -m` used to
    start an empty file and leave everything before the rename in the old one --
    where `tkus log` for the new name never looked."""

    def test_the_next_commit_folds_the_old_file_into_the_new_one(self):
        self.commit("base")
        self.git("checkout", "-q", "-b", "old")
        self.inject_usage(100000)
        self.commit("first")
        self.git("branch", "-m", "old", "new")
        self.inject_usage(1000)
        self.commit("second")

        self.assertEqual(self.ledger_files(), [".tkus/Tester/new.jsonl"])
        self.assertEqual(len(self.entries()), 2)
        self.assertEqual(self.git("status", "--porcelain").strip(), "")

    def test_rename_then_repeated_amends_loses_nothing(self):
        """The reported case: $297 of $299 vanished from `tkus log`."""
        self.commit("base")
        self.git("checkout", "-q", "-b", "old")
        self.inject_usage(100000)
        self.commit("first")
        self.git("branch", "-m", "old", "new")
        self.env["GIT_EDITOR"] = "true"
        for usage in (1000, 2000):
            self.inject_usage(usage)
            self.git("commit", "-q", "--amend", "--no-edit")

        self.assertEqual(self.ledger_files(), [".tkus/Tester/new.jsonl"])
        entries = self.entries()
        self.assertEqual(len(entries), 3)

        out = subprocess.run([sys.executable, "-m", "tkus", "log", "--json"],
                             cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
                             check=True)
        data = json.loads(out.stdout.decode())
        self.assertEqual(data["orphaned"], [])
        self.assertEqual([c["subject"] for c in data["commits"]], ["first"])
        self.assertAlmostEqual(data["total"], sum(e["usd"] for e in entries),
                               places=9)

    def test_a_chain_of_renames_folds_every_step(self):
        self.commit("base")
        self.git("checkout", "-q", "-b", "a")
        self.inject_usage(1000)
        self.commit("on a")
        self.git("branch", "-m", "a", "b")
        self.inject_usage(2000)
        self.commit("on b")
        self.git("branch", "-m", "b", "c")
        self.inject_usage(3000)
        self.commit("on c")

        self.assertEqual(self.ledger_files(), [".tkus/Tester/c.jsonl"])
        self.assertEqual(len(self.entries()), 3)

    def test_a_recreated_old_name_keeps_its_own_file(self):
        """If `old` exists again it is a live branch, and its file is its own."""
        self.commit("base")
        self.git("checkout", "-q", "-b", "old")
        self.inject_usage(1000)
        self.commit("first")
        self.git("branch", "-m", "old", "new")
        self.git("branch", "old")
        self.inject_usage(2000)
        self.commit("second")

        self.assertEqual(self.ledger_files(),
                         [".tkus/Tester/new.jsonl", ".tkus/Tester/old.jsonl"])

    def test_another_identitys_file_is_left_alone(self):
        self.commit("base")
        self.git("checkout", "-q", "-b", "old")
        other = repoledger.absolute_path(self.repo, ".tkus/Someone Else/old.jsonl")
        os.makedirs(os.path.dirname(other))
        with open(other, "w") as fh:
            fh.write(json.dumps({"usd": 1.0, "parent": None}) + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "their work")
        self.git("branch", "-m", "old", "new")
        self.inject_usage(1000)
        self.commit("mine")

        self.assertIn(".tkus/Someone Else/old.jsonl", self.ledger_files())

    def test_a_commit_with_no_usage_still_folds(self):
        """Otherwise the split outlives every commit until one happens to use
        an agent, and `tkus log` is wrong for all of them."""
        self.commit("base")
        self.git("checkout", "-q", "-b", "old")
        self.inject_usage(1000)
        self.commit("first")
        self.git("branch", "-m", "old", "new")
        self.commit("no agent here")

        self.assertEqual(self.ledger_files(), [".tkus/Tester/new.jsonl"])
        self.assertEqual(len(self.entries()), 1)


class TestRenameCommand(RepoLedgerTestCase):
    """`tkus rename` covers the name changes the reflog cannot see: a new branch
    cut from the old one before deleting it, or a rename made on GitHub or in
    another clone. Without it the old entries stay in a file `tkus log` for the
    new name never reads."""

    def tkus(self, *args):
        return subprocess.run([sys.executable, "-m", "tkus"] + list(args),
                              cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)

    def recut(self):
        """Work on `old`, then carry on as `new` the way the reflog can't see."""
        self.commit("base")
        self.git("checkout", "-q", "-b", "old")
        self.inject_usage(100000)
        self.commit("first")
        self.git("checkout", "-q", "-b", "new")
        self.git("branch", "-D", "old")

    def aliases(self, branch="new"):
        return self.git("config", "--get-all", "branch.%s.tkusRenamedFrom" % branch,
                        check=False).split()

    def test_the_next_commit_folds_the_named_file(self):
        self.recut()
        self.assertEqual(self.tkus("rename", "old").returncode, 0)
        self.inject_usage(1000)
        self.commit("second")

        self.assertEqual(self.ledger_files(), [".tkus/Tester/new.jsonl"])
        self.assertEqual(len(self.entries()), 2)
        self.assertEqual(self.git("status", "--porcelain").strip(), "")

    def test_without_the_command_the_ledger_stays_split(self):
        """The case being fixed, so the test above means something."""
        self.recut()
        self.inject_usage(1000)
        self.commit("second")

        self.assertEqual(self.ledger_files(),
                         [".tkus/Tester/new.jsonl", ".tkus/Tester/old.jsonl"])

    def test_a_commit_with_no_usage_folds(self):
        self.recut()
        self.tkus("rename", "old")
        self.commit("no agent here")

        self.assertEqual(self.ledger_files(), [".tkus/Tester/new.jsonl"])
        self.assertEqual(len(self.entries()), 1)

    def test_an_abandoned_commit_neither_loses_nor_doubles(self):
        """The fold is staged in pre-commit, so an abandoned commit leaves it in
        the index. The next commit must redo it from HEAD, not trust that."""
        self.recut()
        self.tkus("rename", "old")
        self.inject_usage(1000)
        with open(os.path.join(self.repo, "f.txt"), "a") as fh:
            fh.write("x")
        self.git("add", "f.txt")
        self.env["GIT_EDITOR"] = "false"
        self.git("commit", check=False)
        self.env["GIT_EDITOR"] = "true"
        self.git("commit", "-q", "-m", "for real this time")

        self.assertEqual(self.ledger_files(), [".tkus/Tester/new.jsonl"])
        self.assertEqual(len(self.entries()), 2)

    def test_log_credits_folded_entries_to_the_commits_that_spent_them(self):
        self.recut()
        self.tkus("rename", "old")
        self.inject_usage(1000)
        self.commit("second")

        data = json.loads(self.tkus("log", "--json").stdout.decode())
        self.assertEqual(data["orphaned"], [])
        self.assertEqual(sorted(c["subject"] for c in data["commits"]),
                         ["first", "second"])
        self.assertAlmostEqual(data["total"], sum(e["usd"] for e in self.entries()),
                               places=9)

    def test_the_alias_follows_a_later_branch_m(self):
        """Branch config moves with `git branch -m`, so a pending rename does
        not strand itself on the name it was made under."""
        self.recut()
        self.tkus("rename", "old")
        self.git("branch", "-m", "new", "newer")
        self.commit("second")

        self.assertEqual(self.ledger_files(), [".tkus/Tester/newer.jsonl"])

    def test_refuses_while_the_old_branch_still_exists(self):
        """The fold leaves a live branch's file alone, so accepting this would
        record a rename that silently never happens."""
        self.commit("base")
        self.git("checkout", "-q", "-b", "old")
        self.inject_usage(1000)
        self.commit("first")
        self.git("checkout", "-q", "-b", "new")

        out = self.tkus("rename", "old")
        self.assertEqual(out.returncode, 1)
        self.assertIn("git branch -D old", out.stderr.decode())
        self.assertEqual(self.aliases(), [])

    def test_refuses_a_name_with_no_ledger(self):
        self.recut()
        self.assertEqual(self.tkus("rename", "nonesuch").returncode, 1)
        self.assertEqual(self.aliases(), [])

    def test_refuses_to_rename_a_branch_into_itself(self):
        self.recut()
        self.assertEqual(self.tkus("rename", "new", "new").returncode, 1)
        self.assertEqual(self.aliases(), [])

    def test_running_it_twice_records_it_once(self):
        self.recut()
        self.tkus("rename", "old")
        self.tkus("rename", "old")
        self.assertEqual(self.aliases(), ["old"])

    def test_an_explicit_target_need_not_be_checked_out(self):
        self.recut()
        self.git("checkout", "-q", "-b", "elsewhere")
        self.assertEqual(self.tkus("rename", "old", "new").returncode, 0)
        self.assertEqual(self.aliases("new"), ["old"])
        self.assertEqual(self.aliases("elsewhere"), [])

    def test_with_no_arguments_it_lists_ledgers_of_deleted_branches(self):
        self.recut()
        self.git("checkout", "-q", "-b", "live")
        self.inject_usage(1000)
        self.commit("on live")
        self.git("checkout", "-q", "new")
        self.git("merge", "-q", "--no-edit", "live")

        out = self.tkus("rename")
        self.assertEqual(out.returncode, 0)
        listing = out.stdout.decode()
        self.assertIn("old", listing)
        self.assertNotIn("live", listing)


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


class TestReportWithoutInstall(RepoLedgerTestCase):
    """`tkus report` works in an uninstalled repo -- but says so.

    Reading is not recording. `report` queries the agents' transcripts directly
    and filters by repository, so it never needs a hook. The risk is that its
    "since the beginning" header reads like a backlog awaiting attribution when
    in fact nothing is recording and nothing ever will be.
    """

    def _report(self):
        out = subprocess.run([sys.executable, "-m", "tkus", "report"],
                             cwd=self.repo, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=self.env)
        return out.returncode, out.stdout.decode()

    def _uninstall(self):
        subprocess.run([sys.executable, "-m", "tkus", "uninstall"], cwd=self.repo,
                       env=self.env, stdout=subprocess.DEVNULL, check=True)
        self.assertFalse(hooks.is_installed(self.repo))

    def test_succeeds_and_warns_when_not_installed(self):
        self._uninstall()
        code, text = self._report()
        self.assertEqual(code, 0)
        self.assertIn("not installed", text)

    def test_warns_even_when_there_is_no_usage_to_show(self):
        """The empty case is the common one in a fresh repo, and the one where
        the reason for the emptiness matters most."""
        self._uninstall()
        code, text = self._report()
        self.assertIn("no AI usage found", text)
        self.assertIn("not installed", text)

    def test_is_silent_once_installed(self):
        code, text = self._report()
        self.assertEqual(code, 0)
        self.assertNotIn("not installed", text)

    def test_report_creates_no_state_when_uninstalled(self):
        self._uninstall()
        shutil.rmtree(os.path.join(cursor.git_dir(self.repo), "tkus"),
                      ignore_errors=True)
        self._report()
        self.assertFalse(os.path.exists(
            os.path.join(cursor.git_dir(self.repo), "tkus")))

    def test_partial_install_still_counts_as_not_installed(self):
        """One hook missing means recording is broken, not merely degraded."""
        os.remove(os.path.join(hooks.hooks_path(self.repo), "pre-commit"))
        self.assertFalse(hooks.is_installed(self.repo))

    def test_a_foreign_hook_does_not_count_as_installed(self):
        directory = hooks.hooks_path(self.repo)
        for name in hooks.HOOKS:
            with open(os.path.join(directory, name), "w") as fh:
                fh.write("#!/bin/sh\necho someone elses hook\n")
        self.assertFalse(hooks.is_installed(self.repo))
