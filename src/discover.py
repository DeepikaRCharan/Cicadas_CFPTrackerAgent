"""Discovery: a hand-written tool-use loop that finds live calls for one
profile.

Build step 8 (docs/project-brief.md, Workflow > step 2). No agent framework:
a plain `while` loop over messages.create (via call_llm). The model decides
each next move from what it just found, using:

- **Server tools** (run by Anthropic inside the API call): web_search and
  web_fetch. The API counts a tool's `max_uses` across a whole exchange
  (every turn since the last plain user message), so each request sets it to
  the uses already billed in the exchange plus the budget left: the hard
  limit holds even though the code can't intercept an individual search, and
  the tool definitions stay identical turn to turn, so the prompt cache
  survives. A spent tool stays listed; the API refuses further uses, unbilled.
- **Decision tools** (run here): check_tracker, record_result,
  accept_candidate, finish. Every decision the model makes goes through one
  of them with a `reason`, so each one lands in the trace as a structured
  record -- and the code can refuse a bad one (an aggregator or blocked URL,
  an already-tracked venue, live_cfp for a page not read, a finish before the
  probes ran).

Two phases, each its own conversation, sequenced by code:

1. **Probes** -- one search per other venue type (brief: every run). The
   phase is given exactly that many searches and is pushed back once if it
   tries to finish with a probe not run.
2. **Main** -- the paper's own venue type, with whatever budget is left and a
   code-written summary of phase 1 (queries, verdicts, accepted, pages read).

The loop only *finds* candidates. Extraction, classify, judge-fit and the
tracker merge happen afterwards, in code (src/run.py), for every accepted
candidate. Every turn is written to a readable Markdown trace (traces/): each
tool call in order, numbered, with the one-line reason the model wrote before
it (verbatim from its own text, or "(no reason stated before this call)" --
never written by the code), a one-line result summary logged by the code from
what actually came back, and the full result list collapsed in <details>
(left out of the terminal rendering).
"""

from __future__ import annotations

import datetime
import html
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from src.config import (
    AGGREGATOR_DOMAINS,
    BLOCKED_DOMAINS,
    BUCKET_VOCABULARY,
    DISCOVERY_FETCH_MAX_CONTENT_TOKENS,
    DISCOVERY_MAX_CANDIDATES,
    DISCOVERY_MAX_FETCHES,
    DISCOVERY_MAX_LOOP_COST_USD,
    DISCOVERY_MAX_SEARCHES,
    DISCOVERY_MAX_TURNS,
    DISCOVERY_PROBE_COST_SHARE,
    DISCOVERY_PROBE_MAX_FETCHES,
    DISCOVERY_PROBE_MAX_TURNS,
    DISCOVERY_PROBE_RESERVE,
    DISCOVERY_FETCHES_PER_TURN_EST,
    DISCOVERY_SEARCH_RESULT_TOKENS_EST,
    DISCOVERY_SEARCHES_PER_TURN_EST,
    DISCOVERY_TURN_OUTPUT_TOKENS_EST,
    MODEL_DISCOVERY,
    WEB_TOOL_TYPES,
)
from src.profile_schema import Profile
from src.tracker import deadline_passed, is_open, lookup_venue, normalize_url
from src.tracker_schema import CallStatus, ProfileClassification, TrackerFile, VenueTier

BUCKET_LABELS = {
    VenueTier.ACADEMIC_PRACTITIONER: "Academic & Practitioner",
    VenueTier.INDUSTRY_TECHNICAL_MEDIA: "Industry & Technical Media",
    VenueTier.CORPORATE_INSTITUTIONAL: "Corporate & Institutional",
    VenueTier.OTHER_AVENUES: "Other Avenues",
}
RESULT_VERDICTS = ["live_cfp", "stale", "aggregator", "not_a_call", "irrelevant"]
MAX_ACCEPT_REFUSALS = 2  # per candidate call page: then recorded as refused, not retried
DEADLINE_EVIDENCE = ["on_call_page", "on_dates_page", "rolling", "not_found"]
# A fetched page with less text than this (a JavaScript shell, a cookie wall)
# is not evidence of anything.
MIN_READABLE_CHARS = 300


# --- Limits and results ------------------------------------------------------


@dataclass
class Limits:
    turns: int = DISCOVERY_MAX_TURNS
    searches: int = DISCOVERY_MAX_SEARCHES
    probe_reserve: int = DISCOVERY_PROBE_RESERVE
    fetches: int = DISCOVERY_MAX_FETCHES
    candidates: int = DISCOVERY_MAX_CANDIDATES
    cost_usd: float = DISCOVERY_MAX_LOOP_COST_USD
    # The probe phase's own caps; the main phase is guaranteed the rest.
    probe_turns: int = DISCOVERY_PROBE_MAX_TURNS
    probe_fetches: int = DISCOVERY_PROBE_MAX_FETCHES
    probe_cost_share: float = DISCOVERY_PROBE_COST_SHARE

    @property
    def probe_cost_usd(self) -> float:
        return self.cost_usd * self.probe_cost_share


@dataclass
class Candidate:
    name: str
    year: int | None
    call_page_url: str
    dates_page_url: str | None
    deadline_evidence: str
    reason: str
    conflict: bool = False  # identity conflict: flagged by run.py, no model calls spent
    deadline_quote: str = ""
    quote_page: str = ""  # the fetched page the code found deadline_quote on


@dataclass
class DiscoveryResult:
    candidates: list[Candidate]
    stop_reason: str
    turns: int
    searches: int
    fetches: int
    probes_missing: list[VenueTier]
    cost: float = 0.0
    # page_key(url) -> readable text the loop fetched; the hand-off's
    # fallback when its own fetch is blocked
    fetched_text: dict[str, str] = field(default_factory=dict)
    judged: list["Judgement"] = field(default_factory=list)


@dataclass
class Judgement:
    """One record_result or accept_candidate call, as the code handled it --
    the trace's Candidates section and the run's counts come from these."""

    tool: str
    label: str  # the URL, or "name (year) -- URL" for accept_candidate
    outcome: str  # "accepted", "refused", or record_result's verdict
    reason: str  # the model's reason, given in the call
    reply: str  # the code's reply, first line


# --- Queries -------------------------------------------------------------------


def conference_cycle_year(today: datetime.date) -> int:
    """The year of the editions whose calls are open now. From July on, this
    year's conferences have mostly closed their calls (the 2026-10-09 run's
    "2026" seed found AIED 2026, deadline February 2026), so the upcoming
    cycle is next year's."""
    return today.year + 1 if today.month >= 7 else today.year


def build_queries(
    profile: Profile, classification: ProfileClassification, today: datetime.date
) -> tuple[list[str], dict[VenueTier, str]]:
    """Seed queries from the profile (brief, Workflow > step 2).

    - Main: the first two keywords, each paired with one of the paper's own
      bucket's phrases, for every bucket the paper was classified into --
      both with the upcoming conference cycle's year (bare seeds returned
      only published articles in live runs).
    - Probes: one per other bucket -- the first keyword with that bucket's
      first phrase, no year."""
    topics = profile.keywords[:2]
    year = conference_cycle_year(today)
    main: list[str] = []
    for bucket in classification.paper_bucket:
        main += [f"{topic} {phrase} {year}" for topic, phrase in zip(topics, BUCKET_VOCABULARY[bucket.value])]
    probes = {
        bucket: f"{topics[0]} {BUCKET_VOCABULARY[bucket.value][0]}"
        for bucket in VenueTier
        if bucket not in classification.paper_bucket
    }
    return main, probes


