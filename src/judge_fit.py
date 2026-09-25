"""Judge fit: how well does a paper fit a venue, under a given bucket.

Build step 4 (docs/project-brief.md, "Build order"). Two LLM calls (judging
model) per (profile, venue) pair, structurally separated so a wrong
venue_tier can only ever move genre_fit:

- **Tier-blind call** (once per venue, however many buckets it's assigned):
  scores theme_fit and format_fit. It receives the profile, the venue's page
  text, and its ExtractionFacts -- and nothing about venue_tier at all: no
  bucket name, no lens, no venue_tier list. Neither dimension can be swayed
  by which bucket classify picked, because the call has no way to know.
- **Tier-aware call** (once per bucket in venue_tier): scores genre_fit only,
  under that bucket's lens (brief, "Venue buckets and rubric" -- the bucket
  tells the model what to look at for genre_fit specifically). Other Avenues
  has no genre_fit at all; its tier-aware call produces no_other_fit_reasoning
  instead.

Code then assembles each bucket's fit entry from the shared theme_fit/
format_fit plus that bucket's own genre_fit (or no_other_fit_reasoning) --
same TierFit/StandardFit/OtherAvenuesFit shapes as before, no schema change.

This split replaced an earlier single-call design after testing showed the
tier label leaking into format_fit's reasoning even though format_fit has
nothing to do with venue type -- a deliberately wrong tier moved format_fit's
score, not just genre_fit's. Splitting the calls makes that structurally
impossible instead of relying on prompt wording to keep the model in its lane.

Skipped entirely, by code, before any model call, when the call is closed,
its deadline has passed, or access is invite_only (brief, Workflow > step 5;
"Not scored, on purpose"). `members_only` is not a skip condition -- shown to
the researcher, not judged, per the brief.

The model is never asked for `overall` -- TierFitBase (src/tracker_schema.py)
computes it as the simple average of the dimension scores actually present.
No keyword counts, no formulas feed a dimension score itself (brief, "What
counts as agentic").

No tracker read/write, no email, no discovery here.

Run with: python -m src.judge_fit
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from src.config import MODEL_JUDGE
from src.tracker_schema import (
    AccessType,
    CallStatus,
    DimensionScore,
    ExtractionFacts,
    TIER_FIT_MODEL,
    TierFit,
    VenueTier,
)
from src.llm import call_llm
from src.profile_schema import Profile

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"

BUCKET_DISPLAY_NAMES: dict[VenueTier, str] = {
    VenueTier.ACADEMIC_PRACTITIONER: "Academic & Practitioner",
    VenueTier.INDUSTRY_TECHNICAL_MEDIA: "Industry & Technical Media",
    VenueTier.CORPORATE_INSTITUTIONAL: "Corporate & Institutional",
    VenueTier.OTHER_AVENUES: "Other Avenues",
}

# genre_fit's lens, per bucket (brief, "Venue buckets and rubric" -- the
# three standard buckets only; Other Avenues doesn't score genre_fit at all).
GENRE_FIT_LENS: dict[VenueTier, str] = {
    VenueTier.ACADEMIC_PRACTITIONER: (
        "research rigor and method -- would this venue's reviewers accept the paper's level of evidence?"
    ),
    VenueTier.INDUSTRY_TECHNICAL_MEDIA: (
        "voice and register -- does this outlet publish writing that sounds like the paper?"
    ),
    VenueTier.CORPORATE_INSTITUTIONAL: (
        "audience usability -- would policymakers or executives find the paper usable?"
    ),
}

_SHARED_RULES = """Rules, followed for every dimension:

