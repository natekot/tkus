"""Copilot adapter behaviour, pinned to what the real database actually contains."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone

from tkus.providers import copilot
from tkus.providers.base import parse_timestamp

from . import fixtures

FAR_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


class CopilotTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "copilot")
        os.environ["TKUS_COPILOT_HOME"] = self.home
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.provider = copilot.CopilotProvider()

    def tearDown(self):
        os.environ.pop("TKUS_COPILOT_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_remote(self, url):
        subprocess.run(["git", "remote", "add", "origin", url],
                       cwd=self.repo, check=True)

    def collect(self, since=None, until=FAR_FUTURE):
        return self.provider.collect(self.repo, since, until)


class TestRemoteParsing(unittest.TestCase):
    def test_https_and_ssh_forms_reduce_to_owner_name(self):
        """Copilot records `owner/name`; the local remote can be in any form."""
        for url in (
            "https://github.com/acme/widget.git",
            "https://github.com/acme/widget",
            "git@github.com:acme/widget.git",
            "ssh://git@github.com/acme/widget.git",
            "https://user@github.com/acme/widget.git",
            "https://github.com/acme/widget/",
        ):
            self.assertEqual(copilot.parse_remote(url), "acme/widget", url)

    def test_nested_group_preserved(self):
        self.assertEqual(
            copilot.parse_remote("git@gitlab.com:group/sub/widget.git"),
            "group/sub/widget")

    def test_junk_returns_none(self):
        for url in ("", "not a url", "/local/path"):
            self.assertIsNone(copilot.parse_remote(url))


class TestAttribution(CopilotTestCase):
    def test_matches_by_repository_slug(self):
        self.set_remote("git@github.com:acme/widget.git")
        fixtures.write_copilot_db(
            self.home,
            [fixtures.usage_row(1, output_tokens=100),
             fixtures.usage_row(2, session_id="s2", output_tokens=999)],
            [("s1", None, "acme/widget"), ("s2", None, "other/thing")])
        records = self.collect()
        self.assertEqual([r.request_id for r in records], ["1"])

    def test_slug_match_is_case_insensitive(self):
        self.set_remote("https://github.com/Acme/Widget.git")
        fixtures.write_copilot_db(
            self.home, [fixtures.usage_row(1, output_tokens=5)],
            [("s1", None, "acme/widget")])
        self.assertEqual(len(self.collect()), 1)

    def test_windows_cwd_from_another_machine_is_not_matched_by_path(self):
        """Recorded cwd can come from another OS entirely; the slug is what counts."""
        self.set_remote("git@github.com:acme/widget.git")
        fixtures.write_copilot_db(
            self.home, [fixtures.usage_row(1, output_tokens=5)],
            [("s1", "C:\\git\\widget", "acme/widget")])
        self.assertEqual(len(self.collect()), 1)

    def test_cwd_fallback_when_repo_has_no_remote(self):
        fixtures.write_copilot_db(
            self.home,
            [fixtures.usage_row(1, output_tokens=5),
             fixtures.usage_row(2, session_id="s2", output_tokens=99)],
            [("s1", self.repo, None), ("s2", os.path.join(self.tmp, "elsewhere"), None)])
        self.assertEqual([r.request_id for r in self.collect()], ["1"])

    def test_no_remote_and_no_cwd_yields_nothing(self):
        fixtures.write_copilot_db(
            self.home, [fixtures.usage_row(1, output_tokens=5)],
            [("s1", None, None)])
        self.assertEqual(self.collect(), [])


class TestTokenAccounting(CopilotTestCase):
    def setUp(self):
        super().setUp()
        self.set_remote("git@github.com:acme/widget.git")

    def test_cached_tokens_are_subtracted_from_input(self):
        """input_tokens already includes the cached tokens -- verified with no
        exceptions on real data. Passing it through unchanged would charge the
        cached tokens twice."""
        fixtures.write_copilot_db(
            self.home,
            [fixtures.usage_row(1, input_tokens=18262, cache_read=18066,
                                cache_write=161, output_tokens=132)],
            [("s1", None, "acme/widget")])
        r = self.collect()[0]
        self.assertEqual(r.input_tokens, 35)      # 18262 - 18066 - 161
        self.assertEqual(r.cache_read, 18066)
        self.assertEqual(r.cache_write_5m, 161)
        self.assertEqual(r.cache_write_1h, 0)

    def test_uncached_input_clamped_at_zero(self):
        """A schema change breaking the invariant must not produce a negative."""
        fixtures.write_copilot_db(
            self.home,
            [fixtures.usage_row(1, input_tokens=10, cache_read=999, cache_write=999)],
            [("s1", None, "acme/widget")])
        self.assertEqual(self.collect()[0].input_tokens, 0)

    def test_reasoning_tokens_carried(self):
        fixtures.write_copilot_db(
            self.home, [fixtures.usage_row(1, reasoning=349)],
            [("s1", None, "acme/widget")])
        self.assertEqual(self.collect()[0].reasoning_tokens, 349)

    def test_recorded_cost_is_carried_verbatim(self):
        fixtures.write_copilot_db(
            self.home, [fixtures.usage_row(1, nano_aiu=6961650000)],
            [("s1", None, "acme/widget")])
        r = self.collect()[0]
        self.assertEqual(r.nano_aiu, 6961650000)
        self.assertFalse(r.unbilled)

    def test_missing_cost_marks_endpoint_unbilled(self):
        """The self-hosted endpoint records no cost -- real compute, no charge."""
        fixtures.write_copilot_db(
            self.home,
            [fixtures.usage_row(1, model="internal-model", nano_aiu=None,
                                input_tokens=5000, output_tokens=200)],
            [("s1", None, "acme/widget")])
        r = self.collect()[0]
        self.assertIsNone(r.nano_aiu)
        self.assertTrue(r.unbilled)
        self.assertEqual(r.input_tokens, 5000)


class TestWindowAndDedup(CopilotTestCase):
    def setUp(self):
        super().setUp()
        self.set_remote("git@github.com:acme/widget.git")

    def test_window_is_exclusive_at_start_inclusive_at_end(self):
        fixtures.write_copilot_db(
            self.home,
            [fixtures.usage_row(1, created_at="2026-08-01T00:00:00.000Z"),
             fixtures.usage_row(2, created_at="2026-08-02T00:00:00.000Z"),
             fixtures.usage_row(3, created_at="2026-08-03T00:00:00.000Z")],
            [("s1", None, "acme/widget")])
        records = self.collect(
            since=parse_timestamp("2026-08-01T00:00:00.000Z"),
            until=parse_timestamp("2026-08-02T00:00:00.000Z"))
        self.assertEqual([r.request_id for r in records], ["2"])

    def test_rows_are_deduped_by_id(self):
        fixtures.write_copilot_db(
            self.home, [fixtures.usage_row(1), fixtures.usage_row(2)],
            [("s1", None, "acme/widget")])
        ids = [r.request_id for r in self.collect()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_unparseable_timestamp_skipped_not_fatal(self):
        fixtures.write_copilot_db(
            self.home,
            [fixtures.usage_row(1, created_at="not-a-date"),
             fixtures.usage_row(2, created_at="2026-08-02T00:00:00.000Z")],
            [("s1", None, "acme/widget")])
        self.assertEqual([r.request_id for r in self.collect()], ["2"])


class TestSafety(CopilotTestCase):
    def test_missing_database_returns_empty(self):
        """Most users have no Copilot; the hook must stay silent, not fail."""
        self.assertEqual(self.collect(), [])

    def test_missing_usage_table_returns_empty(self):
        """Older Copilot builds predate the table."""
        os.makedirs(self.home, exist_ok=True)
        conn = sqlite3.connect(os.path.join(self.home, "session-store.db"))
        conn.execute("create table sessions (id TEXT)")
        conn.commit(); conn.close()
        self.assertEqual(self.collect(), [])

    def test_connection_is_read_only(self):
        """Copilot may be running against this file; never write to it."""
        self.set_remote("git@github.com:acme/widget.git")
        path = fixtures.write_copilot_db(
            self.home, [fixtures.usage_row(1)], [("s1", None, "acme/widget")])
        conn = copilot.connect_readonly(path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("delete from assistant_usage_events")
        finally:
            conn.close()

    def test_query_touches_no_prose_column(self):
        """The database also stores prompts and responses. Reading them could
        leak them into a commit message pushed to a shared remote."""
        query = copilot._QUERY.lower()
        for column in ("user_message", "assistant_response", "summary",
                       "search_index", "turns"):
            self.assertNotIn(column, query)

    def test_query_selects_only_allowed_columns(self):
        import re
        selected = re.findall(r"\b([us]\.[a-z_]+)", copilot._QUERY)
        self.assertTrue(selected)
        for column in selected:
            self.assertIn(column, copilot.ALLOWED_COLUMNS, column)

    def test_no_select_star(self):
        """A wildcard would start reading whatever columns GitHub adds next."""
        self.assertNotIn("*", copilot._QUERY)


if __name__ == "__main__":
    unittest.main()