# Words a reworded probe may drop without changing the venue type it asks for.
_PROBE_FILLER = frozenset({"a", "an", "the", "for", "of", "to", "on", "in"})


def _probe_words(text: str) -> set[str]:
    """Lowercase words without punctuation or filler, plurals folded
    ("submissions" -> "submission")."""
    words = "".join(c if c.isalnum() else " " for c in text.lower()).split()
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in _PROBE_FILLER}


def probe_covered(bucket: VenueTier, queries: list[str]) -> bool:
    """A probe counts as run when any search used one of that bucket's
    phrases or a near-variant of one: every content word of the phrase, in
    any order, with filler words ("for", "the") optional and plurals folded.
    "open call submissions" covers "open call for submissions"; "call for
    submissions" does not (no "open"). The model may reword the seed, but
    not skip the venue type."""
    phrases = [_probe_words(p) for p in BUCKET_VOCABULARY[bucket.value]]
    return any(phrase <= _probe_words(q) for q in queries for phrase in phrases if phrase)


def _on_domain(url: str, domains: frozenset[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def is_aggregator(url: str) -> bool:
    return _on_domain(url, AGGREGATOR_DOMAINS)


def is_blocked(url: str) -> bool:
    return _on_domain(url, BLOCKED_DOMAINS)


def page_key(url: str) -> str:
    """Key for "has this page been read?": the normalized URL, ignoring case.
    Only for that check -- tracker identity keeps paths case-sensitive."""
    return normalize_url(url).lower()


_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")  # [text](url), ![alt](src) -> text
_MD_REF_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")  # [text][ref] -> text
_AUTOLINK = re.compile(r"<(?:https?://|mailto:)[^>\s]*>")
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_QUOTES = str.maketrans({c: "'" for c in "\u2018\u2019\u201a\u201b\u2032\u00b4"}
                        | {c: '"' for c in "\u201c\u201d\u201e\u201f\u2033\u00ab\u00bb"})
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"})


def normalize_for_quote(text: str) -> str:
    """The same text with formatting differences removed, applied to both the
    quote and the page: link and HTML markup stripped to their visible text,
    markdown emphasis/heading markers dropped, curly quotes and every dash
    unified, whitespace collapsed, lowercase. Only formatting goes -- the
    words themselves are untouched."""
    text = html.unescape(text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_REF_LINK.sub(r"\1", text)
    text = _AUTOLINK.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    text = text.translate(_QUOTES).translate(_DASHES)
    text = re.sub(r"[*_`#>]+", " ", text)
    return " ".join(text.split()).lower()


def quote_found(quote: str, page_text: str) -> bool:
    """Whether `quote`'s words appear, whole, in order and contiguous, in
    `page_text`, after normalize_for_quote on both -- formatting differences
    (link markup, curly quotes, dashes, spacing) never block a real quote,
    and a quote whose words aren't on the page still fails."""
    q = normalize_for_quote(quote)
    if not q:
        return False
    # whole words only: "7 oct" must not match inside "7 october"
    return re.search(r"(?<!\w)" + re.escape(q) + r"(?!\w)", normalize_for_quote(page_text)) is not None


_MONTHS = {m: i for i, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1) for m in (name, name[:3], name[:4])}
_MONTH = r"([A-Za-z]{3,9})\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_DATE_PATTERNS = (
    (re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MONTH},?\s+(\d{{4}})\b"), (1, 2, 3)),  # 19 October 2026
    (re.compile(rf"\b{_MONTH}\s+{_DAY},?\s+(\d{{4}})\b"), (2, 1, 3)),  # October 19, 2026
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), None),  # 2026-10-19
)


def quote_dates(quote: str) -> list[datetime.date]:
    """Every full date (day, month and year) written in a deadline quote.
    A date without a day or year is skipped -- it can't say whether the
    deadline has passed."""
    found = []
    for pattern, order in _DATE_PATTERNS:
        for m in pattern.finditer(quote):
            try:
                if order is None:
                    found.append(datetime.date(int(m[1]), int(m[2]), int(m[3])))
                    continue
                day, month, year = (m[i] for i in order)
                if month.lower() in _MONTHS:
                    found.append(datetime.date(int(year), _MONTHS[month.lower()], int(day)))
            except ValueError:  # 31 February
                continue
    return found


def _direct_page_text(url: str) -> str:
    """The page fetched by code, outside the loop (no API cost), with
    navigation, header and footer stripped -- as the hand-off reads it."""
    from src.extract import fetch_page, html_to_text

    return html_to_text(fetch_page(url))


def fetched_page(result_content) -> tuple[bool, str | None, int]:
    """(readable, text, characters) for a web_fetch result. A PDF counts as
    readable (the model reads it) but has no plain text to keep."""
    source = getattr(getattr(result_content, "content", None), "source", None)
    if getattr(source, "type", "") == "base64":
        return True, None, len(getattr(source, "data", "") or "")
    text = getattr(source, "data", "") or ""
    chars = len(text.strip())
    return chars >= MIN_READABLE_CHARS, (text if chars >= MIN_READABLE_CHARS else None), chars


