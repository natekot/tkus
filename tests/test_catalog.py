"""Reading the pricing catalog out of the installed Claude Code binary.

The catalog is an undocumented internal of another program, so the contract
these tests defend is not "it parses" but "it never lies": every failure mode
has to come back as None so the caller keeps using the bundled table, and a
machine with no Claude Code must not look like a pricing change.

The blobs here are synthetic. Committing a slice of a real 272MB binary would
pin this suite to one release of someone else's build, and the parser is what
is under test, not Anthropic's price list.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tkus import catalog
from tkus.pricing import RateTable

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TIERS = {
    "tier_a": (2, 8, 2.5, 4, 0.2),
    "tier_b": (10, 40, 12.5, 20, 1),
    "tier_c": (1, 5, 1.25, 2, 0.1),
}
MODELS = [
    ("claude-alpha-1", "tier_a", "claude-alpha-1-20250101"),
    ("claude-alpha-2", "tier_a", "claude-alpha-2-20250202"),
    ("claude-beta-1", "tier_b", "claude-beta-1-20250303"),
    ("claude-gamma-1", "tier_c", "claude-gamma-1-20250404"),
    ("claude-gamma-2", "tier_c", None),
    ("claude-delta-1", "tier_b", "claude-delta-1-20250505"),
]


def build_blob(tiers=None, models=None, pad=64):
    """A byte string shaped like the region of the binary around the catalog."""
    tiers = TIERS if tiers is None else tiers
    models = MODELS if models is None else models
    tier_text = ",".join(
        "%s:{input:%s,output:%s,cache_write_5m:%s,cache_write_1h:%s,"
        "cache_read:%s,web_search:0.01}" % ((name,) + tuple(values))
        for name, values in tiers.items())
    parts = []
    for model_id, tier, first_party in models:
        provider = ('provider_ids:{first_party:"%s",bedrock:"us.anthropic.%s-v1:0"},'
                    % (first_party, model_id)) if first_party else ""
        parts.append('{id:"%s",family:"x",display_name:"X",%s'
                     'max_output_tokens:{default:8192},pricing:"%s",capabilities:[]}'
                     % (model_id, provider, tier))
    body = ('schema_version:1,pricing_tiers:{%s},models:[%s]'
            % (tier_text, ",".join(parts)))
    return (b"\x00\xff" * pad) + body.encode() + (b"\x00\xff" * pad)


def write_blob(directory, name="fake-claude", **kw):
    path = os.path.join(directory, name)
    with open(path, "wb") as fh:
        fh.write(build_blob(**kw))
    return path


class CatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestExtract(CatalogTestCase):
    def test_parses_tiers_and_models(self):
        data = catalog.extract(write_blob(self.tmp))
        self.assertIsNotNone(data)
        self.assertEqual(len(data["tiers"]), 3)
        self.assertEqual(len(data["models"]), 6)
        self.assertEqual(data["tiers"]["tier_a"]["input"], 2.0)
        self.assertEqual(data["tiers"]["tier_a"]["cache_read"], 0.2)

    def test_rate_for_resolves_through_the_tier(self):
        data = catalog.extract(write_blob(self.tmp))
        self.assertEqual(catalog.rate_for(data, "claude-beta-1")["output"], 40.0)
        self.assertIsNone(catalog.rate_for(data, "claude-does-not-exist"))

    def test_aliases_skip_models_without_a_dated_id(self):
        data = catalog.extract(write_blob(self.tmp))
        aliases = catalog.aliases(data)
        self.assertEqual(aliases["claude-alpha-1-20250101"], "claude-alpha-1")
        self.assertNotIn("claude-gamma-2", aliases.values())

    def test_marker_split_across_a_read_boundary_is_still_found(self):
        """The scan reads in chunks; without an overlap the marker is missed."""
        original = catalog.CHUNK
        try:
            catalog.CHUNK = 8      # forces the marker to straddle chunks
            data = catalog.extract(write_blob(self.tmp, pad=13))
            self.assertIsNotNone(data)
            self.assertEqual(len(data["models"]), 6)
        finally:
            catalog.CHUNK = original

    def test_a_model_missing_its_tier_cannot_steal_the_next_one(self):
        """The body pattern is length-bounded so entries cannot run together."""
        blob = build_blob().replace(b'pricing:"tier_b"', b'unpriced:"x"', 1)
        path = os.path.join(self.tmp, "b")
        with open(path, "wb") as fh:
            fh.write(blob)
        data = catalog.extract(path)
        ids = [m["id"] for m in data["models"]]
        self.assertNotIn("claude-beta-1", ids)
        self.assertIn("claude-gamma-1", ids)


class TestExtractRefusesRatherThanGuessing(CatalogTestCase):
    """Every failure returns None so the bundled table stays in use."""

    def test_missing_file(self):
        self.assertIsNone(catalog.extract(os.path.join(self.tmp, "nope")))

    def test_file_without_the_marker(self):
        path = os.path.join(self.tmp, "plain")
        with open(path, "wb") as fh:
            fh.write(b"not a claude binary" * 100)
        self.assertIsNone(catalog.extract(path))

    def test_truncated_right_after_the_marker(self):
        path = os.path.join(self.tmp, "cut")
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 64 + b"pricing_tiers:{")
        self.assertIsNone(catalog.extract(path))

    def test_too_few_tiers_reads_as_a_format_change(self):
        data = catalog.extract(write_blob(self.tmp, tiers={"only": (1, 2, 3, 4, 5)}))
        self.assertIsNone(data)

    def test_too_few_models_reads_as_a_format_change(self):
        data = catalog.extract(write_blob(self.tmp, models=MODELS[:2]))
        self.assertIsNone(data)

    def test_a_directory_is_not_a_binary(self):
        self.assertIsNone(catalog.extract(self.tmp))


class TestMultiplierConflicts(CatalogTestCase):
    """rates.json stores cache pricing as multipliers on input. The catalog
    prices each tier explicitly, so it can prove that assumption still holds."""

    def test_conforming_tiers_report_nothing(self):
        data = catalog.extract(write_blob(self.tmp))
        self.assertEqual(catalog.multiplier_conflicts(data, RateTable.load()), [])

    def test_a_deviating_tier_is_reported(self):
        tiers = dict(TIERS, tier_a=(2, 8, 2.5, 4, 0.9))   # cache_read should be 0.2
        data = catalog.extract(write_blob(self.tmp, tiers=tiers))
        conflicts = catalog.multiplier_conflicts(data, RateTable.load())
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["field"], "cache_read")
        self.assertEqual(conflicts[0]["actual"], 0.9)
        self.assertEqual(conflicts[0]["expected"], 0.2)


class TestBinaryDiscovery(CatalogTestCase):
    def test_env_override_wins(self):
        path = write_blob(self.tmp)
        old = os.environ.get("TKUS_CLAUDE_BINARY")
        os.environ["TKUS_CLAUDE_BINARY"] = path
        try:
            self.assertEqual(catalog.find_binary(), path)
            self.assertIsNotNone(catalog.extract())
        finally:
            if old is None:
                del os.environ["TKUS_CLAUDE_BINARY"]
            else:
                os.environ["TKUS_CLAUDE_BINARY"] = old

    def test_version_is_read_from_the_install_path(self):
        self.assertEqual(
            catalog.version(os.path.join("a", "claude", "versions", "2.1.227")),
            "2.1.227")

    def test_version_falls_back_rather_than_raising(self):
        self.assertIsInstance(catalog.version(os.path.join("a", "b")), str)


class TestAgainstTheRealBinary(unittest.TestCase):
    """Guards against a Claude Code release that moves the catalog.

    Skips where Claude Code is not installed, so the suite still passes in CI.
    """

    def setUp(self):
        self.path = catalog.find_binary()
        if not self.path:
            self.skipTest("Claude Code is not installed here")

    def test_the_installed_catalog_still_parses(self):
        data = catalog.extract(self.path)
        self.assertIsNotNone(
            data, "Claude Code %s no longer exposes a readable catalog"
                  % catalog.version(self.path))
        self.assertGreaterEqual(len(data["models"]), catalog.MIN_MODELS)

    def test_every_bundled_model_agrees_with_the_catalog(self):
        """The bundled table must not drift from first-party pricing unnoticed.

        Excludes models inside a dated window: the catalog has no date
        dimension, so introductory pricing legitimately differs.
        """
        from datetime import datetime, timezone
        from tkus.__main__ import _rate_drift
        data = catalog.extract(self.path)
        if data is None:
            self.skipTest("catalog unreadable")
        drift = _rate_drift(RateTable.load(), data,
                            datetime.now(timezone.utc))
        self.assertEqual(drift["missing"], [], "models absent from rates.json")
        loose = [c for c in drift["changed"] if not c["dated"] and not c["pinned"]]
        self.assertEqual(loose, [], "unexplained price drift in rates.json")


class TestRatesCheckCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = dict(os.environ, PYTHONPATH=PROJECT)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_check(self, *args, **env):
        environ = dict(self.env)
        environ.update(env)
        out = subprocess.run(
            [sys.executable, "-m", "tkus", "rates", "--check"] + list(args),
            cwd=self.tmp, env=environ, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
        return out.returncode, out.stdout.decode()

    def test_absent_claude_code_is_not_drift(self):
        """Exit 0: a machine without Claude Code has nothing to disagree with."""
        code, text = self.run_check(
            TKUS_CLAUDE_BINARY=os.path.join(self.tmp, "nothing-here"))
        self.assertEqual(code, 0)
        self.assertIn("could not read", text)

    def test_reports_drift_and_exits_nonzero(self):
        path = write_blob(self.tmp)
        code, text = self.run_check(TKUS_CLAUDE_BINARY=path)
        self.assertEqual(code, 1)
        self.assertIn("claude-alpha-1", text)
        self.assertIn("absent from the bundled table", text)

    def test_writes_nothing(self):
        path = write_blob(self.tmp)
        before = sorted(os.listdir(self.tmp))
        self.run_check(TKUS_CLAUDE_BINARY=path)
        self.assertEqual(sorted(os.listdir(self.tmp)), before)

    def test_json_output_is_parseable(self):
        path = write_blob(self.tmp)
        code, text = self.run_check("--json", TKUS_CLAUDE_BINARY=path)
        self.assertEqual(code, 1)
        data = json.loads(text)
        self.assertTrue(data["available"])
        self.assertEqual(len(data["missing"]), 6)

    def test_json_output_when_unavailable(self):
        code, text = self.run_check(
            "--json", TKUS_CLAUDE_BINARY=os.path.join(self.tmp, "nope"))
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(text)["available"])


if __name__ == "__main__":
    unittest.main()


class TestRatesUpdate(unittest.TestCase):
    """`--update` writes list prices into the *global override*, so its job is
    as much about what it refuses to touch as what it writes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = os.path.join(self.tmp, "config")
        self.env = dict(os.environ, PYTHONPATH=PROJECT,
                        XDG_CONFIG_HOME=self.config)
        self.override = os.path.join(self.config, "tkus", "rates.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_update(self, *args, **kw):
        models = kw.pop("models", None)
        tiers = kw.pop("tiers", None)
        path = write_blob(self.tmp, models=models, tiers=tiers)
        env = dict(self.env, TKUS_CLAUDE_BINARY=path)
        out = subprocess.run(
            [sys.executable, "-m", "tkus", "rates", "--update"] + list(args),
            cwd=self.tmp, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
        return out.returncode, out.stdout.decode()

    def written(self):
        with open(self.override) as fh:
            return json.load(fh)

    def test_dry_run_writes_nothing(self):
        code, text = self.run_update()
        self.assertEqual(code, 0)
        self.assertIn("dry run", text)
        self.assertFalse(os.path.exists(self.override))

    def test_yes_writes_the_override(self):
        code, text = self.run_update("--yes")
        self.assertEqual(code, 0)
        data = self.written()
        self.assertEqual(data["models"]["claude-alpha-1"]["standard"][0]["input"], 2.0)
        self.assertEqual(data["aliases"]["claude-alpha-1-20250101"], "claude-alpha-1")

    def test_never_touches_the_bundled_table(self):
        bundled = os.path.join(PROJECT, "tkus", "rates.json")
        with open(bundled, "rb") as fh:
            before = fh.read()
        self.run_update("--yes")
        with open(bundled, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_a_locally_overridden_model_is_left_alone(self):
        """Almost certainly a negotiated rate; a list price must not undo it."""
        os.makedirs(os.path.dirname(self.override))
        with open(self.override, "w") as fh:
            json.dump({"models": {"claude-alpha-1": {"standard": [
                {"from": None, "until": None, "input": 0.5, "output": 1.0}]}}}, fh)
        code, text = self.run_update("--yes")
        self.assertIn("left alone: claude-alpha-1", text)
        self.assertIn("already overridden locally", text)
        self.assertEqual(
            self.written()["models"]["claude-alpha-1"]["standard"][0]["input"], 0.5)

    def test_a_dated_window_is_left_alone(self):
        """A window with dates says something the catalog cannot: it carries no
        date dimension, so it cannot tell a price change from a promotion."""
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        with open(os.path.join(repo, ".tkus.json"), "w") as fh:
            json.dump({"models": {"claude-alpha-1": {"standard": [
                {"from": None, "until": "2026-12-31", "input": 99.0,
                 "output": 99.0}]}}}, fh)
        path = write_blob(self.tmp)
        out = subprocess.run(
            [sys.executable, "-m", "tkus", "rates", "--update", "--yes"],
            cwd=repo, env=dict(self.env, TKUS_CLAUDE_BINARY=path),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        text = out.stdout.decode()
        self.assertIn("left alone: claude-alpha-1", text)
        self.assertIn("dated window", text)
        self.assertNotIn("claude-alpha-1", self.written().get("models", {}))

    def test_a_changed_undated_model_closes_its_window_rather_than_editing_it(self):
        """The reprice guarantee: history keeps the rates that applied to it."""
        models = MODELS + [("claude-opus-5", "tier_b", None)]   # 10/40 vs bundled 5/25
        code, text = self.run_update("--yes", models=models)
        windows = self.written()["models"]["claude-opus-5"]["standard"]
        self.assertEqual(len(windows), 2)
        old, new = windows[0], windows[1]
        self.assertEqual(old["input"], 5.0)
        self.assertIsNotNone(old["until"])       # closed, not rewritten
        self.assertEqual(new["input"], 10.0)
        self.assertIsNone(new["until"])

    def test_fast_pricing_survives_an_update(self):
        """The catalog has no speed dimension, so it must not clear one."""
        models = MODELS + [("claude-opus-5", "tier_b", None)]
        self.run_update("--yes", models=models)
        env = dict(self.env)
        out = subprocess.run([sys.executable, "-m", "tkus", "rates", "--json"],
                             cwd=self.tmp, env=env, stdout=subprocess.PIPE)
        rows = json.loads(out.stdout.decode())["models"]
        fast = [r for r in rows
                if r["model"] == "claude-opus-5" and r["speed"] == "fast"]
        self.assertEqual(len(fast), 1)
        self.assertEqual(fast[0]["input"], 10.0)

    def test_historical_prices_are_unchanged_after_an_update(self):
        models = MODELS + [("claude-opus-5", "tier_b", None)]
        self.run_update("--yes", models=models)
        out = subprocess.run(
            [sys.executable, "-m", "tkus", "rates", "--json", "--at", "2026-01-01"],
            cwd=self.tmp, env=dict(self.env), stdout=subprocess.PIPE)
        rows = json.loads(out.stdout.decode())["models"]
        opus = [r for r in rows
                if r["model"] == "claude-opus-5" and r["speed"] == "standard"][0]
        self.assertEqual(opus["input"], 5.0)

    def test_a_corrupt_override_is_never_overwritten(self):
        os.makedirs(os.path.dirname(self.override))
        with open(self.override, "w") as fh:
            fh.write("{not json")
        code, text = self.run_update("--yes")
        self.assertEqual(code, 1)
        self.assertIn("rates.json", text)
        self.assertNotIn("Traceback", text)
        with open(self.override) as fh:
            self.assertEqual(fh.read(), "{not json")

    def test_absent_claude_code_writes_nothing(self):
        out = subprocess.run(
            [sys.executable, "-m", "tkus", "rates", "--update", "--yes"],
            cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=dict(self.env, TKUS_CLAUDE_BINARY=os.path.join(self.tmp, "none")))
        self.assertEqual(out.returncode, 0)
        self.assertIn("could not read", out.stdout.decode())
        self.assertFalse(os.path.exists(self.override))


class TestPinnedRates(unittest.TestCase):
    """A pinned rate outranks the catalog.

    Claude Code's table is a convenience, not an authority: 2.1.227 still
    carries Sonnet 5's cancelled increase to 3/15. Where a rate has been checked
    against published pricing, `--check` must still report the disagreement --
    it is a real signal -- but `--update` must never write the catalog's value.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = os.path.join(self.tmp, "config")
        self.env = dict(os.environ, PYTHONPATH=PROJECT,
                        XDG_CONFIG_HOME=self.config)
        self.override = os.path.join(self.config, "tkus", "rates.json")
        self.blob = write_blob(
            self.tmp, models=MODELS + [("claude-sonnet-5", "tier_b", None)])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_rates(self, *args):
        out = subprocess.run(
            [sys.executable, "-m", "tkus", "rates"] + list(args),
            cwd=self.tmp, env=dict(self.env, TKUS_CLAUDE_BINARY=self.blob),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return out.returncode, out.stdout.decode()

    def test_check_reports_a_pinned_disagreement(self):
        code, text = self.run_rates("--check")
        self.assertEqual(code, 1)
        self.assertIn("[pinned]", text)
        self.assertIn("cancelled", text)

    def test_update_refuses_to_overwrite_a_pinned_rate(self):
        code, text = self.run_rates("--update", "--yes")
        self.assertIn("pinned to published pricing", text)
        # Other models are still written; only the pinned one is held back, so
        # the refusal has to be checked in the file rather than by its absence.
        with open(self.override) as fh:
            written = json.load(fh)
        self.assertNotIn("claude-sonnet-5", written.get("models", {}))
        self.assertIn("claude-alpha-1", written.get("models", {}))

    def test_sonnet_5_does_not_rise_on_the_cancelled_date(self):
        """The regression this pin exists to prevent: a 50% overcharge."""
        out = subprocess.run(
            [sys.executable, "-m", "tkus", "rates", "--json", "--at", "2026-09-01"],
            cwd=self.tmp, env=dict(os.environ, PYTHONPATH=PROJECT),
            stdout=subprocess.PIPE)
        rows = json.loads(out.stdout.decode())["models"]
        sonnet = [r for r in rows if r["model"] == "claude-sonnet-5"][0]
        self.assertEqual(sonnet["input"], 2.0)
        self.assertEqual(sonnet["output"], 10.0)

    def test_sonnet_5_is_priced_the_same_on_every_date(self):
        for date in ("2026-01-01", "2026-08-31", "2026-09-01", "2027-01-01"):
            out = subprocess.run(
                [sys.executable, "-m", "tkus", "rates", "--json", "--at", date],
                cwd=self.tmp, env=dict(os.environ, PYTHONPATH=PROJECT),
                stdout=subprocess.PIPE)
            rows = json.loads(out.stdout.decode())["models"]
            sonnet = [r for r in rows if r["model"] == "claude-sonnet-5"][0]
            self.assertEqual(sonnet["input"], 2.0, "wrong on %s" % date)
