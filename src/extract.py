"""Extraction: fetch one venue page, pull ExtractionFacts out of it.

Build step 2 (docs/project-brief.md, "Build order"). Fills ExtractionFacts
only -- venue_tier/venue_tier_reasoning are classify's job, not extraction's
(brief, Rules > "Who fills what"), and ExtractionFacts doesn't even have
those fields to fill. No classify, no tracker read/write, no email here.

Run with: python -m src.extract
"""

from __future__ import annotations

import datetime
import json
import sys
from html.parser import HTMLParser
from pathlib import Path

import requests
from pydantic import ValidationError

from src.config import MODEL_EXTRACTION
from src.llm import call_llm
from src.tracker_schema import ExtractionFacts

HARDCODED_URL = "https://eci2027.org/call-for-abstracts/"

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

FETCH_TIMEOUT_SECONDS = 15
FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CicadasBot/0.1)"}

# Tags whose text content is never useful CFP content -- stripped outright,
# not just de-emphasized, so nothing from them can reach the model. In
# particular a page's <footer> is boilerplate maintained site-wide by
# whoever hosts it and can legitimately point at a *different* event (see
# the eci2027.org page this module is built against: its footer links to
# congressespn.org, an unrelated congress run by the same conference
# organizer). Stripping the tag is a stronger guarantee than a prompt
# instruction -- the model never sees the text to begin with.
_SKIP_TAGS = frozenset({"script", "style", "head", "footer", "nav"})
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "tr", "section", "article", "header", "ul", "ol", "table",
    }
)


class _PageTextExtractor(HTMLParser):
    """Turns HTML into plain text: strips script/style/head/footer/nav
    (and anything nested inside them) entirely, and inserts a line break
    at each block-level tag so the result reads like a page, not a run-on
    sentence."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._tag_stack: list[str] = []
        self._chunks: list[str] = []

    def _skipping(self) -> bool:
        return any(tag in _SKIP_TAGS for tag in self._tag_stack)

    def handle_starttag(self, tag, attrs):
        self._tag_stack.append(tag)
        if tag in _BLOCK_TAGS and not self._skipping():
            self._chunks.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS and not self._skipping():
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS and not self._skipping():
            self._chunks.append("\n")
        for i in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[i] == tag:
                del self._tag_stack[i:]
                break

    def handle_data(self, data):
        if not self._skipping():
            self._chunks.append(data)

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        lines = (line.strip() for line in raw.splitlines())
        return "\n".join(line for line in lines if line)


def fetch_page(url: str) -> str:
    """Fetch a URL's HTML. Raises RuntimeError, with the URL and cause
    named, on anything short of a 200 -- there is no fallback path here;
    a fetch failure stops the run (brief/CLAUDE.md: no silent failures,
    never guess)."""
    try:
        response = requests.get(url, timeout=FETCH_TIMEOUT_SECONDS, headers=FETCH_HEADERS)
    except requests.RequestException as exc:
        raise RuntimeError(f"failed to fetch {url}: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"failed to fetch {url}: HTTP {response.status_code}")
    return response.text


def html_to_text(html: str) -> str:
    parser = _PageTextExtractor()
    parser.feed(html)
    return parser.get_text()


EXTRACTION_SYSTEM_PROMPT = """You are the extraction step of Cicadas, a CFP tracker agent. You are given \
the plain-text content of one conference or publication venue's call-for-papers page. Your only job is to \
pull out the fact fields defined by the record_extraction_facts tool -- nothing else. A separate step \
classifies the venue and a separate step scores fit; do not attempt either here.

Rules, followed exactly:

- Extract only what the page explicitly states. Never guess, infer beyond what's written, or fill in a \
plausible-sounding default. If it isn't on the page, the field is null.
- Every field you set to null -- except a null deadline when has_deadline is false -- must have its field \
name listed in missing_fields. The allowed names are exactly: deadline, theme, submission_type, location, \
mode, fees, format_requirements, submission_opens.
- deadline_as_stated must be the deadline date phrase exactly as worded on the page, verbatim -- never \
reformatted, paraphrased, or normalized. Include only the date phrase itself (e.g. "31 March 2027"), not \
a surrounding label like "Deadline for Abstract Submission".
- Do not infer facts from other facts. In particular: a stated location does NOT by itself mean the event \
is in-person -- mode is null unless the page explicitly says in-person, virtual, or hybrid. The same goes \
for every other field: if the page states X and you're tempted to conclude Y because X usually implies Y, \
leave the field null instead.
- If the page states only a month for the deadline (e.g. "February 2027"), set deadline to the 1st of that \
month (e.g. "2027-02-01"), deadline_precision to "month", and deadline_as_stated to the page's own wording \
("February 2027"). If the page states an exact day, set deadline_precision to "exact".
- has_deadline is false only when the page genuinely describes a rolling, pitch-based, or no-deadline \
process. It is a different case from "a deadline should exist but isn't stated on this page" -- that case \
is has_deadline=true, deadline=null, deadline_precision=null, deadline_as_stated=null, with "deadline" in \
missing_fields. Never conflate the two.
- submission_status reflects what the page says about submissions right now: "not_yet_open", "open", \
"closed", or "unknown" if the page simply doesn't say. This field is never null.
- submission_opens is the page's own wording for when submissions open, if stated (this can differ from the \
deadline wording).
- complete is false if and only if at least one CRITICAL field -- deadline (only when has_deadline is \
true), theme, or submission_type -- is null. Optional fields (location, mode, fees, format_requirements, \
submission_opens) being null never affects complete.
- The page text has already had its footer and navigation chrome removed. If any leftover boilerplate, or \
any reference to an event other than the one this call-for-papers page is actually for, still appears in \
the text, ignore it completely -- extract facts about this page's own venue only.