# --- Prompt and tools ------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the discovery step of Cicadas, an agent that finds live calls for \
submissions (conferences, journals' special issues, trade outlets, policy \
forums) that might suit one research paper. You search and read the web, and \
decide, one step at a time, what to look at next. You do not judge fit -- \
that happens later, from the venue's own page. Your job is to find venues' \
own, currently live call pages and hand them over.

How to work:
- Before each tool call -- every search, fetch, record_result, \
check_tracker, accept_candidate and finish -- write one short line of text: \
what you are about to do and why. One line per call, even when you make \
several calls in a row.
- Judge results from their search snippets and titles where you can: \
reject what is plainly stale, off-topic or not a call without fetching it. \
But live_cfp is only for a page you have fetched in this run, that came \
back readable, and that shows an open call or a future deadline. The code \
refuses live_cfp, and accept_candidate, for any page you have not read -- a \
snippet is never enough. Fetches are scarce: spend them on the most \
promising leads, and never fetch the same URL twice.
- Some domains are blocked outright (mass-produced, often predatory \
conference organizers). They are refused: don't record them as live or \
look for them elsewhere.
- For every result you look at closely, call record_result with a verdict: \
live_cfp (a current call on the venue's own site), stale (a past edition or \
closed call), aggregator (a listing site such as WikiCFP or a conference \
index), not_a_call, or irrelevant.
- Aggregators are leads, not candidates: follow through to the venue's own \
call page (search for it or fetch it) before accepting.
- accept_candidate checks the tracker itself and refuses venues already \
tracked for this paper, so accept a live call directly. Use check_tracker \
only to decide whether a lead is worth fetching.
- accept_candidate needs deadline_quote: the exact words, copied from the \
call page or dates page you read, that state the deadline -- or, for a \
rolling call, that say submissions are rolling; or, for "not_found", that \
say the call is open. The code checks the quote against those pages and \
refuses the candidate if it isn't there.
- A call page without a deadline: look for the venue's "important dates" or \
"key dates" page and pass it as dates_page_url. Use deadline_evidence \
"rolling" only when the page says submissions are accepted on a rolling or \
continuous basis. If you cannot find a deadline and the page does not say \
the call is open now, it is not a live call -- do not accept it. Accept \
with "not_found" only for a call the page clearly shows as open whose \
deadline is not stated -- never guess one.
- The year you pass to accept_candidate: the edition year stated on the \
venue's page (e.g. the 2027 congress); for a one-off call with no edition \
year (a journal special issue or research topic), the year of its \
submission deadline; null only for a rolling outlet. Never infer it from \
today's date.
- Refine a query (add a year, a narrower subfield, "special issue", a \
region) when results are stale, off-topic or only aggregators. Never \
combine several venue types in one query, and never repeat a query.
- Call finish, with a reason, when this phase's searches are used up or \
have stopped turning up new live calls. Fewer good candidates beat many \
weak ones.
Every tool result ends with your remaining budget. When a budget runs out, \
further uses of that tool are refused.
"""

CLIENT_TOOLS = [
    {
        "name": "check_tracker",
        "description": "Check whether a venue is already tracked, before accepting it. Identity is the venue's name + edition year, checked against its URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The venue's own name, e.g. 'European Congress of Immunology 2027'."},
                "year": {"type": ["integer", "null"], "description": "Edition year stated on the venue's page; null for a rolling outlet."},
                "url": {"type": "string", "description": "The venue's own call page."},
            },
            "required": ["name", "year", "url"],
        },
    },
    {
        "name": "record_result",
        "description": "Record your verdict on one search result or fetched page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "verdict": {"type": "string", "enum": RESULT_VERDICTS},
                "reason": {"type": "string", "description": "One sentence: what on the page led to this verdict."},
            },
            "required": ["url", "verdict", "reason"],
        },
    },
    {
        "name": "accept_candidate",
        "description": "Hand a venue's live call page over for extraction and fit judging. The call page must have been fetched and readable. Checks the tracker itself: a venue already tracked for this paper is refused.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "year": {"type": ["integer", "null"], "description": "Edition year stated on the venue's page; null for a rolling outlet."},
                "call_page_url": {"type": "string", "description": "The venue's own call page -- never an aggregator listing."},
                "dates_page_url": {"type": ["string", "null"], "description": "Separate 'important dates' page, when the deadline is stated there."},
                "deadline_evidence": {"type": "string", "enum": DEADLINE_EVIDENCE},
                "deadline_quote": {"type": "string", "description": (
                    "Exact words copied from the call page or dates page you read: the deadline statement (or the "
                    "rolling-submissions statement, or for not_found the statement that the call is open). Checked by code.")},
                "reason": {"type": "string", "description": "Why this is a live call worth judging for this paper."},
            },
            "required": ["name", "year", "call_page_url", "dates_page_url", "deadline_evidence", "deadline_quote", "reason"],
        },
    },
    {
        "name": "finish",
        "description": "End this phase of discovery.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Why you are stopping now."}},
            "required": ["reason"],
        },
    },
]


def _paper_block(profile, classification, today) -> str:
    buckets = ", ".join(BUCKET_LABELS[b] for b in classification.paper_bucket)
    return (
        f"Today's date: {today.isoformat()}. The upcoming conference cycle is {conference_cycle_year(today)}. "
        "A call whose deadline is before today is closed: record it stale, don't accept it.\n\n"
        f"Paper: {profile.title}\n\nAbstract:\n{profile.abstract}\n\n"
        f"Keywords: {', '.join(profile.keywords)}\n\n"
        f"Cicadas classified this paper as: {buckets}\n"
        f"Why: {classification.paper_bucket_reasoning}\n\n"
    )


def _probe_opening(profile, classification, probes, state, today) -> str:
    lines = "\n".join(f"- {BUCKET_LABELS[b]}: {q}" for b, q in probes.items())
    return (
        _paper_block(profile, classification, today)
        + "PHASE 1 of 2 -- PROBES. The classification above is a prediction made from the paper "
        "alone; probes let real venues test it. Run each of these as its own search (reword it if you "
        "like, but keep that venue type's phrase in the query), judge what comes back, and fetch and "
        "accept any live call you find. Main searches for the paper's own venue type come in phase 2 -- "
        "don't run them now.\n"
        f"{lines}\n\n"
        f"Phase 1 budget: {state.searches_left()} searches (one per probe), {state.fetches_left()} fetches "
        f"(shared with phase 2), {state.limits.candidates - len(state.candidates)} accepted candidates. "
        "Call finish when every probe has been run and judged."
    )


def _main_opening(profile, classification, main, state, today) -> str:
    seeds = "\n".join(f"- {q}" for q in main)
    summary = state.phase_summary()
    return (
        _paper_block(profile, classification, today)
        + "PHASE 2 of 2 -- MAIN SEARCHES for the paper's own venue type. Start from these seeds and "
        "refine as results suggest:\n"
        f"{seeds}\n\n"
        f"What phase 1 already did (don't redo it):\n{summary}\n\n"
        f"Phase 2 budget: {state.searches_left()} searches, {state.fetches_left()} fetches, "
        f"{state.limits.candidates - len(state.candidates)} accepted candidates. Call finish when done."
    )


# --- Trace -----------------------------------------------------------------------


NO_REASON = "(no reason stated before this call)"
REASON_MAX_CHARS = 240
PENDING_RESULT = "- result: (not in this response -- waiting for the next one)"


def stated_reason(text: str) -> str:
    """The one-line reason the model wrote before a call: the last line of
    its preceding text, verbatim but for markdown emphasis (`**`), which is
    stripped; if that line is long, its last sentence, cut at
    REASON_MAX_CHARS. Never composed by the code."""
    lines = [line.replace("**", "").strip() for line in text.strip().splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return NO_REASON
    line = lines[-1].lstrip(">#- ").strip() or lines[-1]
    if len(line) > REASON_MAX_CHARS:
        sentences = [x for x in line.replace("! ", ". ").replace("? ", ". ").split(". ") if x.strip()]
        line = sentences[-1].strip() if sentences else line
        if len(line) > REASON_MAX_CHARS:
            line = line[:REASON_MAX_CHARS].rstrip() + "..."
    return f'"{line}"'


def _result_summary(name: str, result) -> tuple[str, list[str]]:
    """(one-line summary, full list) for one server tool call's result, from
    what the API actually returned."""
    if result is None:
        return "ran server-side (filtered results before the model saw them)", []
    content = result.content
    if name == "web_search":
        if not isinstance(content, list):
            return f"search error: {getattr(content, 'error_code', content)}", []
        if not content:
            return "no results", []
        items = []
        for item in content:
            age = f" ({item.page_age})" if getattr(item, "page_age", None) else ""
            items.append(f"- {item.title} — {item.url}{age}")
        return f"{len(content)} result(s); top: {items[0][2:]}", items
    if getattr(content, "type", "") == "web_fetch_result":
        title = getattr(getattr(content, "content", None), "title", None)
        readable, page_text, chars = fetched_page(content)
        named = f" — {title}" if title else ""
        if not readable:
            return f"**UNREADABLE** ({chars} characters of text): not usable as evidence — {content.url}", []
        kind = "readable PDF" if page_text is None else f"readable, {chars:,} characters"
        return f"{kind}{named} — {content.url}", []
    return f"fetch error: {getattr(content, 'error_code', content)}", []


class Trace:
    """A readable Markdown record of one profile's run.

    Every tool call is numbered, in the order the model made it. The reason
    the model wrote before a call is shown with it -- or once over a group,
    labelled with the call numbers, when one line of text preceded several
    calls. Each server call gets a one-line result summary, filled in where
    the call is even when its result only arrives with the next response;
    each decision call gets the code's reply. Long lists go in collapsed
    <details> blocks: `markdown()` is the file and the GitHub run page,
    `terminal()` drops them for a local run's console."""

    def __init__(self, title: str):
        self.lines: list[str] = [f"# {title}", ""]
        self.tool_calls: Counter[str] = Counter()
        self._slots: dict[str, int] = {}  # placeholder line index, by key, still to be filled
        self._pending: dict[str, str] = {}  # server call id -> tool name, result not seen yet
        # Display-only hook (the one exception to the 7 Oct freeze): when set,
        # called as listener(event, data) as the trace is written, for live
        # rendering (src/demo.py). It never changes what the trace records or
        # what discovery does; a listener that raises is detached with a warning.
        self.listener = None
        self._call_numbers: dict[str, int] = {}  # call id -> number, for the listener
        self._stated_before: dict[str, str | None] = {}  # decision call id -> inline reason, for the listener

    def _emit(self, event: str, **data) -> None:
        if self.listener is None:
            return
        try:
            self.listener(event, data)
        except Exception as exc:  # display only: never stop or change the run
            print(f"[trace] WARNING: display listener failed ({type(exc).__name__}: {exc}) -- detached; "
                  "the run and its trace continue unchanged", file=sys.stderr)
            self.listener = None

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)
        self._emit("lines", lines=list(lines))

    def heading(self, text: str) -> None:
        self.lines += ["", f"## {text}", ""]
        self._emit("heading", text=text)

    @staticmethod
    def _details_lines(summary: str, items: list[str]) -> list[str]:
        if not items:
            return []
        return [f"<details><summary>{html.escape(summary)}</summary>", "",
                *(html.escape(item, quote=False) for item in items), "", "</details>", ""]

    def details(self, summary: str, items: list[str]) -> None:
        """A collapsed list: shown in the .md file and on the run page, left
        out of the terminal."""
        self.lines += self._details_lines(summary, items)

    def markdown(self) -> str:
        return "\n".join(self.lines) + "\n"

    def terminal(self) -> str:
        out, hidden = [], False
        for line in self.lines:
            if line.startswith("<details>"):
                hidden = True
            elif hidden:
                hidden = line != "</details>"
            else:
                out.append(line)
        return "\n".join(out) + "\n"

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.markdown(), encoding="utf-8")
        return path

    # --- placeholders filled in later (a later response, a handler) ---

    def _slot(self, key: str, placeholder: str) -> None:
        self._slots[key] = len(self.lines)
        self.lines.append(placeholder)

    def _fill(self, key: str, new_lines: list[str]) -> bool:
        index = self._slots.pop(key, None)
        if index is None:
            return False
        self.lines[index:index + 1] = new_lines
        shift = len(new_lines) - 1
        for other, i in self._slots.items():
            if i > index:
                self._slots[other] = i + shift
        return True

    def _result_lines(self, name: str, result, note: str = "") -> list[str]:
        summary, items = _result_summary(name, result)
        details = self._details_lines(f"All {len(items)} results", items)
        return [f"- result: {summary}{note}"] + (["", *details[:-1]] if details else [])

    def close_pending(self) -> None:
        """At the end of a phase: a result that never arrived says so."""
        for call_id in list(self._pending):
            self._fill(f"result:{call_id}", ["- result: (no result returned before this phase ended)"])
        self._pending.clear()

    def _number(self, name: str) -> int:
        self.tool_calls[name] += 1
        return sum(self.tool_calls.values())

    # --- one response ---

    def response_blocks(self, response) -> None:
        """Everything the model and the server did in one response, in
        order. Results for server calls made in an earlier response are
        filled in where those calls were logged. Decision-tool calls get a
        slot their handler fills with the code's reply."""
        content = response.content
        results = {getattr(b, "tool_use_id", None): b for b in content
                   if getattr(b, "type", "") in ("web_search_tool_result", "web_fetch_tool_result")}
        call_ids = {b.id for b in content if getattr(b, "type", "") in ("server_tool_use", "tool_use")}
        for result_id, result in results.items():
            if result_id not in call_ids and result_id in self._pending:
                name = self._pending.pop(result_id)
                self._fill(f"result:{result_id}",
                           self._result_lines(name, result, " (arrived with the next response)"))
                self._emit("late_result", number=self._call_numbers.get(result_id), tool=name,
                           summary=_result_summary(name, result)[0])

        texts = [b.text.strip() for b in content if getattr(b, "type", "") == "text" and b.text.strip()]
        if texts:
            self.details("Model's full text this turn",
                         [f"> {line}" if line.strip() else ">" for t in texts for line in t.splitlines()])

        groups: list[tuple[str | None, list]] = []  # (text written before the calls, calls)
        for block in content:
            kind = getattr(block, "type", "")
            if kind == "text" and block.text.strip():
                groups.append((block.text, []))
            elif kind in ("server_tool_use", "tool_use"):
                if not groups:
                    groups.append((None, []))
                groups[-1][1].append(block)
        for text, calls in groups:
            if not calls:
                continue
            reason = stated_reason(text) if text else NO_REASON
            numbers = [self._number(block.name) for block in calls]
            single = len(calls) == 1
            if not single:
                label = f"#{numbers[0]}–#{numbers[-1]}"
                self.lines += [f"Reason stated before calls {label}: {reason}" if text
                               else f"(no reason stated before calls {label})", ""]
                self._emit("reason_group", first=numbers[0], last=numbers[-1], reason=reason if text else None)
            for number, block in zip(numbers, calls):
                if block.type == "server_tool_use":
                    self._server_call(number, block, results.get(block.id), reason if single else None)
                else:
                    self._decision_slot(number, block, reason if single else None)

    def _server_call(self, number: int, block, result, reason: str | None) -> None:
        inp = block.input if isinstance(block.input, dict) else {}
        target = (f"`{inp.get('query', '?')}`" if block.name == "web_search"
                  else inp.get("url", "?") if block.name == "web_fetch" else "")
        self.lines.append(f"**#{number} {block.name}** {target}".rstrip())
        if reason is not None:
            self.lines.append(f"- reason (before the call): {reason}")
        self._call_numbers[block.id] = number
        if result is None and block.name in ("web_search", "web_fetch"):
            self._pending[block.id] = block.name
            self._slot(f"result:{block.id}", PENDING_RESULT)
            self.lines.append("")
            summary = None
        else:
            self.lines += [*self._result_lines(block.name, result), ""]
            summary = _result_summary(block.name, result)[0]
        self._emit("server_call", number=number, tool=block.name, target=inp.get("query") or inp.get("url"),
                   reason=reason, summary=summary)

    def _decision_slot(self, number: int, block, reason: str | None) -> None:
        args = block.input if isinstance(block.input, dict) else {}
        shown = {k: v for k, v in args.items() if k != "reason"}
        self.lines.append(f"**#{number} {block.name}** {json.dumps(shown, ensure_ascii=False)}")
        if reason is not None:
            self.lines.append(f"- reason (before the call): {reason}")
        self._call_numbers[block.id] = number
        self._stated_before[block.id] = reason
        self._slot(f"decision:{block.id}", "- code: (not handled)")
        self.lines.append("")

    def decision(self, block_id: str, name: str, args: dict, reply: str, refused: bool) -> None:
        lines = [f"- reason (in the call): {args['reason']}"] if args.get("reason") else []
        lines.append(f"- {'REFUSED by code' if refused else 'code'}: {reply.splitlines()[0] if reply else ''}")
        number = self._call_numbers.get(block_id)
        if not self._fill(f"decision:{block_id}", lines):  # a call response_blocks never saw
            number = self._number(name)
            shown = {k: v for k, v in args.items() if k != "reason"}
            self.lines += [f"**#{number} {name}** {json.dumps(shown, ensure_ascii=False)}", *lines, ""]
        self._emit("decision", number=number, tool=name, args=dict(args), reason=self._stated_before.pop(block_id, None),
                   reason_in_call=args.get("reason"), reply=reply.splitlines()[0] if reply else "", refused=refused)

    def candidates(self, judged: list["Judgement"]) -> None:
        """The Candidates section: every record_result / accept_candidate
        decision, as the code handled it."""
        self.heading("Candidates")
        if not judged:
            self.add("No results were judged.")
        for j in judged:
            if j.tool == "accept_candidate":
                status = "ACCEPTED" if j.outcome == "accepted" else "REFUSED BY CODE (accept_candidate)"
            elif j.outcome == "refused":
                status = "REFUSED BY CODE (record_result)"
            elif j.outcome == "live_cfp":
                status = "JUDGED LIVE"
            else:
                status = f"REJECTED ({j.outcome})"
            self.add(f"- **{status}**: {j.label}", f"  - reason (in the call): {j.reason or '(none given)'}",
                     f"  - code: {j.reply}")


