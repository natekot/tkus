"""Claude Code telemetry receiver, for `tkus audit` only.

Transcripts miss some of the requests Claude Code bills for: compaction, the
sub-requests behind WebFetch and WebSearch, and small helper calls. Claude
Code's OpenTelemetry export does not -- it emits one `api_request` event per
request, carrying the same `request_id` the transcripts use. This module
receives those events on localhost and keeps a few fields of each, so the audit
can say what the transcripts missed.

Commit recording does not depend on it. It is opt-in, and when it is not
running nothing else changes.

Privacy: every event also carries the user's email and account identifiers.
Only the fields in KEEP are written -- no prompt, response, or identity.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterable, List, Optional
from urllib.error import URLError
from urllib.request import urlopen

from .pricing import _global_config_dir
from .providers.base import format_timestamp, parse_timestamp

DEFAULT_PORT = 4318
HOST = "127.0.0.1"
EVENTS = ("api_request", "api_error")

# The only attributes ever written. Everything else in an event -- email,
# account and organization ids, prompts if the user enabled them -- is dropped.
KEEP = (
    "session.id", "request_id", "model", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "cost_usd_micros",
    "speed", "query_source", "agent.name", "status_code", "attempt",
)
NUMERIC = ("input_tokens", "output_tokens", "cache_read_tokens",
           "cache_creation_tokens", "cost_usd_micros", "status_code", "attempt")

TASK_NAME = "tkus-telemetry"


def directory() -> str:
    return os.path.join(_global_config_dir(), "telemetry")


def _receiver_log() -> str:
    return os.path.join(directory(), "receiver.jsonl")


def endpoint(port: int = DEFAULT_PORT) -> str:
    return "http://%s:%d/v1/logs" % (HOST, port)


def claude_env(port: int = DEFAULT_PORT) -> Dict[str, str]:
    """What Claude Code needs in `env` to send its events here."""
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": endpoint(port),
    }


# --------------------------------------------------------------------------
# Parsing OTLP/JSON
# --------------------------------------------------------------------------

def _value(any_value: dict):
    """An OTLP AnyValue. intValue is a string in OTLP/JSON (64-bit safe)."""
    if not isinstance(any_value, dict):
        return None
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in any_value:
            value = any_value[key]
            return int(value) if key == "intValue" else value
    return None


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    try:
        text = str(value)
        return float(text) if "." in text else int(text)
    except (TypeError, ValueError):
        return None


def _when(attrs: dict, record: dict) -> Optional[datetime]:
    stamp = attrs.get("event.timestamp")
    if isinstance(stamp, str):
        parsed = parse_timestamp(stamp)
        if parsed is not None:
            return parsed
    nanos = record.get("timeUnixNano") or record.get("observedTimeUnixNano")
    try:
        return datetime.fromtimestamp(int(nanos) / 1e9, timezone.utc)
    except (TypeError, ValueError):
        return None


def events_from_otlp(payload: dict) -> List[dict]:
    """The api_request / api_error events in an OTLP logs export, trimmed to KEEP."""
    out = []
    for resource in payload.get("resourceLogs") or []:
        for scope in resource.get("scopeLogs") or []:
            for record in scope.get("logRecords") or []:
                attrs = {}
                for attr in record.get("attributes") or []:
                    if isinstance(attr, dict) and "key" in attr:
                        attrs[attr["key"]] = _value(attr.get("value"))
                name = attrs.get("event.name")
                if not name:
                    body = _value(record.get("body") or {})
                    name = str(body or "").rsplit(".", 1)[-1]
                if name not in EVENTS:
                    continue
                when = _when(attrs, record)
                if when is None:
                    continue
                event = {"event": name, "at": format_timestamp(when)}
                for key in KEEP:
                    if attrs.get(key) is None:
                        continue
                    value = attrs[key]
                    event[key] = _number(value) if key in NUMERIC else value
                out.append(event)
    return out


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

_lock = threading.Lock()


def store(events: Iterable[dict]) -> int:
    """Append events to telemetry/YYYY-MM.jsonl by their own month."""
    count = 0
    with _lock:
        os.makedirs(directory(), exist_ok=True)
        for event in events:
            path = os.path.join(directory(), event["at"][:7] + ".jsonl")
            with open(path, "a") as fh:
                fh.write(json.dumps(event, sort_keys=True) + "\n")
            count += 1
    return count


def load(since: datetime, until: datetime) -> List[dict]:
    """Events with since < at <= until, deduplicated by request_id."""
    out = []
    seen = set()
    if not os.path.isdir(directory()):
        return out
    months = sorted(name for name in os.listdir(directory())
                    if name.endswith(".jsonl") and name[:4].isdigit())
    for name in months:
        if name[:7] < format_timestamp(since)[:7] or name[:7] > format_timestamp(until)[:7]:
            continue
        with open(os.path.join(directory(), name), "r", errors="replace") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                when = parse_timestamp(event.get("at", ""))
                if when is None or when <= since or when > until:
                    continue
                key = (event.get("event"), event.get("request_id"))
                if event.get("request_id") and key in seen:
                    continue
                seen.add(key)
                event["at"] = when
                out.append(event)
    return out


def starts() -> List[datetime]:
    """When the receiver was started. A start inside an interval means it was
    down for part of it, so telemetry for that interval is incomplete."""
    out = []
    try:
        with open(_receiver_log(), "r") as fh:
            for line in fh:
                try:
                    stamp = parse_timestamp(json.loads(line).get("at", ""))
                except ValueError:
                    continue
                if stamp is not None:
                    out.append(stamp)
    except OSError:
        pass
    return sorted(out)


def port() -> int:
    """The port the receiver last started on."""
    found = DEFAULT_PORT
    try:
        with open(_receiver_log(), "r") as fh:
            for line in fh:
                try:
                    found = int(json.loads(line).get("port") or found)
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass
    return found


def is_up(port: int = DEFAULT_PORT) -> bool:
    try:
        with urlopen("http://%s:%d/health" % (HOST, port), timeout=2) as resp:
            return resp.status == 200
    except (URLError, OSError, ValueError):
        return False


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._reply(200, b"ok", "text/plain")
        else:
            self._reply(404, b"", "text/plain")

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/v1/logs":
            self._reply(404, b"", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                body = gzip.decompress(body)
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
        except (ValueError, OSError, EOFError) as exc:
            self._reply(400, json.dumps({"error": str(exc)}).encode(),
                        "application/json")
            return
        store(events_from_otlp(payload))
        self._reply(200, b"{}", "application/json")

    def _reply(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 -- stdlib signature
        pass  # runs headless; a log line per export would only grow a file


class _Server(ThreadingHTTPServer):
    """Owns its port outright. HTTPServer sets SO_REUSEADDR, which on Windows
    lets a second receiver bind a port the first is listening on -- both then
    run, and each gets some of the requests."""

    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return _Server((HOST, port), Handler)


def serve(port: int = DEFAULT_PORT) -> None:
    try:
        server = make_server(port)
    except OSError:
        # Another receiver already has the port -- one `setup` started, say,
        # before logon started this one. Succeed, so the task does not keep
        # restarting it; fail for anything else on the port.
        if is_up(port):
            return
        raise
    os.makedirs(directory(), exist_ok=True)
    with open(_receiver_log(), "a") as fh:
        fh.write(json.dumps({"kind": "start", "at": format_timestamp(
            datetime.now(timezone.utc)), "port": port}) + "\n")
    try:
        server.serve_forever()
    finally:
        server.server_close()


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

def claude_settings_path() -> str:
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(home, "settings.json")


def merged_settings(current: dict, port: int = DEFAULT_PORT):
    # type: (dict, int) -> tuple
    """(new settings, conflicts). A conflict is an existing telemetry setting
    that points somewhere else -- an organization's collector, say. That is
    never overwritten."""
    env = dict(current.get("env") or {})
    wanted = claude_env(port)
    conflicts = []
    for key, value in sorted(env.items()):
        if key in wanted and value != wanted[key]:
            conflicts.append((key, value))
        elif key.startswith("OTEL_EXPORTER_OTLP_") and key not in wanted:
            # A general OTLP endpoint would still route other signals elsewhere,
            # and suggests telemetry is already set up for someone else.
            conflicts.append((key, value))
    env.update(wanted)
    out = dict(current)
    out["env"] = env
    return out, conflicts


def pythonw() -> str:
    """The windowless interpreter on Windows, so logon does not open a console."""
    if os.name == "nt":
        candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.isfile(candidate):
            return candidate
    return sys.executable


def serve_command(port: int = DEFAULT_PORT) -> List[str]:
    return [pythonw(), "-m", "tkus", "telemetry", "serve", "--port", str(port)]


def schedule_at_logon(port: int = DEFAULT_PORT) -> Optional[str]:
    """Register the receiver to start at logon. Returns None on success, or the
    reason it could not."""
    if os.name != "nt":
        return "automatic start is only set up on Windows"
    if not shutil.which("schtasks"):
        return "schtasks not found"
    domain, name = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME", "")
    user = "%s\\%s" % (domain, name) if domain else name
    handle, path = tempfile.mkstemp(suffix=".xml")
    os.close(handle)
    try:
        # schtasks reads the XML as the encoding it declares, UTF-16.
        with open(path, "w", encoding="utf-16") as fh:
            fh.write(task_xml(serve_command(port), user))
        result = subprocess.run(
            ["schtasks", "/create", "/tn", TASK_NAME, "/xml", path, "/f"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    finally:
        os.remove(path)
    if result.returncode != 0:
        return result.stdout.decode(errors="replace").strip()
    return None


def task_xml(command, user):
    # type: (List[str], str) -> str
    """The logon task, as Task Scheduler XML.

    `schtasks /create /sc onlogon` takes Task Scheduler's defaults, and three
    of them stop a receiver meant to run all session: it is killed when a
    laptop goes on battery, is not started on battery, and is killed after 72
    hours. The receiver died within a day that way. XML is the only way
    schtasks sets them.

    A receiver that dies anyway is started again within five minutes by a
    repeating trigger; while one runs, IgnoreNew makes those starts no-ops.
    RestartOnFailure cannot do this: it retries only a launch that failed,
    not a process that exited.
    """
    from xml.sax.saxutils import escape
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>tkus: receives Claude Code telemetry for `tkus audit`</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <StartWhenAvailable>true</StartWhenAvailable>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
""".format(user=escape(user), command=escape(command[0]),
           arguments=escape(subprocess.list2cmdline(command[1:])))


def run_task() -> bool:
    """Start the logon task now, so the task owns the receiver and restarts it."""
    if os.name != "nt" or not shutil.which("schtasks"):
        return False
    result = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def start_detached(port: int = DEFAULT_PORT) -> None:
    kwargs = {}  # type: dict
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED, NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(serve_command(port), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, **kwargs)
