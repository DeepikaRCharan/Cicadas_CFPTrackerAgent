"""Classify: what bucket does a paper, or a venue, actually belong to.

Build step 3 (docs/project-brief.md, "Build order"). Two separate LLM calls
via the judging model (brief, Architecture decisions > Model tiering: "the
paper-side half of classify runs once per profile and is cached, the
venue-side half runs once per candidate"):

- classify_paper: reads a profile's own content, returns paper_bucket +
  paper_bucket_cues + paper_bucket_reasoning, validated against
  ProfileClassification.
- classify_venue: reads a venue page's full text (not just extraction's
  theme field), returns venue_tier + venue_tier_reasoning.

Per "What counts as agentic" (brief): the model reads and weighs the actual
text against the bucket definitions every time. Nothing here maps a keyword
or a label to a bucket in code -- that would defeat the point.

No judge-fit, no tracker read/write, no email here.

Run with: python -m src.classify
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.config import MODEL_JUDGE
from src.extract import HARDCODED_URL, fetch_page, html_to_text
from src.llm import call_llm
from src.profile_schema import Profile
from src.tracker_schema import ProfileClassification, VenueTier

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"

# The four buckets, as read against actual content -- shared verbatim
# between the paper and venue prompts so both calls reason against the same
# definitions (brief, "Venue buckets and rubric").
BUCKET_DEFINITIONS = """The four buckets any paper or venue is read against, based on its actual \
content -- never inferred from a label, filename, subject-matter overlap, or formatting alone:

1. academic_practitioner -- Peer-reviewed or practitioner-professional venues/writing for domain \
experts: research conferences, academic journals, professional-practitioner tracks. The register is \
rigorous, evidence-scoped, citation-aware, written for readers already fluent in the field.
2. industry_technical_media -- Trade press, technical blogs, industry media outlets. Framed for a \
practitioner/industry readership, timely and applied rather than peer-reviewed; publishes articles, not \
necessarily via a formal CFP.
3. corporate_institutional -- Policy, executive, or institutional audiences: government bodies, think \
tanks, corporate reports, design summits. Framed for decision-makers rather than domain researchers or \
trade readers; often invite-only or application-gated.
4. other_avenues -- None of the above genuinely fits. The catch-all; a signal to flag as lower-confidence, \
not a default to reach for lightly."""

PAPER_CLASSIFY_SYSTEM_PROMPT = f"""You are the paper-classify step of Cicadas, a CFP tracker agent. \
You are given one paper's own title, abstract, keywords, and preferred format. Your job is to decide \
which bucket the paper's OWN writing -- its register, rigor, and implied audience -- actually belongs to. \
This is not "where should this paper be submitted" -- it's "what kind of writing is this, read on its own \
terms." A later step uses your call to judge how well specific venues fit this paper.

{BUCKET_DEFINITIONS}

Read the whole title, abstract, and keywords and weigh them as a whole. Do not pattern-match individual \
words to a bucket, and do not classify by subject matter alone -- a paper about wildlife policy is not \
automatically corporate_institutional just because "policy" appears in it; what matters is how the paper \
itself is written and framed. Two buckets only if the paper genuinely, substantively straddles two of the \
definitions above -- not merely because its subject matter touches multiple fields. Almost always pick \
exactly one.

paper_bucket_cues must be 3-4 actual phrases or specific features drawn from the paper's own title, \
abstract, or keywords -- quote or closely paraphrase them, not a generic restatement of the bucket \
definition.

Call record_paper_classification exactly once with your findings."""

VENUE_CLASSIFY_SYSTEM_PROMPT = f"""You are the venue-classify step of Cicadas, a CFP tracker agent. You \
are given the plain-text content of one conference or publication venue's own page. Your job is to decide \
which bucket this venue -- its actual audience, purpose, and register -- belongs to.

{BUCKET_DEFINITIONS}

Read the whole page text and weigh it as a whole -- the audience it's written for, who it says will judge \
or attend, how it talks about itself -- not just its stated theme or topic list. Do not pattern-match \
individual words to a bucket. Two buckets only if the venue genuinely, substantively straddles two of the \
definitions above. Almost always pick exactly one.

venue_tier_reasoning must show real reasoning grounded in what the page actually says, not a restatement \
of the bucket definition.

