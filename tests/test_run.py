"""Tests for src/run.py -- build step 8: hand-off and re-check. Extract,
classify and judge-fit are replaced with fakes; no API calls.

Run with: python -m unittest tests/test_run.py -v
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.discover import Candidate, Trace
from src.run import Run
from src.tracker import load_tracker, record_paper_classification, upsert_venue
from src.tracker_schema import ExtractionFacts, TrackerFile, VenueTier
from tests import render_fixtures as fx

ECI_URL = "https://eci2027.org/call-for-abstracts/"


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

    def fetch_text(self, urls):
        self.calls.append(("fetch", tuple(urls)))
        if any("broken" in u for u in urls):
            raise RuntimeError("failed to fetch: HTTP 403")
        return "page text"

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
        run.live, run.rechecked, run.failures = set(self.profiles), set(), []
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
        self.assertIn(("fetch", (ECI_URL, "https://eci2027.org/dates")), fakes.calls)
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
                   self.trace, {url.replace("https://", ""): "loop copy " * 50})
        self.assertEqual([c[0] for c in fakes.calls], ["fetch", "extract", "classify", "judge"])
        self.assertEqual(run.failures, [])
        self.assertIn("using the discovery loop's fetched copy", "\n".join(self.trace.lines))

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


if __name__ == "__main__":
    unittest.main()
