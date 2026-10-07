"""Tests for src/run.py -- build step 8: hand-off and re-check. Extract,
classify and judge-fit are replaced with fakes; no API calls.

Run with: python -m unittest tests/test_run.py -v
"""

import contextlib
import datetime
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.discover import Candidate, Trace
from src.run import EXIT_CRASHED, EXIT_OK, EXIT_PARTIAL, Run, publish_trace, summary_line, write_problems
from src.run import main as run_main
from src.tracker import load_tracker, record_paper_classification, save_tracker, upsert_venue
from src.tracker_schema import ExtractionFacts, TrackerFile, VenueTier
from tests import render_fixtures as fx

ECI_URL = "https://eci2027.org/call-for-abstracts/"
READABLE_PAGE = "Call for abstracts. Submission deadline: 31 March 2027. " * 10  # over the 300-character rule
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SPRINGER_URL = "https://link.springer.com/journal/10980/collections"
SPRINGER_WALL = (FIXTURES / "springer_js_wall.txt").read_text(encoding="utf-8")
SPRINGER_LOOP = (FIXTURES / "springer_loop_fetch.txt").read_text(encoding="utf-8").split("\n", 4)[4]  # minus log header


def facts(**overrides):
    data = fx.eci_extraction().model_dump(exclude={"venue_tier", "venue_tier_reasoning"})
    data.update(overrides)
    return ExtractionFacts(**data)


INCOMPLETE = dict(deadline=None, deadline_precision=None, deadline_as_stated=None, complete=False,
                  missing_fields=["deadline", "format_requirements", "fees", "mode"])


class Fakes:
    def __init__(self, profile, extraction_by_url):
        self.calls = []
        self.extraction_by_url = extraction_by_url
        self.profile = profile
        self.extract = NS(fetch_text=self.fetch_text, extract_facts_from_text=self.extract_facts)
        self.classify = NS(classify_venue=self.classify_venue)
        self.judge_fit = NS(judge_fit_for_venue=self.judge)

    pages: dict = {}  # url -> text the direct fetch returns (default: a normal readable page)

    def fetch_text(self, urls):
        self.calls.append(("fetch", tuple(urls)))
        if any("broken" in u for u in urls):
            raise RuntimeError("failed to fetch: HTTP 403")
        if len(urls) == 1 and urls[0] in self.pages:
            return self.pages[urls[0]]
        return READABLE_PAGE

    def extract_facts(self, url, page_text, today):
        self.calls.append(("extract", url))
        return self.extraction_by_url[url]

    def classify_venue(self, url, page_text):
        self.calls.append(("classify", url))
        return NS(venue_tier=[VenueTier.ACADEMIC_PRACTITIONER], venue_tier_reasoning="research congress")

    def judge(self, profile, extraction, page_text, url, venue_tier, today):
        self.calls.append(("judge", url))
        return fx._fit(profile, (90, "on topic"), (80, "research"), (70, "abstract"))


class RunTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "tracker.json"
        self.profiles = {fx.IMMUNE: fx.load_profiles()[fx.IMMUNE]}
        self.profile = self.profiles[fx.IMMUNE]
        self.tracker = TrackerFile()
        record_paper_classification(self.tracker, fx.IMMUNE, fx._classification(
            self.profile, VenueTier.ACADEMIC_PRACTITIONER, ["a", "b", "c"], "r"))
        self.trace = Trace("t")

    def tearDown(self):
        self._dir.cleanup()

    def make_run(self, extraction_by_url):
        run = Run.__new__(Run)
        fakes = Fakes(self.profile, extraction_by_url)
        run.extract, run.classify, run.judge_fit = fakes.extract, fakes.classify, fakes.judge_fit
        run.tracker, run.tracker_path, run.profiles = self.tracker, self.path, self.profiles
        run.decisions, run.now, run.today = {}, fx.RUN_1, fx.RUN_1.date()
        run.live, run.rechecked, run.failures, run.paper_failures = set(self.profiles), set(), [], []
        return run, fakes

    def quiet(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)


