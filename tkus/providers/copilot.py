"""GitHub Copilot CLI adapter.

Reads ~/.copilot/session-store.db, table `assistant_usage_events` -- one row per
model request, with token counts and the exact cost GitHub charged.

Two things differ from the Claude adapter and drive the code below:

1. **Copilot reports its own price.** `total_nano_aiu` is the cost in billionths
   of a GitHub AI Unit, and it is exact: verified on every priced row in a
   787-row sample, it equals the sum over `token_details_json` of
   `tokenCount * costPerBatch / batchSize`. So no rate table is consulted and
   there is no staleness risk. GitHub documents 1 AI Unit = $0.01.

2. **`input_tokens` already contains the cached tokens.** Verified with no
   exceptions on the sample: `input_tokens == uncached + cache_read + cache_write`.
   Passing it through unchanged while also reporting cache reads would charge the
   cached tokens twice -- a ~48x overstatement of the input line on real data.

That database also holds full prompt and response text (`turns`, `sessions.summary`,
the `search_index_*` tables). Only the usage table and three non-prose columns of
`sessions` are ever read; see ALLOWED_COLUMNS.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from datetime import datetime
from urllib.parse import quote
from typing import List, Optional

from .. import paths
from .base import Provider, UsageRecord, parse_timestamp, register

DB_NAME = "session-store.db"

# Every column this adapter is permitted to read. Deliberately excludes every
# prose-bearing column in the database; a test asserts the query touches nothing
# outside this set.
ALLOWED_COLUMNS = frozenset([
    "u.id", "u.session_id", "u.model", "u.input_tokens", "u.output_tokens",
    "u.cache_read_tokens", "u.cache_write_tokens", "u.reasoning_tokens",
    "u.total_nano_aiu", "u.api_endpoint", "u.created_at",
    # s.id is the join key only; s.summary and the turns/search_index tables
    # hold the prose and are never referenced.
    "s.id", "s.repository", "s.cwd",
])

# Named columns rather than SELECT *: the schema is undocumented and may gain
# columns, and a wildcard would start pulling in whatever GitHub adds next.
_QUERY = """
SELECT u.id, u.session_id, u.model, u.input_tokens, u.output_tokens,
       u.cache_read_tokens, u.cache_write_tokens, u.reasoning_tokens,
       u.total_nano_aiu, u.api_endpoint, u.created_at,
       s.repository, s.cwd
FROM assistant_usage_events u
LEFT JOIN sessions s ON s.id = u.session_id
"""

_SSH_REMOTE = re.compile(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?$")
_HTTP_REMOTE = re.compile(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$")


def copilot_home() -> str:
    override = os.environ.get("TKUS_COPILOT_HOME") or os.environ.get("COPILOT_HOME")
    return override or os.path.expanduser("~/.copilot")


def database_path() -> str:
    return os.path.join(copilot_home(), DB_NAME)


def parse_remote(url: str) -> Optional[str]:
    """Reduce a git remote URL to `owner/name`, which is what Copilot records."""
    if not url:
        return None
    url = url.strip()
    for pattern in (_SSH_REMOTE, _HTTP_REMOTE):
        m = pattern.match(url)
        if m:
            return m.group(2)
    return None


def repo_slug(repo_root: str) -> Optional[str]:
    """`owner/name` for this repo's origin remote, or None if it has no remote."""
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=repo_root,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_remote(out.stdout.decode("utf-8", "replace"))


def _is_within(child: str, parent: str) -> bool:
    return paths.is_within(child, parent)


