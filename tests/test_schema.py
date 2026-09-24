"""Tests for src/tracker_schema.py, src/profile_schema.py, and
src/decisions_schema.py.

One valid + one invalid example per schema rule (see docs/project-brief.md
for the shapes these encode). No pipeline logic is exercised here — these
only check that the schema accepts/rejects the shapes it's supposed to.

Run with: python -m unittest tests/test_schema.py -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pydantic import ValidationError

from decisions_schema import DecisionsFile, ResearcherDecision, find_untracked_ids
from profile_schema import Profile
from tracker_schema import (
    SCHEMA_VERSION,
    AcademicPractitionerFit,
    ConferenceEntry,
    CorporateInstitutionalFit,
    DimensionScore,
    Extraction,
    ExtractionFacts,
    NotificationEntry,
    NotificationReason,
    OtherAvenuesFit,
    ProfileClassification,
    ProfileMatch,
    TrackerFile,
)


def make_extraction(**overrides):
    base = dict(
        has_deadline=True,
        deadline="2026-12-15",
        deadline_precision="exact",
        deadline_as_stated="15 December 2026",
        submission_opens="November 2026",
        call_status="closed",
        submission_type="extended abstract",
        format_requirements="6 pages",
        fees="none",
        mode="virtual",
        theme="stem education",
        location="Florence, Italy",
        venue_tier=["academic_practitioner"],
        venue_tier_reasoning="peer-reviewed track",
        complete=True,
        missing_fields=[],
    )
    base.update(overrides)
    return base


def make_extraction_facts(**overrides):
    base = make_extraction(**overrides)
    base.pop("venue_tier", None)
    base.pop("venue_tier_reasoning", None)
    return base


def make_academic_fit(**overrides):
    base = dict(
        theme_alignment={"score": 80, "reasoning": "on-theme"},
        nature_fit={"score": 60, "reasoning": "research-rigor framing"},
        logistical_feasibility={"score": 70, "reasoning": "ample runway"},
        scored_at="2026-10-01T09:00:00Z",
        profile_hash="h1",
    )
    base.update(overrides)
    return base


def make_corporate_fit(**overrides):
    base = dict(
        theme_alignment={"score": 80, "reasoning": "on-theme"},
        audience_fit={"score": 65, "reasoning": "policymaker-heavy"},
        logistical_feasibility={"score": 60, "reasoning": "invite-only"},
        scored_at="2026-10-01T09:00:00Z",
        profile_hash="h1",
    )
    base.update(overrides)
    return base


def make_profile_classification(**overrides):
    base = dict(
        paper_bucket=["academic_practitioner"],
        paper_bucket_cues=["cue one", "cue two", "cue three"],
        paper_bucket_reasoning="reads as a peer-reviewed research paper",
        profile_hash="h1",
        classified_at="2026-10-01T09:00:00Z",
        snapshot_sent=None,
    )
    base.update(overrides)
    return base


class TestFitByTierMatchesVenueTier(unittest.TestCase):
    """Rule 1: every match's fit_by_tier keys must equal extraction.venue_tier."""

    def test_valid_keys_match_declared_tier(self):
        ConferenceEntry(
            id="c1",
            name="Conf",
            url="https://example.org",
            first_seen="2026-10-01T09:00:00Z",
            last_checked="2026-10-01T09:00:00Z",
            extraction=make_extraction(venue_tier=["academic_practitioner"]),
            matches={
                "p1": ProfileMatch(
                    fit_by_tier={"academic_practitioner": make_academic_fit()},
                )
            },
        )

    def test_invalid_keys_dont_match_declared_tier(self):
        with self.assertRaises(ValidationError):
            ConferenceEntry(
                id="c1",
                name="Conf",
                url="https://example.org",
                first_seen="2026-10-01T09:00:00Z",
                last_checked="2026-10-01T09:00:00Z",
                extraction=make_extraction(venue_tier=["academic_practitioner"]),
                matches={
                    "p1": ProfileMatch(
                        # scored under corporate_institutional, but the venue
                        # was only ever classified as academic_practitioner
                        fit_by_tier={"corporate_institutional": make_corporate_fit()},
                    )
                },
            )