class TestHandOff(RunTestCase):
    def test_new_candidate_goes_through_pipeline_with_explicit_year(self):
        run, fakes = self.make_run({ECI_URL: facts()})
        candidate = Candidate("ECI 2027", 2027, ECI_URL, None, "on_call_page", "live")
        self.quiet(run.hand_off, self.profile, candidate, self.trace)
        self.assertEqual([c[0] for c in fakes.calls], ["fetch", "extract", "classify", "judge"])
        saved = load_tracker(self.path)
        self.assertEqual([c.id for c in saved.conferences], ["eci-2027"])
        self.assertEqual(saved.conferences[0].matches[fx.IMMUNE].fit_by_tier[VenueTier.ACADEMIC_PRACTITIONER].overall, 80)
        self.assertIn("outcome: **created** `eci-2027`", "\n".join(self.trace.lines))

    def test_dates_page_fetched_with_call_page_and_remembered(self):
        run, fakes = self.make_run({ECI_URL: facts()})
        candidate = Candidate("ECI 2027", 2027, ECI_URL, "https://eci2027.org/dates", "on_dates_page", "live")
        self.quiet(run.hand_off, self.profile, candidate, self.trace)
        self.assertIn(("fetch", (ECI_URL,)), fakes.calls)  # each page fetched (and checked) on its own
        self.assertIn(("fetch", ("https://eci2027.org/dates",)), fakes.calls)
        self.assertEqual(load_tracker(self.path).conferences[0].dates_url, "https://eci2027.org/dates")

    def test_incomplete_candidate_tracked_but_not_judged(self):
        run, fakes = self.make_run({ECI_URL: facts(**INCOMPLETE)})
        self.quiet(run.hand_off, self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "not_found", "r"), self.trace)
        self.assertNotIn("judge", [c[0] for c in fakes.calls])
        entry = load_tracker(self.path).conferences[0]
        self.assertFalse(entry.extraction.complete)
        self.assertEqual(entry.matches[fx.IMMUNE].fit_by_tier, {})

    def test_conflict_flagged_without_model_calls(self):
        run, fakes = self.make_run({ECI_URL: facts()})
        self.quiet(run.hand_off, self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "on_call_page", "r"), self.trace)
        fakes.calls.clear()
        other = Candidate("ECI 2027", 2027, "https://mirror.example/eci2027", None, "on_call_page", "r", conflict=True)
        self.quiet(run.hand_off, self.profile, other, self.trace)
        self.assertEqual(fakes.calls, [])
        self.assertEqual(len(load_tracker(self.path).identity_conflicts), 1)

    def test_not_found_deadline_read_as_rolling_is_not_tracked(self):
        rolling = facts(has_deadline=False, deadline=None, deadline_precision=None, deadline_as_stated=None,
                        missing_fields=["format_requirements", "fees", "mode"])
        run, fakes = self.make_run({ECI_URL: rolling})
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            run.hand_off(self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "not_found", "r"), self.trace)
        self.assertNotIn("judge", [c[0] for c in fakes.calls])
        self.assertFalse(self.path.exists())  # nothing merged, nothing saved
        self.assertIn("needs a look", "\n".join(self.trace.lines))
        self.assertIn("WARNING", stderr.getvalue())
        self.assertEqual(len(run.failures), 1)

    def test_genuinely_rolling_candidate_is_tracked(self):
        rolling = facts(has_deadline=False, deadline=None, deadline_precision=None, deadline_as_stated=None,
                        missing_fields=["format_requirements", "fees", "mode"])
        run, fakes = self.make_run({ECI_URL: rolling})
        self.quiet(run.hand_off, self.profile, Candidate("Pitches", None, ECI_URL, None, "rolling", "r"), self.trace)
        self.assertEqual([c.id for c in load_tracker(self.path).conferences], ["pitches"])

    def test_blocked_direct_fetch_falls_back_to_loop_copy(self):
        url = "https://broken.example/special-issue"
        run, fakes = self.make_run({url: facts()})
        self.quiet(run.hand_off, self.profile, Candidate("Special Issue 2026", 2026, url, None, "on_call_page", "r"),
                   self.trace, {url.replace("https://", "").lower(): "loop copy " * 50})
        self.assertEqual([c[0] for c in fakes.calls], ["fetch", "extract", "classify", "judge"])
        self.assertEqual(run.failures, [])
        self.assertIn("using the discovery loop's readable copy", "\n".join(self.trace.lines))

    def test_blocked_direct_fetch_without_loop_copy_fails_visibly(self):
        url = "https://broken.example/special-issue"
        run, fakes = self.make_run({url: facts()})
        self.quiet(run.hand_off, self.profile, Candidate("Special Issue 2026", 2026, url, None, "on_call_page", "r"),
                   self.trace, {})
        self.assertEqual(len(run.failures), 1)

    def test_one_failure_is_visible_and_run_continues(self):
        run, fakes = self.make_run({ECI_URL: facts()})
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            run.hand_off(self.profile, Candidate("Broken 2027", 2027, "https://broken.example/cfp", None, "on_call_page", "r"), self.trace)
            run.hand_off(self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "on_call_page", "r"), self.trace)
        self.assertEqual(len(run.failures), 1)
        self.assertIn("WARNING", stderr.getvalue())
        self.assertIn("**FAILED**", "\n".join(self.trace.lines))
        self.assertEqual([c.id for c in load_tracker(self.path).conferences], ["eci-2027"])


