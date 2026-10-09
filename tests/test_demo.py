"""Tests for src/demo.py -- the display-only demo layer. No API calls.

Run with: python -m unittest tests/test_demo.py -v
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console

from src import demo
from src.demo import DemoSession, Row, parse_trace
from src.tracker import record_paper_classification, save_tracker
from src.tracker_schema import TrackerFile, VenueTier
from tests import render_fixtures as fx
from tests.discovery_scenario import run_scenario

GOLDEN = json.loads((Path(__file__).resolve().parent / "fixtures" / "discovery_golden.json").read_text(encoding="utf-8"))


def comparable(events):
    """Events the renderer draws, in a form both sources share."""
    out = []
    for kind, d in events:
        if kind == "server_call":
            out.append(("call", d["number"], d["tool"], d["reason"]))
        elif kind == "late_result":
            out.append(("late", d["number"], d["summary"]))
        elif kind == "reason_group":
            out.append(("group", d["first"], d["last"], d["reason"]))
        elif kind == "decision":
            out.append(("decision", d["number"], d["tool"], d["refused"], d["reason"], d["reason_in_call"], d["reply"]))
    return sorted(out, key=lambda e: (e[1], e[0]))


class TestReplayMatchesLive(unittest.TestCase):
    def test_parsed_trace_gives_the_live_hooks_events(self):
        live = []
        run_scenario(lambda kind, data: live.append((kind, data)))
        text = GOLDEN["markdown"].replace("# scenario", "# Discovery trace: immune-tolerance, 2026-10-01 00:00 UTC", 1)
        replayed = parse_trace(text)
        self.assertEqual(comparable(replayed.events), comparable(live))
        self.assertEqual(replayed.profile_id, "immune-tolerance")


class RendererTestCase(unittest.TestCase):
    def setUp(self):
        fx.build()
        self.tracker = fx.build.tracker
        self.out = io.StringIO()
        self.session = DemoSession(self.tracker, Console(file=self.out, width=80, highlight=False, color_system=None))
        self._dir = tempfile.TemporaryDirectory()
        self.md_path = Path(self._dir.name) / "runs" / "demo.md"

    def tearDown(self):
        self._dir.cleanup()

    def paper(self, profile_id, handoffs, events=()):
        self.session.start_paper(1, 2, profile_id, "A paper", self.tracker.profiles[profile_id])
        for kind, data in events:
            self.session.listener(kind, data)
        self.session.listener("heading", {"text": "Hand-off: extract, classify, judge fit, merge"})
        for cid, outcome in handoffs:
            self.session.handoff(cid, profile_id, outcome, cid)
        self.session.paper_done()


class TestRenderer(RendererTestCase):
    def test_four_sections_in_order(self):
        self.paper(fx.IMMUNE, [("eci-2027", "updated")])
        text = self.out.getvalue()
        positions = [text.index(t) for t in ('1. Reading the paper (agent judgment)', '2. Searching (agent-directed)', '3. Verifying (code-checked)', '4. Scoring (agent judgment)')]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Read as:", text)
        self.assertIn("Theme 96:", text)

    def test_results_sorted_by_overall_and_threshold_strictly_above_50(self):
        self.paper(fx.IMMUNE, [("eci-2027", "updated"), ("kyoto-treg-biology-symposium-2027", "updated")])
        self.paper(fx.ELEPHANT, [("africa-wildlife-corridors-policy-forum-2027", "updated")])
        self.session.finish(0.123, self.md_path)
        overalls = [r.overall for r in sorted(self.session.rows, key=lambda r: -(r.overall or 0))]
        md = self.md_path.read_text(encoding="utf-8")
        table = [l for l in md.splitlines() if l.startswith("| ") and "Venue" not in l]
        self.assertEqual([int(l.split(" | ")[-2]) for l in table], overalls)  # sorted, highest first
        self.assertTrue(all(("**YES**" in l) == (int(l.split(" | ")[-2]) > 50) for l in table))
        self.assertIn("about $0.12", md)

    def test_threshold_boundary(self):
        base = dict(venue="v", location="l", paper="p", bucket="b", deadline="d")
        self.assertFalse(Row(overall=50, **base).clears)
        self.assertTrue(Row(overall=51, **base).clears)
        self.assertFalse(Row(**base).clears)

    def test_unscored_venue_listed_with_why_and_dashes(self):
        self.paper(fx.IMMUNE, [("eci-2027", "updated")])
        self.tracker.conferences[0].matches[fx.IMMUNE].fit_by_tier = {}
        self.session.rows.clear()
        self.session._handed = [("eci-2027", fx.IMMUNE, "updated")]
        self.session.paper_done()
        self.session.finish(None, self.md_path)
        self.assertIn("| - | - | - | - | no |", self.md_path.read_text(encoding="utf-8"))

    def test_verifying_reasons(self):
        decisions = [
            ("decision", dict(number=4, tool="accept_candidate", args={"name": "X", "call_page_url": "https://x.org"},
                              reason=None, reason_in_call="r", reply="not accepted: deadline_quote 'a' does not appear on https://x.org",
                              refused=True)),
            ("decision", dict(number=5, tool="record_result", args={"url": "https://y.org", "verdict": "stale"},
                              reason=None, reason_in_call="old", reply="noted", refused=False)),
        ]
        self.paper(fx.IMMUNE, [], decisions)
        text = self.out.getvalue()
        self.assertIn("REFUSED", text)
        self.assertIn("quote mismatch", text)
        self.assertIn("stale (past", text)

    def test_large_font_width_never_overflows(self):
        self.paper(fx.IMMUNE, [("eci-2027", "updated")])
        self.session.finish(None, None)
        self.assertTrue(all(len(line.rstrip()) <= 80 for line in self.out.getvalue().splitlines()))


class TestRunDemoFlag(unittest.TestCase):
    def test_demo_flag_streams_sections_and_writes_the_backup(self):
        from src.run import main as run_main

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tracker = TrackerFile()
            profiles = fx.load_profiles()
            for pid, profile in profiles.items():
                record_paper_classification(tracker, pid, fx._classification(profile, VenueTier.ACADEMIC_PRACTITIONER,
                                                                             ["cue a", "cue b", "cue c"], "reasoning"))
            save_tracker(tracker, root / "tracker.json")
            (root / "decisions.json").write_text("{}", encoding="utf-8")
            out = io.StringIO()
            with mock.patch("src.run.discover", return_value=NS(candidates=[], fetched_text={}, judged=[])), \
                    mock.patch("src.run.LOGS_DIR", str(root / "logs")), mock.patch.object(demo, "RUNS_DIR", str(root / "runs")), \
                    fx.fixture_profiles_live(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = run_main(["--demo", "--tracker", str(root / "tracker.json"), "--decisions", str(root / "decisions.json"),
                                 "--traces-dir", str(root / "traces")])
            self.assertEqual(code, 0)
            shown = out.getvalue()
            for title in ("Paper 1 of 2", "Paper 2 of 2", '1. Reading the paper (agent judgment)', '2. Searching (agent-directed)', '3. Verifying (code-checked)', '4. Scoring (agent judgment)',
                          "Results", "cue a"):
                self.assertIn(title, shown)
            self.assertNotIn("[run] trace written", shown)  # the pipeline's own output went to the log
            self.assertTrue(any((root / "logs").glob("*_demo_stdout.log")))
            self.assertIn("[run] trace written", next((root / "logs").glob("*_demo_stdout.log")).read_text(encoding="utf-8"))
            self.assertEqual(len(list((root / "runs").glob("*_demo.md"))), 1)
            self.assertEqual(len(list((root / "traces").glob("*.md"))), 2)  # traces written exactly as without --demo

    def test_warnings_go_to_run_problems_at_the_end_and_in_full_to_the_log(self):
        # rehearsal 2026-10-09_052551: [tracker]/[run] warnings landed mid-output on stderr
        from src.run import main as run_main

        def fake_discover(profile, *args, **kwargs):
            if profile.id == fx.ELEPHANT:
                raise RuntimeError("529 overloaded\nsecond line of detail")
            return NS(candidates=[], fetched_text={}, judged=[])

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tracker = TrackerFile()
            for pid, profile in fx.load_profiles().items():
                record_paper_classification(tracker, pid, fx._classification(profile, VenueTier.ACADEMIC_PRACTITIONER,
                                                                             ["cue a", "cue b", "cue c"], "reasoning"))
            save_tracker(tracker, root / "tracker.json")
            (root / "decisions.json").write_text("{}", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("src.run.discover", side_effect=fake_discover), \
                    mock.patch("src.run.LOGS_DIR", str(root / "logs")), mock.patch.object(demo, "RUNS_DIR", str(root / "runs")), \
                    fx.fixture_profiles_live(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = run_main(["--demo", "--tracker", str(root / "tracker.json"), "--decisions", str(root / "decisions.json"),
                                 "--traces-dir", str(root / "traces")])
            self.assertEqual(code, 3)  # partial
            self.assertEqual(err.getvalue(), "")  # nothing reached the terminal's stderr
            shown = out.getvalue()
            self.assertGreater(shown.index("Run problems"), shown.index("TOTALS:"))  # at the end
            problems = shown[shown.index("Run problems"):]
            self.assertIn("[tracker] The whole search for elephant-corridors failed", problems)
            self.assertIn("[run] PARTIAL: 1 failure(s)", problems)
            self.assertNotIn("WARNING:", problems)
            self.assertNotIn("Traceback", shown)
            self.assertEqual(shown.count("Running total:"), 4)  # two checkpoints per paper, no strays
            log = next((root / "logs").glob("*_demo_stdout.log")).read_text(encoding="utf-8")
            self.assertIn("[tracker] WARNING: The whole search for elephant-corridors failed", log)
            self.assertIn("Traceback (most recent call last)", log)  # logged in full
            self.assertIn("[run] PARTIAL", log)
            md = next((root / "runs").glob("*_demo.md")).read_text(encoding="utf-8")
            self.assertIn("## Run problems", md)
            self.assertIn("RuntimeError: 529 overloaded second line of detail", md)  # continuation joined, one line

    def test_group_problems_joins_continuations_and_skips_tracebacks(self):
        lines = ["[tracker] WARNING: hand-off of CSEDU 2027 (2027) failed: ValidationError: 1 validation error",
                 "  Value error, missing_fields ['fees'] must exactly equal ['fees', 'submission_opens']",
                 "Traceback (most recent call last):", '  File "x.py", line 1', "RuntimeError: boom",
                 "[run] PARTIAL: 1 failure(s)"]
        self.assertEqual(demo.group_problems(lines), [
            ("tracker", "hand-off of CSEDU 2027 (2027) failed: ValidationError: 1 validation error Value error, "
                        "missing_fields ['fees'] must exactly equal ['fees', 'submission_opens']"),
            ("run", "PARTIAL: 1 failure(s)")])

    def test_clean_run_has_no_run_problems_section(self):
        out = io.StringIO()
        session = demo.DemoSession(TrackerFile(), Console(file=out, width=100, highlight=False))
        session.finish(None, None, problems=[])
        self.assertNotIn("Run problems", out.getvalue())


class TestRunningCounter(RendererTestCase):
    def test_steps_count_every_tool_call_and_usage_comes_from_the_meter(self):
        live = []
        run_scenario(lambda kind, data: live.append((kind, data)))
        calls = sum(1 for kind, _ in live if kind in ("server_call", "decision"))
        self.session.usage_fn = lambda: demo.Usage(7, 123_456, 0.0789)
        self.paper(fx.IMMUNE, [("eci-2027", "updated")], live)
        self.assertEqual(self.session.steps, calls)
        self.assertIn(f"Running total: {calls} steps | 7 model calls | 123,456 tokens | $0.079 spent", self.out.getvalue())

    def test_checkpoints_after_searching_and_scoring_and_a_totals_line(self):
        self.session.usage_fn = lambda: demo.Usage(3, 1_000, 0.01)
        self.paper(fx.IMMUNE, [("eci-2027", "updated")])
        self.session.finish(None, self.md_path)
        text = self.out.getvalue()
        first, second = [i for i in range(len(text)) if text.startswith("Running total:", i)][:2]
        self.assertLess(first, text.index("3. Verifying (code-checked)"))
        self.assertGreater(second, text.index("4. Scoring (agent judgment)"))
        self.assertIn("TOTALS: 0 steps, 3 model calls, 1,000 tokens, $0.010 spent", text)
        md = self.md_path.read_text(encoding="utf-8")
        self.assertIn("_Running total: 0 steps | 3 model calls | 1,000 tokens | $0.010 spent_", md)
        self.assertIn("**TOTALS: 0 steps, 3 model calls, 1,000 tokens, $0.010 spent", md)

    def test_no_footer_on_a_file_or_pipe(self):
        self.paper(fx.IMMUNE, [("eci-2027", "updated")])
        self.assertIsNone(self.session._live)  # StringIO is not a terminal; the checkpoints cover it

    def test_replay_reads_per_paper_usage_from_the_trace(self):
        trace = (Path(__file__).resolve().parent.parent / "traces" / "20261007T054110Z_elephant-corridors.md").read_text(encoding="utf-8")
        usage = parse_trace(trace).usage
        # haiku 10 calls 6,500+53,939+188,310+3,340 tokens $0.159; sonnet 4 calls 12,447+1,840 tokens $0.043
        self.assertEqual((usage.model_calls, usage.tokens), (14, 6_500 + 53_939 + 188_310 + 3_340 + 12_447 + 1_840))
        self.assertAlmostEqual(usage.cost, 0.202, places=6)

    def test_llm_usage_reads_the_runs_meter(self):
        from src import llm
        with mock.patch.dict(llm.USAGE, {"claude-haiku-4-5-20251001": dict(
                calls=2, input_tokens=1000, output_tokens=100, cache_write_tokens=0, cache_read_tokens=0, searches=1, fetches=0)},
                clear=True):
            u = demo.llm_usage()
        self.assertEqual((u.model_calls, u.tokens), (2, 1100))
        self.assertAlmostEqual(u.cost, 1000 / 1e6 * 1.0 + 100 / 1e6 * 5.0 + 0.01)


if __name__ == "__main__":
    unittest.main()
