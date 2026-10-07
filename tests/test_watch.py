"""Tests for src/watch.py -- build step 10: the daily deadline watcher.
Synthetic deadlines and dates; Resend is always mocked, nothing is sent.

Run with: python -m unittest tests/test_watch.py -v
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

from src import watch
from src.tracker import TrackerError, load_tracker, record_paper_classification, save_tracker, upsert_venue
from src.tracker_schema import DecisionState, NotificationReason, TrackerFile, VenueTier
from tests import render_fixtures as fx

ENV = {"RESEND_API_KEY": "re_test_key", "NOTIFY_TO": "researcher@example.com"}
ECI, KYOTO, FORUM, MONGABAY = "eci-2027", "kyoto-treg-biology-symposium-2027", "africa-wildlife-corridors-policy-forum-2027", "mongabay-contributor-pitches"
D = datetime.date


def week_before(tracker, conference_id, profile_id):
    conference = next(c for c in tracker.conferences if c.id == conference_id)
    return [n for n in conference.matches[profile_id].notifications if n.reason == NotificationReason.WEEK_BEFORE]


def decision(tracker, conference_id, profile_id):
    return next(c for c in tracker.conferences if c.id == conference_id).matches[profile_id].decision


class WatchTestCase(unittest.TestCase):
    """ECI 2027 (exact, 31 Mar 2027) and the Kyoto symposium (month-only,
    April 2027) for immune-tolerance, both above the threshold; the policy
    forum (exact, 15 Jan 2027) for elephant-corridors, below it; Mongabay,
    rolling, for elephant-corridors."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        self.path, self.decisions_path = root / "tracker.json", root / "decisions.json"
        self.write_decisions({})
        self.profiles = fx.load_profiles()
        immune, elephant = self.profiles[fx.IMMUNE], self.profiles[fx.ELEPHANT]
        tracker = TrackerFile()
        live = set(self.profiles)
        with contextlib.redirect_stdout(io.StringIO()):
            for pid in live:
                record_paper_classification(tracker, pid, fx._classification(
                    self.profiles[pid], VenueTier.ACADEMIC_PRACTITIONER, ["a", "b", "c"], "r"))
            for kwargs in [
                dict(name="ECI 2027", year=2027, url="https://eci2027.org/call-for-abstracts/",
                     extraction=fx.eci_extraction(), profile_id=fx.IMMUNE,
                     fit_by_tier=fx._fit(immune, (90, "t"), (80, "g"), (70, "f"))),
                dict(name="Kyoto Treg Biology Symposium 2027", year=2027, url="https://treg-symposium.jp/2027",
                     extraction=fx.symposium_extraction(), profile_id=fx.IMMUNE,
                     fit_by_tier=fx._fit(immune, (92, "t"), (80, "g"), (60, "f"))),
                dict(name="Africa Wildlife Corridors Policy Forum 2027", year=2027,
                     url="https://corridorsforum.org/2027/call", extraction=fx.forum_extraction(),
                     profile_id=fx.ELEPHANT, fit_by_tier=fx._fit(
                         elephant, (90, "t"), (30, "g"), (20, "f"), tier=VenueTier.CORPORATE_INSTITUTIONAL)),
                dict(name="Mongabay contributor pitches", year=None, url="https://news.mongabay.com/submissions/",
                     extraction=fx.mongabay_extraction(), profile_id=fx.ELEPHANT, fit_by_tier=fx._fit(
                         elephant, (88, "t"), (72, "g"), (80, "f"), tier=VenueTier.INDUSTRY_TECHNICAL_MEDIA)),
            ]:
                upsert_venue(tracker, live_profile_ids=live, now=fx.RUN_1, **kwargs)
        save_tracker(tracker, self.path)
        self.sender = mock.Mock(return_value="email-1")

    def tearDown(self):
        self._dir.cleanup()

    def write_decisions(self, decisions):
        self.decisions_path.write_text(json.dumps(decisions), encoding="utf-8")

    def watch(self, today, profiles=None, **kwargs):
        now = datetime.datetime.combine(today, datetime.time(12, 17), datetime.timezone.utc)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = watch.run_watch(self.path, self.decisions_path, profiles or self.profiles, today, now,
                                     env=ENV, sender=kwargs.pop("sender", self.sender), **kwargs)
        self.output = out.getvalue()
        return result

    def sent_subjects(self):
        return [c.args[2].subject for c in self.sender.call_args_list]


class TestQuietDay(WatchTestCase):
    def test_nothing_due_leaves_file_byte_identical(self):
        before = self.path.read_bytes()
        result = self.watch(D(2026, 10, 7))
        self.assertEqual((result.closed, result.logged, result.sent, result.failures), ([], [], [], []))
        self.assertFalse(result.changed)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.sender.assert_not_called()

    def test_missing_tracker_is_an_error_not_an_empty_start(self):
        self.path.unlink()
        with self.assertRaises(TrackerError):
            self.watch(D(2026, 10, 7))
        self.assertFalse(self.path.exists())