# --- The loop ----------------------------------------------------------------------


@dataclass
class _State:
    limits: Limits
    probes: dict[VenueTier, str]
    model: str = MODEL_DISCOVERY
    phase: str = "probes"
    phase_search_cap: int = 0
    phase_searches: int = 0
    phase_turns: int = 0
    phase_fetches: int = 0
    turns: int = 0
    searches: int = 0
    fetches: int = 0
    # Uses billed since the last plain user message: the API counts a web
    # tool's max_uses across that whole exchange, not per request.
    exchange_searches: int = 0
    exchange_fetches: int = 0
    fetched: set[str] = field(default_factory=set)  # normalized URLs fetched and readable
    unreadable: set[str] = field(default_factory=set)  # normalized URLs fetched but empty
    fetched_text: dict[str, str] = field(default_factory=dict)
    queries: list[str] = field(default_factory=list)  # searches that returned results
    recorded: list[tuple[str, str, str]] = field(default_factory=list)  # (url, verdict, reason)
    candidates: list[Candidate] = field(default_factory=list)
    judged: list[Judgement] = field(default_factory=list)
    cost: float = 0.0
    max_turn_cost: float = 0.0
    # Tokens in the current conversation's prompt (everything written to it
    # so far); what the next turn re-reads from the cache.
    context_tokens: int = 0
    finished: str | None = None
    finish_pushed_back: bool = False
    limit_logged: set[str] = field(default_factory=set)
    # Server calls whose result hasn't come back yet: a search or fetch made
    # just before a decision-tool call gets its result at the start of the
    # next response, not the same one. id -> (tool name, input).
    pending_calls: dict[str, tuple[str, dict]] = field(default_factory=dict)
    # accept_candidate refusals per call page (page_key); at MAX_ACCEPT_REFUSALS
    # the candidate is recorded as refused and not evaluated again.
    accept_refusals: Counter = field(default_factory=Counter)
    log_dir: Path | None = None  # where each web_fetch's page text is saved
    fetch_logs: int = 0

    def start_phase(self, phase: str, search_cap: int) -> None:
        self.phase, self.phase_search_cap, self.phase_searches = phase, search_cap, 0
        self.phase_turns = self.phase_fetches = 0
        self.pending_calls.clear()  # a new conversation: an old call's result never arrives
        self.context_tokens = 0  # each phase is a fresh conversation
        self.finished, self.finish_pushed_back = None, False
        self.new_exchange()

    def new_exchange(self) -> None:
        self.exchange_searches = self.exchange_fetches = 0

    def searches_left(self) -> int:
        return max(0, min(self.limits.searches - self.searches, self.phase_search_cap - self.phase_searches))

    def fetches_left(self) -> int:
        left = self.limits.fetches - self.fetches
        if self.phase == "probes":
            left = min(left, self.limits.probe_fetches - self.phase_fetches)
        return max(0, left)

    def was_read(self, url: str) -> bool:
        return page_key(url) in self.fetched

    def missing_probes(self) -> list[VenueTier]:
        return [b for b in self.probes if not probe_covered(b, self.queries)]

    def budget_line(self) -> str:
        missing = self.missing_probes() if self.phase == "probes" else []
        probes = f" (probes still to run: {', '.join(BUCKET_LABELS[b] for b in missing)})" if missing else ""
        pages = ""
        if self.fetched or self.unreadable:
            pages = (f"; pages read: {', '.join(sorted(self.fetched)) or 'none'}"
                     + (f"; unreadable (don't refetch): {', '.join(sorted(self.unreadable))}" if self.unreadable else ""))
        return (
            f"[budget] {self.phase} phase: searches left {self.searches_left()}{probes}; "
            f"fetches left {self.fetches_left()}; "
            f"candidates left {self.limits.candidates - len(self.candidates)}; "
            f"turns left {self.limits.turns - self.turns}; "
            f"spend so far ${self.cost:.3f} of ${self.limits.cost_usd:.2f}{pages}"
        )

    def phase_summary(self) -> str:
        lines = [f"- searched: {q}" for q in self.queries] or ["- (no searches returned results)"]
        lines += [f"- {verdict}: {url} -- {reason}" for url, verdict, reason in self.recorded]
        lines += [f"- accepted: {c.name} ({c.call_page_url})" for c in self.candidates]
        if self.fetched:
            lines.append(f"- pages already read: {', '.join(sorted(self.fetched))}")
        if self.unreadable:
            lines.append(f"- unreadable, don't refetch: {', '.join(sorted(self.unreadable))}")
        return "\n".join(lines)


