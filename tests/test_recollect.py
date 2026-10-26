"""`tkus reprice --recollect` -- ledger entries re-read from the transcripts.

An entry is only rewritten when replaying the old collection reproduces it to
the token, and only from the worktree that does: worktrees' windows overlap,
so re-reading the wrong one would claim another branch's work.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tkus import recollect, repoledger
from tkus.pricing import RateTable

from . import fixtures
from .test_repoledger import RepoLedgerTestCase

T0 = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def stamp(minutes):
    when = T0 + timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def row(out, reqs=1, model="claude-opus-5", provider="claude-code", usd=0.0):
    return {"provider": provider, "model": model, "reqs": reqs, "out": out,
            "usd": usd}


def entry(*rows, since=stamp(0), until=stamp(60), **extra):
    out = {"at": until, "since": since, "until": until, "parent": "abc",
           "currency": "USD", "rates_version": "t",
           "usd": sum(r["usd"] for r in rows), "providers": list(rows)}
    out.update(extra)
    return out


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects)
        os.environ["TKUS_CLAUDE_PROJECTS"] = self.projects
        self.main = self.place("repo")
        self.table = RateTable.load(None)

    def tearDown(self):
        os.environ.pop("TKUS_CLAUDE_PROJECTS", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def place(self, name):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        return path

    def session(self, where, *lines, **kw):
        fixtures.write_project(self.projects, fixtures.encode(where), list(lines))

    def subagent(self, where, *lines):
        fixtures.write_subagent(self.projects, fixtures.encode(where), list(lines))

    def line(self, where, request_id, out, minutes=30):
        return fixtures.assistant_line(request_id, cwd=where, output_tokens=out,
                                       timestamp=stamp(minutes))

    def reread(self, e):
        return recollect.reread(e, recollect.Transcripts([self.main]), self.table)


class TestReread(Harness):
    def test_subagent_usage_added(self):
        self.session(self.main, self.line(self.main, "a", 1000))
        self.subagent(self.main, self.line(self.main, "b", 3000))
        new, why = self.reread(entry(row(1000, usd=0.025)))
        self.assertIsNone(why)
        [only] = new["providers"]
        self.assertEqual((only["reqs"], only["out"]), (2, 4000))
        # claude-opus-5 output at $25/MTok.
        self.assertAlmostEqual(new["usd"], 0.1)
        self.assertIs(new["recollected"], True)
        self.assertEqual((new["since"], new["until"]), (stamp(0), stamp(60)))

    def test_final_streaming_line_used(self):
        self.session(self.main, self.line(self.main, "a", 10),
                     self.line(self.main, "a", 900, minutes=31))
        new, _ = self.reread(entry(row(10)))
        self.assertEqual(new["providers"][0]["out"], 900)

    def test_complete_entry_unchanged(self):
        self.session(self.main, self.line(self.main, "a", 1000))
        self.assertEqual(self.reread(entry(row(1000))), (None, None))

    def test_pruned_transcripts_left_alone(self):
        """Fewer tokens on disk than recorded: re-reading would lower it."""
        self.session(self.main, self.line(self.main, "a", 1000))
        self.subagent(self.main, self.line(self.main, "b", 3000))
        new, why = self.reread(entry(row(1000), row(500, model="claude-sonnet-5")))
        self.assertIsNone(new)
        self.assertEqual(why, recollect.UNREPRODUCED)

    def test_already_reread(self):
        self.assertEqual(self.reread(entry(row(1), recollected=True)),
                         (None, recollect.ALREADY))

    def test_copilot_rows_kept(self):
        self.session(self.main, self.line(self.main, "a", 1000))
        self.subagent(self.main, self.line(self.main, "b", 1000))
        copilot = row(77, model="claude-sonnet-4.6", provider="copilot", usd=0.5)
        new, _ = self.reread(entry(row(1000, usd=0.025), copilot))
        self.assertIn(copilot, new["providers"])
        self.assertAlmostEqual(new["usd"], 0.05 + 0.5)


class TestWhichWorktree(Harness):
    def test_only_the_reproducing_worktree_is_read(self):
        """Two worktrees busy in the same hour: each entry gains only its own
        subagents. A deleted sibling worktree is still found by its sessions."""
        other = self.place("repo-wt")
        self.session(self.main, self.line(self.main, "a", 1000))
        self.session(other, self.line(other, "b", 2000))
        self.subagent(other, self.line(other, "c", 5000))
        new, _ = self.reread(entry(row(2000)))
        [only] = new["providers"]
        self.assertEqual(only["out"], 7000)
        self.assertEqual(self.reread(entry(row(1000))), (None, None))

    def test_disagreeing_places_are_ambiguous(self):
        """An entry with no Claude usage is reproduced by every place without
        any; if they would re-read it differently, nothing says which."""
        other = self.place("repo-wt")
        self.subagent(other, self.line(other, "c", 5000))
        copilot = row(77, provider="copilot", usd=0.5)
        self.assertEqual(self.reread(entry(copilot)), (None, recollect.AMBIGUOUS))


class TestCommand(RepoLedgerTestCase):
    def tkus(self, *args):
        out = subprocess.run([sys.executable, "-m", "tkus", "reprice"] + list(args),
                             cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        self.assertEqual(out.returncode, 0, out.stdout.decode())
        return out.stdout.decode()

    def add_subagent(self, at, output_tokens=3000):
        encoded = os.path.realpath(self.repo).replace(os.sep, "-")
        fixtures.write_subagent(self.projects, encoded, [fixtures.assistant_line(
            "sub", cwd=os.path.realpath(self.repo), timestamp=at,
            output_tokens=output_tokens)])

    def test_dry_run_then_write_survives_the_next_commit(self):
        self.inject_usage(1000)
        self.commit("one")
        [first] = self.entries()
        self.add_subagent(first["until"])

        self.assertIn("dry run", self.tkus("--recollect"))
        self.assertEqual(self.entries(), [first])

        self.assertIn("staged", self.tkus("--recollect", "--yes"))
        # The next commit rebuilds main's file from HEAD; the re-read entry
        # must survive that.
        self.inject_usage(500)
        self.commit("two")
        first_now, second = self.entries()
        self.assertEqual(first_now["providers"][0]["out"], 4000)
        self.assertTrue(first_now["recollected"])
        self.assertEqual(second["providers"][0]["out"], 500)
        self.assertFalse(os.path.exists(os.path.join(
            self.repo, ".git", "tkus", repoledger.REPLACEMENTS_FILE)))

        self.assertIn("already re-read", self.tkus("--recollect"))

    def test_fill_and_recollect_are_exclusive(self):
        out = subprocess.run([sys.executable, "-m", "tkus", "reprice", "--fill",
                              "--recollect"], cwd=self.repo, env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertNotEqual(out.returncode, 0)


if __name__ == "__main__":
    unittest.main()
