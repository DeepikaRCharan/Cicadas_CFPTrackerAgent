"""Tests for src/tracker.py -- build step 5: tracker read/write and the
decisions.json sync. No API calls; every run happens against temp files.

Run with: python -m unittest tests/test_tracker.py -v
"""

import contextlib
import datetime
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tracker import (
    decide_call_status,
    deadline_passed,
    DecisionsError,
    TrackerError,
    load_decisions,
    load_tracker,
    make_id,
    mark_reported,
    normalize_url,
    record_paper_classification,
    save_tracker,
    sync_decisions,
    unreported_notifications,
    upsert_venue,
)
from src.tracker_schema import (
    CallStatus,
    DecisionState,
    Extraction,
    ExtractionFacts,
    NotificationReason,
    ProfileClassification,
    StandardFit,
    VenueTier,
)
from tests import render_fixtures as fx

RUN_1 = datetime.datetime(2026, 10, 7, 9, 0, tzinfo=datetime.timezone.utc)
RUN_2 = datetime.datetime(2026, 11, 7, 9, 0, tzinfo=datetime.timezone.utc)
ECI_URL = "https://eci2027.org/call-for-abstracts/"


def make_extraction(**overrides):
    base = dict(
        has_deadline=True,
        deadline="2027-01-15",
        deadline_precision="exact",
        deadline_as_stated="15 January 2027",
        submission_opens="November 2026",
        call_status="not_yet_open",
        submission_type="abstract",
        format_requirements="300 words",
        fees=None,
        mode=None,
        theme="immunology",
        location="Dublin, Ireland",
        access="open",
        venue_tier=["academic_practitioner"],
        venue_tier_reasoning="scientific congress",
        complete=True,
        missing_fields=["fees", "mode"],
    )
    base.update(overrides)
    return Extraction(**base)


def make_facts(**overrides):
    data = make_extraction(**overrides).model_dump(exclude={"venue_tier", "venue_tier_reasoning"})
    return ExtractionFacts(**data)


def make_fit(theme=90, genre=80, fmt=70, profile_hash="h-immune"):
    return {
        VenueTier.ACADEMIC_PRACTITIONER: StandardFit(
            theme_fit={"score": theme, "reasoning": "on topic"},
            genre_fit={"score": genre, "reasoning": "research congress"},
            format_fit={"score": fmt, "reasoning": "abstract length fits"},
            scored_at=RUN_1,
            profile_hash=profile_hash,
        )
    }


def make_classification(profile_hash="h-immune"):
    return ProfileClassification(
        paper_bucket=["academic_practitioner"],
        paper_bucket_cues=["cue one", "cue two", "cue three"],
        paper_bucket_reasoning="peer-reviewed research",
        profile_hash=profile_hash,
        classified_at=RUN_1,
    )


class TrackerTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)
        self.tracker_path = self.dir / "tracker.json"
        self.decisions_path = self.dir / "decisions.json"
        self.write_decisions({})

    def tearDown(self):
        self._dir.cleanup()

    def write_decisions(self, data):
        self.decisions_path.write_text(json.dumps(data), encoding="utf-8")

    def run_once(self, now, candidates, live=("immune",)):
        """One pipeline run: load, sync decisions, merge candidates, save."""
        live = set(live)
        tracker = load_tracker(self.tracker_path)
        tracker.last_run = now
        decisions = load_decisions(self.decisions_path)
        warnings = sync_decisions(tracker, decisions, live)
        for profile_id in live:
            if profile_id not in tracker.profiles:
                record_paper_classification(tracker, profile_id, make_classification(f"h-{profile_id}"))
        results = [
            upsert_venue(tracker, live_profile_ids=live, now=now, decisions=decisions, **c) for c in candidates
        ]
        save_tracker(tracker, self.tracker_path)
        return tracker, results, warnings

    def eci(self, **overrides):
        base = dict(
            name="ECI 2027", year=2027, url=ECI_URL, extraction=make_extraction(),
            profile_id="immune", fit_by_tier=make_fit(),
        )
        base.update(overrides)
        return base