- Read both texts and cite specifics from them -- the paper's own wording and the venue page's own \
wording -- never general knowledge about the field instead.
- An unknown or unstated fact is not a strike against the venue. If the page or the paper doesn't say \
something you'd want to know, say so plainly in your reasoning rather than scoring it down or assuming \
the best.
- Score 0-100, roughly: 80+ is a strong fit, 50-79 is workable with caveats, under 50 is poor.
- The reasoning must explain the number -- what specifically pulled it up or down -- never just restate the \
score in words.
- Do not use keyword counts, term overlap, or any numeric formula to arrive at a score. Read both texts as \
a human reviewer would and weigh them.
- Never invent an "overall" score or mention one -- you are not asked for it and it is not part of your \
output."""


# --- Tier-blind call: theme_fit + format_fit, zero tier information -------

TIER_BLIND_SYSTEM_PROMPT = f"""You are the judge-fit step of Cicadas, a CFP tracker agent. You are given \
one paper's own title, abstract, and keywords; one venue's full call-for-papers page text; and a few \
already-extracted facts about that venue. Your job here is to score exactly two dimensions: theme_fit and \
format_fit. You are deliberately NOT told what type of venue this is, or how it's classified -- score these \
two dimensions purely from the paper's and the venue page's own content, uninfluenced by venue type, \
because neither dimension is supposed to depend on it.

- theme_fit -- Does the venue want work on this paper's subject? Compare the paper's core subject -- not \
words it merely mentions -- against the venue's stated theme, topics, tracks, and categories. Central \
overlap with a named track or theme scores high; a loose or tangential connection scores low.
- format_fit -- Can the paper be submitted in a form and length this venue accepts? Judge size and shape \
only, not content: abstract, full paper, article, pitch, talk proposal, length limits -- reasoned from the \
paper itself, noting any adaptation it would need (e.g. "would need condensing to a short abstract"). This \
is about shape, not about what kind of venue this is -- a call for a 300-word abstract takes a 300-word \
abstract regardless of the venue's audience or purpose.

{_SHARED_RULES}

Call record_theme_and_format_scores exactly once with your findings."""


# --- Tier-aware call: genre_fit only, under one bucket's lens --------------

def _genre_fit_system_prompt(bucket: VenueTier) -> str:
    bucket_name = BUCKET_DISPLAY_NAMES[bucket]
    genre_lens = GENRE_FIT_LENS[bucket]
    return f"""You are the judge-fit step of Cicadas, a CFP tracker agent. You are given one paper's own \
title, abstract, and keywords; one venue's full call-for-papers page text; and a few already-extracted \
facts about that venue. This venue has been classified under the "{bucket_name}" bucket. Your only job here \
is to score genre_fit under that bucket's lens.

- genre_fit -- Does the venue publish this type of writing, for this kind of reader? A genre is a type of \
text with its own conventions, register, and expected audience. For the "{bucket_name}" bucket specifically, \
judge genre_fit on: {genre_lens}

{_SHARED_RULES}

Call record_genre_fit_score exactly once with your findings."""


# --- Tier-aware call for Other Avenues: no_other_fit_reasoning only --------

OTHER_AVENUES_REASONING_SYSTEM_PROMPT = """You are the judge-fit step of Cicadas, a CFP tracker agent. You \
are given one paper's own title, abstract, and keywords; one venue's full call-for-papers page text; and a \
few already-extracted facts about that venue. This venue has been classified as "Other Avenues" -- a \
catch-all used only when none of the three named buckets (Academic & Practitioner, Industry & Technical \
Media, Corporate & Institutional) genuinely fit it. Your only job here is to explain why, grounded in the \
venue page's own content -- not the paper's -- since this is about what kind of venue it is, not about fit.

Never use general knowledge about the field instead of the page's actual content. Never restate the bucket \
definitions as if that were the reasoning -- point to what the page specifically says or doesn't say.