def _server_tools(state: _State) -> list[dict]:
    """max_uses = uses already billed in this exchange + budget left: the
    exchange total, so it caps the run at exactly the budget and stays the
    same between turns (cache-friendly). A spent tool stays listed -- its
    further uses are refused by the API, unbilled -- because changing the
    tool list would invalidate the whole cached prefix. It is only left out
    when a fresh exchange starts with nothing left (max_uses can't be 0)."""
    search_type, fetch_type = WEB_TOOL_TYPES[state.model]
    blocked = sorted(BLOCKED_DOMAINS)
    tools = []
    search_uses = state.exchange_searches + state.searches_left()
    fetch_uses = state.exchange_fetches + state.fetches_left()
    if search_uses:
        tools.append({"type": search_type, "name": "web_search", "blocked_domains": blocked, "max_uses": search_uses})
    if fetch_uses:
        tools.append({
            "type": fetch_type, "name": "web_fetch", "blocked_domains": blocked, "max_uses": fetch_uses,
            "max_content_tokens": DISCOVERY_FETCH_MAX_CONTENT_TOKENS,
        })
    return tools


def _count_usage(state: _State, response) -> None:
    """Budgets count what the API billed (usage.server_tool_use): a search
    refused with max_uses_exceeded is not a use. Only searches that returned
    results count toward probes, and only fetches that came back readable
    unlock live_cfp / accept_candidate for their URL."""
    results = {getattr(b, "tool_use_id", None): b for b in response.content
               if getattr(b, "type", "") in ("web_search_tool_result", "web_fetch_tool_result")}
    calls = {b.id: (b.name, b.input) for b in response.content
             if getattr(b, "type", "") == "server_tool_use" and isinstance(b.input, dict)}
    ok_searches = ok_fetches = 0
    for result_id, result in results.items():
        if result_id in calls:
            name, inp = calls[result_id]
        elif result_id in state.pending_calls:  # the call was made in an earlier response
            name, inp = state.pending_calls.pop(result_id)
        else:
            continue
        if name == "web_search" and isinstance(result.content, list):
            ok_searches += 1
            state.queries.append(inp.get("query", ""))
        elif name == "web_fetch":
            _log_fetch(state, inp.get("url", "?"), result.content)
        if name == "web_fetch" and getattr(result.content, "type", "") == "web_fetch_result":
            ok_fetches += 1
            readable, text, _ = fetched_page(result.content)
            for url in {u for u in (inp.get("url"), getattr(result.content, "url", None)) if u}:
                key = page_key(url)
                if readable:
                    state.fetched.add(key)
                    state.unreadable.discard(key)
                    if text:
                        state.fetched_text[key] = text
                elif key not in state.fetched:
                    state.unreadable.add(key)
    for call_id, call in calls.items():
        if call_id not in results:
            state.pending_calls[call_id] = call
    server = getattr(getattr(response, "usage", None), "server_tool_use", None)
    searches = ok_searches if server is None else (getattr(server, "web_search_requests", 0) or 0)
    fetches = ok_fetches if server is None else (getattr(server, "web_fetch_requests", 0) or 0)
    state.searches += searches
    state.phase_searches += searches
    state.fetches += fetches
    state.phase_fetches += fetches
    state.exchange_searches += searches
    state.exchange_fetches += fetches
    usage = getattr(response, "usage", None)
    if usage is not None:
        from src.llm import usage_cost

        turn_cost = usage_cost(
            state.model, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0, getattr(usage, "cache_read_input_tokens", 0) or 0,
            searches,
        )
        state.cost += turn_cost
        state.max_turn_cost = max(state.max_turn_cost, turn_cost)
        state.context_tokens += (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)


