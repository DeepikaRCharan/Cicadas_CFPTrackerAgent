"""Tests for src/render.py -- build step 6: the email renderer.

Runs against the fixture in tests/render_fixtures.py (three monthly runs
plus a reminder, built through the real tracker functions). No API calls.
To read the rendered emails themselves: python -m tests.render_fixtures

Run with: python -m unittest tests/test_render.py -v
"""

import contextlib
import copy
import datetime
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.render import NOT_STATED, render_digest
from src.tracker import mark_sent, record_paper_classification, upsert_venue
from src.tracker_schema import NotificationReason, TrackerFile
from tests import render_fixtures as fx

EMAILS = fx.build()
FIRST, SECOND, QUIET, REMINDER = EMAILS.values()
FINAL_TRACKER = fx.build.tracker  # captured now: later fixture builds (test_notify) replace it


def section(body: str, title: str) -> str:
    """The text of one digest section, between its header and the next."""
    parts = body.split("\n" + "=" * 76 + "\n")  # [preamble, TITLE, content, TITLE, content, ...]
    return parts[[p.strip() for p in parts].index(title.upper()) + 1]


class TestDigestLayout(unittest.TestCase):
    def test_sections_in_brief_order_every_month(self):
        for email in (FIRST, SECOND, QUIET):
            positions = [email.body.index(f"\n{t}\n") for t in
                         ("NEW THIS MONTH", "TIER ALERT", "UPDATES", "STILL OPEN", "NEEDS A LOOK")]
            self.assertEqual(positions, sorted(positions))

    def test_snapshot_comes_first_on_first_run_only(self):
        self.assertEqual(FIRST.body.count("FIRST LOOK:"), 2)
        self.assertLess(FIRST.body.index("FIRST LOOK:"), FIRST.body.index("NEW THIS MONTH"))
        self.assertEqual(FIRST.body.count("Cicadas will mainly look for"), 1)
        self.assertEqual(FIRST.body.count("Each conference below is scored per dimension"), 1)
        self.assertEqual(FIRST.body.count("Main search:   Academic & Practitioner"), 2)
        self.assertLess(FIRST.body.rindex("FIRST LOOK:"), FIRST.body.index("Cicadas will mainly look for"))
        self.assertEqual(sorted(FIRST.receipt.snapshot_profile_ids), [fx.ELEPHANT, fx.IMMUNE])
        self.assertNotIn("FIRST LOOK:", SECOND.body)
        self.assertEqual(SECOND.receipt.snapshot_profile_ids, [])

    def test_no_raw_nulls_or_enum_values_leak(self):
        for email in EMAILS.values():
            for token in ("None\n", ": None", "null", "not_yet_open", "academic_practitioner", "members_only"):
                self.assertNotIn(token, email.body)


class TestFieldRendering(unittest.TestCase):
    def test_missing_fields_say_not_stated(self):
        eci = section(FIRST.body, "New this month").split("* Mongabay")[0]
        self.assertIn(f"Mode:          {NOT_STATED}", eci)
        self.assertIn(f"Fees:          {NOT_STATED}", eci)

    def test_rolling_venue_shows_rolling(self):
        mongabay = section(FIRST.body, "New this month").split("* Mongabay")[1]
        self.assertIn("Deadline:      Rolling", mongabay)
        self.assertIn(f"Location:      {NOT_STATED}", mongabay)
        self.assertIn("Rolling", section(QUIET.body, "Still open"))

    def test_call_column_wording(self):
        self.assertIn("Call:          Opens November 2026", FIRST.body)
        self.assertIn("Call:          Open now", FIRST.body)

    def test_month_only_deadline_labelled(self):
        self.assertIn("April 2027 (exact date not published)", SECOND.body)

    def test_per_dimension_scores_with_reasoning(self):
        eci = section(FIRST.body, "New this month").split("* Mongabay")[0]
        for line in ("fitness 82/100", "Theme fit 96/100", "Genre fit 85/100", "Format fit 65/100",
                     "abstract, which is feasible"):
            self.assertIn(line, eci)


