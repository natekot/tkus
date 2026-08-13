"""Collection behavior, each test pinned to something observed in real data."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from tkus.providers import claude_code
from tkus.providers.base import parse_timestamp

from . import fixtures

FAR_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


class ProviderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects)
        os.environ["TKUS_CLAUDE_PROJECTS"] = self.projects
        self.provider = claude_code.ClaudeCodeProvider()

    def tearDown(self):
        os.environ.pop("TKUS_CLAUDE_PROJECTS", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def repo(self, name="repo"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        return path

    def collect(self, repo, since=None, until=FAR_FUTURE):
        return self.provider.collect(repo, since, until)


class TestDeduplication(ProviderTestCase):
    def test_repeated_request_id_counted_once(self):
        """Usage repeats on every line sharing a requestId (one per content
        block). Counting them all roughly doubles the total."""
        repo = self.repo()
        lines = [
            fixtures.assistant_line("req-1", cwd=repo, output_tokens=100)
            for _ in range(5)
        ]
        lines.append(fixtures.assistant_line("req-2", cwd=repo, output_tokens=7))
        fixtures.write_project(self.projects, fixtures.encode(repo), lines)

        records = self.collect(repo)
        self.assertEqual(len(records), 2)
        self.assertEqual(sum(r.output_tokens for r in records), 107)

    def test_dedup_spans_multiple_session_files(self):
        repo = self.repo()
        encoded = fixtures.encode(repo)
        directory = os.path.join(self.projects, encoded)
        os.makedirs(directory, exist_ok=True)
        for name in ("a.jsonl", "b.jsonl"):
            with open(os.path.join(directory, name), "w") as fh:
                fh.write(fixtures.assistant_line("shared", cwd=repo, output_tokens=50) + "\n")

        self.assertEqual(len(self.collect(repo)), 1)


class TestFiltering(ProviderTestCase):
    def test_synthetic_model_excluded(self):
        repo = self.repo()
        fixtures.write_project(self.projects, fixtures.encode(repo), [
            fixtures.assistant_line("a", model="<synthetic>", output_tokens=999),
            fixtures.assistant_line("b", cwd=repo, output_tokens=10),
        ])
        records = self.collect(repo)
        self.assertEqual([r.request_id for r in records], ["b"])

    def test_sidechain_included(self):
        """Subagent requests are billed like any other."""
        repo = self.repo()
        fixtures.write_project(self.projects, fixtures.encode(repo), [
            fixtures.assistant_line("main", cwd=repo, output_tokens=10),
            fixtures.assistant_line("sub", cwd=repo, output_tokens=20, sidechain=True),
        ])
        self.assertEqual(sum(r.output_tokens for r in self.collect(repo)), 30)

    def test_non_assistant_lines_ignored(self):
        repo = self.repo()
        fixtures.write_project(self.projects, fixtures.encode(repo), [
            '{"type":"mode","mode":"normal"}',
            fixtures.assistant_line("a", cwd=repo, output_tokens=5),
        ])
        self.assertEqual(len(self.collect(repo)), 1)

    def test_truncated_line_tolerated(self):
        """A session being written to can end mid-line."""
        repo = self.repo()
        fixtures.write_project(self.projects, fixtures.encode(repo), [
            fixtures.assistant_line("a", cwd=repo, output_tokens=5),
            '{"type":"assistant","requestId":"b","mess',
        ])
        self.assertEqual(len(self.collect(repo)), 1)


class TestCwdScoping(ProviderTestCase):
    def test_encoding_collision_rejected_by_cwd_check(self):
        """`/a/b-c` and `/a/b/c` both encode to `-a-b-c`, so the directory glob
        over-matches; the per-record cwd check is what keeps them apart."""
        wanted = self.repo(os.path.join("a", "b", "c"))
        other = self.repo("a-b-c-other")

        self.assertEqual(
            fixtures.encode(os.path.join(self.tmp, "a", "b", "c")),
            fixtures.encode(os.path.join(self.tmp, "a", "b")) + "-c",
        )

        fixtures.write_project(self.projects, fixtures.encode(wanted), [
            fixtures.assistant_line("mine", cwd=wanted, output_tokens=10),
            fixtures.assistant_line("theirs", cwd=other, output_tokens=999),
        ])

        records = self.collect(wanted)
        self.assertEqual([r.request_id for r in records], ["mine"])

    def test_subdirectory_sessions_are_collected(self):
        """A session started in a subdirectory lands in its own project dir."""
        repo = self.repo()
        sub = os.path.join(repo, "packages", "api")
        os.makedirs(sub, exist_ok=True)
        fixtures.write_project(self.projects, fixtures.encode(repo), [
            fixtures.assistant_line("root", cwd=repo, output_tokens=1),
        ])
        fixtures.write_project(self.projects, fixtures.encode(sub), [
            fixtures.assistant_line("sub", cwd=sub, output_tokens=2),
        ])
        ids = sorted(r.request_id for r in self.collect(repo))
        self.assertEqual(ids, ["root", "sub"])

    def test_sibling_repo_excluded(self):
        repo = self.repo("alpha")
        sibling = self.repo("beta")
        fixtures.write_project(self.projects, fixtures.encode(sibling), [
            fixtures.assistant_line("x", cwd=sibling, output_tokens=99),
        ])
        self.assertEqual(self.collect(repo), [])


class TestWindow(ProviderTestCase):
    def test_since_is_exclusive_and_until_inclusive(self):
        """The cursor is the end of an already-attributed window, so a record
        exactly at the cursor must not be counted twice."""
        repo = self.repo()
        fixtures.write_project(self.projects, fixtures.encode(repo), [
            fixtures.assistant_line("early", cwd=repo, output_tokens=1,
                                    timestamp="2026-08-01T00:00:00.000Z"),
            fixtures.assistant_line("boundary", cwd=repo, output_tokens=2,
                                    timestamp="2026-08-02T00:00:00.000Z"),
            fixtures.assistant_line("late", cwd=repo, output_tokens=4,
                                    timestamp="2026-08-03T00:00:00.000Z"),
        ])
        since = parse_timestamp("2026-08-02T00:00:00.000Z")
        until = parse_timestamp("2026-08-03T00:00:00.000Z")
        ids = sorted(r.request_id for r in self.collect(repo, since, until))
        self.assertEqual(ids, ["late"])


class TestCacheFields(ProviderTestCase):
    def test_ttl_breakdown_preserved(self):
        """1h and 5m cache writes bill at different multipliers, so they must
        not be collapsed into one number."""
        repo = self.repo()
        fixtures.write_project(self.projects, fixtures.encode(repo), [
            fixtures.assistant_line("a", cwd=repo, cache_1h=1000, cache_5m=250),
        ])
        record = self.collect(repo)[0]
        self.assertEqual(record.cache_write_1h, 1000)
        self.assertEqual(record.cache_write_5m, 250)

    def test_flat_field_used_only_when_breakdown_absent(self):
        repo = self.repo()
        fixtures.write_project(self.projects, fixtures.encode(repo), [
            fixtures.assistant_line("a", cwd=repo, flat_cache_creation=500),
        ])
        record = self.collect(repo)[0]
        self.assertEqual(record.cache_write_1h, 0)
        self.assertEqual(record.cache_write_5m, 500)


class TestTimestampParsing(unittest.TestCase):
    def test_trailing_z_accepted(self):
        """Python 3.9's fromisoformat rejects this shape, which is exactly what
        Claude Code writes."""
        dt = parse_timestamp("2026-08-09T02:10:53.829Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.microsecond, 829000)

    def test_offset_converted_to_utc(self):
        a = parse_timestamp("2026-08-09T07:00:00+05:00")
        b = parse_timestamp("2026-08-09T02:00:00Z")
        self.assertEqual(a, b)

    def test_variable_fractional_digits(self):
        for value in ("2026-08-09T02:10:53Z", "2026-08-09T02:10:53.1Z",
                      "2026-08-09T02:10:53.123456789Z"):
            self.assertIsNotNone(parse_timestamp(value), value)

    def test_garbage_rejected(self):
        for value in ("", "not-a-date", None, "2026-13-45T99:99:99Z"):
            self.assertIsNone(parse_timestamp(value))


if __name__ == "__main__":
    unittest.main()
