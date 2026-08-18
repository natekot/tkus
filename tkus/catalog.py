"""Read the model/pricing catalog out of the installed Claude Code binary.

Prices in `rates.json` are maintained by hand, so they rot silently -- which
matters when the deployed users are billed per token. There is no rate-card API
to fetch from: `GET /v1/models` returns capabilities and context sizes but no
pricing at all, and the community JSON that does carry prices lists only
Bedrock regional variants for the current models, where picking the wrong key
overstates by 10% invisibly.

Claude Code itself ships a versioned catalog whose pricing is a field-for-field
match to what `rates.json` stores:

    pricing_tiers:{tier_5_25:{input:5,output:25,cache_write_5m:6.25,
                              cache_write_1h:10,cache_read:0.5,web_search:0.01}}
    models:[{id:"claude-opus-5",...,pricing:"tier_5_25"}]

That is first-party, already on every deployed machine, and readable without a
network call -- so the tool keeps its promise of never reaching out.

This parses an **undocumented internal of another program**. It may change shape
in any Claude Code release, so nothing here raises: every failure returns None
and the caller falls back to the bundled table.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Dict, List, Optional

from . import paths

# The catalog is embedded in a ~270MB executable, so it is located by scanning
# for this marker rather than by parsing the container format.
MARKER = b"pricing_tiers:{"
CHUNK = 8 << 20
# Generous enough for the whole catalog (~20KB observed) without reading the
# rest of the binary into memory.
WINDOW = 60000

_TIER = re.compile(
    r"([a-z0-9_]+):\{input:([0-9.]+),output:([0-9.]+),"
    r"cache_write_5m:([0-9.]+),cache_write_1h:([0-9.]+),cache_read:([0-9.]+)")
# Entries are separated rather than matched across, because a single regex with
# a length bound will happily read past an entry that lacks `pricing` and take
# the *next* model's tier -- silently pricing a 15/75 model at 1/5. Splitting on
# the delimiter makes that structurally impossible.
_ENTRY = '{id:"'
_MODEL_ID = re.compile(r'(claude-[a-z0-9.\-]+)"')
_PRICING = re.compile(r'pricing:"([a-z0-9_]+)"')
_FIRST_PARTY = re.compile(r'first_party:"([^"]+)"')
# A guard against matching something far away inside the trailing fragment,
# which runs to the end of the window rather than to a delimiter.
ENTRY_LIMIT = 800
_VERSION_IN_PATH = re.compile(r"(\d+\.\d+\.\d+)")

# A catalog smaller than this is a sign the format moved and the regexes are
# matching debris rather than data.
MIN_TIERS = 3
MIN_MODELS = 5


def _candidate_paths() -> List[str]:
    """Where a Claude Code executable might live, most authoritative first."""
    override = os.environ.get("TKUS_CLAUDE_BINARY")
    if override:
        return [override]

    out = []  # type: List[str]
    for name in ("claude", "claude.exe"):
        found = _which(name)
        if found:
            out.append(found)

    home = os.path.expanduser("~")
    versions = os.path.join(home, ".local", "share", "claude", "versions")
    try:
        entries = sorted(os.listdir(versions), reverse=True)
    except OSError:
        entries = []
    out.extend(os.path.join(versions, name) for name in entries)

    for base in (os.path.join(home, ".npm-global", "lib", "node_modules"),
                 os.path.join(home, "AppData", "Roaming", "npm", "node_modules"),
                 "/usr/local/lib/node_modules", "/opt/homebrew/lib/node_modules"):
        out.append(os.path.join(base, "@anthropic-ai", "claude-code", "cli.js"))
    return out


def _which(name: str) -> Optional[str]:
    try:
        from shutil import which
    except ImportError:          # pragma: no cover - shutil.which is 3.3+
        return None
    found = which(name)
    if not found:
        return None
    # `claude` is normally a symlink into a versioned directory; the real file
    # is what has to be read.
    try:
        return os.path.realpath(found)
    except OSError:
        return found


def find_binary() -> Optional[str]:
    for path in _candidate_paths():
        try:
            if os.path.isfile(path):
                return path
        except OSError:
            continue
    return None


def version(path: Optional[str] = None) -> str:
    """Best-effort Claude Code version, for stamping onto a drift report.

    Read from the install path first: the versioned directory layout makes this
    free, where executing a 270MB binary to ask it is not.
    """
    if path:
        for part in reversed(os.path.normpath(path).split(os.sep)):
            match = _VERSION_IN_PATH.search(part)
            if match:
                return match.group(1)
    try:
        out = subprocess.run(["claude", "--version"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=15)
        match = _VERSION_IN_PATH.search(out.stdout.decode("utf-8", "replace"))
        if match:
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _read_window(path: str) -> Optional[bytes]:
    """The bytes around the catalog marker, without loading the whole file.

    Chunks overlap by the marker length so a marker split across a read
    boundary is still found.
    """
    overlap = len(MARKER) - 1
    try:
        with open(path, "rb") as handle:
            prev = b""
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    return None
                buf = prev + chunk
                index = buf.find(MARKER)
                if index != -1:
                    return (buf + handle.read(WINDOW))[index:index + WINDOW]
                prev = buf[-overlap:] if overlap else b""
    except (OSError, MemoryError):
        return None


def extract(path: Optional[str] = None) -> Optional[dict]:
    """Parse the catalog, or return None if it cannot be read or trusted."""
    path = path or find_binary()
    if not path:
        return None
    window = _read_window(path)
    if not window:
        return None
    text = window.decode("utf-8", "replace")

    tiers = {}  # type: Dict[str, Dict[str, float]]
    for match in _TIER.finditer(text):
        try:
            values = [float(v) for v in match.groups()[1:]]
        except ValueError:
            continue
        if any(v < 0 for v in values):
            continue
        tiers[match.group(1)] = {
            "input": values[0], "output": values[1],
            "cache_write_5m": values[2], "cache_write_1h": values[3],
            "cache_read": values[4],
        }

    models = []  # type: List[dict]
    seen = set()
    for fragment in text.split(_ENTRY)[1:]:
        name = _MODEL_ID.match(fragment)
        if not name:
            continue
        model_id = name.group(1)
        body = fragment[:ENTRY_LIMIT]
        priced = _PRICING.search(body)
        # No tier inside this entry's own bounds means the entry is not priced.
        # It is dropped, never given its neighbour's rate.
        if not priced or model_id in seen or priced.group(1) not in tiers:
            continue
        seen.add(model_id)
        alias = _FIRST_PARTY.search(body)
        models.append({
            "id": model_id,
            "tier": priced.group(1),
            "first_party": alias.group(1) if alias else None,
        })

    if len(tiers) < MIN_TIERS or len(models) < MIN_MODELS:
        return None
    return {"tiers": tiers, "models": models, "path": path,
            "version": version(path)}


def rate_for(catalog: dict, model_id: str) -> Optional[Dict[str, float]]:
    for entry in catalog["models"]:
        if entry["id"] == model_id:
            return catalog["tiers"].get(entry["tier"])
    return None


def aliases(catalog: dict) -> Dict[str, str]:
    """Dated provider IDs mapped to the canonical names tkus prices by."""
    out = {}
    for entry in catalog["models"]:
        dated = entry.get("first_party")
        if dated and dated != entry["id"]:
            out[dated] = entry["id"]
    return out


def multiplier_conflicts(catalog: dict, table) -> List[dict]:
    """Tiers whose cache prices are not the table's multipliers on input.

    `rates.json` stores cache pricing as three global multipliers, which is only
    valid while every model agrees. The catalog prices each tier explicitly, so
    it can prove that assumption -- or catch the release where it stops holding,
    which would otherwise mispriced every cached token silently.
    """
    out = []
    for name in ("cache_write_1h", "cache_write_5m", "cache_read"):
        factor = table.multiplier(name, {"cache_write_1h": 2.0,
                                         "cache_write_5m": 1.25,
                                         "cache_read": 0.1}[name])
        for tier, prices in sorted(catalog["tiers"].items()):
            expected = prices["input"] * factor
            actual = prices[name]
            if abs(expected - actual) > 1e-9:
                out.append({"tier": tier, "field": name,
                            "expected": expected, "actual": actual})
    return out