class TestSelection(unittest.TestCase):
    def test_tier_alert_names_lowest_dimension(self):
        alert = section(SECOND.body, "Tier Alert")
        self.assertIn("Africa Wildlife Corridors Policy Forum 2027", alert)
        self.assertIn("theme fit 90/100, overall 47/100", alert)
        self.assertIn("What pulled it down: Format fit 20/100", alert)
        self.assertNotIn("Policy Forum", section(SECOND.body, "New this month"))

    def test_update_shows_change_and_decision(self):
        updates = section(SECOND.body, "Updates")
        self.assertIn("What changed:  Applications now open", updates)
        self.assertIn("Your decision: Applying", updates)

    def test_conflict_appears_once(self):
        self.assertIn("https://easychair.org/cfp/ECI2027", section(SECOND.body, "Needs a look"))
        self.assertEqual(len(SECOND.receipt.conflicts), 1)
        # re-flagged in run 3, but already reported
        self.assertIn("Nothing to check.", section(QUIET.body, "Needs a look"))
        self.assertTrue(FINAL_TRACKER.identity_conflicts[0].reported)

    def test_still_open_rule_and_order(self):
        still = section(QUIET.body, "Still open")
        names = [line[2:] for line in still.splitlines() if line.startswith("* ")]
        # sorted by deadline, rolling last; the below-threshold tier alert and
        # the below-threshold elephant x ECI match are not listed
        self.assertEqual(names, ["ECI 2027", "Kyoto Treg Biology Symposium 2027", "Mongabay contributor pitches"])

    def test_still_open_drops_passed_deadline(self):
        after = render_digest(FINAL_TRACKER, fx.load_profiles(), datetime.date(2027, 4, 2))
        self.assertNotIn("ECI 2027", section(after.body, "Still open"))  # 31 March passed
        self.assertIn("Kyoto", section(after.body, "Still open"))  # month-only: April not over yet

    def test_frozen_profile_not_in_still_open_or_snapshot(self):
        live = {fx.IMMUNE: fx.load_profiles()[fx.IMMUNE]}
        email = render_digest(FINAL_TRACKER, live, datetime.date(2026, 12, 7))
        self.assertNotIn("Mongabay", section(email.body, "Still open"))


class TestQuietMonth(unittest.TestCase):
    def test_quiet_month_says_so_and_lists_still_open(self):
        self.assertIn("Nothing new this month.", QUIET.body)
        for title in ("New this month", "Tier Alert", "Updates"):
            self.assertIn("None this month.", section(QUIET.body, title))
        self.assertIn("ECI 2027", section(QUIET.body, "Still open"))
        self.assertEqual(QUIET.receipt.notifications, [])

    def test_busy_month_has_no_quiet_line(self):
        self.assertNotIn("Nothing new this month.", SECOND.body)


class TestReceipt(unittest.TestCase):
    def test_receipt_lists_exactly_what_was_shown(self):
        reasons = sorted(r.entry.reason.value for r in SECOND.receipt.notifications)
        self.assertEqual(reasons, ["new_fit", "submissions_opened", "tier_alert"])
        self.assertEqual(
            sorted(r.entry.reason.value for r in FIRST.receipt.notifications), ["new_fit", "new_fit"]
        )

    def test_failed_send_leaves_everything_for_next_digest(self):
        tracker = TrackerFile()
        profiles = fx.load_profiles()
        now = fx.RUN_1
        with contextlib.redirect_stdout(io.StringIO()):
            record_paper_classification(tracker, fx.IMMUNE, fx._classification(
                profiles[fx.IMMUNE], fx.VenueTier.ACADEMIC_PRACTITIONER, ["a", "b", "c"], "r"))
            upsert_venue(tracker, name="ECI 2027", year=2027, url="https://eci2027.org/call-for-abstracts/",
                         extraction=fx.eci_extraction(), profile_id=fx.IMMUNE, live_profile_ids={fx.IMMUNE},
                         now=now, fit_by_tier=fx._fit(profiles[fx.IMMUNE], (90, "t"), (80, "g"), (70, "f")))
        live = {fx.IMMUNE: profiles[fx.IMMUNE]}
        first = render_digest(tracker, live, now.date())
        # send never happens: no mark_sent
        retry = render_digest(tracker, live, now.date())
        self.assertEqual(retry.body, first.body)
        self.assertIn("FIRST LOOK:", retry.body)
        mark_sent(tracker, retry.receipt, now.date())
        after = render_digest(tracker, live, now.date())
        self.assertNotIn("FIRST LOOK:", after.body)
        self.assertIn("Nothing new this month.", after.body)

    def test_reminder_receipt_carries_week_before_only(self):
        self.assertEqual([r.entry.reason for r in REMINDER.receipt.notifications], [NotificationReason.WEEK_BEFORE])
        self.assertEqual(REMINDER.receipt.conflicts, [])


class TestPurity(unittest.TestCase):
    def test_render_is_deterministic_and_does_not_mutate(self):
        tracker = FINAL_TRACKER
        before = copy.deepcopy(tracker.model_dump())
        a = render_digest(tracker, fx.load_profiles(), datetime.date(2026, 12, 7))
        b = render_digest(tracker, fx.load_profiles(), datetime.date(2026, 12, 7))
        self.assertEqual(a.body, b.body)
        self.assertEqual(tracker.model_dump(), before)


class TestWeekBefore(unittest.TestCase):
    def test_reminder_content(self):
        for line in ("Deadline in one week: ECI 2027", "Location:      Florence, Italy",
                     "Deadline:      31 March 2027", "ID:            eci-2027", "Your decision: Applying"):
            self.assertIn(line, REMINDER.body)

    def test_also_coming_up_within_14_days(self):
        also = REMINDER.body.split("Also coming up")[1]
        self.assertIn("Kyoto Treg Biology Symposium 2027", also)
        self.assertIn("exact date not published", also)
        self.assertNotIn("Mongabay", also)  # rolling: no deadline
        self.assertNotIn("Policy Forum", also)  # deadline passed, and below threshold


if __name__ == "__main__":
    unittest.main()