class TestWeekBefore(WatchTestCase):
    def test_exact_deadline_window_opens_seven_days_before(self):
        self.watch(D(2027, 3, 23))
        self.sender.assert_not_called()
        result = self.watch(D(2027, 3, 24))
        self.assertEqual(result.sent, [ECI])
        entries = week_before(load_tracker(self.path), ECI, fx.IMMUNE)
        self.assertEqual([(e.date, e.deadline, e.reported) for e in entries], [(D(2027, 3, 24), D(2027, 3, 31), True)])

    def test_reminded_once_per_deadline(self):
        self.watch(D(2027, 3, 24))
        before = self.path.read_bytes()
        result = self.watch(D(2027, 3, 25))
        self.assertNotIn(ECI, result.sent)
        self.assertEqual(len(week_before(load_tracker(self.path), ECI, fx.IMMUNE)), 1)
        self.assertEqual(len([s for s in self.sent_subjects() if "ECI 2027" in s]), 1)
        # Kyoto's window opened on the 25th; nothing else changed
        self.assertEqual(result.sent, [KYOTO])
        self.assertNotEqual(self.path.read_bytes(), before)

    def test_missed_days_send_late_not_never(self):
        result = self.watch(D(2027, 3, 30))
        self.assertIn(ECI, result.sent)

    def test_no_reminder_after_deadline(self):
        result = self.watch(D(2027, 4, 1))
        self.assertNotIn(ECI, result.sent)
        self.assertEqual(week_before(load_tracker(self.path), ECI, fx.IMMUNE), [])

    def test_month_only_deadline_seven_days_before_month_starts(self):
        self.watch(D(2027, 3, 24))
        self.assertNotIn(KYOTO, self.watch(D(2027, 3, 24)).sent)
        result = self.watch(D(2027, 3, 25))
        self.assertEqual(result.sent, [KYOTO])
        self.assertIn("exact date not published", self.sent_subjects()[-1])

    def test_month_only_sent_once_seven_days_before_month_starts(self):
        """April 2027 (deadline stored as 1 April): due from 25 March, sent once
        and stamped, never again on the later days of the window."""
        for day in range(1, 25):
            self.assertNotIn(KYOTO, self.watch(D(2027, 3, day)).sent)
        self.assertIn(KYOTO, self.watch(D(2027, 3, 25)).sent)
        for later in (D(2027, 3, 26), D(2027, 4, 1), D(2027, 4, 15), D(2027, 4, 30)):
            self.assertNotIn(KYOTO, self.watch(later).sent)
        entries = week_before(load_tracker(self.path), KYOTO, fx.IMMUNE)
        self.assertEqual([(e.date, e.deadline, e.reported) for e in entries], [(D(2027, 3, 25), D(2027, 4, 1), True)])
        self.assertEqual(len([s for s in self.sent_subjects() if "Kyoto" in s]), 1)

    def test_failed_send_is_retried_next_day_without_duplicating(self):
        failing = mock.Mock(side_effect=RuntimeError("Resend down"))
        result = self.watch(D(2027, 3, 24), sender=failing)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("FAILED", self.output)
        entries = week_before(load_tracker(self.path), ECI, fx.IMMUNE)
        self.assertEqual([e.reported for e in entries], [False])  # logged and saved, not marked

        result = self.watch(D(2027, 3, 25))
        self.assertIn(ECI, result.sent)
        entries = week_before(load_tracker(self.path), ECI, fx.IMMUNE)
        self.assertEqual([(e.date, e.reported) for e in entries], [(D(2027, 3, 24), True)])

    def test_changed_deadline_gets_its_own_reminder(self):
        self.watch(D(2027, 3, 24))
        tracker = load_tracker(self.path)
        eci = next(c for c in tracker.conferences if c.id == ECI)
        eci.extraction = eci.extraction.model_copy(update={"deadline": D(2027, 4, 14), "deadline_as_stated": "14 April 2027"})
        save_tracker(tracker, self.path)
        self.assertNotIn(ECI, self.watch(D(2027, 4, 6)).sent)
        self.assertIn(ECI, self.watch(D(2027, 4, 7)).sent)
        self.assertEqual([e.deadline for e in week_before(load_tracker(self.path), ECI, fx.IMMUNE)],
                         [D(2027, 3, 31), D(2027, 4, 14)])

    def test_pending_below_threshold_not_reminded_but_applying_is(self):
        self.assertNotIn(FORUM, self.watch(D(2027, 1, 8)).sent)
        self.write_decisions({FORUM: "applying"})
        self.assertIn(FORUM, self.watch(D(2027, 1, 9)).sent)

    def test_closed_decision_not_reminded(self):
        self.write_decisions({ECI: "skipped"})
        self.assertNotIn(ECI, self.watch(D(2027, 3, 24)).sent)

    def test_two_profiles_one_email(self):
        tracker = load_tracker(self.path)
        eci = next(c for c in tracker.conferences if c.id == ECI)
        eci.matches[fx.ELEPHANT] = eci.matches[fx.IMMUNE].model_copy(deep=True)
        save_tracker(tracker, self.path)
        result = self.watch(D(2027, 3, 24))
        self.assertEqual(result.sent, [ECI])
        self.assertEqual(self.sender.call_count, 1)
        tracker = load_tracker(self.path)
        for pid in (fx.IMMUNE, fx.ELEPHANT):
            self.assertEqual([e.reported for e in week_before(tracker, ECI, pid)], [True])

    def test_reminder_carries_also_coming_up(self):
        self.watch(D(2027, 3, 25))
        body = next(c.args[2].body for c in self.sender.call_args_list if "ECI 2027" in c.args[2].subject)
        self.assertIn("Also coming up", body)
        self.assertIn("Kyoto Treg Biology Symposium", body)


