"""Pydantic schema for tracker.json.

Shape defined in docs/project-brief.md, "Data model > Tracker (tracker.json)".
TrackerFile is the whole file's contents: a top-level `profiles` block (one
ProfileClassification per profile id — classify's read of the paper itself,
cached), plus `conferences`, one ConferenceEntry per tracked venue with one
ProfileMatch per profile that has been scored against it, keyed by profile
id under `matches`.

This file defines shape and the data-integrity invariants the brief states
outright (deadline vs. rolling, equal-weighted overall, decision ownership,
cross-references between conferences/matches/profiles). It does not
implement extraction, classify, or judge-fit — those write into these
shapes later.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 5


class StrictModel(BaseModel):
    """Base for every model in this file: unknown fields are rejected
    outright rather than silently dropped, so a typo or a stray field from a
    bad LLM write shows up immediately instead of vanishing on the next
    round-trip through tracker.json."""

    model_config = ConfigDict(extra="forbid")


class VenueTier(str, Enum):
    ACADEMIC_PRACTITIONER = "academic_practitioner"
    INDUSTRY_TECHNICAL_MEDIA = "industry_technical_media"
    CORPORATE_INSTITUTIONAL = "corporate_institutional"
    OTHER_AVENUES = "other_avenues"


class SubmissionMode(str, Enum):
    IN_PERSON = "in-person"
    VIRTUAL = "virtual"
    HYBRID = "hybrid"


class DeadlinePrecision(str, Enum):
    EXACT = "exact"
    MONTH = "month"


class CallStatus(str, Enum):
    NOT_YET_OPEN = "not_yet_open"
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


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
    SUBMISSIONS_OPENED = "submissions_opened"
    WEEK_BEFORE = "week_before"


# Critical fields: complete=False iff one of these is null (brief, Rules >
# "Critical vs. optional fields"). deadline only counts while has_deadline is
# True — a null deadline under has_deadline=False is a fact, not a gap.
CRITICAL_FACT_FIELDS = frozenset({"deadline", "theme", "submission_type"})
# Optional fields: nullable, never affect `complete`, but still must be
# named in missing_fields when null — nothing null is ever left unlisted.
OPTIONAL_FACT_FIELDS = frozenset({"location", "mode", "fees", "format_requirements", "submission_opens"})


class ExtractionFacts(StrictModel):
    """The fact fields extraction (light model) fills. `venue_tier` and
    `venue_tier_reasoning` are classify's job, never extraction's (brief,
    Rules > "Who fills what") — see the Extraction subclass below."""

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
    deadline_precision: DeadlinePrecision | None = Field(
        None,
        description=(
            "Set iff deadline is set. 'month' means only a month was stated "
            "('February 2027'); deadline is then the 1st of that month. A "
            "later run finding the exact date upgrades this to 'exact' and "
            "logs deadline_changed."
        ),
    )
    deadline_as_stated: str | None = Field(
        None,
        description="The page's own wording for the deadline, verbatim. Set iff deadline is set.",
    )
    submission_opens: str | None = Field(
        None,
        description="The page's own wording for when submissions open, e.g. 'November 2026'. Optional field.",
    )
    call_status: CallStatus = Field(
        ...,
        description="What the page's call is doing right now. 'unknown' covers not-stated — this field is never null.",
    )
    submission_type: str | None = None
    format_requirements: str | None = None
    fees: str | None = None
    mode: SubmissionMode | None = None
    theme: str | None = None
    location: str | None = None
    complete: bool
    missing_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _deadline_rules(self) -> "ExtractionFacts":
        if not self.has_deadline:
            if self.deadline is not None or self.deadline_precision is not None or self.deadline_as_stated is not None:
                raise ValueError(
                    "has_deadline=False (rolling) but deadline/deadline_precision/"
                    "deadline_as_stated is set — all three must be null when there is no deadline"
                )
            return self

        if self.deadline is not None:
            if self.deadline_precision is None or self.deadline_as_stated is None:
                raise ValueError(
                    "deadline is set but deadline_precision/deadline_as_stated is null — "
                    "both are required whenever a deadline is present"
                )
            if self.deadline_precision == DeadlinePrecision.MONTH and self.deadline.day != 1:
                raise ValueError(
                    "deadline_precision=month requires deadline to be the 1st of that month"
                )
        else:
            # has_deadline=True but unfindable on the page — the "different
            # case" from rolling (brief, Rules): flagged incomplete below,
            # and precision/as_stated carry no meaning without a deadline value.
            if self.deadline_precision is not None or self.deadline_as_stated is not None:
                raise ValueError(
                    "deadline is null but deadline_precision/deadline_as_stated is set — "
                    "neither carries meaning without a deadline value"
                )
        return self

    @model_validator(mode="after")
    def _missing_fields_and_complete(self) -> "ExtractionFacts":
        expected_missing: set[str] = set()
        if self.theme is None:
            expected_missing.add("theme")
        if self.submission_type is None:
            expected_missing.add("submission_type")
        if self.has_deadline and self.deadline is None:
            expected_missing.add("deadline")
        for field_name in OPTIONAL_FACT_FIELDS:
            if getattr(self, field_name) is None:
                expected_missing.add(field_name)

        if set(self.missing_fields) != expected_missing:
            raise ValueError(
                f"missing_fields {sorted(self.missing_fields)} must exactly equal the null "
                f"nullable fields {sorted(expected_missing)} — a null deadline while "
                "has_deadline=False is a fact, not a gap, and is excluded"
            )

        critical_missing = expected_missing & CRITICAL_FACT_FIELDS
        expected_complete = not critical_missing
        if self.complete != expected_complete:
            raise ValueError(
                f"complete={self.complete} but critical fields missing={sorted(critical_missing)} — "
                "complete must be False iff a critical field (deadline when has_deadline, "
                "theme, submission_type) is missing; optional-only gaps don't affect it"
            )
        return self


class Extraction(ExtractionFacts):
    """ExtractionFacts plus classify's own writes. Never instantiate
    ExtractionFacts directly for a tracker entry — this is the shape
    ConferenceEntry.extraction actually holds."""

    venue_tier: list[VenueTier] = Field(
        ...,
        min_length=1,
        max_length=2,
        description="Almost always one bucket; two only when a candidate genuinely straddles buckets.",
    )
    venue_tier_reasoning: str

    @model_validator(mode="after")
    def _venue_tier_no_duplicates(self) -> "Extraction":
        if len(set(self.venue_tier)) != len(self.venue_tier):
            raise ValueError("venue_tier must not repeat the same bucket twice")
        return self


class DimensionScore(StrictModel):
    score: int = Field(..., ge=0, le=100)
    reasoning: str = Field(
        ...,
        min_length=1,
        description="Must show real qualitative reasoning — never just a restatement of the score.",
    )


class TierFitBase(StrictModel):
    """Fields every bucket's fit score carries. Never instantiated directly
    — see StandardFit and OtherAvenuesFit below.

    v5 (brief, "Venue buckets and rubric"): one dimension shape for every
    bucket instead of per-bucket dimension sets — a wrong venue_tier now
    only changes the lens judge-fit applies to kind_fit, instead of swapping
    the whole rubric."""

    overall: int = Field(
        0,
        description=(
            "Computed as the simple average of this bucket's dimension "
            "scores (brief: equal weighting, no hand-tuned weights). Always "
            "recomputed after validation — never supplied-and-checked, so "
            "any value passed in here is overwritten, not rejected. No "
            "range constraint here on purpose: the recomputed average of "
            "0-100 dimension scores is always in range by construction."
        ),
    )
    scored_at: datetime
    profile_hash: str = Field(
        ...,
        min_length=1,
        description="Profile.content_hash() at scoring time; a mismatch on the next run means the profile was revised and this entry is stale.",
    )

    @model_validator(mode="after")
    def _compute_overall(self) -> "TierFitBase":
        dims = [
            value
            for name, value in self.__dict__.items()
            if isinstance(value, DimensionScore)
        ]
        if dims:
            self.overall = round(sum(d.score for d in dims) / len(dims))
        return self


class StandardFit(TierFitBase):
    """The shared three-dimension rubric for Academic & Practitioner,
    Industry & Technical Media, and Corporate & Institutional (brief:
    "same names in every bucket" — kind_fit's lens on what "kind" means
    shifts per bucket, but the field itself doesn't)."""

    topic_fit: DimensionScore
    kind_fit: DimensionScore
    submission_fit: DimensionScore


class OtherAvenuesFit(TierFitBase):
    """Other Avenues keeps topic_fit only, plus the required
    no_other_fit_reasoning — kind_fit and submission_fit don't apply to a
    catch-all bucket with no venue-type lens to judge them against."""

    topic_fit: DimensionScore
    no_other_fit_reasoning: str = Field(
        ...,
        min_length=1,
        description="Why nothing else fit — required whenever a candidate lands in Other Avenues (brief: 'always with reasoning for why nothing else fit').",
    )
    low_confidence: bool = Field(
        True,
        description="topic_fit-only scores are a signal to glance at manually, never fully trusted.",
    )


TierFit = StandardFit | OtherAvenuesFit

# Which model a given bucket's fit_by_tier entry must validate against — the
# "validator branches on what venue_tier actually contains" rule from the
# brief, made concrete.
TIER_FIT_MODEL: dict[VenueTier, type[TierFitBase]] = {
    VenueTier.ACADEMIC_PRACTITIONER: StandardFit,
    VenueTier.INDUSTRY_TECHNICAL_MEDIA: StandardFit,
    VenueTier.CORPORATE_INSTITUTIONAL: StandardFit,
    VenueTier.OTHER_AVENUES: OtherAvenuesFit,
}


class NotificationEntry(StrictModel):
    date: date
    reason: NotificationReason
    deadline: date | None = None


class ProfileClassification(StrictModel):
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


class ProfileMatch(StrictModel):
    """Everything about fit and the researcher's relationship to one
    conference, for one profile. Keyed by profile id under
    ConferenceEntry.matches — never merged across profiles."""

    fit_by_tier: dict[VenueTier, TierFit] = Field(
        default_factory=dict,
        max_length=2,
        description=(
            "One key per bucket assigned by classify; scored independently, "
            "never averaged across buckets. Empty only when the venue's "
            "extraction is incomplete — see ConferenceEntry's validators, "
            "which also require these keys to equal extraction.venue_tier "
            "whenever this is non-empty."
        ),
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


class ConferenceEntry(StrictModel):
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

    @model_validator(mode="after")
    def _fit_by_tier_keys_match_venue_tier(self) -> "ConferenceEntry":
        declared = set(self.extraction.venue_tier)
        for profile_id, match in self.matches.items():
            keys = set(match.fit_by_tier.keys())
            if keys and keys != declared:
                raise ValueError(
                    f"matches[{profile_id!r}].fit_by_tier keys "
                    f"{sorted(k.value for k in keys)} must equal "
                    f"extraction.venue_tier {sorted(k.value for k in declared)}"
                )
        return self

    @model_validator(mode="after")
    def _empty_fit_by_tier_only_when_incomplete(self) -> "ConferenceEntry":
        for profile_id, match in self.matches.items():
            if not match.fit_by_tier and self.extraction.complete:
                raise ValueError(
                    f"matches[{profile_id!r}].fit_by_tier is empty but "
                    "extraction.complete=True — empty fit_by_tier is only valid "
                    "when the venue's extraction is incomplete (flagged, not scored)"
                )
        return self


class TrackerFile(StrictModel):
    schema_version: int = SCHEMA_VERSION
    last_run: datetime | None = Field(
        None,
        description="Null before the first run has ever happened, so a brand-new, empty tracker.json validates.",
    )
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

    @model_validator(mode="after")
    def _match_profile_ids_exist_in_profiles(self) -> "TrackerFile":
        for conference in self.conferences:
            for profile_id in conference.matches:
                if profile_id not in self.profiles:
                    raise ValueError(
                        f"conferences[{conference.id!r}].matches has key {profile_id!r} "
                        "which has no corresponding entry in the top-level profiles block"
                    )
        return self