class TestIdempotency(TrackerTestCase):
    def test_second_run_same_input_creates_no_duplicates(self):
        self.run_once(RUN_1, [self.eci()])
        tracker, results, _ = self.run_once(RUN_2, [self.eci()])
        self.assertEqual(len(tracker.conferences), 1)
        self.assertEqual(results[0].status, "updated")
        self.assertEqual(results[0].logged, [])
        match = tracker.conferences[0].matches["immune"]
        self.assertEqual([n.reason for n in match.notifications], [NotificationReason.NEW_FIT])
        self.assertEqual(tracker.conferences[0].first_seen, RUN_1)
        self.assertEqual(tracker.conferences[0].last_checked, RUN_2)

    def test_url_variants_are_the_same_venue(self):
        self.run_once(RUN_1, [self.eci()])
        variant = "http://www.ECI2027.org/call-for-abstracts?utm_source=x#top"
        tracker, results, _ = self.run_once(RUN_2, [self.eci(url=variant)])
        self.assertEqual(results[0].status, "updated")
        self.assertEqual(len(tracker.conferences), 1)

    def test_query_string_distinguishes_shared_hosts(self):
        self.assertNotEqual(
            normalize_url("https://easychair.org/cfp/?conf=abc2027"),
            normalize_url("https://easychair.org/cfp/?conf=xyz2027"),
        )


class TestIdentity(TrackerTestCase):
    def test_id_is_normalized_name_plus_year(self):
        self.assertEqual(make_id("ECI 2027", 2027), "eci-2027")
        self.assertEqual(make_id("Immunology & Society: Café Summit", 2027), "immunology-and-society-cafe-summit-2027")
        self.assertEqual(make_id("Write For Us", None), "write-for-us")

    def test_same_name_different_year_gives_two_venues(self):
        self.run_once(RUN_1, [self.eci()])
        tracker, results, _ = self.run_once(
            RUN_2, [self.eci(name="ECI 2028", year=2028, url="https://eci2028.org/call-for-abstracts/")]
        )
        self.assertEqual(results[0].status, "created")
        self.assertEqual(sorted(c.id for c in tracker.conferences), ["eci-2027", "eci-2028"])

    def test_same_url_different_year_gives_two_venues(self):
        reused = "https://example-congress.org/cfp"
        self.run_once(RUN_1, [self.eci(name="Example Congress", url=reused)])
        tracker, results, _ = self.run_once(RUN_2, [self.eci(name="Example Congress", year=2028, url=reused)])
        self.assertEqual(results[0].status, "created")
        self.assertEqual(len(tracker.conferences), 2)

    def test_same_name_and_year_different_url_is_flagged_not_merged(self):
        self.run_once(RUN_1, [self.eci()])
        before = load_tracker(self.tracker_path).conferences[0].model_dump()
        other_url = "https://easychair.org/cfp/ECI2027"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tracker, results, _ = self.run_once(
                RUN_2, [self.eci(url=other_url, extraction=make_extraction(location="Elsewhere"))]
            )
        self.assertEqual(results[0].status, "flagged")
        self.assertEqual(len(tracker.conferences), 1)
        self.assertEqual(tracker.conferences[0].model_dump(), before)  # existing entry not touched
        self.assertEqual(len(tracker.identity_conflicts), 1)
        conflict = tracker.identity_conflicts[0]
        self.assertEqual(conflict.reason.value, "same_id_different_url")
        self.assertEqual((conflict.existing_id, conflict.candidate_url), ("eci-2027", other_url))
        self.assertIn("WARNING", stderr.getvalue())

        # re-flagging the same conflict doesn't pile up duplicates
        with contextlib.redirect_stderr(io.StringIO()):
            tracker, _, _ = self.run_once(RUN_2 + datetime.timedelta(days=1), [self.eci(url=other_url)])
        self.assertEqual(len(tracker.identity_conflicts), 1)

    def test_same_url_same_year_different_name_is_flagged(self):
        self.run_once(RUN_1, [self.eci()])
        with contextlib.redirect_stderr(io.StringIO()):
            tracker, results, _ = self.run_once(RUN_2, [self.eci(name="European Congress of Immunology 2027")])
        self.assertEqual(results[0].status, "flagged")
        self.assertEqual(len(tracker.conferences), 1)
        self.assertEqual(tracker.identity_conflicts[0].reason.value, "same_url_same_year_different_name")