class TestRecheck(RunTestCase):
    def seed_incomplete(self):
        with contextlib.redirect_stdout(io.StringIO()):
            ext = fx.eci_extraction().model_copy(update=INCOMPLETE)
            upsert_venue(self.tracker, name="ECI 2027", year=2027, url=ECI_URL, extraction=ext,
                         profile_id=fx.IMMUNE, live_profile_ids={fx.IMMUNE}, now=fx.RUN_1)

    def test_incomplete_now_complete_is_scored_same_run(self):
        self.seed_incomplete()
        run, fakes = self.make_run({ECI_URL: facts()})
        self.quiet(run.recheck, self.profile, self.trace, set())
        self.assertEqual([c[0] for c in fakes.calls], ["fetch", "extract", "judge"])  # no re-classify
        match = load_tracker(self.path).conferences[0].matches[fx.IMMUNE]
        self.assertEqual(match.fit_by_tier[VenueTier.ACADEMIC_PRACTITIONER].overall, 80)
        self.assertIn("now complete", "\n".join(self.trace.lines))

    def test_unchanged_complete_venue_not_rejudged(self):
        run, fakes = self.make_run({ECI_URL: facts()})
        self.quiet(run.hand_off, self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "on_call_page", "r"), self.trace)
        fakes.calls.clear()
        run.rechecked.clear()
        self.quiet(run.recheck, self.profile, self.trace, set())
        self.assertEqual([c[0] for c in fakes.calls], ["fetch", "extract"])

    def test_closed_match_not_rechecked(self):
        self.seed_incomplete()
        self.tracker.conferences[0].matches[fx.IMMUNE].decision = "skipped"
        run, fakes = self.make_run({ECI_URL: facts()})
        self.quiet(run.recheck, self.profile, self.trace, set())
        self.assertEqual(fakes.calls, [])


