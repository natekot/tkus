"""Run the suite across processes: `python -m tests [-j N] [name ...]`.

Nearly all of the suite's time is spent waiting on process spawns -- every
real commit runs git, sh and two Python hooks -- so it parallelises almost
perfectly. Each test runs in a worker process and every test has its own
temporary directory, so they cannot see each other. Tests are dispatched one
at a time, because a handful take far longer than the rest and batching them
would leave one worker running long after the others are idle.

Plain `python -m unittest discover -s tests -t .` still runs it serially.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
import unittest
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Iterator, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _flatten(suite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _run_one(test_id: str) -> Tuple[str, str, List[Tuple[str, str]]]:
    """Run one test; returns (id, outcome, [(kind, report)])."""
    test = unittest.defaultTestLoader.loadTestsFromName(test_id)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(test)
    reports = ([("FAIL", text) for _, text in result.failures]
               + [("ERROR", text) for _, text in result.errors]
               + [("UNEXPECTED SUCCESS", "") for _ in result.unexpectedSuccesses])
    if reports:
        outcome = "E" if result.errors else "F"
    elif result.skipped:
        outcome = "s"
    else:
        outcome = "."
    return test_id, outcome, reports


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                        help="worker processes (default: one per CPU)")
    parser.add_argument("names", nargs="*",
                        help="only run tests whose id starts with one of these, "
                             "e.g. tests.test_log or tests.test_log.TestRebase")
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    suite = unittest.defaultTestLoader.discover("tests", top_level_dir=ROOT)
    ids = [t.id() for t in _flatten(suite)]
    if args.names:
        ids = [i for i in ids if any(i == n or i.startswith(n + ".")
                                     for n in args.names)]
    if not ids:
        print("no tests matched", file=sys.stderr)
        return 1

    started = time.perf_counter()
    failed = []
    skipped = 0
    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for future in as_completed([pool.submit(_run_one, i) for i in ids]):
            test_id, outcome, reports = future.result()
            sys.stderr.write(outcome)
            sys.stderr.flush()
            skipped += outcome == "s"
            failed.extend((test_id, kind, text) for kind, text in reports)
    elapsed = time.perf_counter() - started

    sys.stderr.write("\n")
    for test_id, kind, text in sorted(failed):
        sys.stderr.write("=" * 70 + "\n%s: %s\n" % (kind, test_id)
                         + "-" * 70 + "\n" + text + "\n")
    sys.stderr.write("-" * 70 + "\nRan %d tests in %.1fs (%d workers)\n\n"
                     % (len(ids), elapsed, max(1, args.jobs)))
    if failed:
        sys.stderr.write("FAILED (problems=%d, skipped=%d)\n" % (len(failed), skipped))
        return 1
    sys.stderr.write("OK (skipped=%d)\n" % skipped)
    return 0