def _log_fetch(state: _State, url: str, content) -> None:
    """Save what one web_fetch in the loop returned to logs/: the page text
    exactly as the model saw it (what deadline quotes are checked against),
    or the error. One file per fetch."""
    if state.log_dir is None:
        return
    state.fetch_logs += 1
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_url(url).lower()).strip("-")[:60] or "page"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(state.log_dir) / f"{stamp}_loop_fetch_{state.fetch_logs:02d}_{slug}.txt"
    if getattr(content, "type", "") == "web_fetch_result":
        readable, text, chars = fetched_page(content)
        body = text if text is not None else ("(PDF -- read by the model, no plain text kept)" if readable
                                               else getattr(getattr(getattr(content, "content", None), "source", None), "data", "") or "")
        head = f"URL requested: {url}\nURL fetched: {content.url}\nReadable: {readable} ({chars} characters)\n"
    else:
        body, head = "", f"URL requested: {url}\nFetch error: {getattr(content, 'error_code', content)}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(head + "\n" + body, encoding="utf-8")
    except OSError as exc:  # a log that can't be written never stops discovery, but says so
        print(f"[discover] WARNING: couldn't save fetched page text to {path}: {exc}")


def _project_next_turn(state: _State) -> float:
    """Plausible cost of the next turn, from the budget actually left: the
    searches and fetches it could still make (capped at the most one turn has
    been seen to make), one cached re-read of the conversation per server
    pass, their results written to the cache, a turn's output, and the search
    fees. With no searches left, the projection has no search cost at all.

    Calls made together share a pass: the 2026-10-09 run's turn with two
    fetches re-read its context twice, not three times, and its turn with two
    searches once past the opening. So a pass for the opening, one for a
    batch of searches and one for a batch of fetches -- at most 3, not one
    per call."""
    from src.llm import usage_cost

    searches = min(state.searches_left(), DISCOVERY_SEARCHES_PER_TURN_EST)
    fetches = min(state.fetches_left(), DISCOVERY_FETCHES_PER_TURN_EST)
    passes = 1 + (searches > 0) + (fetches > 0)
    context = max(state.context_tokens, 5_000)  # the opening alone is a few thousand tokens
    new_tokens = searches * DISCOVERY_SEARCH_RESULT_TOKENS_EST + fetches * DISCOVERY_FETCH_MAX_CONTENT_TOKENS + 2_000
    return usage_cost(state.model, 0, DISCOVERY_TURN_OUTPUT_TOKENS_EST, cache_write_tokens=new_tokens,
                      cache_read_tokens=passes * context, searches=searches)


def _log_limits(state: _State, trace: Trace) -> None:
    checks = (("search", state.limits.searches - state.searches, state.limits.searches),
              ("fetch", state.fetches_left(), state.limits.fetches))
    for key, left, limit in checks:
        if left <= 0 and key not in state.limit_logged:
            state.limit_logged.add(key)
            message = f"LIMIT: {key} limit ({limit}) reached -- further web_{key} uses are refused"
            print(f"[discover] {message}")
            trace.add(f"**{message}**", "")


