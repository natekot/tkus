"""Cursor semantics: exactly-once attribution, abort safety, amend rollback."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from tkus import cursor
from tkus.providers.base import format_timestamp, parse_timestamp

T1 = parse_timestamp("2026-08-01T00:00:00.000Z")
T2 = parse_timestamp("2026-08-02T00:00:00.000Z")
T3 = parse_timestamp("2026-08-03T00:00:00.000Z")


class CursorTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)


class TestCursorLifecycle(CursorTestCase):
    def test_starts_empty(self):
        self.assertIsNone(cursor.cursor_since(self.repo))

    def test_state_lives_inside_git_dir(self):
        """Never in the worktree, so it is never accidentally committed."""
        cursor.write_pending(self.repo, T1)
        path = os.path.join(cursor.git_dir(self.repo), "tkus", "pending.json")
        self.assertTrue(os.path.isfile(path))
        tracked = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.repo,
            stdout=subprocess.PIPE).stdout.decode()
        self.assertNotIn("tkus", tracked)

    def test_pending_does_not_advance_cursor(self):
        """prepare-commit-msg runs before the commit exists; the user may still
        abort in the editor."""
        cursor.write_pending(self.repo, T1)
        self.assertIsNone(cursor.cursor_since(self.repo))

    def test_post_commit_promotes_pending(self):
        cursor.write_pending(self.repo, T1)
        cursor.promote_pending(self.repo)
        self.assertEqual(cursor.cursor_since(self.repo), T1)

    def test_promote_is_idempotent(self):
        """post-commit can fire without a preceding prepare-commit-msg."""
        cursor.write_pending(self.repo, T1)
        cursor.promote_pending(self.repo)
        self.assertIsNone(cursor.promote_pending(self.repo))
        self.assertEqual(cursor.cursor_since(self.repo), T1)

    def test_aborted_commit_leaves_cursor_untouched(self):
        cursor.write_pending(self.repo, T1)
        cursor.promote_pending(self.repo)
        cursor.write_pending(self.repo, T2)  # second commit started...
        # ...and abandoned: post-commit never runs.
        self.assertEqual(cursor.cursor_since(self.repo), T1)

    def test_sequential_commits_do_not_overlap(self):
        cursor.write_pending(self.repo, T1)
        cursor.promote_pending(self.repo)
        first_end = cursor.cursor_since(self.repo)

        cursor.write_pending(self.repo, T2)
        cursor.promote_pending(self.repo)
        second_start = first_end

        self.assertEqual(second_start, T1)
        self.assertEqual(cursor.cursor_since(self.repo), T2)


class TestAmend(CursorTestCase):
    def _commit(self, end, amending=False):
        cursor.write_pending(self.repo, end, amending=amending)
        cursor.promote_pending(self.repo)

    def test_amend_rolls_back_to_previous_window(self):
        """Otherwise the amended commit would re-count usage already attributed
        to it, or lose its own window entirely."""
        self._commit(T1)
        self._commit(T2)
        self.assertEqual(cursor.cursor_since(self.repo, amending=True), T1)

    def test_repeated_amend_keeps_the_same_rollback_point(self):
        """A second amend must not start from the first amend's own end --
        that would silently drop everything between T1 and T2."""
        self._commit(T1)
        self._commit(T2)

        self.assertEqual(cursor.cursor_since(self.repo, amending=True), T1)
        self._commit(T3, amending=True)
        self.assertEqual(cursor.cursor_since(self.repo, amending=True), T1)

        self._commit(T3, amending=True)
        self.assertEqual(cursor.cursor_since(self.repo, amending=True), T1)

    def test_amend_of_first_ever_commit_falls_back_to_full_history(self):
        self._commit(T1)
        self.assertIsNone(cursor.cursor_since(self.repo, amending=True))

    def test_normal_commit_after_amend_advances_normally(self):
        T4 = parse_timestamp("2026-08-04T00:00:00.000Z")
        self._commit(T1)
        self._commit(T2)
        self._commit(T3, amending=True)
        self._commit(T4)
        # The next commit starts where the amended one ended...
        self.assertEqual(cursor.cursor_since(self.repo), T4)
        # ...and amending *that* commit rolls back to the amended end, not past it.
        self.assertEqual(cursor.cursor_since(self.repo, amending=True), T3)


class TestCorruption(CursorTestCase):
    def test_unreadable_state_is_treated_as_empty(self):
        """A corrupt cursor must not block commits."""
        path = os.path.join(cursor.state_dir(self.repo), "cursor.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        self.assertIsNone(cursor.cursor_since(self.repo))

    def test_reset_clears_state(self):
        cursor.write_pending(self.repo, T1)
        cursor.promote_pending(self.repo)
        cursor.reset(self.repo)
        self.assertIsNone(cursor.cursor_since(self.repo))


if __name__ == "__main__":
    unittest.main()
