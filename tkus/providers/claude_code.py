"""Claude Code adapter.

Reads ~/.claude/projects/<encoded-cwd>/*.jsonl, plus the subagent transcripts
beside them at <session>/subagents/*.jsonl. Only usage/model/timestamp/cwd
fields are read -- never prompt or response text, which also lives in these
files and must not reach a commit message.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

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
    return all_dirs()


def all_dirs() -> List[str]:
    """Every project directory, for usage not scoped to one repository."""
    root = projects_root()
    if not os.path.isdir(root):
        return []
    return sorted(
        os.path.join(root, name)
        for name in os.listdir(root)
        if os.path.isdir(os.path.join(root, name))
    )


def transcript_files(directory: str):
    # type: (str) -> Tuple[List[str], List[str]]
    """(session files, subagent files) in one project directory.

    Subagents write their own transcript under the session that started them,
    and their requests are not repeated in the session file -- skipping them
    drops all subagent usage.
    """
    sessions = sorted(glob.glob(os.path.join(directory, "*.jsonl")))
    subagents = sorted(glob.glob(os.path.join(directory, "*", "subagents", "*.jsonl")))
    return sessions, subagents


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



def complete_requests(lines):
    # type: (Iterable[Tuple[UsageRecord, bool]]) -> List[Tuple[UsageRecord, bool]]
    """One (record, is_subagent) per request, from its lines in reading order.

    A request is written as one line per content block, and the lines written
    while it streams carry partial usage -- on real data the first line held
    28% fewer output tokens than the last. So each request is dated by its
    first line but counted with its most complete one; ties go to the later
    line. It belongs to the file kind of its first line.
    """
    requests = {}  # type: Dict[str, Tuple[UsageRecord, bool]]
    for record, is_subagent in lines:
        known = requests.get(record.request_id)
        if known is None:
            requests[record.request_id] = (record, is_subagent)
            continue
        first, first_kind = known
        if record.output_tokens >= first.output_tokens:
            record.timestamp = min(record.timestamp, first.timestamp)
            requests[record.request_id] = (record, first_kind)
        elif record.timestamp < first.timestamp:
            first.timestamp = record.timestamp
    return list(requests.values())


def split_window(requests, since, until):
    # type: (Iterable[Tuple[UsageRecord, bool]], Optional[datetime], datetime) -> Tuple[List[UsageRecord], List[UsageRecord]]
    """(session records, subagent records) dated in (since, until]. Windowing
    after `complete_requests` is what counts a request spanning a commit once."""
    sessions = []  # type: List[UsageRecord]
    subagents = []  # type: List[UsageRecord]
    for record, is_subagent in requests:
        if since is not None and record.timestamp <= since:
            continue
        if record.timestamp > until:
            continue
        (subagents if is_subagent else sessions).append(record)
    return sessions, subagents


class ClaudeCodeProvider(Provider):
    name = "claude-code"

    def collect(self, repo_root, since, until):
        # type: (str, Optional[datetime], datetime) -> List[UsageRecord]
        sessions, subagents = self.collect_split(repo_root, since, until)
        return sessions + subagents

    def collect_split(self, repo_root, since, until):
        # type: (Optional[str], Optional[datetime], datetime) -> Tuple[List[UsageRecord], List[UsageRecord]]
        """(session records, subagent records).

        A `repo_root` of None collects every project with no cwd check -- the
        whole account, which is what a bill covers.
        """
        directories = all_dirs() if repo_root is None else candidate_dirs(repo_root)
        lines = []  # type: List[Tuple[UsageRecord, bool]]
        for directory in directories:
            for path, is_subagent in self.files(directory):
                for obj in _iter_records(path):
                    record = self._to_record(obj, repo_root)
                    if record is not None:
                        lines.append((record, is_subagent))
        return split_window(complete_requests(lines), since, until)

    @staticmethod
    def files(directory):
        # type: (str) -> List[Tuple[str, bool]]
        """(path, is_subagent) for every transcript in a project directory, in
        the order they are read."""
        sessions, subagents = transcript_files(directory)
        return [(p, False) for p in sessions] + [(p, True) for p in subagents]

    def _to_record(self, obj, repo_root):
        # type: (dict, Optional[str]) -> Optional[UsageRecord]
        if obj.get("type") != "assistant":
            return None

        request_id = obj.get("requestId")
        if not request_id:
            return None

        if repo_root is not None:
            cwd = obj.get("cwd")
            if not cwd or not _is_within(cwd, repo_root):
                return None

        when = parse_timestamp(obj.get("timestamp", ""))
        if when is None:
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