class _Handlers:
    def __init__(self, state, tracker, profile_id, trace, today: datetime.date, fetch_text=_direct_page_text):
        self.state, self.tracker, self.profile_id, self.trace = state, tracker, profile_id, trace
        self.today, self.fetch_text = today, fetch_text

    def run(self, block) -> dict:
        args = block.input if isinstance(block.input, dict) else {}
        handler = getattr(self, f"_{block.name}", None)
        page = page_key(str(args.get("call_page_url", ""))) if block.name == "accept_candidate" else None
        if handler is None:
            reply, refused = f"unknown tool {block.name!r}", True
        elif page is not None and self.state.accept_refusals[page] >= MAX_ACCEPT_REFUSALS:
            reply, refused = (f"not accepted: this call page was already refused {MAX_ACCEPT_REFUSALS} times and is "
                              "recorded as refused -- not evaluated again; move on to other leads"), True
        else:
            try:
                reply, refused = handler(args)
            except (KeyError, TypeError, ValueError) as exc:
                reply, refused = f"bad arguments: {exc}", True
            if page is not None and refused:
                self.state.accept_refusals[page] += 1
                if self.state.accept_refusals[page] == MAX_ACCEPT_REFUSALS:
                    reply += (f" -- refused {MAX_ACCEPT_REFUSALS} times: recorded as refused, and further attempts "
                              "on this call page won't be evaluated")
        self.trace.decision(block.id, block.name, args, reply, refused)
        first_line = reply.split("\n")[0]
        if block.name == "record_result":
            self.state.judged.append(Judgement(
                block.name, str(args.get("url", "?")), "refused" if refused else str(args.get("verdict", "?")),
                str(args.get("reason", "")), first_line))
        elif block.name == "accept_candidate":
            year = args.get("year")
            label = f"{args.get('name', '?')} ({year if year is not None else 'rolling'}) — {args.get('call_page_url', '?')}"
            if not refused:
                c = self.state.candidates[-1]
                first_line += f'; deadline quote verified on {c.quote_page}: "{c.deadline_quote}"'
            self.state.judged.append(Judgement(block.name, label, "refused" if refused else "accepted",
                                               str(args.get("reason", "")), first_line))
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": f"{reply}\n{self.state.budget_line()}",
            "is_error": refused,
        }

    def _not_read(self, url: str) -> str:
        if page_key(url) in self.state.unreadable:
            return f"{url} was fetched but came back with no readable text -- it can't be evidence"
        return f"{url} has not been fetched in this run"

    def _check_tracker(self, args) -> tuple[str, bool]:
        status, detail = self._tracker_status(args["name"], args["year"], args["url"])
        return f"{status}: {detail}", False

    def _tracker_status(self, name, year, url) -> tuple[str, str]:
        found = lookup_venue(self.tracker, name, year, url)
        status, detail = self._tracker_status_of(found)
        if found.alias is not None:
            detail += f" (tracked as {found.entry.name!r}: same URL and year and a similar name, so the same venue; this name is kept as an alias)"
        return status, detail

    def _tracker_status_of(self, found) -> tuple[str, str]:
        if found.conflict is not None:
            return "conflict", (
                f"clashes with tracked {found.entry.id!r} at {found.entry.url} ({found.conflict.value}); "
                "accepting records it for the researcher to check, nothing is merged"
            )
        entry = found.entry
        if entry is None:
            return "new", f"not tracked (id would be {found.conference_id!r}) -- may be accepted"
        match = entry.matches.get(self.profile_id)
        if match is not None and is_open(match):
            return "tracked_open", f"{entry.id!r} is tracked and open for this paper -- re-checked by code every run, skip it"
        if match is not None:
            return "tracked_closed", f"{entry.id!r} is closed for this paper ({match.decision.value}) -- skip it"
        if deadline_passed(entry.extraction, self.today):  # closed by the deadline, never by a stored reading
            return "tracked_closed", f"{entry.id!r} is tracked and its call is closed -- skip it"
        return "tracked_other_paper", f"{entry.id!r} is tracked for another paper -- may be accepted, to score it for this one"

    def _record_result(self, args) -> tuple[str, bool]:
        url, verdict = args["url"].strip(), args["verdict"]
        if verdict not in RESULT_VERDICTS:
            return f"verdict must be one of {RESULT_VERDICTS}", True
        if is_blocked(url):
            if verdict == "live_cfp":
                return f"{url} is on a blocked domain -- refused outright; do not pursue it", True
            self.state.recorded.append((url, verdict, args.get("reason", "")))
            return "noted (blocked domain -- do not pursue it)", False
        if verdict == "live_cfp" and not self.state.was_read(url):
            return f"live_cfp refused: {self._not_read(url)} -- record a different verdict, or fetch it if you can", True
        self.state.recorded.append((url, verdict, args.get("reason", "")))
        return "noted", False

    def _accept_candidate(self, args) -> tuple[str, bool]:
        state = self.state
        name, year, url = args["name"].strip(), args["year"], args["call_page_url"].strip()
        dates_url = (args.get("dates_page_url") or "").strip() or None
        evidence = args["deadline_evidence"]
        if len(state.candidates) >= state.limits.candidates:
            return f"candidate limit ({state.limits.candidates}) reached -- not accepted; call finish", True
        if not name or not url.startswith(("http://", "https://")):
            return "need a name and an http(s) call_page_url", True
        if year is not None and not (isinstance(year, int) and 2000 <= year <= 2100):
            return f"year {year!r} is not an edition year -- use the year on the venue's page, or null if rolling", True
        if evidence not in DEADLINE_EVIDENCE:
            return f"deadline_evidence must be one of {DEADLINE_EVIDENCE}", True
        if evidence == "on_dates_page" and not dates_url:
            return "deadline_evidence is on_dates_page but no dates_page_url was given", True
        for u in filter(None, (url, dates_url)):
            if is_blocked(u):
                return f"{u} is on a blocked domain -- refused outright; do not look for it elsewhere", True
            if is_aggregator(u):
                return f"{u} is an aggregator listing -- follow through to the venue's own call page", True
        status, detail = self._tracker_status(name, year, url)
        if status in ("tracked_open", "tracked_closed"):
            return f"not accepted -- {detail}", True
        if not state.was_read(url):
            return f"not accepted: the call page {self._not_read(url)} -- fetch a readable copy first", True
        if evidence == "on_dates_page" and not state.was_read(dates_url):
            return f"not accepted: deadline_evidence is on_dates_page but {self._not_read(dates_url)}", True
        quote = (args.get("deadline_quote") or "").strip()
        pages = [u for u in (url, dates_url) if u]
        texts = [state.fetched_text.get(page_key(u)) for u in pages]
        if not quote:
            return "not accepted: deadline_quote is empty -- copy the deadline wording from the page you read", True
        quote_page = next((u for u, t in zip(pages, texts) if t and quote_found(quote, t)), None)
        checked_directly = ""
        if quote_page is None and any(texts):
            # The loop's copy is cut at DISCOVERY_FETCH_MAX_CONTENT_TOKENS: on a page
            # with a long menu (EDUCON 2027, 2026-10-09) it ends before the post
            # itself. Check the same pages fetched directly, menus stripped --
            # the quote must still appear word for word.
            for u in pages:
                try:
                    if quote_found(quote, self.fetch_text(u)):
                        quote_page, checked_directly = u, " (on a direct fetch: the loop's copy was cut short)"
                        break
                except RuntimeError as exc:
                    checked_directly += f" (direct fetch failed: {exc})"
        if quote_page is None:
            where = " or ".join(pages)
            if not any(texts):
                return f"not accepted: no text was kept for {where} (e.g. a PDF), so the quote can't be checked", True
            return (f"not accepted: deadline_quote {quote!r} does not appear on {where}{checked_directly} -- copy the "
                    "exact wording from the page, or don't accept it"), True
        dates = quote_dates(quote) if evidence in ("on_call_page", "on_dates_page") else []
        if dates and max(dates) < self.today:
            return (f"not accepted: deadline passed ({max(dates).isoformat()} is before today, "
                    f"{self.today.isoformat()}) -- move on to other leads"), True
        candidate_id = lookup_venue(self.tracker, name, year, url).conference_id
        if any(lookup_venue(self.tracker, c.name, c.year, c.call_page_url).conference_id == candidate_id
               for c in state.candidates):
            return f"{candidate_id!r} was already accepted this run", True
        state.candidates.append(Candidate(name, year, url, dates_url, evidence, args.get("reason", ""),
                                          conflict=status == "conflict", deadline_quote=quote, quote_page=quote_page))
        tail = " -- candidate limit now reached, discovery will end" if len(state.candidates) >= state.limits.candidates else ""
        return f"accepted as {candidate_id!r} ({status}){checked_directly}{tail}", False

    def _finish(self, args) -> tuple[str, bool]:
        state = self.state
        missing = state.missing_probes() if state.phase == "probes" else []
        if missing and state.searches_left() and not state.finish_pushed_back:
            state.finish_pushed_back = True
            seeds = "; ".join(f"{BUCKET_LABELS[b]}: {state.probes[b]}" for b in missing)
            return f"probes not run yet -- run each as its own search before finishing ({seeds})", True
        state.finished = args.get("reason", "") or "(no reason given)"
        return f"{state.phase} phase finished", False


