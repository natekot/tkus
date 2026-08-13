"""Synthetic Claude Code transcripts, so tests do not depend on real usage.

The real transcripts keep growing while a session is open, which makes them
unusable as an exact fixture.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional


def assistant_line(
    request_id: str,
    model: str = "claude-opus-5",
    cwd: str = "/repo",
    timestamp: str = "2026-08-09T02:10:53.829Z",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_1h: int = 0,
    cache_5m: int = 0,
    cache_read: int = 0,
    web_search: int = 0,
    speed: str = "standard",
    service_tier: str = "standard",
    sidechain: bool = False,
    flat_cache_creation: Optional[int] = None,
) -> str:
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "speed": speed,
        "service_tier": service_tier,
        "server_tool_use": {"web_search_requests": web_search},
    }
    if flat_cache_creation is None:
        usage["cache_creation"] = {
            "ephemeral_1h_input_tokens": cache_1h,
            "ephemeral_5m_input_tokens": cache_5m,
        }
        usage["cache_creation_input_tokens"] = cache_1h + cache_5m
    else:
        # Older shape: only the flat field, no per-TTL breakdown.
        usage["cache_creation_input_tokens"] = flat_cache_creation

    return json.dumps(
        {
            "type": "assistant",
            "requestId": request_id,
            "cwd": cwd,
            "timestamp": timestamp,
            "isSidechain": sidechain,
            "message": {"model": model, "usage": usage},
        }
    )


def write_project(root: str, encoded_name: str, lines: List[str]) -> str:
    """Create <root>/<encoded_name>/session.jsonl containing `lines`."""
    directory = os.path.join(root, encoded_name)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "session.jsonl")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def encode(path: str) -> str:
    return path.replace(os.sep, "-")


# --------------------------------------------------------------------------
# GitHub Copilot CLI
# --------------------------------------------------------------------------

COPILOT_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT,
                       host_type TEXT, branch TEXT, summary TEXT);
CREATE TABLE assistant_usage_events (
  id INTEGER PRIMARY KEY, session_id TEXT, model TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_write_tokens INTEGER, reasoning_tokens INTEGER, total_nano_aiu INTEGER,
  request_multiplier REAL, api_endpoint TEXT, token_details_json TEXT,
  created_at TEXT);
CREATE TABLE turns (id INTEGER PRIMARY KEY, session_id TEXT,
                    user_message TEXT, assistant_response TEXT);
"""


def usage_row(
    row_id: int,
    session_id: str = "s1",
    model: str = "claude-sonnet-4.6",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    reasoning: int = 0,
    nano_aiu: Optional[int] = 0,
    created_at: str = "2026-08-01T12:00:00.000Z",
) -> tuple:
    """One assistant_usage_events row.

    `input_tokens` is the raw column, which in real data already includes the
    cached tokens. `nano_aiu=None` marks an unbilled (self-hosted) endpoint.
    """
    endpoint = "/v1/messages" if nano_aiu is not None else "/chat/completions"
    return (row_id, session_id, model, input_tokens, output_tokens, cache_read,
            cache_write, reasoning, nano_aiu, 1.0 if nano_aiu is not None else 0.0,
            endpoint, None, created_at)


def write_copilot_db(home: str, rows: List[tuple], sessions: List[tuple]) -> str:
    """Create <home>/session-store.db. `sessions` are (id, cwd, repository)."""
    import sqlite3

    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, "session-store.db")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(COPILOT_SCHEMA)
    for sid, cwd, repository in sessions:
        conn.execute(
            "insert into sessions values (?,?,?,?,?,?)",
            (sid, cwd, repository, "github", "main", "PROSE-SHOULD-NEVER-BE-READ"),
        )
    conn.executemany(
        "insert into assistant_usage_events values (%s)" % ",".join("?" * 13), rows)
    conn.commit()
    conn.close()
    return path
