"""The telemetry receiver and the reconciliation `tkus audit` builds on it.

Claude Code's OTLP export carries the user's email and account ids on every
event; the receiver must keep none of it.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tkus import audit, telemetry
from tkus.pricing import RateTable
from tkus.providers.base import UsageRecord

AT = datetime(2026, 8, 9, 2, 10, 53, tzinfo=timezone.utc)


def attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": value}}


def otlp(*records):
    return {"resourceLogs": [{
        "resource": {"attributes": [attr("service.name", "claude-code")]},
        "scopeLogs": [{"scope": {"name": "com.anthropic.claude_code.events"},
                       "logRecords": list(records)}]}]}


def api_request(request_id="req_1", source="repl_main_thread", micros=125000,
                at="2026-08-09T02:10:53.829Z", **tokens):
    attrs = [
        attr("event.name", "api_request"), attr("event.timestamp", at),
        attr("session.id", "sess-1"), attr("request_id", request_id),
        attr("model", "claude-opus-5"), attr("query_source", source),
        attr("cost_usd_micros", micros), attr("speed", "normal"),
        attr("user.email", "someone@example.com"),
        attr("user.account_uuid", "acct-uuid"),
        attr("organization.id", "org-id"),
    ]
    for key in ("input_tokens", "output_tokens", "cache_read_tokens",
                "cache_creation_tokens"):
        attrs.append(attr(key, tokens.get(key, 0)))
    return {"timeUnixNano": "1786241453829000000",
            "body": {"stringValue": "claude_code.api_request"},
            "attributes": attrs}


class ConfigHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestParsing(unittest.TestCase):
    def test_identity_is_never_kept(self):
        [event] = telemetry.events_from_otlp(otlp(api_request(output_tokens=5)))
        self.assertTrue(set(event) <= set(telemetry.KEEP) | {"event", "at"})
        self.assertNotIn("someone@example.com", json.dumps(event))
        self.assertNotIn("acct-uuid", json.dumps(event))

    def test_counts_are_numbers(self):
        """intValue is a string in OTLP/JSON."""
        [event] = telemetry.events_from_otlp(otlp(api_request(output_tokens=5)))
        self.assertEqual(event["output_tokens"], 5)
        self.assertEqual(event["cost_usd_micros"], 125000)

    def test_other_events_ignored(self):
        other = api_request()
        other["attributes"][0] = attr("event.name", "tool_result")
        self.assertEqual(telemetry.events_from_otlp(otlp(other)), [])

    def test_name_falls_back_to_body(self):
        record = api_request()
        record["attributes"] = record["attributes"][1:]
        [event] = telemetry.events_from_otlp(otlp(record))
        self.assertEqual(event["event"], "api_request")


class TestServer(ConfigHome):
    def setUp(self):
        super().setUp()
        self.server = telemetry.make_server(0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def post(self, body, headers=None):
        request = Request("http://127.0.0.1:%d/v1/logs" % self.port, data=body,
                          headers=dict({"Content-Type": "application/json"},
                                       **(headers or {})))
        try:
            with urlopen(request, timeout=5) as resp:
                return resp.status
        except HTTPError as exc:
            return exc.code

    def stored(self):
        path = os.path.join(telemetry.directory(), "2026-08.jsonl")
        with open(path) as fh:
            return [json.loads(line) for line in fh]

    def test_events_stored_by_month(self):
        status = self.post(json.dumps(otlp(api_request(output_tokens=5))).encode())
        self.assertEqual(status, 200)
        [event] = self.stored()
        self.assertEqual(event["request_id"], "req_1")

    def test_gzip_accepted(self):
        body = gzip.compress(json.dumps(otlp(api_request())).encode())
        self.assertEqual(self.post(body, {"Content-Encoding": "gzip"}), 200)
        self.assertEqual(len(self.stored()), 1)

    def test_malformed_body_rejected_and_server_survives(self):
        self.assertEqual(self.post(b"{not json"), 400)
        self.assertEqual(self.post(json.dumps(otlp(api_request())).encode()), 200)

    def test_health(self):
        self.assertTrue(telemetry.is_up(self.port))

    def test_second_receiver_steps_aside(self):
        """Logon can start one while `setup` already has: the second must
        succeed, or the task restarts it every minute."""
        self.assertIsNone(telemetry.serve(self.port))


class TestLoad(ConfigHome):
    def test_window_and_duplicates(self):
        events = telemetry.events_from_otlp(otlp(
            api_request("a"), api_request("a"),
            api_request("b", at="2026-08-20T00:00:00.000Z")))
        telemetry.store(events)
        loaded = telemetry.load(AT - timedelta(seconds=1), AT + timedelta(days=1))
        self.assertEqual([e["request_id"] for e in loaded], ["a"])


class TestSetup(unittest.TestCase):
    def test_merges_without_touching_other_settings(self):
        current = {"theme": "dark", "env": {"FOO": "1"}}
        merged, conflicts = telemetry.merged_settings(current)
        self.assertEqual(conflicts, [])
        self.assertEqual(merged["theme"], "dark")
        self.assertEqual(merged["env"]["FOO"], "1")
        self.assertEqual(merged["env"]["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"],
                         "http://127.0.0.1:4318/v1/logs")

    def test_existing_collector_is_a_conflict(self):
        current = {"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.corp:4317"}}
        _, conflicts = telemetry.merged_settings(current)
        self.assertEqual(conflicts, [("OTEL_EXPORTER_OTLP_ENDPOINT",
                                      "https://otel.corp:4317")])

    def test_rerunning_setup_is_not_a_conflict(self):
        merged, _ = telemetry.merged_settings({})
        self.assertEqual(telemetry.merged_settings(merged)[1], [])


class TestLogonTask(unittest.TestCase):
    """`schtasks /sc onlogon` defaults stop the receiver on battery and after
    72 hours; the XML has to turn each of them off."""

    def settings(self):
        import xml.etree.ElementTree as ET
        text = telemetry.task_xml(["C:\py\pythonw.exe", "-m", "tkus",
                                   "telemetry", "serve"], "DOM\me & co")
        root = ET.fromstring(text.split("?>", 1)[1])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        return root, ns

    def test_runs_on_battery_and_forever(self):
        root, ns = self.settings()
        get = lambda path: root.find("t:Settings/t:" + path, ns).text
        self.assertEqual(get("DisallowStartIfOnBatteries"), "false")
        self.assertEqual(get("StopIfGoingOnBatteries"), "false")
        self.assertEqual(get("ExecutionTimeLimit"), "PT0S")
        self.assertEqual(get("IdleSettings/t:StopOnIdleEnd"), "false")
        self.assertEqual(get("MultipleInstancesPolicy"), "IgnoreNew")

    def test_restarted_after_dying(self):
        root, ns = self.settings()
        self.assertEqual(root.find(
            "t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", ns).text, "PT5M")

    def test_command_and_user(self):
        root, ns = self.settings()
        self.assertEqual(root.find("t:Actions/t:Exec/t:Command", ns).text,
                         "C:\py\pythonw.exe")
        self.assertEqual(root.find("t:Actions/t:Exec/t:Arguments", ns).text,
                         "-m tkus telemetry serve")
        self.assertEqual(root.find("t:Triggers/t:LogonTrigger/t:UserId", ns).text,
                         "DOM\me & co")


def record(request_id, output_tokens=1000, at=AT):
    return UsageRecord(provider="claude-code", request_id=request_id,
                       model="claude-opus-5", timestamp=at,
                       output_tokens=output_tokens)


def event(request_id, output_tokens=1000, source="repl_main_thread",
          micros=25000, at=AT):
    return {"event": "api_request", "request_id": request_id, "at": at,
            "model": "claude-opus-5", "query_source": source,
            "output_tokens": output_tokens, "cost_usd_micros": micros}


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.table = RateTable.load(None)
        self.since = AT - timedelta(hours=1)
        self.until = AT + timedelta(hours=1)

    def run_it(self, records, events):
        return audit.reconcile(records, events, self.table, self.since, self.until)

    def test_hidden_calls_priced_by_claude_code(self):
        result = self.run_it([record("a")],
                             [event("a"), event("c", source="compact", micros=4100000)])
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["hidden"], 1)
        self.assertAlmostEqual(result["hidden_usd"], 4.1)
        # 1000 output tokens of Opus 5 at $25/MTok, plus the hidden call.
        self.assertAlmostEqual(result["telemetry_usd"], 0.025 + 4.1)
        [row] = result["hidden_by_source"]
        self.assertEqual((row["query_source"], row["calls"]), ("compact", 1))

    def test_request_without_telemetry_is_unreported(self):
        result = self.run_it([record("a"), record("b")], [event("a")])
        self.assertEqual(result["unreported"], 1)

    def test_token_mismatch_detected(self):
        result = self.run_it([record("a", output_tokens=8)],
                             [event("a", output_tokens=900)])
        self.assertEqual(result["token_mismatches"], 1)

    def test_outside_window_ignored(self):
        late = AT + timedelta(hours=2)
        result = self.run_it([record("a", at=late)], [event("a", at=late)])
        self.assertEqual((result["matched"], result["hidden"]), (0, 0))


class TestCoverage(unittest.TestCase):
    def test_full_coverage(self):
        self.assertIsNone(audit.coverage_gap(
            [AT - timedelta(days=1)], AT, AT + timedelta(days=1), True))

    def test_started_after_interval_began(self):
        self.assertIn("not running", audit.coverage_gap(
            [AT + timedelta(hours=1)], AT, AT + timedelta(days=1), True))

    def test_restart_inside_interval(self):
        self.assertIn("restarted", audit.coverage_gap(
            [AT - timedelta(days=1), AT + timedelta(hours=1)],
            AT, AT + timedelta(days=1), True))

    def test_down_now(self):
        self.assertIn("not running now", audit.coverage_gap(
            [AT - timedelta(days=1)], AT, AT + timedelta(days=1), False))


class TestPreviousRun(unittest.TestCase):
    def test_latest_earlier_run_of_same_month(self):
        history = [
            {"at": "2026-10-10T00:00:00.000Z", "month": "2026-10", "billed": 100.0},
            {"at": "2026-10-15T00:00:00.000Z", "month": "2026-10", "billed": 200.0},
            {"at": "2026-09-30T00:00:00.000Z", "month": "2026-09", "billed": 900.0},
        ]
        prev = audit.previous_run(history, "2026-10", "2026-10-20T00:00:00.000Z")
        self.assertEqual(prev["billed"], 200.0)

    def test_none_for_a_new_month(self):
        history = [{"at": "2026-09-30T00:00:00.000Z", "month": "2026-09",
                    "billed": 900.0}]
        self.assertIsNone(audit.previous_run(history, "2026-10",
                                             "2026-10-02T00:00:00.000Z"))


if __name__ == "__main__":
    unittest.main()