class TestExitCodes(RunTestCase):
    def test_clean_run_exits_0(self):
        run, _ = self.make_run({ECI_URL: facts()})
        self.quiet(run.hand_off, self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "on_call_page", "r"), self.trace)
        self.assertEqual(run.exit_code(), EXIT_OK)

    def test_candidate_failure_is_partial(self):
        run, _ = self.make_run({ECI_URL: facts()})
        self.quiet(run.hand_off, self.profile, Candidate("Broken 2027", 2027, "https://broken.example/cfp", None, "on_call_page", "r"), self.trace)
        self.assertEqual(run.exit_code(), EXIT_PARTIAL)

    def test_whole_paper_failure_is_partial_and_listed_first(self):
        run, _ = self.make_run({})
        self.quiet(run.hand_off, self.profile, Candidate("Broken 2027", 2027, "https://broken.example/cfp", None, "on_call_page", "r"), self.trace)
        self.quiet(run.fail_paper, self.trace, "elephant-corridors", RuntimeError("529 overloaded"))
        self.assertEqual(run.exit_code(), EXIT_PARTIAL)
        problems = run.problems()
        self.assertEqual(len(problems), 2)
        self.assertTrue(problems[0].startswith("The whole search for elephant-corridors failed, so this digest "
                                               "has nothing new for that paper: RuntimeError: 529 overloaded"))
        self.assertIn("Broken 2027", problems[1])
        self.assertEqual(len(run.failures), 2)  # trace order untouched
        self.assertIn("Broken 2027", run.failures[0])

    def test_partial_and_crash_codes_differ_from_argparse_usage_error(self):
        self.assertEqual(len({EXIT_OK, EXIT_CRASHED, EXIT_PARTIAL, 2}), 4)

    def test_failures_are_error_annotations_on_actions(self):
        run, _ = self.make_run({})
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            run.fail(self.trace, "re-check of eci-2027", TimeoutError("timed out"))
        self.assertIn("::error title=Cicadas run::re-check of eci-2027 failed", out.getvalue())

    def test_unreadable_decisions_crash_and_write_problems_file(self):
        bad = Path(self._dir.name) / "decisions.json"
        bad.write_text('{"eci-2027": "pending"}', encoding="utf-8")
        problems = Path(self._dir.name) / "logs" / "run_problems.json"
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = run_main(["--tracker", str(self.path), "--decisions", str(bad), "--problems-out", str(problems)])
        self.assertEqual(code, EXIT_CRASHED)
        data = json.loads(problems.read_text(encoding="utf-8"))
        self.assertTrue(data["crashed"])
        self.assertIn("pending", data["problems"][0])

    def test_one_paper_failing_still_runs_the_other_and_exits_partial(self):
        record_paper_classification(self.tracker, fx.ELEPHANT, fx._classification(
            fx.load_profiles()[fx.ELEPHANT], VenueTier.ACADEMIC_PRACTITIONER, ["a", "b", "c"], "r"))
        save_tracker(self.tracker, self.path)
        decisions = Path(self._dir.name) / "decisions.json"
        decisions.write_text("{}", encoding="utf-8")
        problems = Path(self._dir.name) / "run_problems.json"
        searched = []

        def fake_discover(profile, *args, **kwargs):
            searched.append(profile.id)
            if profile.id == fx.ELEPHANT:
                raise RuntimeError("529 overloaded")
            return NS(candidates=[], fetched_text={}, judged=[])

        out = io.StringIO()
        with mock.patch("src.run.discover", side_effect=fake_discover), contextlib.redirect_stdout(out),                 contextlib.redirect_stderr(out):
            code = run_main(["--tracker", str(self.path), "--decisions", str(decisions), "--problems-out", str(problems),
                             "--traces-dir", str(Path(self._dir.name) / "traces")])
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertEqual(sorted(searched), sorted([fx.ELEPHANT, fx.IMMUNE]))
        data = json.loads(problems.read_text(encoding="utf-8"))
        self.assertFalse(data["crashed"])
        self.assertEqual(len(data["problems"]), 1)
        self.assertTrue(data["problems"][0].startswith("The whole search for elephant-corridors failed"))
        # every paper's trace still ends with its summary line
        for trace_file in (Path(self._dir.name) / "traces").glob("*.md"):
            last = trace_file.read_text(encoding="utf-8").rstrip().splitlines()[-1]
            self.assertTrue(last.startswith("**Summary for "), last)

    def test_write_problems_round_trips(self):
        path = Path(self._dir.name) / "p.json"
        write_problems(path, False, ["a failed", "b failed"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"crashed": False, "problems": ["a failed", "b failed"]})