Call record_extraction_facts exactly once with your findings."""

EXTRACTION_TOOL = {
    "name": "record_extraction_facts",
    "description": "Record the fact fields extracted from a conference/venue call-for-papers page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "has_deadline": {
                "type": "boolean",
                "description": (
                    "True if the page describes a submission deadline for this venue at all. "
                    "False only for a genuinely rolling/pitch-based/no-deadline process."
                ),
            },
            "deadline": {
                "type": ["string", "null"],
                "description": (
                    "ISO date (YYYY-MM-DD) of the submission deadline. Null when has_deadline is "
                    "false. When only a month is stated, use the 1st of that month. Null (with "
                    "'deadline' in missing_fields) if has_deadline is true but no deadline is "
                    "actually stated on the page."
                ),
            },
            "deadline_precision": {
                "type": ["string", "null"],
                "enum": ["exact", "month", None],
                "description": "'exact' for a specific stated day, 'month' for a month/year only. Null iff deadline is null.",
            },
            "deadline_as_stated": {
                "type": ["string", "null"],
                "description": "The deadline exactly as worded on the page, verbatim. Null iff deadline is null.",
            },
            "submission_opens": {
                "type": ["string", "null"],
                "description": "The page's own wording for when submissions open (e.g. 'November 2026'). Null if not stated.",
            },
            "submission_status": {
                "type": "string",
                "enum": ["not_yet_open", "open", "closed", "unknown"],
                "description": "What the page says about submissions right now. 'unknown' if the page doesn't say. Never null.",
            },
            "submission_type": {
                "type": ["string", "null"],
                "description": "What kind of submission the call asks for (e.g. 'abstract', 'full paper'). Null if not stated.",
            },
            "theme": {
                "type": ["string", "null"],
                "description": "The venue's subject matter, as a short phrase. Null if not stated.",
            },
            "location": {
                "type": ["string", "null"],
                "description": "City/venue location if stated (e.g. 'Florence, Italy'). Null if not stated.",
            },
            "format_requirements": {
                "type": ["string", "null"],
                "description": "Submission format constraints (page limits, word counts, template) if stated. Null if not stated.",
            },
            "fees": {
                "type": ["string", "null"],
                "description": "Submission or registration fees if stated. Null if not stated.",
            },
            "mode": {
                "type": ["string", "null"],
                "enum": ["in-person", "virtual", "hybrid", None],
                "description": (
                    "Whether the event is in-person, virtual, or hybrid, only if the page says so "
                    "explicitly. A stated city/venue location is NOT enough by itself -- do not "
                    "infer in-person from a location. Null if mode isn't explicitly stated."
                ),
            },
            "complete": {
                "type": "boolean",
                "description": (
                    "False iff a CRITICAL field (deadline when has_deadline is true, theme, "
                    "submission_type) is null. Optional-field gaps alone don't make this false."
                ),
            },
            "missing_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Exactly the nullable fields above that you set to null (from: deadline, "
                    "theme, submission_type, location, mode, fees, format_requirements, "
                    "submission_opens). Omit 'deadline' when has_deadline is false -- that's a "
                    "fact, not a gap."
                ),
            },
        },
        "required": [
            "has_deadline", "deadline", "deadline_precision", "deadline_as_stated",
            "submission_opens", "submission_status", "submission_type", "theme", "location",
            "format_requirements", "fees", "mode", "complete", "missing_fields",
        ],
    },
}


def _dump_response(response) -> str:
    if hasattr(response, "model_dump_json"):
        return response.model_dump_json(indent=2)
    return json.dumps(response, indent=2, default=str)


def _write_log(prefix: str, suffix: str, content: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = LOGS_DIR / f"{timestamp}_{prefix}.{suffix}"
    path.write_text(content, encoding="utf-8")
    return path


def extract_facts(url: str) -> ExtractionFacts:
    """Fetch `url`, convert it to plain text, and extract ExtractionFacts
    from it via the extraction model. Raises RuntimeError (fetch/model
    failure) or pydantic.ValidationError (malformed or invalid output) --
    both are visible, run-stopping failures; nothing here is caught and
    papered over with a default."""
    html = fetch_page(url)
    page_text = html_to_text(html)
    page_text_log = _write_log("page_text", "txt", page_text)
    print(f"[extract] fetched {url} -> page text logged to {page_text_log}")

    response = call_llm(
        model=MODEL_EXTRACTION,
        system=EXTRACTION_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Page URL: {url}\n\nPage text:\n\n{page_text}",
            }
        ],
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "record_extraction_facts"},
        max_tokens=1500,
    )

    response_log = _write_log("model_response", "json", _dump_response(response))
    print(f"[extract] raw model response logged to {response_log}")

    tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
    if not tool_use_blocks:
        raise RuntimeError(
            "model did not return a record_extraction_facts tool call "
            f"(stop_reason={response.stop_reason!r})"
        )
    tool_input = tool_use_blocks[0].input

    return ExtractionFacts.model_validate(tool_input)


def main() -> None:
    try:
        facts = extract_facts(HARDCODED_URL)
    except (RuntimeError, ValidationError) as exc:
        print(f"[extract] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    print(facts.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