def readonly_uri(path: str) -> str:
    """SQLite `file:` URI for a read-only open, valid on Windows and POSIX.

    A Windows path cannot be pasted into a URI as-is: backslashes are not
    separators there, and the drive letter needs a leading slash, so
    `C:\\Users\\dev\\.copilot\\session-store.db` has to become
    `file:///C:/Users/dev/.copilot/session-store.db`. Percent-encoding also
    matters because `?` and `#` would otherwise terminate the path, and a space
    in a user's home directory is entirely ordinary.
    """
    absolute = os.path.abspath(path).replace("\\", "/")
    quoted = quote(absolute, safe="/:")
    if not quoted.startswith("/"):
        quoted = "/" + quoted          # drive-letter paths: /C:/Users/...
    return "file://" + quoted + "?mode=ro"


def connect_readonly(path: str) -> sqlite3.Connection:
    """Open the database read-only.

    Copilot may be running against this file. A normal connection could take
    write locks or apply schema migrations to a live database that is not ours.
    """
    return sqlite3.connect(readonly_uri(path), uri=True, timeout=5.0)


class CopilotProvider(Provider):
    name = "copilot"

    def collect(self, repo_root, since, until):
        # type: (str, Optional[datetime], datetime) -> List[UsageRecord]
        path = database_path()
        if not os.path.isfile(path):
            # Not a Copilot user; the hook must stay silent rather than fail.
            return []

        slug = repo_slug(repo_root)
        slug_lower = slug.lower() if slug else None

        try:
            conn = connect_readonly(path)
        except sqlite3.Error:
            return []

        out = []  # type: List[UsageRecord]
        seen = set()
        try:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(_QUERY)
            except sqlite3.Error:
                # Older Copilot builds have no assistant_usage_events table.
                return []
            for row in rows:
                record = self._to_record(row, repo_root, slug_lower, since, until, seen)
                if record is not None:
                    out.append(record)
        finally:
            conn.close()
        return out

    def _to_record(self, row, repo_root, slug_lower, since, until, seen):
        row_id = row["id"]
        if row_id is None or row_id in seen:
            return None

        if not self._matches_repo(row, repo_root, slug_lower):
            return None

        when = parse_timestamp(row["created_at"] or "")
        if when is None:
            return None
        if since is not None and when <= since:
            return None
        if when > until:
            return None

        model = row["model"]
        if not model:
            return None

        cache_read = int(row["cache_read_tokens"] or 0)
        cache_write = int(row["cache_write_tokens"] or 0)
        # input_tokens is a total that already includes the cached tokens.
        # Subtract them so the shared pricing path cannot charge them twice;
        # clamped because a future schema change could break the invariant.
        uncached = max(0, int(row["input_tokens"] or 0) - cache_read - cache_write)

        nano = row["total_nano_aiu"]
        nano = int(nano) if nano is not None else None
        # No recorded cost means an endpoint GitHub does not bill -- a
        # self-hosted model reached through Copilot. Real compute, zero charge.
        unbilled = nano is None

        seen.add(row_id)
        return UsageRecord(
            provider=self.name,
            request_id=str(row_id),
            model=model,
            timestamp=when,
            input_tokens=uncached,
            output_tokens=int(row["output_tokens"] or 0),
            cache_write_1h=0,
            # The embedded rate card prices cache writes at 1.25x input, which
            # is the 5-minute tier; Copilot exposes no 1-hour equivalent.
            cache_write_5m=cache_write,
            cache_read=cache_read,
            reasoning_tokens=int(row["reasoning_tokens"] or 0),
            nano_aiu=nano,
            unbilled=unbilled,
        )

    @staticmethod
    def _matches_repo(row, repo_root, slug_lower):
        """Attribute a row to this repo.

        Prefers `repository` (`owner/name`), which is machine-independent --
        the recorded `cwd` may come from another machine or OS entirely
        (Windows drive letters, inconsistent case). `cwd` is only a fallback
        for repos with no origin remote.
        """
        repository = (row["repository"] or "").strip()
        if slug_lower and repository:
            return repository.lower() == slug_lower
        cwd = row["cwd"]
        if cwd and paths.looks_absolute(cwd):
            return _is_within(cwd, repo_root)
        return False


register(CopilotProvider())
