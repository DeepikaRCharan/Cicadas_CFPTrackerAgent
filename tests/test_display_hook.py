"""The display hook on Trace (the one exception to the 7 Oct freeze) is
display-only: with no listener attached, discovery behaves exactly as it did
before the hook existed; with one attached -- even a failing one -- nothing it
records or decides changes. No API calls.

The golden file was recorded from commit 3eda423 (before the hook) by running
tests/discovery_scenario.py there.

Run with: python -m unittest tests/test_display_hook.py -v
"""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.discovery_scenario import run_scenario

GOLDEN = json.loads((Path(__file__).resolve().parent / "fixtures" / "discovery_golden.json").read_text(encoding="utf-8"))


def normalized(output: dict) -> dict:
    """The same JSON round trip the golden file went through."""
    return json.loads(json.dumps(output, ensure_ascii=False))


class TestNoListener(unittest.TestCase):
    def test_identical_to_the_pre_hook_golden(self):
        output = normalized(run_scenario())
        for key in ("markdown", "terminal", "tool_calls", "stdout", "result", "requests"):
            self.assertEqual(output[key], GOLDEN[key], key)

    def test_golden_covers_every_trace_path(self):
        md = GOLDEN["markdown"]
        for snippet in ("Reason stated before calls #1–#4", "(arrived with the next response)",
                        "does not appear on", "not evaluated again", "- code: accepted as 'eci-2027'",
                        "## Phase 2: main searches", "## Candidates"):
            self.assertIn(snippet, md)


class TestWithListener(unittest.TestCase):
    def test_listener_changes_nothing(self):
        events = []
        output = normalized(run_scenario(lambda event, data: events.append((event, data))))
        self.assertEqual(output, GOLDEN)
        kinds = {e for e, _ in events}
        self.assertEqual(kinds, {"heading", "lines", "reason_group", "server_call", "late_result", "decision"})

    def test_events_carry_what_the_trace_records(self):
        events = []
        run_scenario(lambda event, data: events.append((event, data)))
        late = next(d for e, d in events if e == "late_result")
        self.assertEqual((late["number"], late["tool"]), (5, "web_fetch"))
        self.assertTrue(late["summary"].startswith("readable,"))
        accepted = [d for e, d in events if e == "decision" and d["tool"] == "accept_candidate" and not d["refused"]]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["reason"], '"Accepting ECI 2027."')
        group = next(d for e, d in events if e == "reason_group")
        self.assertEqual((group["first"], group["last"]), (1, 4))

    def test_failing_listener_is_detached_and_changes_nothing(self):
        calls = []

        def broken(event, data):
            calls.append(event)
            raise RuntimeError("terminal gone")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            output = normalized(run_scenario(broken))
        self.assertEqual(output, GOLDEN)
        self.assertEqual(len(calls), 1)  # detached after the first failure
        self.assertIn("display listener failed (RuntimeError: terminal gone) -- detached", err.getvalue())


if __name__ == "__main__":
    unittest.main()