class TestExtraForbid(unittest.TestCase):
    """Rule 2: extra="forbid" on every model in both schema files."""

    def test_valid_no_extra_fields(self):
        DimensionScore(score=80, reasoning="fine")
        Profile(id="x", title="t", abstract="a", keywords=["k"], preferred_format="talk")

    def test_invalid_extra_field_rejected(self):
        with self.assertRaises(ValidationError):
            DimensionScore(score=80, reasoning="fine", bogus_field="nope")
        with self.assertRaises(ValidationError):
            Profile(
                id="x",
                title="t",
                abstract="a",
                keywords=["k"],
                preferred_format="talk",
                bogus_field="nope",
            )


class TestOverallComputed(unittest.TestCase):
    """Rule 3: overall computed in code from dimension scores, not
    supplied-then-checked."""

    def test_valid_overall_computed_from_dimensions(self):
        fit = AcademicPractitionerFit(**make_academic_fit())
        # (80 + 60 + 70) / 3 = 70
        self.assertEqual(fit.overall, 70)

    def test_previously_invalid_now_silently_corrected(self):
        # Under the old rule this raised (overall didn't match the average).
        # Now it must NOT raise — the supplied value is simply overwritten.
        fit = AcademicPractitionerFit(**make_academic_fit(overall=1))
        self.assertNotEqual(fit.overall, 1)
        self.assertEqual(fit.overall, 70)


class TestEmptyFitByTierOnlyWhenIncomplete(unittest.TestCase):
    """Rule 4: fit_by_tier may be empty only when extraction.complete is False."""

    def test_valid_empty_fit_by_tier_when_incomplete(self):
        ConferenceEntry(
            id="c1",
            name="Conf",
            url="https://example.org",
            first_seen="2026-10-01T09:00:00Z",
            last_checked="2026-10-01T09:00:00Z",
            extraction=make_extraction(
                has_deadline=True,
                deadline=None,
                deadline_precision=None,
                deadline_as_stated=None,
                complete=False,
                missing_fields=["deadline"],
            ),
            matches={"p1": ProfileMatch(fit_by_tier={})},
        )

    def test_invalid_empty_fit_by_tier_when_complete(self):
        with self.assertRaises(ValidationError):
            ConferenceEntry(
                id="c1",
                name="Conf",
                url="https://example.org",
                first_seen="2026-10-01T09:00:00Z",
                last_checked="2026-10-01T09:00:00Z",
                extraction=make_extraction(complete=True, missing_fields=[]),
                matches={"p1": ProfileMatch(fit_by_tier={})},
            )


class TestLastRunNullable(unittest.TestCase):
    """Rule 5: TrackerFile.last_run nullable, so an empty tracker validates."""

    def test_valid_empty_tracker(self):
        tf = TrackerFile()
        self.assertIsNone(tf.last_run)
        self.assertEqual(tf.conferences, [])

    def test_invalid_last_run_wrong_type(self):
        with self.assertRaises(ValidationError):
            TrackerFile(last_run="not-a-date")


class TestMatchProfileIdsExistInProfiles(unittest.TestCase):
    """Rule 6: every profile id under matches must exist in profiles."""

    def _conference(self):
        return ConferenceEntry(
            id="c1",
            name="Conf",
            url="https://example.org",
            first_seen="2026-10-01T09:00:00Z",
            last_checked="2026-10-01T09:00:00Z",
            extraction=make_extraction(venue_tier=["academic_practitioner"]),
            matches={
                "p1": ProfileMatch(
                    fit_by_tier={"academic_practitioner": make_academic_fit()},
                )
            },
        )

    def test_valid_profile_id_present(self):
        TrackerFile(
            profiles={"p1": ProfileClassification(**make_profile_classification())},
            conferences=[self._conference()],
        )

    def test_invalid_profile_id_missing(self):
        with self.assertRaises(ValidationError):
            TrackerFile(
                profiles={},  # "p1" referenced by the match below doesn't exist
                conferences=[self._conference()],
            )


