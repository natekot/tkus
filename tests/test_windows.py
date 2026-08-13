"""Windows behaviour, exercised from any host.

Windows is the primary deployment target but no Windows machine is available to
this project, so every rule that differs there is expressed as a pure function
taking an explicit `windows` flag. These tests pin those rules using real
strings observed in a Copilot database captured on a Windows laptop:
`C:\\git\\ctrl` and `c:\\git\\ctrl` both appear, while `git rev-parse
--show-toplevel` reports `C:/git/ctrl`.

They do not prove tkus works on Windows -- only that the platform-specific
logic behaves as intended. See README for what still needs a real Windows run.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest

from tkus import hooks, paths
from tkus.providers import copilot


class TestWindowsPathMatching(unittest.TestCase):
    """The exact strings from the captured Windows database."""

    GIT_ROOT = "C:/git/ctrl"          # as git reports it
    RECORDED = ["C:\\git\\ctrl",      # as Copilot recorded it
                "c:\\git\\ctrl"]      # ...and again, with a lower-case drive

    def test_recorded_paths_match_the_git_root(self):
        for cwd in self.RECORDED:
            self.assertTrue(
                paths.is_within(cwd, self.GIT_ROOT, windows=True),
                "%s should match %s" % (cwd, self.GIT_ROOT))

    def test_subdirectory_matches(self):
        self.assertTrue(paths.is_within(
            "C:\\git\\ctrl\\src\\pkg", self.GIT_ROOT, windows=True))

    def test_sibling_and_prefix_neighbours_do_not_match(self):
        for cwd in ("C:\\git\\ctrl2", "C:\\git\\ctrlx", "C:\\git", "D:\\git\\ctrl"):
            self.assertFalse(
                paths.is_within(cwd, self.GIT_ROOT, windows=True), cwd)

    def test_unrelated_windows_dirs_do_not_match(self):
        # Both of these appear in the real data, from sessions started outside a repo.
        for cwd in ("C:\\Windows\\System32", "c:\\windows\\system32"):
            self.assertFalse(paths.is_within(cwd, self.GIT_ROOT, windows=True))

    def test_trailing_separator_is_irrelevant(self):
        self.assertTrue(paths.is_within(
            "C:\\git\\ctrl\\", "C:/git/ctrl/", windows=True))

    def test_posix_stays_case_sensitive(self):
        """Folding case on POSIX would wrongly merge genuinely distinct paths."""
        self.assertFalse(paths.is_within("/a/B", "/a/b", windows=False))
        self.assertTrue(paths.is_within("/a/b", "/a/b", windows=False))

    def test_posix_does_not_treat_backslash_as_a_separator(self):
        """A backslash is a legal character in a POSIX filename."""
        self.assertFalse(paths.is_within("/a\\b", "/a/b", windows=False))

    def test_windows_path_is_not_corrupted_by_resolution(self):
        """realpath on POSIX would prefix a Windows string with the cwd."""
        self.assertEqual(paths.normalize(paths.resolve("C:\\git\\ctrl"), windows=True),
                         "c:/git/ctrl")

    def test_absoluteness_recognised_for_both_platforms(self):
        for p in ("/usr/local", "C:\\git", "C:/git", "\\\\server\\share"):
            self.assertTrue(paths.looks_absolute(p), p)
        for p in ("", "git", "./git"):
            self.assertFalse(paths.looks_absolute(p), p)


class TestWindowsShapeDetection(unittest.TestCase):
    """Windows rules are applied by path shape as well as by host, so data
    copied off a Windows machine is read correctly anywhere."""

    def test_drive_letter_and_unc_recognised(self):
        for p in ("C:\\git", "c:/git", "\\\\server\\share\\repo"):
            self.assertTrue(paths.has_windows_shape(p), p)

    def test_posix_paths_are_not_windows_shaped(self):
        for p in ("/usr/local", "", "relative/path", "/a\\b"):
            self.assertFalse(paths.has_windows_shape(p), p)

    def test_windows_pair_matches_without_an_explicit_flag(self):
        """This is what makes the integration test below possible off-Windows."""
        self.assertTrue(paths.is_within("C:\\git\\ctrl\\sub", "C:/git/ctrl"))
        self.assertFalse(paths.is_within("C:\\git\\other", "C:/git/ctrl"))

    def test_posix_backslash_filename_still_not_split(self):
        self.assertFalse(paths.is_within("/a\\b", "/a/b"))


class TestWindowsCollectionEndToEnd(unittest.TestCase):
    """The Claude adapter against a transcript recorded on Windows."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects)
        os.environ["TKUS_CLAUDE_PROJECTS"] = self.projects

    def tearDown(self):
        os.environ.pop("TKUS_CLAUDE_PROJECTS", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, dirname, cwd):
        import json
        d = os.path.join(self.projects, dirname)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "session.jsonl"), "w") as fh:
            fh.write(json.dumps({
                "type": "assistant", "requestId": "w1", "cwd": cwd,
                "timestamp": "2026-08-01T12:00:00.000Z",
                "message": {"model": "claude-opus-5",
                            "usage": {"input_tokens": 1, "output_tokens": 500}},
            }) + "\n")

    def test_windows_transcript_matches_git_style_root(self):
        """Claude records `C:\\git\\ctrl`; git reports `C:/git/ctrl`."""
        from datetime import datetime, timezone
        from tkus.providers.claude_code import ClaudeCodeProvider
        # A directory name that no encoding guess will hit, forcing the
        # full-scan fallback -- the path the Windows lookup actually relies on.
        self._write("unguessable-name", "C:\\git\\ctrl")
        records = ClaudeCodeProvider().collect(
            "C:/git/ctrl", None, datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertEqual([r.request_id for r in records], ["w1"])
        self.assertEqual(records[0].output_tokens, 500)

    def test_lowercase_drive_letter_still_matches(self):
        from datetime import datetime, timezone
        from tkus.providers.claude_code import ClaudeCodeProvider
        self._write("unguessable-name", "c:\\git\\ctrl")
        records = ClaudeCodeProvider().collect(
            "C:/git/ctrl", None, datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(len(records), 1)

    def test_neighbouring_windows_repo_excluded(self):
        from datetime import datetime, timezone
        from tkus.providers.claude_code import ClaudeCodeProvider
        self._write("unguessable-name", "C:\\git\\ctrl2")
        records = ClaudeCodeProvider().collect(
            "C:/git/ctrl", None, datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(records, [])


class TestProjectDirectoryEncoding(unittest.TestCase):
    """Claude Code's Windows directory naming is undocumented, so the lookup
    tries several spellings and falls back to scanning every directory."""

    def test_candidates_cover_plausible_drive_spellings(self):
        got = paths.encode_candidates("C:\\Users\\dev\\git\\repo")
        self.assertIn("C:-Users-dev-git-repo", got)   # colon kept
        self.assertIn("C-Users-dev-git-repo", got)    # colon dropped
        self.assertIn("C--Users-dev-git-repo", got)   # colon as dash

    def test_posix_candidate_is_the_documented_encoding(self):
        self.assertIn("-Users-dev-git-repo",
                      paths.encode_candidates("/Users/dev/git/repo"))

    def test_no_duplicates(self):
        got = paths.encode_candidates("/a/b")
        self.assertEqual(len(got), len(set(got)))


class TestSqliteUri(unittest.TestCase):
    """A Windows path cannot go into a file: URI unchanged."""

    @staticmethod
    def build(path):
        # Mirrors readonly_uri without the host's abspath semantics, so the
        # Windows form can be checked from POSIX.
        from urllib.parse import quote
        absolute = path.replace("\\", "/")
        quoted = quote(absolute, safe="/:")
        if not quoted.startswith("/"):
            quoted = "/" + quoted
        return "file://" + quoted + "?mode=ro"

    def test_drive_letter_gets_a_leading_slash(self):
        self.assertEqual(
            self.build("C:\\Users\\dev\\.copilot\\session-store.db"),
            "file:///C:/Users/dev/.copilot/session-store.db?mode=ro")

    def test_spaces_are_percent_encoded(self):
        self.assertIn("Some%20User",
                      self.build("C:\\Users\\Some User\\.copilot\\db.sqlite"))

    def test_real_open_still_works_on_this_host(self):
        tmp = tempfile.mkdtemp()
        try:
            db = os.path.join(tmp, "session-store.db")
            sqlite3.connect(db).close()
            conn = copilot.connect_readonly(db)
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestHookScriptPortability(unittest.TestCase):
    def render(self, python):
        return hooks.TEMPLATE.format(
            marker=hooks.MARKER, name="prepare-commit-msg",
            python=hooks._interpreter(python))

    def test_interpreter_path_uses_forward_slashes(self):
        """Backslash is an escape character to sh."""
        script = self.render("C:\\Python39\\python.exe")
        self.assertIn("C:/Python39/python.exe", script)
        self.assertNotIn("\\", script)

    def test_interpreter_path_is_quoted(self):
        """`C:/Program Files/...` would otherwise split into two words."""
        script = self.render("C:/Program Files/Python39/python.exe")
        self.assertIn('"C:/Program Files/Python39/python.exe" -m tkus', script)

    def test_chained_hook_runs_even_without_an_executable_bit(self):
        """Windows checkouts frequently lack the bit; skipping the user's own
        hook silently would be worse than running it through sh."""
        script = self.render("python3")
        self.assertIn("sh \"$hook_dir/prepare-commit-msg.local\"", script)

    def test_written_hook_has_unix_line_endings(self):
        """A CRLF script fails under sh with `bad interpreter: /bin/sh^M`."""
        tmp = tempfile.mkdtemp()
        try:
            import subprocess
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            hooks.install(tmp)
            for name in hooks.HOOKS:
                with open(os.path.join(tmp, ".git", "hooks", name), "rb") as fh:
                    self.assertNotIn(b"\r\n", fh.read(), name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