class TestDecisionsSync(TrackerTestCase):
    def decision(self, tracker):
        return tracker.conferences[0].matches["immune"].decision

    def test_decisions_edit_shows_up_on_next_run(self):
        tracker, _, _ = self.run_once(RUN_1, [self.eci()])
        self.assertEqual(self.decision(tracker), DecisionState.PENDING)

        self.write_decisions({"eci-2027": "applying"})
        tracker, _, _ = self.run_once(RUN_2, [])
        self.assertEqual(self.decision(load_tracker(self.tracker_path)), DecisionState.APPLYING)

        self.write_decisions({"eci-2027": "skipped"})
        tracker, _, _ = self.run_once(RUN_2, [])
        self.assertEqual(self.decision(tracker), DecisionState.SKIPPED)

        self.write_decisions({})  # line deleted: no researcher decision any more
        tracker, _, _ = self.run_once(RUN_2, [])
        self.assertEqual(self.decision(tracker), DecisionState.PENDING)

    def test_researcher_decision_wins_over_unconfirmed(self):
        tracker, _, _ = self.run_once(RUN_1, [self.eci()])
        tracker.conferences[0].matches["immune"].decision = DecisionState.UNCONFIRMED
        save_tracker(tracker, self.tracker_path)
        self.write_decisions({"eci-2027": "applied"})
        tracker, _, _ = self.run_once(RUN_2, [])
        self.assertEqual(self.decision(tracker), DecisionState.APPLIED)

    def test_unknown_id_warns_and_run_continues(self):
        self.run_once(RUN_1, [self.eci()])
        self.write_decisions({"eci-2027": "applying", "eci-2O27": "skipped"})
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tracker, _, warnings = self.run_once(RUN_2, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("eci-2O27", warnings[0])
        self.assertIn("[tracker] WARNING", stderr.getvalue())
        self.assertIn("eci-2O27", stderr.getvalue())
        self.assertEqual(self.decision(tracker), DecisionState.APPLYING)  # the good line still applied

    def test_bad_value_stops_the_run_naming_the_line(self):
        self.write_decisions({"eci-2027": "pending"})
        with self.assertRaises(DecisionsError) as ctx:
            load_decisions(self.decisions_path)
        self.assertIn("eci-2027", str(ctx.exception))

    def test_duplicate_key_stops_the_run(self):
        self.decisions_path.write_text('{"eci-2027": "applying", "eci-2027": "skipped"}', encoding="utf-8")
        with self.assertRaises(DecisionsError):
            load_decisions(self.decisions_path)

    def test_new_match_picks_up_existing_decision(self):
        self.run_once(RUN_1, [self.eci()])
        self.write_decisions({"eci-2027": "applying"})
        tracker, _, _ = self.run_once(RUN_2, [self.eci(profile_id="elephant")], live=("immune", "elephant"))
        self.assertEqual(tracker.conferences[0].matches["elephant"].decision, DecisionState.APPLYING)

    def test_agent_never_writes_decisions_file(self):
        self.write_decisions({"eci-2027": "applying"})
        before = self.decisions_path.read_bytes()
        self.run_once(RUN_1, [self.eci()])
        self.run_once(RUN_2, [self.eci()])
        self.assertEqual(self.decisions_path.read_bytes(), before)


class TestSafeWrites(TrackerTestCase):
    def test_invalid_entry_leaves_tracker_unchanged(self):
        self.run_once(RUN_1, [self.eci()])
        before = self.tracker_path.read_bytes()
        tracker = load_tracker(self.tracker_path)
        # an in-memory edit that breaks a schema rule: fit keyed under a
        # bucket the venue was never classified into
        match = tracker.conferences[0].matches["immune"]
        match.fit_by_tier = {VenueTier.CORPORATE_INSTITUTIONAL: make_fit()[VenueTier.ACADEMIC_PRACTITIONER]}
        with self.assertRaises(TrackerError):
            save_tracker(tracker, self.tracker_path)
        self.assertEqual(self.tracker_path.read_bytes(), before)
        self.assertEqual([p.name for p in self.dir.iterdir()], sorted(["decisions.json", "tracker.json"]))

    def test_failed_replace_leaves_tracker_unchanged_and_no_temp_file(self):
        self.run_once(RUN_1, [self.eci()])
        before = self.tracker_path.read_bytes()
        tracker = load_tracker(self.tracker_path)
        tracker.last_run = RUN_2  # a valid change, so validation passes
        with mock.patch("src.tracker.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                save_tracker(tracker, self.tracker_path)
        self.assertEqual(self.tracker_path.read_bytes(), before)
        self.assertEqual([p.name for p in self.dir.iterdir()], sorted(["decisions.json", "tracker.json"]))

    def test_corrupt_tracker_stops_the_run(self):
        self.tracker_path.write_text('{"schema_version": 6, "conferences": "oops"}', encoding="utf-8")
        before = self.tracker_path.read_bytes()
        with self.assertRaises(TrackerError):
            load_tracker(self.tracker_path)
        self.assertEqual(self.tracker_path.read_bytes(), before)

    def test_missing_tracker_is_an_empty_first_run(self):
        with contextlib.redirect_stdout(io.StringIO()):
            tracker = load_tracker(self.tracker_path)
        self.assertEqual(tracker.conferences, [])


class TestProfileSwap(TrackerTestCase):
    def test_swapped_profile_entries_kept_as_history(self):
        # run 1: immune is live and tracks ECI and an immune-only venue
        immune_only = self.eci(name="Treg Workshop", url="https://treg-workshop.org/cfp")
        self.run_once(RUN_1, [self.eci(), immune_only])
        before = json.loads(self.tracker_path.read_text(encoding="utf-8"))

        # the researcher decides on both; then immune is swapped for elephant
        self.write_decisions({"eci-2027": "applying", "treg-workshop-2027": "skipped"})
        tracker, _, warnings = self.run_once(RUN_2, [self.eci(profile_id="elephant")], live=("elephant",))
        after = json.loads(self.tracker_path.read_text(encoding="utf-8"))

        self.assertEqual(warnings, [])  # both ids are still tracked
        self.assertEqual(after["profiles"]["immune"], before["profiles"]["immune"])
        for conf_id in ("eci-2027", "treg-workshop-2027"):
            old = next(c for c in before["conferences"] if c["id"] == conf_id)
            new = next(c for c in after["conferences"] if c["id"] == conf_id)
            self.assertEqual(new["matches"]["immune"], old["matches"]["immune"])  # frozen, decisions not applied
        eci = next(c for c in tracker.conferences if c.id == "eci-2027")
        self.assertEqual(eci.matches["elephant"].decision, DecisionState.APPLYING)

    def test_frozen_profile_cannot_be_written(self):
        self.run_once(RUN_1, [self.eci()])
        with self.assertRaises(TrackerError):
            self.run_once(RUN_2, [self.eci()], live=("elephant",))


class TestReporting(TrackerTestCase):
    def test_entries_stay_unreported_when_send_never_happens(self):
        self.run_once(RUN_1, [self.eci()])
        # the email for run 1 never went out (no mark_reported call)
        tracker, _, _ = self.run_once(RUN_2, [self.eci()])
        pending = unreported_notifications(tracker)
        self.assertEqual([(p.conference.id, p.entry.reason) for p in pending], [("eci-2027", NotificationReason.NEW_FIT)])
        self.assertEqual(pending[0].entry.run_at, RUN_1)  # logged in run 1, still waiting in run 2

        # a successful send flips the flag, and it survives the save
        mark_reported(pending)
        save_tracker(tracker, self.tracker_path)
        self.assertEqual(unreported_notifications(load_tracker(self.tracker_path)), [])
        tracker, results, _ = self.run_once(RUN_2 + datetime.timedelta(days=30), [self.eci()])
        self.assertEqual(results[0].logged, [])  # new_fit is never logged twice

    def test_below_threshold_logs_nothing(self):
        tracker, results, _ = self.run_once(RUN_1, [self.eci(fit_by_tier=make_fit(theme=40, genre=40, fmt=40))])
        self.assertEqual(results[0].logged, [])
        self.assertEqual(unreported_notifications(tracker), [])

    def test_tier_alert_from_stored_scores(self):
        _, results, _ = self.run_once(RUN_1, [self.eci(fit_by_tier=make_fit(theme=90, genre=10, fmt=20))])
        self.assertEqual(results[0].logged, [NotificationReason.TIER_ALERT])

    def test_updates_only_after_venue_was_sent(self):
        self.run_once(RUN_1, [self.eci()])
        # not yet reported: a change on re-check is not an Update
        tracker, results, _ = self.run_once(RUN_2, [self.eci(extraction=make_facts(call_status="open"), fit_by_tier=None)])
        self.assertEqual(results[0].logged, [])

        mark_reported(unreported_notifications(tracker))
        save_tracker(tracker, self.tracker_path)
        later = make_facts(call_status="open", deadline="2027-01-20", deadline_as_stated="20 January 2027")
        _, results, _ = self.run_once(RUN_2 + datetime.timedelta(days=1), [self.eci(extraction=later, fit_by_tier=None)])
        self.assertEqual(results[0].logged, [NotificationReason.DEADLINE_CHANGED])

    def test_submissions_opened_after_sent(self):
        tracker, _, _ = self.run_once(RUN_1, [self.eci()])
        mark_reported(unreported_notifications(tracker))
        save_tracker(tracker, self.tracker_path)
        _, results, _ = self.run_once(RUN_2, [self.eci(extraction=make_facts(call_status="open"), fit_by_tier=None)])
        self.assertEqual(results[0].logged, [NotificationReason.SUBMISSIONS_OPENED])

    def test_snapshot_sent_survives_reclassification(self):
        tracker, _, _ = self.run_once(RUN_1, [])
        tracker.profiles["immune"].snapshot_sent = RUN_1.date()
        record_paper_classification(tracker, "immune", make_classification(profile_hash="h-revised"))
        self.assertEqual(tracker.profiles["immune"].snapshot_sent, RUN_1.date())


class TestCallStatusByCode(unittest.TestCase):
    """Open or closed is decided by code from the deadline and the run date;
    a call is closed only after its deadline has passed."""

    def facts(self, **overrides):
        data = fx.eci_extraction().model_dump(exclude={"venue_tier", "venue_tier_reasoning"})
        data.update(overrides)
        return ExtractionFacts(**data)

    def decide(self, today, **overrides):
        return decide_call_status(self.facts(**overrides), today)

    def test_deadline_day_is_still_open_even_if_extraction_read_closed(self):
        facts, note = self.decide(datetime.date(2027, 3, 31), call_status="closed")
        self.assertEqual(facts.call_status, CallStatus.OPEN)
        self.assertIn("set to open by code (extraction read closed)", note)
        self.assertIn("has not passed", note)

    def test_closed_the_day_after_the_deadline_whatever_extraction_read(self):
        facts, note = self.decide(datetime.date(2027, 4, 1), call_status="open")
        self.assertEqual(facts.call_status, CallStatus.CLOSED)
        self.assertIn("passed on 2027-03-31", note)

    def test_not_yet_open_before_the_deadline_stands(self):
        facts, note = self.decide(datetime.date(2026, 10, 7))
        self.assertEqual((facts.call_status, note), (CallStatus.NOT_YET_OPEN, None))

    def test_month_only_deadline_open_until_the_month_ends(self):
        month = dict(deadline="2027-04-01", deadline_precision="month", deadline_as_stated="April 2027")
        self.assertEqual(self.decide(datetime.date(2027, 4, 30), call_status="closed", **month)[0].call_status, CallStatus.OPEN)
        self.assertEqual(self.decide(datetime.date(2027, 5, 1), call_status="open", **month)[0].call_status, CallStatus.CLOSED)

    def test_no_deadline_to_judge_by_closed_becomes_unknown(self):
        rolling = dict(has_deadline=False, deadline=None, deadline_precision=None, deadline_as_stated=None,
                       missing_fields=["format_requirements", "fees", "mode"])
        facts, note = self.decide(datetime.date(2030, 1, 1), call_status="closed", **rolling)
        self.assertEqual(facts.call_status, CallStatus.UNKNOWN)
        self.assertIn("no deadline to judge it by", note)
        incomplete = dict(deadline=None, deadline_precision=None, deadline_as_stated=None, complete=False,
                          missing_fields=["deadline", "format_requirements", "fees", "mode"])
        self.assertEqual(self.decide(datetime.date(2027, 1, 1), call_status="closed", **incomplete)[0].call_status,
                         CallStatus.UNKNOWN)

    def test_deadline_passed_helper(self):
        f = self.facts()
        self.assertFalse(deadline_passed(f, datetime.date(2027, 3, 31)))
        self.assertTrue(deadline_passed(f, datetime.date(2027, 4, 1)))


if __name__ == "__main__":
    unittest.main()
