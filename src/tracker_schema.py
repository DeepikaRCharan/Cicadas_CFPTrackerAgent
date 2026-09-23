"""Pydantic schema for tracker.json.

Shape defined in docs/project-brief.md, "Data model > Tracker (tracker.json)".
TrackerFile is the whole file's contents: a top-level `profiles` block (one
ProfileClassification per profile id — classify's read of the paper itself,
cached), plus `conferences`, one ConferenceEntry per tracked venue with one
ProfileMatch per profile that has been scored against it, keyed by profile
id under `matches`.

This file defines shape and the data-integrity invariants the brief states
outright (deadline vs. rolling, equal-weighted overall, decision ownership).
It does not implement extraction, classify, or judge-fit — those write into
these shapes later.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = 3


class VenueTier(str, Enum):
    ACADEMIC_PRACTITIONER = "academic_practitioner"
    INDUSTRY_TECHNICAL_MEDIA = "industry_technical_media"
    CORPORATE_INSTITUTIONAL = "corporate_institutional"
    OTHER_AVENUES = "other_avenues"


class SubmissionMode(str, Enum):
    IN_PERSON = "in-person"
    VIRTUAL = "virtual"
    HYBRID = "hybrid"


class DecisionState(str, Enum):
    """Decision states table (brief, Data model > Tracker > Rules).

    | Decision    | Set by     | Meaning                                   | Reminders |
    |-------------|------------|--------------------------------------------|-----------|
    | pending     | agent      | Found and scored; no response yet         | Yes       |
    | applying    | researcher | Plans to apply                            | Yes       |
    | applied     | researcher | Closed                                    | No        |
    | skipped     | researcher | Closed, decided against                   | No        |
    | unconfirmed | agent      | Closed: deadline passed, no confirmation  | No        |

    Never set by both agent and researcher for the same field. The agent's
    only status write, ever, is the pending -> unconfirmed closure.
    """

    PENDING = "pending"
    APPLYING = "applying"
    APPLIED = "applied"
    SKIPPED = "skipped"
    UNCONFIRMED = "unconfirmed"


# Reference constants mirroring the table above — for later ownership checks
# in the loader/writer, not enforced here (a single field can't validate
# "who called this setter").
AGENT_SET_DECISIONS = frozenset({DecisionState.PENDING, DecisionState.UNCONFIRMED})
RESEARCHER_SET_DECISIONS = frozenset(
    {DecisionState.APPLYING, DecisionState.APPLIED, DecisionState.SKIPPED}
)
CLOSED_DECISIONS = frozenset(
    {DecisionState.APPLIED, DecisionState.SKIPPED, DecisionState.UNCONFIRMED}
)
REMINDER_ELIGIBLE_DECISIONS = frozenset({DecisionState.PENDING, DecisionState.APPLYING})


class NotificationReason(str, Enum):
    NEW_FIT = "new_fit"
    DEADLINE_CHANGED = "deadline_changed"
    WEEK_BEFORE = "week_before"


class Extraction(BaseModel):
    has_deadline: bool
    deadline: date | None = Field(
        None,
        description=(
            "Null for two different reasons that must never be conflated: "
            "(1) has_deadline=False, a genuinely rolling/pitch-based outlet — "
            "real information, not a gap; (2) has_deadline=True but the date "
            "wasn't findable on the page — must also set complete=False and "
            "list 'deadline' in missing_fields."
        ),
    )
    submission_type: str
    format_requirements: str
    fees: str
    mode: SubmissionMode
    theme: str
    venue_tier: list[VenueTier] = Field(
        ...,
        min_length=1,
        max_length=2,
        description="Almost always one bucket; two only when a candidate genuinely straddles buckets.",
    )
    venue_tier_reasoning: str
    complete: bool
    missing_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _venue_tier_no_duplicates(self) -> "Extraction":
        if len(set(self.venue_tier)) != len(self.venue_tier):
            raise ValueError("venue_tier must not repeat the same bucket twice")
        return self

    @model_validator(mode="after")
    def _missing_fields_imply_incomplete(self) -> "Extraction":
        if self.missing_fields and self.complete:
            raise ValueError("complete=True but missing_fields is non-empty")
        return self

    @model_validator(mode="after")
    def _deadline_rolling_vs_unfindable(self) -> "Extraction":
        if not self.has_deadline and self.deadline is not None:
            raise ValueError(
                "has_deadline=False (rolling) but deadline is set — these are mutually exclusive"
            )
        if self.has_deadline and self.deadline is None:
            if self.complete or "deadline" not in self.missing_fields:
                raise ValueError(
                    "has_deadline=True with no deadline value must be flagged incomplete "
                    "(complete=False, 'deadline' in missing_fields) — rolling and "
                    "unfindable-deadline are different cases, never conflate them"
                )
        return self


class DimensionScore(BaseModel):
    score: int = Field(..., ge=0, le=100)
    reasoning: str = Field(
        ...,
        min_length=1,
        description="Must show real qualitative reasoning — never just a restatement of the score.",
    )


class TierFitBase(BaseModel):
    """Fields every bucket's fit score carries, regardless of which extra
    dimensions that bucket adds. Never instantiated directly — see the four
    bucket-specific subclasses below."""

    theme_alignment: DimensionScore
    overall: int = Field(..., ge=0, le=100)
    scored_at: datetime
    profile_hash: str = Field(
        ...,
        min_length=1,
        description="Profile.content_hash() at scoring time; a mismatch on the next run means the profile was revised and this entry is stale.",
    )

    @model_validator(mode="after")
    def _overall_is_simple_average(self) -> "TierFitBase":
        dims = [
            value
            for name, value in self.__dict__.items()
            if isinstance(value, DimensionScore)
        ]
        if dims:
            expected = round(sum(d.score for d in dims) / len(dims))
            if self.overall != expected:
                raise ValueError(
                    f"overall={self.overall} does not match the simple average of "
                    f"this bucket's dimension scores ({expected}) — brief requires "
                    "equal weighting, no hand-tuned weights"
                )
        return self


class AcademicPractitionerFit(TierFitBase):
    nature_fit: DimensionScore
    logistical_feasibility: DimensionScore


class IndustryTechnicalMediaFit(TierFitBase):
    voice_register_match: DimensionScore
    timeliness: DimensionScore
    submission_model: DimensionScore


class CorporateInstitutionalFit(TierFitBase):
    audience_fit: DimensionScore
    logistical_feasibility: DimensionScore


class OtherAvenuesFit(TierFitBase):
    low_confidence: bool = Field(
        True,
        description="Theme-alignment-only scores are a signal to glance at manually, never fully trusted.",
    )


TierFit = (
    AcademicPractitionerFit | IndustryTechnicalMediaFit | CorporateInstitutionalFit | OtherAvenuesFit
)

# Which model a given bucket's fit_by_tier entry must validate against — the
# "validator branches on what venue_tier actually contains" rule from the
# brief, made concrete.
TIER_FIT_MODEL: dict[VenueTier, type[TierFitBase]] = {
    VenueTier.ACADEMIC_PRACTITIONER: AcademicPractitionerFit,
    VenueTier.INDUSTRY_TECHNICAL_MEDIA: IndustryTechnicalMediaFit,
    VenueTier.CORPORATE_INSTITUTIONAL: CorporateInstitutionalFit,
    VenueTier.OTHER_AVENUES: OtherAvenuesFit,
}


class NotificationEntry(BaseModel):
    date: date
    reason: NotificationReason
    deadline: date | None = None


class ProfileClassification(BaseModel):
    """Classify's read of the paper itself — the top-level `profiles` block,
    keyed by profile id. The cache for "read the paper once per profile"
    (brief, Architecture decisions > Model tiering) and what the first-run
    snapshot email renders from (brief, Workflow > First run per profile).

    A changed `profile_hash` (the profile was revised) means this entry gets
    re-classified on the next run, but `snapshot_sent` is untouched — a
    revision never re-triggers the one-time snapshot. A swapped-out profile's
    entry stays here, frozen, exactly like its tracker conference entries.
    """

    paper_bucket: list[VenueTier] = Field(
        ...,
        min_length=1,
        max_length=2,
        description="Bucket(s) classify assigned to the paper itself, not a venue.",
    )
    paper_bucket_cues: list[str] = Field(
        ...,
        min_length=3,
        max_length=4,
        description="3-4 textual cues that drove the bucket call — rendered verbatim in the first-run snapshot.",
    )
    paper_bucket_reasoning: str = Field(..., min_length=1)
    profile_hash: str = Field(
        ...,
        min_length=1,
        description="Profile.content_hash() at classification time; a mismatch means the profile was revised and needs re-classifying.",
    )
    classified_at: datetime
    snapshot_sent: date | None = Field(
        None,
        description="Null until the one-time first-run snapshot has gone out for this profile; never reset by a later revision.",
    )

    @model_validator(mode="after")
    def _paper_bucket_no_duplicates(self) -> "ProfileClassification":
        if len(set(self.paper_bucket)) != len(self.paper_bucket):
            raise ValueError("paper_bucket must not repeat the same bucket twice")
        return self


class ProfileMatch(BaseModel):
    """Everything about fit and the researcher's relationship to one
    conference, for one profile. Keyed by profile id under
    ConferenceEntry.matches — never merged across profiles."""

    fit_by_tier: dict[VenueTier, TierFit] = Field(
        ...,
        min_length=1,
        max_length=2,
        description="One key per bucket assigned by classify; scored independently, never averaged across buckets.",
    )
    decision: DecisionState = DecisionState.PENDING
    notifications: list[NotificationEntry] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _fit_matches_declared_tier(self) -> "ProfileMatch":
        for tier, fit in self.fit_by_tier.items():
            expected_model = TIER_FIT_MODEL[tier]
            if type(fit) is not expected_model:
                raise ValueError(
                    f"fit_by_tier['{tier.value}'] must be a {expected_model.__name__} "
                    f"(that bucket's own dimensions), got {type(fit).__name__}"
                )
        return self


class ConferenceEntry(BaseModel):
    id: str = Field(
        ...,
        min_length=1,
        description="Normalized name + year. Identity also checked against url — see brief, Rules > Identity.",
    )
    name: str
    url: str
    first_seen: datetime
    last_checked: datetime
    extraction: Extraction
    matches: dict[str, ProfileMatch] = Field(
        default_factory=dict,
        description="Keyed by profile id, not conference — a candidate found under two profiles' searches gets two match entries here, still one ConferenceEntry.",
    )


class TrackerFile(BaseModel):
    schema_version: int = SCHEMA_VERSION
    last_run: datetime
    profiles: dict[str, ProfileClassification] = Field(
        default_factory=dict,
        description="Keyed by profile id. One entry per profile ever onboarded, including swapped-out ones (frozen, not removed).",
    )
    conferences: list[ConferenceEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _pin_schema_version(self) -> "TrackerFile":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version {self.schema_version} != expected {SCHEMA_VERSION}; migrate before writing"
            )
        return self