class TestTraceOutput(RunTestCase):
    def test_accepted_venue_shows_extraction_bucket_and_every_dimension(self):
        run, _ = self.make_run({ECI_URL: facts()})
        self.assertEqual(self.quiet(run.hand_off, self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "on_call_page", "r"),
                                    self.trace), "created")
        text = "\n".join(self.trace.lines)
        for snippet in ("- extraction:", "  - submission type: abstract; theme: Immunology",
                        "  - deadline: 31 March 2027 (exact); call: not_yet_open, opens November 2026",
                        "  - location: Florence, Italy; access: open; mode: not stated; fees: not stated",
                        "not stated on the page: format_requirements, fees, mode",
                        "- venue bucket(s): Academic & Practitioner -- research congress",
                        "- fit for immune-tolerance, Academic & Practitioner: overall 80/100",
                        "  - theme_fit 90: on topic", "  - genre_fit 80: research", "  - format_fit 70: abstract"):
            self.assertIn(snippet, text)

    def test_incomplete_venue_says_not_scored_and_why(self):
        run, _ = self.make_run({ECI_URL: facts(**INCOMPLETE)})
        self.quiet(run.hand_off, self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "not_found", "r"), self.trace)
        self.assertIn("- fit for immune-tolerance: not scored (extraction incomplete)", "\n".join(self.trace.lines))

    def test_hand_off_outcomes(self):
        run, _ = self.make_run({ECI_URL: facts()})
        broken = Candidate("Broken 2027", 2027, "https://broken.example/cfp", None, "on_call_page", "r")
        self.assertEqual(self.quiet(run.hand_off, self.profile, broken, self.trace), "failed")
        eci = Candidate("ECI 2027", 2027, ECI_URL, None, "on_call_page", "r")
        self.quiet(run.hand_off, self.profile, eci, self.trace)
        self.assertEqual(self.quiet(run.hand_off, self.profile, eci, self.trace), "skipped")

    def test_summary_line(self):
        from collections import Counter
        from src.discover import Judgement
        judged = [Judgement("record_result", "u1", "stale", "old", "noted"),
                  Judgement("record_result", "u2", "live_cfp", "open", "noted"),
                  Judgement("accept_candidate", "ECI", "accepted", "r", "accepted"),
                  Judgement("accept_candidate", "X", "refused", "r", "not accepted")]
        line = summary_line("immune-tolerance", judged, {"created": 1, "updated": 0, "failed": 0}, 2,
                            Counter({"web_search": 6, "web_fetch": 4, "record_result": 2}), 0.2149, 0)
        self.assertEqual(line, "**Summary for immune-tolerance:** 4 decision(s) on results -- 1 judged live, 1 accepted, "
                               "1 rejected, 1 refused by code; hand-off: 1 created; 2 re-checked; 0 failure(s); "
                               "12 tool calls (6 web_search, 4 web_fetch, 2 record_result); cost about $0.215")

    def test_publish_trace_prints_terminal_form_and_appends_step_summary(self):
        trace = Trace("t")
        trace.add("**#1 web_search** `q`", "- result: 2 result(s); top: A — https://a")
        trace.details("All 2 results", ["- A — https://a", "- B <x> — https://b"])
        path = Path(self._dir.name) / "traces" / "t.md"
        summary = Path(self._dir.name) / "summary.md"
        summary.write_text("existing\n", encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(summary)}), contextlib.redirect_stdout(out):
            publish_trace(trace, path)
        printed = out.getvalue()
        self.assertIn("- result: 2 result(s); top: A", printed)
        self.assertNotIn("<details>", printed)
        self.assertNotIn("https://b", printed)
        page = summary.read_text(encoding="utf-8")
        self.assertTrue(page.startswith("existing\n"))
        self.assertIn("<details><summary>All 2 results</summary>", page)
        self.assertIn("- B &lt;x&gt; — https://b", page)
        self.assertEqual(path.read_text(encoding="utf-8"), trace.markdown())

    def test_no_step_summary_outside_actions(self):
        out = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(out):
            publish_trace(Trace("t"), Path(self._dir.name) / "t.md")
        self.assertIn("# t", out.getvalue())