def _run_phase(state: _State, handlers: _Handlers, trace: Trace, call, opening: str) -> str | None:
    """One phase as its own conversation. Returns a stop reason that ends
    discovery outright (a limit, the model stopping), or None when the
    phase simply finished."""
    limits = state.limits
    messages: list[dict] = [{"role": "user", "content": opening}]
    nudged = False
    probes = state.phase == "probes"
    while True:
        if state.turns >= limits.turns:
            return f"LIMIT: turn limit ({limits.turns}) reached"
        projected = _project_next_turn(state)
        if probes and (state.phase_turns >= limits.probe_turns or state.cost + projected > limits.probe_cost_usd):
            # The probe phase's own budget: it ends here, and the main phase
            # still gets everything reserved for it.
            hit = (f"{limits.probe_turns} turns" if state.phase_turns >= limits.probe_turns else
                   f"${limits.probe_cost_usd:.3f} spend (spent ${state.cost:.3f}; next turn projected ${projected:.3f})")
            state.finished = f"probe phase budget reached ({hit}) -- the main searches keep their reserved budget"
            trace.add(f"**{state.finished}**")
            return None
        if state.cost + projected > limits.cost_usd and not probes and state.phase_turns == 0:
            # Never starved: the main phase's first turn always runs, even if
            # the probe phase cost more than projected. The overrun is at most
            # one main turn, and the trace says so.
            trace.add(f"**main phase's first turn runs despite the loop cost limit (spent ${state.cost:.3f}; "
                      f"next turn projected ${projected:.3f}) -- the main searches are never starved by the probes**")
        elif state.cost + projected > limits.cost_usd:
            return (f"LIMIT: loop cost limit (${limits.cost_usd:.2f}) -- another turn could exceed it "
                    f"(spent ${state.cost:.3f}; next turn projected ${projected:.3f} with "
                    f"{state.searches_left()} searches and {state.fetches_left()} fetches left)")
        # Automatic prompt caching: each turn resends the whole conversation,
        # so the cached prefix is read back at a tenth of the input price.
        response = call(model=state.model, system=SYSTEM_PROMPT, messages=messages,
                        tools=CLIENT_TOOLS + _server_tools(state), max_tokens=16000,
                        cache_control={"type": "ephemeral"})
        state.turns += 1
        state.phase_turns += 1
        trace.heading(f"Turn {state.turns} ({state.phase} phase)")
        _count_usage(state, response)
        trace.response_blocks(response)
        _log_limits(state, trace)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
            trace.add("_(server paused a long turn; resuming)_")
            continue
        tool_uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        if not tool_uses:
            if response.stop_reason == "end_turn" and not nudged:
                nudged = True
                trace.add("_(model ended its turn without calling finish; asked once to continue or finish)_")
                messages.append({"role": "user", "content": "Continue this phase, or call finish with a reason."})
                state.new_exchange()  # a plain user message restarts the API's max_uses count
                continue
            return f"model stopped without calling finish (stop_reason={response.stop_reason})"

        results = [handlers.run(block) for block in tool_uses]
        if len(state.candidates) >= limits.candidates:
            return f"LIMIT: candidate limit ({limits.candidates}) reached"
        if state.finished is not None:
            trace.add(f"**{state.phase} phase finished: {state.finished}**")
            return None
        messages.append({"role": "user", "content": results})


def discover(
    profile: Profile,
    classification: ProfileClassification,
    tracker: TrackerFile,
    *,
    trace: Trace,
    today: datetime.date,
    limits: Limits | None = None,
    model: str = MODEL_DISCOVERY,
    call=None,
    log_dir: str | Path | None = None,
    fetch_text=_direct_page_text,
) -> DiscoveryResult:
    """Run discovery for one profile: the probe phase, then the main phase.
    Reads the tracker (for check_tracker) but never writes it. `call` is
    call_llm, and `fetch_text` the direct page fetch a quote missing from the
    loop's copy is checked against, unless a test injects fakes."""
    if call is None:
        from src.llm import call_llm as call
    limits = limits or Limits()
    main, probes = build_queries(profile, classification, today)
    if model not in WEB_TOOL_TYPES:
        raise ValueError(f"no web tool versions configured for {model!r} (config.WEB_TOOL_TYPES)")
    state = _State(limits=limits, probes=probes, model=model, log_dir=Path(log_dir) if log_dir else None)
    handlers = _Handlers(state, tracker, profile.id, trace, today, fetch_text)
    probe_searches = min(len(probes), limits.probe_reserve, limits.searches)

    trace.add(
        f"Paper: {profile.title} (`{profile.id}`)",
        f"Loop model: {model}, web tools {' + '.join(WEB_TOOL_TYPES[model])}",
        f"Read as: {', '.join(BUCKET_LABELS[b] for b in classification.paper_bucket)}",
        "Cues that drove that call:", *(f"- {cue}" for cue in classification.paper_bucket_cues),
        f"Reasoning: {classification.paper_bucket_reasoning}", "",
        f"Limits: {limits.turns} turns, {limits.searches} searches ({probe_searches} for probes), "
        f"{limits.fetches} fetches, {limits.candidates} candidates, ${limits.cost_usd:.2f} loop spend",
        f"Probe phase capped at {limits.probe_turns} turns, {limits.probe_fetches} fetches and "
        f"${limits.probe_cost_usd:.3f} spend; the rest is reserved for the main searches",
        "", "Main seeds: " + "; ".join(f"`{q}`" for q in main),
        "Probe seeds: " + "; ".join(f"{BUCKET_LABELS[b]}: `{q}`" for b, q in probes.items()),
    )

    stop_reason: str | None = None
    if probes and probe_searches:
        state.start_phase("probes", probe_searches)
        trace.heading("Phase 1: probes")
        stop_reason = _run_phase(state, handlers, trace, call, _probe_opening(profile, classification, probes, state, today))
        trace.close_pending()
    if stop_reason is None:
        state.start_phase("main", limits.searches - state.searches)
        if state.searches_left() or not (probes and probe_searches):
            trace.heading("Phase 2: main searches")
            stop_reason = _run_phase(state, handlers, trace, call, _main_opening(profile, classification, main, state, today))
            trace.close_pending()
            if stop_reason is None:
                stop_reason = f"finish: {state.finished}"
        else:
            stop_reason = "main phase skipped: no searches left after the probes"

    missing = state.missing_probes()
    print(f"[discover] {profile.id}: STOPPED: {stop_reason}")
    trace.heading("Stopped")
    trace.add(f"**{stop_reason}**", "",
              f"Used {state.turns} turns, {state.searches} searches, {state.fetches} fetches, "
              f"about ${state.cost:.3f}; accepted {len(state.candidates)} candidate(s).")
    if missing:
        message = f"probes not run: {', '.join(BUCKET_LABELS[b] for b in missing)}"
        print(f"[discover] {profile.id}: WARNING: {message}")
        trace.add(f"**WARNING: {message}**")
    trace.candidates(state.judged)
    return DiscoveryResult(state.candidates, stop_reason, state.turns, state.searches, state.fetches, missing,
                           state.cost, dict(state.fetched_text), list(state.judged))