Call record_no_other_fit_reasoning exactly once with your findings."""


# Flat score/reasoning field pairs, not a nested object per dimension --
# tool calls asking the model to build multiple nested objects in one turn
# proved unreliable in testing (the model would occasionally collapse a
# nested object into a raw JSON string instead of structured fields, failing
# validation). Flat fields removed the failure in practice; each dimension is
# reassembled into a DimensionScore in code right after the call returns.
def _score_field(dimension: str) -> dict:
    return {
        "type": "integer",
        "minimum": 0,
        "maximum": 100,
        "description": f"{dimension} score, 0-100: roughly 80+ strong, 50-79 workable with caveats, under 50 poor.",
    }


def _reasoning_field(dimension: str) -> dict:
    return {
        "type": "string",
        "description": f"Reasoning for the {dimension} score. Cites the paper and the page; explains the score rather than restating it.",
    }


TIER_BLIND_FIT_TOOL = {
    "name": "record_theme_and_format_scores",
    "description": "Record theme_fit and format_fit -- the two dimensions that don't depend on venue type.",
    "input_schema": {
        "type": "object",
        "properties": {
            "theme_fit_score": _score_field("theme_fit"),
            "theme_fit_reasoning": _reasoning_field("theme_fit"),
            "format_fit_score": _score_field("format_fit"),
            "format_fit_reasoning": _reasoning_field("format_fit"),
        },
        "required": ["theme_fit_score", "theme_fit_reasoning", "format_fit_score", "format_fit_reasoning"],
    },
}

GENRE_FIT_TOOL = {
    "name": "record_genre_fit_score",
    "description": "Record genre_fit for this venue-tier bucket's lens.",
    "input_schema": {
        "type": "object",
        "properties": {
            "genre_fit_score": _score_field("genre_fit"),
            "genre_fit_reasoning": _reasoning_field("genre_fit"),
        },
        "required": ["genre_fit_score", "genre_fit_reasoning"],
    },
}

OTHER_AVENUES_REASONING_TOOL = {
    "name": "record_no_other_fit_reasoning",
    "description": "Record why none of the three named buckets genuinely fit this venue.",
    "input_schema": {
        "type": "object",
        "properties": {
            "no_other_fit_reasoning": {
                "type": "string",
                "description": "Grounded in the venue page's own content.",
            },
        },
        "required": ["no_other_fit_reasoning"],
    },
}


class _TierBlindFit(BaseModel):
    """The tier-blind call's own output shape -- assembled into a full
    StandardFit/OtherAvenuesFit alongside a bucket's tier-aware result. Not a
    tracker.json shape itself, so it lives here rather than in
    tracker_schema.py (no schema change)."""

    model_config = ConfigDict(extra="forbid")

    theme_fit: DimensionScore
    format_fit: DimensionScore


def _tool_input(response, tool_name: str) -> dict:
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    raise RuntimeError(f"model did not call {tool_name} (stop_reason={response.stop_reason!r})")


def _format_extraction_summary(extraction: ExtractionFacts) -> str:
    """A short, human-readable digest of the fact fields most relevant to
    judge-fit (theme, submission_type, format_requirements, and the deadline/
    access/mode context) -- the full page text is given separately, so this
    just orients the model rather than duplicating it."""
    lines = [
        f"theme (as extracted): {extraction.theme or 'not stated'}",
        f"submission_type: {extraction.submission_type or 'not stated'}",
        f"format_requirements: {extraction.format_requirements or 'not stated'}",
        f"call_status: {extraction.call_status.value}",
        f"access: {extraction.access.value if extraction.access else 'not stated'}",
        f"mode: {extraction.mode.value if extraction.mode else 'not stated'}",
        f"fees: {extraction.fees or 'not stated'}",
    ]
    if extraction.has_deadline:
        lines.append(f"deadline: {extraction.deadline_as_stated or 'not stated'}")
    else:
        lines.append("deadline: none (rolling / pitch-based venue)")
    return "\n".join(lines)


def _build_user_content(profile: Profile, extraction: ExtractionFacts, page_text: str, url: str) -> str:
    return (
        f"PAPER\n"
        f"Title: {profile.title}\n\n"
        f"Abstract:\n{profile.abstract}\n\n"
        f"Keywords: {', '.join(profile.keywords)}\n\n"
        f"---\n\n"
        f"VENUE\n"
        f"URL: {url}\n\n"
        f"Extracted facts:\n{_format_extraction_summary(extraction)}\n\n"
        f"Full page text:\n\n{page_text}"
    )


def skip_reason(extraction: ExtractionFacts, today: datetime.date) -> str | None:
    """None means judge-fit should run; otherwise the human-readable reason
    it's being skipped (brief, Workflow > step 5 -- checked by code, before
    any model call, never left to the model to notice)."""
    if extraction.call_status == CallStatus.CLOSED:
        return "call_status is 'closed'"
    if extraction.has_deadline and extraction.deadline is not None and extraction.deadline < today:
        return f"deadline {extraction.deadline.isoformat()} has already passed (today={today.isoformat()})"
    if extraction.access == AccessType.INVITE_ONLY:
        return "access is 'invite_only'"
    return None


def judge_theme_and_format(
    profile: Profile,
    extraction: ExtractionFacts,
    page_text: str,
    url: str,
) -> _TierBlindFit:
    """The tier-blind call. Once per (profile, venue), regardless of how many
    buckets that venue is assigned -- venue_tier is never passed in, so this
    call has no way to let a bucket influence theme_fit or format_fit."""
    response = call_llm(
        model=MODEL_JUDGE,
        system=TIER_BLIND_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_content(profile, extraction, page_text, url)}],
        tools=[TIER_BLIND_FIT_TOOL],
        tool_choice={"type": "tool", "name": "record_theme_and_format_scores"},
        max_tokens=1500,
    )
    tool_output = _tool_input(response, "record_theme_and_format_scores")
    return _TierBlindFit.model_validate(
        {
            "theme_fit": {
                "score": tool_output["theme_fit_score"],
                "reasoning": tool_output["theme_fit_reasoning"],
            },
            "format_fit": {
                "score": tool_output["format_fit_score"],
                "reasoning": tool_output["format_fit_reasoning"],
            },
        }
    )


def judge_genre_fit(
    profile: Profile,
    extraction: ExtractionFacts,
    page_text: str,
    url: str,
    bucket: VenueTier,
) -> DimensionScore:
    """The tier-aware call for one of the three standard buckets. Never
    called for Other Avenues -- see judge_no_other_fit_reasoning."""
    response = call_llm(
        model=MODEL_JUDGE,
        system=_genre_fit_system_prompt(bucket),
        messages=[{"role": "user", "content": _build_user_content(profile, extraction, page_text, url)}],
        tools=[GENRE_FIT_TOOL],
        tool_choice={"type": "tool", "name": "record_genre_fit_score"},
        max_tokens=1000,
    )
    tool_output = _tool_input(response, "record_genre_fit_score")
    return DimensionScore.model_validate(
        {"score": tool_output["genre_fit_score"], "reasoning": tool_output["genre_fit_reasoning"]}
    )


def judge_no_other_fit_reasoning(
    profile: Profile,
    extraction: ExtractionFacts,
    page_text: str,
    url: str,
) -> str:
    """The tier-aware call for the Other Avenues bucket -- reasoning instead
    of a genre_fit score, since the catch-all has no lens to score against."""
    response = call_llm(
        model=MODEL_JUDGE,
        system=OTHER_AVENUES_REASONING_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_content(profile, extraction, page_text, url)}],
        tools=[OTHER_AVENUES_REASONING_TOOL],
        tool_choice={"type": "tool", "name": "record_no_other_fit_reasoning"},
        max_tokens=800,
    )
    tool_output = _tool_input(response, "record_no_other_fit_reasoning")
    return tool_output["no_other_fit_reasoning"]


def _assemble_fit(
    bucket: VenueTier,
    tier_blind: _TierBlindFit,
    profile: Profile,
    *,
    genre_fit: DimensionScore | None = None,
    no_other_fit_reasoning: str | None = None,
) -> TierFit:
    """Combines the shared tier-blind theme_fit/format_fit with one bucket's
    own tier-aware result into that bucket's TierFit shape (StandardFit or
    OtherAvenuesFit -- see TIER_FIT_MODEL). No schema change: this is the
    same shape the old single-call design produced, just assembled from two
    calls instead of populated by one."""
    model_cls = TIER_FIT_MODEL[bucket]
    common = {
        "scored_at": datetime.datetime.now(datetime.timezone.utc),
        "profile_hash": profile.content_hash(),
    }
    if bucket == VenueTier.OTHER_AVENUES:
        return model_cls.model_validate(
            {
                "theme_fit": tier_blind.theme_fit.model_dump(),
                "no_other_fit_reasoning": no_other_fit_reasoning,
                **common,
            }
        )
    return model_cls.model_validate(
        {
            "theme_fit": tier_blind.theme_fit.model_dump(),
            "genre_fit": genre_fit.model_dump(),
            "format_fit": tier_blind.format_fit.model_dump(),
            **common,
        }
    )


def judge_fit_for_venue(
    profile: Profile,
    extraction: ExtractionFacts,
    page_text: str,
    url: str,
    venue_tier: list[VenueTier],
    today: datetime.date | None = None,
    tier_blind: _TierBlindFit | None = None,
) -> dict[VenueTier, TierFit]:
    """Judges every bucket in `venue_tier` independently (brief: "never
    averaged across buckets"). Returns {} -- logging why, visibly -- when the
    code-level skip check fires; otherwise one TierFit per bucket, assembled
    from one shared tier-blind call plus one tier-aware call per bucket.

    `tier_blind` lets a caller supply an already-computed tier-blind result
    instead of making a fresh one -- used by this module's own tier-
    sensitivity test (see main()) to guarantee theme_fit/format_fit are
    identical when the same (profile, venue) pair is re-scored under a
    different bucket; ordinary callers omit it and get one computed here."""
    if today is None:
        today = datetime.datetime.now(datetime.timezone.utc).date()

    reason = skip_reason(extraction, today)
    if reason is not None:
        print(f"[judge_fit] SKIPPED {profile.id} x {url}: {reason}")
        return {}

    if tier_blind is None:
        print(f"[judge_fit] tier-blind call (theme_fit + format_fit, no tier info): {profile.id} x {url}")
        tier_blind = judge_theme_and_format(profile, extraction, page_text, url)

    result: dict[VenueTier, TierFit] = {}
    for bucket in venue_tier:
        if bucket == VenueTier.OTHER_AVENUES:
            print(f"[judge_fit] tier-aware call (no_other_fit_reasoning): {profile.id} x {url}")
            reasoning = judge_no_other_fit_reasoning(profile, extraction, page_text, url)
            result[bucket] = _assemble_fit(bucket, tier_blind, profile, no_other_fit_reasoning=reasoning)
        else:
            print(f"[judge_fit] tier-aware call (genre_fit, bucket={bucket.value}): {profile.id} x {url}")
            genre = judge_genre_fit(profile, extraction, page_text, url, bucket)
            result[bucket] = _assemble_fit(bucket, tier_blind, profile, genre_fit=genre)
    return result


def _load_profile(path: Path) -> Profile:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"failed to read profile {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"malformed JSON in profile {path}: {exc}") from exc
    return Profile.model_validate(data)


def _print_result(label: str, fit_by_tier: dict[VenueTier, TierFit]) -> None:
    print(f"\n=== {label} ===")
    if not fit_by_tier:
        print("(skipped -- see the [judge_fit] SKIPPED line above)")
        return
    for bucket, fit in fit_by_tier.items():
        print(f"-- bucket: {bucket.value} --")
        print(fit.model_dump_json(indent=2))


def _dimension_names(fit: TierFit) -> list[str]:
    return [name for name, value in fit.__dict__.items() if isinstance(value, DimensionScore)]


def _print_summary_table(rows: list[tuple[str, VenueTier, TierFit | None]]) -> None:
    print("\n=== Summary table ===")
    header = f"{'Test':<45} {'bucket':<24} {'theme':>6} {'genre':>6} {'format':>7} {'overall':>8}"
    print(header)
    print("-" * len(header))
    for label, bucket, fit in rows:
        if fit is None:
            print(f"{label:<45} {bucket.value:<24} {'--':>6} {'--':>6} {'--':>7} {'--':>8}")
            continue
        theme = fit.theme_fit.score
        genre = getattr(fit, "genre_fit", None)
        genre_s = genre.score if genre is not None else "--"
        fmt = getattr(fit, "format_fit", None)
        fmt_s = fmt.score if fmt is not None else "--"
        print(f"{label:<45} {bucket.value:<24} {theme:>6} {genre_s!s:>6} {fmt_s!s:>7} {fit.overall:>8}")


def _print_tier_sensitivity(
    baseline_bucket: VenueTier,
    baseline_fit: TierFit | None,
    wrong_bucket: VenueTier,
    wrong_fit: TierFit | None,
) -> None:
    print(
        f"\n=== Tier sensitivity: {baseline_bucket.value} (real tier) "
        f"vs {wrong_bucket.value} (deliberately wrong tier) ==="
    )
    if baseline_fit is None or wrong_fit is None:
        print("[judge_fit] one of the two runs was skipped -- nothing to compare")
        return
    if type(baseline_fit) is not type(wrong_fit):
        print(
            f"[judge_fit] shapes differ ({type(baseline_fit).__name__} vs {type(wrong_fit).__name__}) "
            "-- comparing overall only"
        )
        delta = wrong_fit.overall - baseline_fit.overall
        print(f"  overall: {baseline_fit.overall} -> {wrong_fit.overall} (delta {delta:+d})")
        return

    for name in _dimension_names(baseline_fit):
        b_score = getattr(baseline_fit, name).score
        w_score = getattr(wrong_fit, name).score
        print(f"  {name}: {b_score} -> {w_score} (delta {w_score - b_score:+d})")
    delta = wrong_fit.overall - baseline_fit.overall
    print(f"  overall: {baseline_fit.overall} -> {wrong_fit.overall} (delta {delta:+d})")


def main() -> None:
    # Imported here, not at module level, to keep judge_fit.py's own import
    # graph focused on judging; extract/classify are only needed by this
    # standalone test harness, not by judge_fit() itself.
    from src.classify import classify_venue
    from src.extract import HARDCODED_URL, extract_facts, fetch_page, html_to_text

    try:
        immune_tolerance = _load_profile(PROFILES_DIR / "immune-tolerance.json")
        elephant_corridors = _load_profile(PROFILES_DIR / "elephant-corridors.json")

        url = HARDCODED_URL
        print(f"[judge_fit] fetching venue page: {url}")
        page_text = html_to_text(fetch_page(url))

        print(f"[judge_fit] extracting facts: {url}")
        extraction = extract_facts(url)

        print(f"[judge_fit] classifying venue: {url}")
        venue_classification = classify_venue(url, page_text)
        real_tier = venue_classification.venue_tier
        print(f"[judge_fit] real venue_tier = {[t.value for t in real_tier]}")
        print(f"[judge_fit] venue_tier_reasoning: {venue_classification.venue_tier_reasoning}")

        # Test 1 and Test 3 re-score the SAME (profile, venue) pair under two
        # different tiers. They share one tier-blind call -- computed once,
        # here -- so theme_fit/format_fit are guaranteed identical between
        # them, isolating whatever the tier swap does to genre_fit/overall.
        print(
            "\n[judge_fit] --- shared tier-blind call for immune-tolerance x ECI 2027 "
            "(reused by Test 1 and Test 3) ---"
        )
        it_tier_blind = judge_theme_and_format(immune_tolerance, extraction, page_text, url)

        print("\n[judge_fit] --- Test 1: immune-tolerance x ECI 2027, real tier ---")
        test1 = judge_fit_for_venue(
            immune_tolerance, extraction, page_text, url, real_tier, tier_blind=it_tier_blind
        )

        print("\n[judge_fit] --- Test 2: elephant-corridors x ECI 2027, real tier (deliberate mismatch) ---")
        test2 = judge_fit_for_venue(elephant_corridors, extraction, page_text, url, real_tier)

        wrong_tier = [VenueTier.INDUSTRY_TECHNICAL_MEDIA]
        print(
            "\n[judge_fit] --- Test 3: immune-tolerance x ECI 2027, "
            "deliberately wrong tier (industry_technical_media) ---"
        )
        test3 = judge_fit_for_venue(
            immune_tolerance, extraction, page_text, url, wrong_tier, tier_blind=it_tier_blind
        )
    except (RuntimeError, ValidationError) as exc:
        print(f"[judge_fit] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    _print_result("Test 1 -- immune-tolerance x ECI 2027 (real tier)", test1)
    _print_result("Test 2 -- elephant-corridors x ECI 2027 (real tier, deliberate mismatch)", test2)
    _print_result("Test 3 -- immune-tolerance x ECI 2027 (wrong tier: industry_technical_media)", test3)

    baseline_bucket = real_tier[0]
    wrong_bucket = wrong_tier[0]
    _print_summary_table(
        [
            ("Test 1 -- immune-tolerance (real tier)", baseline_bucket, test1.get(baseline_bucket)),
            ("Test 2 -- elephant-corridors (real tier, mismatch)", baseline_bucket, test2.get(baseline_bucket)),
            ("Test 3 -- immune-tolerance (wrong tier)", wrong_bucket, test3.get(wrong_bucket)),
        ]
    )
    _print_tier_sensitivity(
        baseline_bucket,
        test1.get(baseline_bucket),
        wrong_bucket,
        test3.get(wrong_bucket),
    )


if __name__ == "__main__":
    main()