class TestCallStatusInTheRun(RunTestCase):
    """The SER2027 case: extraction read the call as closed on its deadline
    day; code decides it is open, so it is judged."""

    def test_closed_on_deadline_day_is_open_and_judged(self):
        on_the_day = facts(deadline="2026-10-07", deadline_as_stated="7 October 2026 at 23:59:59 UTC", call_status="closed")
        run, fakes = self.make_run({ECI_URL: on_the_day})  # run date 2026-10-07
        self.quiet(run.hand_off, self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "on_call_page", "r"), self.trace)
        self.assertIn("judge", [c[0] for c in fakes.calls])
        entry = load_tracker(self.path).conferences[0]
        self.assertEqual(entry.extraction.call_status.value, "open")
        self.assertIn("call status set to open by code (extraction read closed)", "\n".join(self.trace.lines))

    def test_passed_deadline_is_closed_even_if_extraction_read_open(self):
        passed = facts(deadline="2026-10-06", deadline_as_stated="6 October 2026", call_status="open")
        run, _ = self.make_run({ECI_URL: passed})
        self.quiet(run.hand_off, self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "on_call_page", "r"), self.trace)
        self.assertIn("call status set to closed by code", "\n".join(self.trace.lines))

    def test_recheck_applies_the_same_rule(self):
        run, _ = self.make_run({ECI_URL: facts()})
        self.quiet(run.hand_off, self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "on_call_page", "r"), self.trace)
        run2, _ = self.make_run({ECI_URL: facts(call_status="closed")})
        self.quiet(run2.recheck, self.profile, self.trace, set())
        self.assertEqual(load_tracker(self.path).conferences[0].extraction.call_status.value, "open")
        self.assertIn("`eci-2027`: call status set to open by code", "\n".join(self.trace.lines))

    def test_judge_fit_skip_is_month_aware(self):
        from src.judge_fit import skip_reason
        month = facts(deadline="2027-04-01", deadline_precision="month", deadline_as_stated="April 2027", call_status="open")
        self.assertIsNone(skip_reason(month, datetime.date(2027, 4, 15)))  # deadline could still be 30 April
        self.assertIn("has already passed", skip_reason(month, datetime.date(2027, 5, 1)))


class TestHandOffAlias(RunTestCase):
    def test_name_variant_merges_into_the_tracked_venue(self):
        run, _ = self.make_run({ECI_URL: facts()})
        self.quiet(run.hand_off, self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "on_call_page", "r"), self.trace)
        tracker = load_tracker(self.path)
        tracker.conferences[0].matches.clear()  # as if tracked without a match for this paper (e.g. not scored)
        save_tracker(tracker, self.path)
        self.tracker = load_tracker(self.path)
        run, fakes = self.make_run({ECI_URL: facts()})
        outcome = self.quiet(run.hand_off, self.profile,
                             Candidate("European Congress of Immunology 2027", 2027, ECI_URL, None, "on_call_page", "r"), self.trace)
        self.assertEqual(outcome, "updated")
        saved = load_tracker(self.path)
        self.assertEqual(([c.id for c in saved.conferences], saved.identity_conflicts), (["eci-2027"], []))
        self.assertEqual(saved.conferences[0].aliases, ["European Congress of Immunology 2027"])
        self.assertNotIn("classify", [c[0] for c in fakes.calls])  # the tracked venue's buckets are reused