class TestCompleteRequiresMissingFields(unittest.TestCase):
    """Rule 7: complete=False requires non-empty missing_fields."""

    def test_valid_incomplete_with_missing_fields(self):
        Extraction(**make_extraction(
            has_deadline=True,
            deadline=None,
            deadline_precision=None,
            deadline_as_stated=None,
            complete=False,
            missing_fields=["deadline"],
        ))

    def test_invalid_incomplete_without_missing_fields(self):
        with self.assertRaises(ValidationError):
            Extraction(**make_extraction(complete=False, missing_fields=[]))


class TestOtherAvenuesRequiresReasoning(unittest.TestCase):
    """Rule 8: OtherAvenuesFit requires no_other_fit_reasoning."""

    def test_valid_with_reasoning(self):
        OtherAvenuesFit(
            theme_alignment={"score": 40, "reasoning": "loose thematic overlap only"},
            no_other_fit_reasoning="doesn't fit any of the other three buckets",
            scored_at="2026-10-01T09:00:00Z",
            profile_hash="h1",
        )

    def test_invalid_without_reasoning(self):
        with self.assertRaises(ValidationError):
            OtherAvenuesFit(
                theme_alignment={"score": 40, "reasoning": "loose thematic overlap only"},
                scored_at="2026-10-01T09:00:00Z",
                profile_hash="h1",
            )


class TestExtractionFactsSplit(unittest.TestCase):
    """Rule 9 (v4): ExtractionFacts holds the fact fields extraction fills;
    venue_tier/venue_tier_reasoning belong only to Extraction (classify's
    write), not ExtractionFacts."""

    def test_valid_extraction_facts_without_venue_tier(self):
        ExtractionFacts(**make_extraction_facts())

    def test_invalid_extraction_facts_rejects_venue_tier(self):
        with self.assertRaises(ValidationError):
            ExtractionFacts(**make_extraction())  # still carries venue_tier/venue_tier_reasoning


class TestDeadlinePrecisionRequiredWithDeadline(unittest.TestCase):
    """Rule 10 (v4): a present deadline requires deadline_precision and
    deadline_as_stated."""

    def test_valid_deadline_with_precision_and_as_stated(self):
        Extraction(**make_extraction(
            deadline="2026-12-15",
            deadline_precision="exact",
            deadline_as_stated="15 December 2026",
        ))

    def test_invalid_deadline_without_precision(self):
        with self.assertRaises(ValidationError):
            Extraction(**make_extraction(
                deadline="2026-12-15",
                deadline_precision=None,
                deadline_as_stated="15 December 2026",
            ))


class TestMonthPrecisionFirstOfMonth(unittest.TestCase):
    """Rule 11 (v4): month-precision deadline must be stored as the 1st of
    that month."""

    def test_valid_month_precision_is_first_of_month(self):
        Extraction(**make_extraction(
            deadline="2027-02-01",
            deadline_precision="month",
            deadline_as_stated="February 2027",
        ))

    def test_invalid_month_precision_not_first_of_month(self):
        with self.assertRaises(ValidationError):
            Extraction(**make_extraction(
                deadline="2027-02-15",
                deadline_precision="month",
                deadline_as_stated="February 2027",
            ))


class TestHasDeadlineFalseAllNull(unittest.TestCase):
    """Rule 12 (v4): has_deadline=False requires deadline, deadline_precision,
    and deadline_as_stated all null."""

    def test_valid_rolling_venue_all_deadline_fields_null(self):
        Extraction(**make_extraction(
            has_deadline=False,
            deadline=None,
            deadline_precision=None,
            deadline_as_stated=None,
        ))

    def test_invalid_rolling_venue_with_deadline_as_stated_set(self):
        with self.assertRaises(ValidationError):
            Extraction(**make_extraction(
                has_deadline=False,
                deadline=None,
                deadline_precision=None,
                deadline_as_stated="rolling submissions",
            ))