class TestClosure(WatchTestCase):
    def test_exact_deadline_closes_two_days_after(self):
        self.watch(D(2027, 4, 1))
        self.assertEqual(decision(load_tracker(self.path), ECI, fx.IMMUNE), DecisionState.PENDING)
        result = self.watch(D(2027, 4, 2))
        self.assertEqual(decision(load_tracker(self.path), ECI, fx.IMMUNE), DecisionState.UNCONFIRMED)
        self.assertEqual(len([c for c in result.closed if c.startswith(ECI)]), 1)

    def test_below_threshold_match_also_closes(self):
        self.watch(D(2027, 1, 17))
        self.assertEqual(decision(load_tracker(self.path), FORUM, fx.ELEPHANT), DecisionState.UNCONFIRMED)

    def test_month_only_closes_two_days_after_month_ends(self):
        self.watch(D(2027, 5, 1))
        self.assertEqual(decision(load_tracker(self.path), KYOTO, fx.IMMUNE), DecisionState.PENDING)
        self.watch(D(2027, 5, 2))
        self.assertEqual(decision(load_tracker(self.path), KYOTO, fx.IMMUNE), DecisionState.UNCONFIRMED)

    def test_rolling_never_closes(self):
        self.watch(D(2030, 1, 1))
        self.assertEqual(decision(load_tracker(self.path), MONGABAY, fx.ELEPHANT), DecisionState.PENDING)

    def test_researcher_decision_wins_over_closure(self):
        self.write_decisions({ECI: "applying"})
        self.watch(D(2027, 4, 2))
        self.assertEqual(decision(load_tracker(self.path), ECI, fx.IMMUNE), DecisionState.APPLYING)
        before = self.path.read_bytes()
        self.watch(D(2027, 4, 3))
        self.assertEqual(self.path.read_bytes(), before)  # no flip-flop between sync and closure

    def test_swapped_out_profile_stays_frozen(self):
        only_elephant = {fx.ELEPHANT: self.profiles[fx.ELEPHANT]}
        self.watch(D(2027, 4, 2), profiles=only_elephant)
        self.assertEqual(decision(load_tracker(self.path), ECI, fx.IMMUNE), DecisionState.PENDING)

    def test_second_run_after_closure_changes_nothing(self):
        self.watch(D(2027, 4, 2))
        before = self.path.read_bytes()
        result = self.watch(D(2027, 4, 3))
        self.assertFalse(result.changed)
        self.assertEqual(self.path.read_bytes(), before)


class TestDryRunAndCli(WatchTestCase):
    def test_dry_run_saves_and_sends_nothing(self):
        before = self.path.read_bytes()
        result = self.watch(D(2027, 4, 2), dry_run=True)
        self.assertTrue(result.closed)
        self.assertEqual(self.path.read_bytes(), before)
        self.sender.assert_not_called()

    def test_dry_run_prints_the_reminder(self):
        self.watch(D(2027, 3, 24), dry_run=True)
        self.assertIn("Deadline in one week: ECI 2027", self.output)
        self.sender.assert_not_called()

    def test_cli_exit_codes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            ok = watch.main(["--tracker", str(self.path), "--decisions", str(self.decisions_path), "--today", "2026-10-07"])
            missing = watch.main(["--tracker", str(self.path.with_name("nope.json")), "--decisions", str(self.decisions_path)])
            self.write_decisions({ECI: "pending"})
            bad = watch.main(["--tracker", str(self.path), "--decisions", str(self.decisions_path), "--today", "2026-10-07"])
        self.assertEqual((ok, missing, bad), (0, 1, 1))
        self.assertIn("tracker.json unchanged", out.getvalue())


if __name__ == "__main__":
    unittest.main()
