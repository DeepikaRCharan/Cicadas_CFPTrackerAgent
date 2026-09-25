"""Judge fit: how well does a paper fit a venue, under a given bucket.

Build step 4 (docs/project-brief.md, "Build order"). One LLM call (judging
model) per (profile, bucket) pair: theme_fit, genre_fit, and format_fit for
the three standard buckets; theme_fit + no_other_fit_reasoning for
other_avenues. The bucket steers the lens judge_fit applies to genre_fit
(brief, "Venue buckets and rubric") but the field names stay the same across
buckets -- a wrong venue_tier only changes the lens, not the whole rubric.

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

from pydantic import ValidationError

from src.config import MODEL_JUDGE
from src.tracker_schema import (
    AccessType,
    CallStatus,
    DimensionScore,
    ExtractionFacts,
    OtherAvenuesFit,
    StandardFit,
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


def _standard_fit_system_prompt(bucket: VenueTier) -> str:
    bucket_name = BUCKET_DISPLAY_NAMES[bucket]
    genre_lens = GENRE_FIT_LENS[bucket]
    return f"""You are the judge-fit step of Cicadas, a CFP tracker agent. You are given one paper's own \
title, abstract, and keywords; one venue's full call-for-papers page text; and a few already-extracted \
facts about that venue. Your job is to score how well the paper fits this venue, scored specifically under \
the "{bucket_name}" bucket, on three dimensions: theme_fit, genre_fit, and format_fit.

- theme_fit -- Does the venue want work on this paper's subject? Compare the paper's core subject -- not \
words it merely mentions -- against the venue's stated theme, topics, tracks, and categories. Central \
overlap with a named track or theme scores high; a loose or tangential connection scores low.
- genre_fit -- Does the venue publish this type of writing, for this kind of reader? A genre is a type of \
text with its own conventions, register, and expected audience. For the "{bucket_name}" bucket specifically, \
judge genre_fit on: {genre_lens}
- format_fit -- Can the paper be submitted in a form and length this venue accepts? Judge size and shape \
only, not content: abstract, full paper, article, pitch, talk proposal, length limits -- reasoned from the \
paper itself, noting any adaptation it would need (e.g. "would need condensing to a short abstract").

{_SHARED_RULES}

Call record_fit_scores exactly once with your findings."""


OTHER_AVENUES_JUDGE_SYSTEM_PROMPT = f"""You are the judge-fit step of Cicadas, a CFP tracker agent. You are \
given one paper's own title, abstract, and keywords; one venue's full call-for-papers page text; and a few \
already-extracted facts about that venue. This venue has been classified as "Other Avenues" -- a catch-all \
for when none of the three named buckets (Academic & Practitioner, Industry & Technical Media, Corporate & \
Institutional) genuinely fit. Score only theme_fit here -- genre_fit and format_fit don't apply to a \
catch-all with no venue-type lens to judge them against.

- theme_fit -- Does the venue want work on this paper's subject? Compare the paper's core subject -- not \
words it merely mentions -- against the venue's stated theme, topics, and categories.
- no_other_fit_reasoning -- Explain, from the venue page's own content, why none of the three named buckets \
genuinely fit this venue.

{_SHARED_RULES}