class TestUnreadableDirectFetch(RunTestCase):
    """The Springer case: the hand-off's own fetch got a 'JavaScript is
    disabled' shell (286 characters of text) while the search loop's copy was readable."""

    def springer_run(self, loop_copy=True):
        seen = {}
        springer_facts = facts(deadline="2026-12-31", deadline_as_stated="31 December 2026", call_status="open")
        run, fakes = self.make_run({SPRINGER_URL: springer_facts})
        fakes.pages = {SPRINGER_URL: SPRINGER_WALL}
        original = fakes.extract.extract_facts_from_text
        fakes.extract.extract_facts_from_text = lambda url, text, today: (seen.setdefault("text", text), original(url, text, today))[1]
        loop = {"link.springer.com/journal/10980/collections": SPRINGER_LOOP} if loop_copy else {}
        candidate = Candidate("Landscape Ecology in Latin America", 2026, SPRINGER_URL, None, "on_call_page", "r",
                              deadline_quote="Submission status Open Submission deadline 31 December 2026")
        outcome = self.quiet(run.hand_off, self.profile, candidate, self.trace, loop)
        return run, fakes, outcome, seen.get("text")

    def test_saved_wall_page_fails_the_loops_readability_rule(self):
        from src.run import readable_text
        self.assertIn("JavaScript is disabled", SPRINGER_WALL)
        self.assertFalse(readable_text(SPRINGER_WALL))
        self.assertTrue(readable_text(SPRINGER_LOOP))

    def test_wall_page_replaced_by_the_loops_readable_copy(self):
        run, fakes, outcome, text = self.springer_run()
        self.assertEqual(outcome, "created")
        self.assertEqual(text, SPRINGER_LOOP)  # extraction read the loop's copy, never the shell
        self.assertIn("Submission deadline\n31 December 2026", text)
        trace = "\n".join(self.trace.lines)
        self.assertIn(f"- direct fetch of {SPRINGER_URL} unreadable: 286 characters of text", trace)
        self.assertIn("using the discovery loop's readable copy", trace)
        self.assertEqual(run.failures, [])

    def test_wall_page_without_a_loop_copy_fails_visibly_and_tracks_nothing(self):
        run, fakes, outcome, text = self.springer_run(loop_copy=False)
        self.assertEqual(outcome, "failed")
        self.assertIsNone(text)  # never extracted from the shell
        self.assertIn("call page https://link.springer.com/journal/10980/collections unreadable", run.failures[0])
        self.assertFalse(self.path.exists())

    def test_unreadable_dates_page_left_out_but_call_page_kept(self):
        dates = "https://eci2027.org/dates"
        run, fakes = self.make_run({ECI_URL: facts()})
        fakes.pages = {dates: SPRINGER_WALL}
        seen = {}
        original = fakes.extract.extract_facts_from_text
        fakes.extract.extract_facts_from_text = lambda url, text, today: (seen.setdefault("text", text), original(url, text, today))[1]
        self.quiet(run.hand_off, self.profile, Candidate("ECI 2027", 2027, ECI_URL, dates, "on_call_page", "r"), self.trace, {})
        self.assertEqual(seen["text"], READABLE_PAGE)
        self.assertIn(f"- direct fetch of {dates} unreadable", "\n".join(self.trace.lines))
        self.assertIn("left out", "\n".join(self.trace.lines))

    def test_recheck_of_a_wall_page_fails_and_leaves_the_entry_unchanged(self):
        run, fakes = self.make_run({ECI_URL: facts()})
        self.quiet(run.hand_off, self.profile, Candidate("ECI 2027", 2027, ECI_URL, None, "on_call_page", "r"), self.trace)
        before = self.path.read_bytes()
        run2, fakes2 = self.make_run({ECI_URL: facts(has_deadline=False, deadline=None, deadline_precision=None,
                                                     deadline_as_stated=None, missing_fields=["format_requirements", "fees", "mode"])})
        fakes2.pages = {ECI_URL: SPRINGER_WALL}
        # re-check fetches [url] with no dates page, so the fake's per-URL page applies
        self.quiet(run2.recheck, self.profile, self.trace, set())
        self.assertNotIn("extract", [c[0] for c in fakes2.calls])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("page unreadable (286 characters of text", run2.failures[0])
        self.assertIn("tracked entry left unchanged", run2.failures[0])


if __name__ == "__main__":
    unittest.main()
