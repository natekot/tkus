"""`tkus audit` -- this month's Claude Code usage, every project, against the bill.

The ledger covers only repositories with tkus installed, and a bill covers the
whole account, so the audit reads transcripts directly and ignores the ledger.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from . import fixtures

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# claude-opus-5 at list: $5 in / $25 out per MTok, so 1M output tokens is $25.
AUGUST = "2026-08-09T02:10:53.829Z"


class AuditHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        self.config = os.path.join(self.tmp, "config")
        os.makedirs(self.projects)
        self.history = os.path.join(self.config, "tkus", "audit.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self):
        fixtures.write_project(self.projects, "-alpha", [
            fixtures.assistant_line("a", cwd="/alpha", timestamp=AUGUST,
                                    output_tokens=1000000),
            # A different month, outside the window.
            fixtures.assistant_line("old", cwd="/alpha",
                                    timestamp="2026-07-31T23:59:59.999Z",
                                    output_tokens=1000000),
        ])
        fixtures.write_subagent(self.projects, "-beta", [
            fixtures.assistant_line("b", cwd="/beta", timestamp=AUGUST,
                                    output_tokens=1000000),
        ])

    def audit(self, *args):
        env = dict(os.environ, PYTHONPATH=PROJECT, XDG_CONFIG_HOME=self.config,
                   TKUS_CLAUDE_PROJECTS=self.projects,
                   CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "claude"))
        out = subprocess.run(
            [sys.executable, "-m", "tkus", "audit", "--month", "2026-08"]
            + list(args),
            cwd=self.tmp, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return out.returncode, out.stdout.decode()

    def audit_json(self, *args):
        code, text = self.audit("--json", *args)
        return code, json.loads(text)


class TestAudit(AuditHarness):
    def test_every_project_and_subagents_counted(self):
        self.write()
        code, data = self.audit_json("--billed", "50")
        self.assertEqual(code, 0)
        self.assertAlmostEqual(data["estimate"], 50.0)
        self.assertEqual(data["sessions"], {"reqs": 1, "usd": 25.0})
        self.assertEqual(data["subagents"], {"reqs": 1, "usd": 25.0})

    def test_mismatch_exits_1(self):
        self.write()
        code, data = self.audit_json("--billed", "40")
        self.assertEqual(code, 1)
        self.assertFalse(data["within_tolerance"])
        self.assertAlmostEqual(data["difference_pct"], 25.0)

    def test_tolerance_is_configurable(self):
        self.write()
        code, _ = self.audit_json("--billed", "40", "--tolerance", "30")
        self.assertEqual(code, 0)

    def test_each_run_appended_to_history(self):
        self.write()
        self.audit_json("--billed", "50")
        self.audit_json("--billed", "40")
        with open(self.history) as fh:
            runs = [json.loads(line) for line in fh]
        self.assertEqual([r["billed"] for r in runs], [50.0, 40.0])
        self.assertEqual(runs[0]["month"], "2026-08")

    def test_billed_required_without_a_terminal(self):
        self.write()
        code, text = self.audit()
        self.assertNotEqual(code, 0)
        self.assertIn("--billed", text)
        self.assertFalse(os.path.exists(self.history))

    def test_text_report(self):
        self.write()
        code, text = self.audit("--billed", "50")
        self.assertEqual(code, 0)
        self.assertIn("subagents", text)
        self.assertIn("ok:", text)


class TestAuditTelemetry(AuditHarness):
    def write_telemetry(self, started="2026-07-01T00:00:00.000Z"):
        directory = os.path.join(self.config, "tkus", "telemetry")
        os.makedirs(directory)
        with open(os.path.join(directory, "receiver.jsonl"), "w") as fh:
            fh.write(json.dumps({"kind": "start", "at": started, "port": 1}) + "\n")
        events = [
            {"event": "api_request", "at": AUGUST, "request_id": "a",
             "model": "claude-opus-5", "output_tokens": 1000000,
             "cost_usd_micros": 25000000, "query_source": "repl_main_thread"},
            {"event": "api_request", "at": AUGUST, "request_id": "hidden",
             "model": "claude-opus-5", "output_tokens": 100,
             "cost_usd_micros": 4000000, "query_source": "compact"},
        ]
        with open(os.path.join(directory, "2026-08.jsonl"), "w") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")

    def test_hidden_calls_join_the_estimate(self):
        self.write()
        self.write_telemetry()
        code, data = self.audit_json("--billed", "54")
        self.assertEqual(code, 0)
        self.assertAlmostEqual(data["transcripts"], 50.0)
        self.assertAlmostEqual(data["estimate"], 54.0)
        tele = data["telemetry"]
        self.assertEqual((tele["matched"], tele["hidden"], tele["unreported"]),
                         (1, 1, 1))
        self.assertEqual(tele["hidden_by_source"][0]["query_source"], "compact")

    def test_no_telemetry_no_section(self):
        self.write()
        _, data = self.audit_json("--billed", "50")
        self.assertIsNone(data["telemetry"])


class TestAuditInterval(AuditHarness):
    def test_second_run_compares_since_the_first(self):
        self.write()
        self.audit_json("--billed", "40")
        _, data = self.audit_json("--billed", "50")
        interval = data["interval"]
        self.assertAlmostEqual(interval["billed_delta"], 10.0)
        self.assertIsNone(interval["telemetry_usd"])
        self.assertEqual(interval["telemetry_gap"], "telemetry is not set up")

    def test_first_run_has_no_interval(self):
        self.write()
        _, data = self.audit_json("--billed", "50")
        self.assertIsNone(data["interval"])


class TestBillingTimeZone(unittest.TestCase):
    def test_month_starts_at_local_midnight(self):
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo("America/Denver")
        except Exception:  # Python 3.8, or Windows without tzdata
            self.skipTest("no time zone database")
        from datetime import datetime, timezone
        from tkus.__main__ import _month_window
        at = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        month, start, end = _month_window(None, at, zone)
        self.assertEqual(month, "2026-10")
        self.assertEqual(start, datetime(2026, 10, 1, 6, tzinfo=timezone.utc))
        self.assertEqual(end, at)

    def test_past_month_ends_at_next_local_midnight(self):
        from datetime import datetime, timedelta, timezone
        from tkus.__main__ import _month_window
        at = datetime(2026, 10, 20, tzinfo=timezone.utc)
        _, start, end = _month_window("2026-12", at.replace(year=2027))
        self.assertEqual(start, datetime(2026, 12, 1, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2027, 1, 1, tzinfo=timezone.utc)
                         - timedelta(microseconds=1))


if __name__ == "__main__":
    unittest.main()
