"""Claude Code adapter.

Reads ~/.claude/projects/<encoded-cwd>/*.jsonl. Only usage/model/timestamp/cwd
fields are read -- never prompt or response text, which also lives in these
files and must not reach a commit message.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from typing import Iterator, List, Optional

from .. import paths
from .base import Provider, UsageRecord, parse_timestamp, register

# Not a billable model; appears in transcripts for locally-generated messages.
SYNTHETIC_MODEL = "<synthetic>"


def projects_root() -> str:
    override = os.environ.get("TKUS_CLAUDE_PROJECTS")
    if override:
        return override
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(home, "projects")


def encode_path(path: str) -> str:
    """Claude Code's project-directory encoding: path separators become dashes."""
    return path.replace("\\", "-").replace("/", "-")


def _is_within(child: str, parent: str) -> bool:
    """True if `child` is `parent` or beneath it.

    Delegates to the shared helper so Windows drive-letter case and mixed
    separators are handled -- `git rev-parse` reports `C:/git/x` while agents
    record `C:\\git\\x`, sometimes with a lower-case drive.
    """
    return paths.is_within(child, parent)


def candidate_dirs(repo_root: str) -> List[str]:
    """Project directories that may hold sessions for this repo.

    A session started in a subdirectory of the repo gets its own project
    directory whose encoded name begins with the repo root's encoding, so a
    prefix glob catches both. The glob can over-match -- `/a/b-c` and `/a/b/c`
    both encode to `-a-b-c` -- which is why every record's `cwd` is verified
    separately in `collect`.
    """
    root = projects_root()
    if not os.path.isdir(root):
        return []
    resolved = paths.resolve(repo_root)
    # The encoding of a Windows drive letter is not documented, so try each
    # plausible spelling rather than committing to one guess.
    matches = []
    for prefix in paths.encode_candidates(resolved):
        for path in glob.glob(os.path.join(root, glob.escape(prefix) + "*")):
            if path not in matches:
                matches.append(path)
    if matches:
        return sorted(matches)
    # Encoding is not fully reversible for unusual path characters. If the
    # prefix guess finds nothing, fall back to scanning every project directory
    # and let the per-record cwd check decide. Correctness over speed here.
    return sorted(
        os.path.join(root, name)
        for name in os.listdir(root)
        if os.path.isdir(os.path.join(root, name))
    )


def _iter_records(path: str) -> Iterator[dict]:
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue  # tolerate a torn final line on a live session
    except OSError:
        return


class ClaudeCodeProvider(Provider):
    name = "claude-code"

    def collect(self, repo_root, since, until):
        # type: (str, Optional[datetime], datetime) -> List[UsageRecord]
        seen = set()
        out = []  # type: List[UsageRecord]

        for directory in candidate_dirs(repo_root):
            for path in sorted(glob.glob(os.path.join(directory, "*.jsonl"))):
                for obj in _iter_records(path):
                    record = self._to_record(obj, repo_root, since, until, seen)
                    if record is not None:
                        out.append(record)
        return out

    def _to_record(self, obj, repo_root, since, until, seen):
        if obj.get("type") != "assistant":
            return None

        # Usage is repeated across every line sharing a requestId (one line per
        # content block). Counting them all roughly doubles the total.
        request_id = obj.get("requestId")
        if not request_id or request_id in seen:
            return None

        cwd = obj.get("cwd")
        if not cwd or not _is_within(cwd, repo_root):
            return None

        when = parse_timestamp(obj.get("timestamp", ""))
        if when is None:
            return None
        if since is not None and when <= since:
            return None
        if when > until:
            return None

        message = obj.get("message") or {}
        model = message.get("model")
        if not model or model == SYNTHETIC_MODEL:
            return None

        usage = message.get("usage") or {}
        if not isinstance(usage, dict):
            return None

        # Cache writes bill at different multipliers per TTL (1h at 2x, 5m at
        # 1.25x). The flat cache_creation_input_tokens field collapses them, so
        # read the breakdown and fall back only when it is absent.
        creation = usage.get("cache_creation") or {}
        cw1h = int(creation.get("ephemeral_1h_input_tokens") or 0)
        cw5m = int(creation.get("ephemeral_5m_input_tokens") or 0)
        if not creation:
            cw5m = int(usage.get("cache_creation_input_tokens") or 0)

        server_tools = usage.get("server_tool_use") or {}

        seen.add(request_id)
        return UsageRecord(
            provider=self.name,
            request_id=request_id,
            model=model,
            timestamp=when,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_write_1h=cw1h,
            cache_write_5m=cw5m,
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            web_search_requests=int(server_tools.get("web_search_requests") or 0),
            speed=str(usage.get("speed") or "standard"),
            service_tier=str(usage.get("service_tier") or "standard"),
        )


register(ClaudeCodeProvider())