class TestMissingFieldsExactlyMatchNullFields(unittest.TestCase):
    """Rule 13 (v4): every null nullable field (critical or optional) must
    be listed in missing_fields, and nothing else."""

    def test_valid_null_optional_field_listed(self):
        Extraction(**make_extraction(
            location=None,
            complete=True,
            missing_fields=["location"],
        ))

    def test_invalid_null_optional_field_not_listed(self):
        with self.assertRaises(ValidationError):
            Extraction(**make_extraction(
                location=None,
                complete=True,
                missing_fields=[],
            ))


class TestCompleteOnlyFalseForCriticalGap(unittest.TestCase):
    """Rule 14 (v4): complete is False iff a critical field (deadline when
    has_deadline, theme, submission_type) is missing — an optional-only gap
    still leaves complete=True."""

    def test_valid_optional_only_gap_still_complete(self):
        Extraction(**make_extraction(
            location=None,
            mode=None,
            complete=True,
            missing_fields=["location", "mode"],
        ))

    def test_invalid_critical_gap_marked_complete(self):
        with self.assertRaises(ValidationError):
            Extraction(**make_extraction(
                theme=None,
                complete=True,
                missing_fields=["theme"],
            ))


class TestSubmissionsOpenedNotificationReason(unittest.TestCase):
    """Rule 15 (v4): NotificationReason gains submissions_opened."""

    def test_valid_submissions_opened_reason(self):
        entry = NotificationEntry(date="2026-11-01", reason="submissions_opened", deadline=None)
        self.assertEqual(entry.reason, NotificationReason.SUBMISSIONS_OPENED)

    def test_invalid_unknown_reason(self):
        with self.assertRaises(ValidationError):
            NotificationEntry(date="2026-11-01", reason="bogus_reason", deadline=None)


class TestSchemaVersionV4(unittest.TestCase):
    """Rule 16 (v4): schema bumped to version 4; a stale version 3 file must
    not silently validate."""

    def test_valid_schema_version_is_4(self):
        self.assertEqual(SCHEMA_VERSION, 4)
        tf = TrackerFile()
        self.assertEqual(tf.schema_version, 4)

    def test_invalid_schema_version_3_rejected(self):
        with self.assertRaises(ValidationError):
            TrackerFile(schema_version=3)


class TestDecisionsSchema(unittest.TestCase):
    """Rule 17 (v4): decisions.json is id -> applying | applied | skipped
    only; pending/unconfirmed (agent-only states) are an error here."""

    def test_valid_decisions_file(self):
        decisions = DecisionsFile.model_validate(
            {"eci-2027": "applying", "example-summit-2027": "skipped"}
        )
        self.assertEqual(decisions.root["eci-2027"], ResearcherDecision.APPLYING)

    def test_invalid_agent_only_state_rejected(self):
        with self.assertRaises(ValidationError):
            DecisionsFile.model_validate({"eci-2027": "pending"})


class TestFindUntrackedIds(unittest.TestCase):
    """Rule 18 (v4): the decisions.json check function reports every key
    that isn't a tracked conference id."""

    def test_valid_all_ids_tracked(self):
        decisions = DecisionsFile.model_validate({"eci-2027": "applying"}).root
        self.assertEqual(find_untracked_ids(decisions, {"eci-2027", "other-2027"}), [])

    def test_invalid_id_not_tracked_is_reported(self):
        decisions = DecisionsFile.model_validate(
            {"eci-2027": "applying", "typo-conf-2027": "skipped"}
        ).root
        self.assertEqual(find_untracked_ids(decisions, {"eci-2027"}), ["typo-conf-2027"])


if __name__ == "__main__":
    unittest.main()
