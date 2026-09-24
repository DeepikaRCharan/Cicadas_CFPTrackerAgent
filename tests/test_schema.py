"""Tests for src/tracker_schema.py and src/profile_schema.py.

One valid + one invalid example per schema-fix rule from the review pass
(see docs/project-brief.md for the shapes these encode). No pipeline logic
is exercised here — these only check that the schema accepts/rejects the
shapes it's supposed to.

Run with: python -m unittest tests/test_schema.py -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pydantic import ValidationError

from profile_schema import Profile
from tracker_schema import (
    AcademicPractitionerFit,
    ConferenceEntry,
    CorporateInstitutionalFit,
    DimensionScore,
    Extraction,
    OtherAvenuesFit,
    ProfileClassification,
    ProfileMatch,
    TrackerFile,
)


def make_extraction(**overrides):
    base = dict(
        has_deadline=True,
        deadline="2026-12-15",
        submission_type="extended abstract",
        format_requirements="6 pages",
        fees="none",
        mode="virtual",
        theme="stem education",
        venue_tier=["academic_practitioner"],
        venue_tier_reasoning="peer-reviewed track",
        complete=True,
        missing_fields=[],
    )
    base.update(overrides)
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


if __name__ == "__main__":
    unittest.main()