Call record_venue_classification exactly once with your findings."""

PAPER_CLASSIFY_TOOL = {
    "name": "record_paper_classification",
    "description": "Record which bucket(s) a paper's own writing belongs to.",
    "input_schema": {
        "type": "object",
        "properties": {
            "paper_bucket": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "academic_practitioner", "industry_technical_media",
                        "corporate_institutional", "other_avenues",
                    ],
                },
                "minItems": 1,
                "maxItems": 2,
                "description": (
                    "A LIST of bucket names, even when there's only one -- e.g. [\"academic_practitioner\"], "
                    "never a bare string. Almost always exactly one bucket; two only if the paper genuinely "
                    "straddles two."
                ),
            },
            "paper_bucket_cues": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 4,
                "description": "3-4 actual phrases/features from the paper's own text that drove this call.",
            },
            "paper_bucket_reasoning": {
                "type": "string",
                "description": "Why these bucket(s), reasoned from the cues above.",
            },
        },
        "required": ["paper_bucket", "paper_bucket_cues", "paper_bucket_reasoning"],
    },
}

VENUE_CLASSIFY_TOOL = {
    "name": "record_venue_classification",
    "description": "Record which bucket(s) a venue/call-for-papers page belongs to.",
    "input_schema": {
        "type": "object",
        "properties": {
            "venue_tier": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "academic_practitioner", "industry_technical_media",
                        "corporate_institutional", "other_avenues",
                    ],
                },
                "minItems": 1,
                "maxItems": 2,
                "description": (
                    "A LIST of bucket names, even when there's only one -- e.g. [\"academic_practitioner\"], "
                    "never a bare string. Almost always exactly one bucket; two only if the venue genuinely "
                    "straddles two."
                ),
            },
            "venue_tier_reasoning": {
                "type": "string",
                "description": "Why this bucket, grounded in the page's actual text.",
            },
        },
        "required": ["venue_tier", "venue_tier_reasoning"],
    },
}


class VenueClassification(BaseModel):
    """classify_venue's own output shape: venue_tier + venue_tier_reasoning,
    the same two fields Extraction (src/tracker_schema.py) eventually
    carries once a later build step merges extraction and classify into one
    tracker entry. Kept local to this step rather than folded into
    Extraction there -- that file has no ExtractionFacts to combine with
    yet, and a venue classified on its own has nothing else to validate
    against."""

    model_config = ConfigDict(extra="forbid")

    venue_tier: list[VenueTier] = Field(..., min_length=1, max_length=2)
    venue_tier_reasoning: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _venue_tier_no_duplicates(self) -> "VenueClassification":
        if len(set(self.venue_tier)) != len(self.venue_tier):
            raise ValueError("venue_tier must not repeat the same bucket twice")
        return self


def _tool_input(response, tool_name: str) -> dict:
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    raise RuntimeError(f"model did not call {tool_name} (stop_reason={response.stop_reason!r})")


def _coerce_to_list(tool_output: dict, field: str) -> dict:
    """The tool schema declares `field` as an array (minItems 1), but a
    single-bucket call sometimes comes back as a bare string instead of a
    one-item list -- a representation slip the model makes despite the
    schema, not a content guess. Normalizing the shape here isn't filling in
    unstated content; the bucket value itself is untouched."""
    value = tool_output.get(field)
    if isinstance(value, str):
        tool_output = {**tool_output, field: [value]}
    return tool_output


def load_profile(path: Path) -> Profile:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"failed to read profile {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"malformed JSON in profile {path}: {exc}") from exc
    return Profile.model_validate(data)


def classify_paper(profile: Profile) -> ProfileClassification:
    response = call_llm(
        model=MODEL_JUDGE,
        system=PAPER_CLASSIFY_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Title: {profile.title}\n\n"
                    f"Abstract:\n{profile.abstract}\n\n"
                    f"Keywords: {', '.join(profile.keywords)}\n\n"
                    f"Preferred format: {profile.preferred_format}"
                ),
            }
        ],
        tools=[PAPER_CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "record_paper_classification"},
        max_tokens=1200,
    )
    tool_output = _coerce_to_list(_tool_input(response, "record_paper_classification"), "paper_bucket")
    return ProfileClassification.model_validate(
        {
            **tool_output,
            "profile_hash": profile.content_hash(),
            "classified_at": datetime.datetime.now(datetime.timezone.utc),
        }
    )


def classify_venue(url: str, page_text: str) -> VenueClassification:
    response = call_llm(
        model=MODEL_JUDGE,
        system=VENUE_CLASSIFY_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Page URL: {url}\n\nPage text:\n\n{page_text}",
            }
        ],
        tools=[VENUE_CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "record_venue_classification"},
        max_tokens=1200,
    )
    tool_output = _coerce_to_list(_tool_input(response, "record_venue_classification"), "venue_tier")
    return VenueClassification.model_validate(tool_output)


def main() -> None:
    try:
        immune_tolerance = load_profile(PROFILES_DIR / "immune-tolerance.json")
        elephant_corridors = load_profile(PROFILES_DIR / "elephant-corridors.json")

        results: list[tuple[str, BaseModel]] = []
        for profile in (immune_tolerance, elephant_corridors):
            print(f"[classify] classifying paper: {profile.id}")
            results.append((f"Paper classify -- {profile.id}", classify_paper(profile)))

        print(f"[classify] fetching venue page: {HARDCODED_URL}")
        page_text = html_to_text(fetch_page(HARDCODED_URL))
        print(f"[classify] classifying venue: {HARDCODED_URL}")
        results.append((f"Venue classify -- {HARDCODED_URL}", classify_venue(HARDCODED_URL, page_text)))
    except (RuntimeError, ValidationError) as exc:
        print(f"[classify] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    for label, result in results:
        print(f"\n=== {label} ===")
        print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