Call record_other_avenues_fit exactly once with your findings."""


_DIMENSION_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "0-100: roughly 80+ strong, 50-79 workable with caveats, under 50 poor.",
        },
        "reasoning": {
            "type": "string",
            "description": "Cites the paper and the page; explains the score rather than restating it.",
        },
    },
    "required": ["score", "reasoning"],
}

STANDARD_FIT_TOOL = {
    "name": "record_fit_scores",
    "description": "Record theme_fit, genre_fit, and format_fit for this venue-tier bucket.",
    "input_schema": {
        "type": "object",
        "properties": {
            "theme_fit": _DIMENSION_SCHEMA,
            "genre_fit": _DIMENSION_SCHEMA,
            "format_fit": _DIMENSION_SCHEMA,
        },
        "required": ["theme_fit", "genre_fit", "format_fit"],
    },
}

OTHER_AVENUES_FIT_TOOL = {
    "name": "record_other_avenues_fit",
    "description": "Record theme_fit and no_other_fit_reasoning for the Other Avenues catch-all bucket.",
    "input_schema": {
        "type": "object",
        "properties": {
            "theme_fit": _DIMENSION_SCHEMA,
            "no_other_fit_reasoning": {
                "type": "string",
                "description": "Why none of the three named buckets genuinely fit this venue.",
            },
        },
        "required": ["theme_fit", "no_other_fit_reasoning"],
    },
}


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


def judge_fit(
    profile: Profile,
    extraction: ExtractionFacts,
    page_text: str,
    url: str,
    bucket: VenueTier,
) -> TierFit:
    """Score one (profile, bucket) pair. Caller is responsible for having
    already checked skip_reason -- this function always makes a model call."""
    model_cls = TIER_FIT_MODEL[bucket]
    if bucket == VenueTier.OTHER_AVENUES:
        tool, tool_name, system = OTHER_AVENUES_FIT_TOOL, "record_other_avenues_fit", OTHER_AVENUES_JUDGE_SYSTEM_PROMPT
    else:
        tool, tool_name, system = STANDARD_FIT_TOOL, "record_fit_scores", _standard_fit_system_prompt(bucket)

    response = call_llm(
        model=MODEL_JUDGE,
        system=system,
        messages=[{"role": "user", "content": _build_user_content(profile, extraction, page_text, url)}],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool_name},
        max_tokens=2000,
    )
    tool_output = _tool_input(response, tool_name)
    return model_cls.model_validate(
        {
            **tool_output,
            "scored_at": datetime.datetime.now(datetime.timezone.utc),
            "profile_hash": profile.content_hash(),
        }
    )


def judge_fit_for_venue(
    profile: Profile,
    extraction: ExtractionFacts,
    page_text: str,
    url: str,
    venue_tier: list[VenueTier],
    today: datetime.date | None = None,
) -> dict[VenueTier, TierFit]:
    """Judges every bucket in `venue_tier` independently (brief: "never
    averaged across buckets"). Returns {} -- logging why, visibly -- when the
    code-level skip check fires; otherwise one TierFit per bucket."""
    if today is None:
        today = datetime.datetime.now(datetime.timezone.utc).date()

    reason = skip_reason(extraction, today)
    if reason is not None:
        print(f"[judge_fit] SKIPPED {profile.id} x {url}: {reason}")
        return {}

    result: dict[VenueTier, TierFit] = {}
    for bucket in venue_tier:
        print(f"[judge_fit] scoring {profile.id} x {url} under bucket={bucket.value}")
        result[bucket] = judge_fit(profile, extraction, page_text, url, bucket)
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

        print("\n[judge_fit] --- Test 1: immune-tolerance x ECI 2027, real tier ---")
        test1 = judge_fit_for_venue(immune_tolerance, extraction, page_text, url, real_tier)

        print("\n[judge_fit] --- Test 2: elephant-corridors x ECI 2027, real tier (deliberate mismatch) ---")
        test2 = judge_fit_for_venue(elephant_corridors, extraction, page_text, url, real_tier)

        wrong_tier = [VenueTier.INDUSTRY_TECHNICAL_MEDIA]
        print(
            "\n[judge_fit] --- Test 3: immune-tolerance x ECI 2027, "
            "deliberately wrong tier (industry_technical_media) ---"
        )
        test3 = judge_fit_for_venue(immune_tolerance, extraction, page_text, url, wrong_tier)
    except (RuntimeError, ValidationError) as exc:
        print(f"[judge_fit] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    _print_result("Test 1 -- immune-tolerance x ECI 2027 (real tier)", test1)
    _print_result("Test 2 -- elephant-corridors x ECI 2027 (real tier, deliberate mismatch)", test2)
    _print_result("Test 3 -- immune-tolerance x ECI 2027 (wrong tier: industry_technical_media)", test3)

    baseline_bucket = real_tier[0]
    wrong_bucket = wrong_tier[0]
    _print_tier_sensitivity(
        baseline_bucket,
        test1.get(baseline_bucket),
        wrong_bucket,
        test3.get(wrong_bucket),
    )


if __name__ == "__main__":
    main()
