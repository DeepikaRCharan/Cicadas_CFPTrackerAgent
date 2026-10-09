"""Demo presentation layer: renders what a run's trace and the tracker
already record, in four readable sections per paper, then a results table.
Display only -- it never calls a model, never changes the tracker, and never
changes what discovery or the hand-off do.

    python -m src.run --demo --tracker scratch/tracker.json   # live: streams as the run happens
    python -m src.demo                                        # replay the latest saved traces (dry render)
    python -m src.demo traces/20261007T054110Z_*.md           # replay chosen traces

Live, it listens to the Trace display hook (src/discover.py, `Trace.listener`)
and to the hand-off results run.py hands it; replayed, it parses the saved
trace Markdown into the same events. Either way the same renderer draws:

1. Reading the paper (agent judgment) -- the bucket the paper was read as, its cues, the reasoning.
2. Searching (agent-directed) -- each search and fetch as it happens, with the model's stated reason.
3. Verifying (code-checked) -- each candidate accepted or rejected, and why (quote mismatch,
   page not read, closed, incomplete ...).
4. Scoring (agent judgment) -- per venue, its bucket(s) and theme/genre/format scores, one line each.

Throughout, a running counter -- steps (tool calls), model calls, tokens and
dollars -- sits in a footer on a terminal, refreshed live, and is printed as a
checkpoint after Searching and Scoring. Then a results table (sorted by
overall score; rows clearing the 50% threshold highlighted), a final totals
line, and a Markdown copy of everything in runs/<date>_demo.md as a backup.
Live, tokens and dollars come from the run's own usage meter (src/llm.py);
replayed, only per paper, from each trace's usage lines. Styling is for large terminal fonts: no dimmed text, one column of
flowing text, a compact table.
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich import box
from rich.console import Console
from rich.live import Live
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from src.config import NEW_FIT_MIN, TRACES_DIR, TRACKER_PATH
from src.tracker_schema import AccessType, CallStatus, ConferenceEntry, OtherAvenuesFit, StandardFit, TrackerFile, VenueTier

RUNS_DIR = "runs"
WIDE = 120  # terminal columns at which the results table gets one column per field
VENUE_MAX = 70  # characters of a venue name shown in the results table
PROBLEM_MAX = 240  # characters of a run problem shown in the demo; the log keeps all of it
BUCKETS = {
    VenueTier.ACADEMIC_PRACTITIONER: "Academic & Practitioner",
    VenueTier.INDUSTRY_TECHNICAL_MEDIA: "Industry & Technical Media",
    VenueTier.CORPORATE_INSTITUTIONAL: "Corporate & Institutional",
    VenueTier.OTHER_AVENUES: "Other Avenues",
}
SHORT_BUCKETS = {
    VenueTier.ACADEMIC_PRACTITIONER: "Academic",
    VenueTier.INDUSTRY_TECHNICAL_MEDIA: "Industry",
    VenueTier.CORPORATE_INSTITUTIONAL: "Corporate",
    VenueTier.OTHER_AVENUES: "Other",
}
VERDICTS = {
    "stale": "stale (past edition or closed call)",
    "aggregator": "aggregator listing, not the venue's own page",
    "not_a_call": "not a call for submissions",
    "irrelevant": "off topic",
}
# Why the code refused a candidate, from its reply (src/discover.py wording)
REFUSAL_REASONS = [
    ("already refused", "refused twice already, not re-checked"),
    ("does not appear on", "quote mismatch"),
    ("deadline_quote is empty", "no deadline quote"),
    ("no readable text", "page unreadable"),
    ("has not been fetched", "page not read yet"),
    ("not been fetched in this run", "page not read yet"),
    ("aggregator listing", "aggregator listing"),
    ("blocked domain", "blocked domain"),
    ("tracked and open", "already tracked"),
    ("is closed for this paper", "closed"),
    ("call is closed", "closed"),
    ("not an edition year", "bad edition year"),
    ("candidate limit", "candidate limit reached"),
    ("already accepted this run", "already accepted"),
]


def _one_line(text: str | None, limit: int = 110) -> str:
    """First sentence, on one line, cut at `limit` characters."""
    if not text:
        return ""
    flat = " ".join(text.split())
    first = re.split(r"(?<=[.!?])\s", flat, maxsplit=1)[0]
    return first if len(first) <= limit else first[: limit - 3].rstrip() + "..."


def _short_summary(summary: str) -> str:
    """A result summary without the trailing URL (it repeats the call's own
    target, or is long enough to crowd out the title)."""
    return re.sub(r" — https?://\S+( \([^)]*\))?$", r"\1", summary)


def _refusal_reason(reply: str) -> str:
    return next((label for key, label in REFUSAL_REASONS if key in reply), "refused by code")


def _deadline(entry: ConferenceEntry) -> str:
    facts = entry.extraction
    if not facts.has_deadline:
        return "Rolling"
    return facts.deadline_as_stated or "Not stated"


def _not_scored_reason(entry: ConferenceEntry) -> str:
    facts = entry.extraction
    if not facts.complete:
        missing = [f for f in facts.missing_fields if f in ("deadline", "theme", "submission_type")]
        return f"incomplete -- not stated on the page: {', '.join(m.replace('_', ' ') for m in missing)}"
    if facts.call_status == CallStatus.CLOSED:
        return "closed -- the deadline has passed"
    if facts.access == AccessType.INVITE_ONLY:
        return "invite only"
    return "not scored"


@dataclass
class Row:
    venue: str
    location: str
    paper: str
    bucket: str
    deadline: str
    theme: int | None = None
    genre: int | None = None
    format: int | None = None
    overall: int | None = None

    @property
    def clears(self) -> bool:
        return self.overall is not None and self.overall > NEW_FIT_MIN


@dataclass
class Usage:
    model_calls: int = 0
    tokens: int = 0  # input + output + cache writes + cache reads: every token the API processed
    cost: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.model_calls + other.model_calls, self.tokens + other.tokens, self.cost + other.cost)


def llm_usage() -> Usage:
    """The run's usage so far, from src/llm.py's per-model meter."""
    from src.llm import USAGE, usage_cost

    total = Usage()
    for model, t in USAGE.items():
        total += Usage(t["calls"], t["input_tokens"] + t["output_tokens"] + t["cache_write_tokens"] + t["cache_read_tokens"],
                       usage_cost(model, t["input_tokens"], t["output_tokens"], t["cache_write_tokens"],
                                  t["cache_read_tokens"], t["searches"]))
    return total


@dataclass
class DemoSession:
    """Draws the demo from events (live hook or replayed trace) and tracker
    entries, to the terminal and to a Markdown backup."""

    tracker: TrackerFile
    console: Console = field(default_factory=lambda: Console(highlight=False, soft_wrap=False))
    md: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    _mode: str = "search"
    _decisions: list[dict] = field(default_factory=list)
    _handed: list[tuple[str, str, str]] = field(default_factory=list)  # (conference id or name, profile id, outcome)
    _paper: str = ""
    usage_fn: Callable[[], Usage] | None = None  # live: llm_usage; replay: None (per-paper figures set by replay)
    steps: int = 0  # tool calls seen: searches, fetches and the model's decisions
    replay_usage: Usage = field(default_factory=Usage)
    _live: Live | None = None

    # --- the running counter ---

    def usage(self) -> Usage:
        return self.usage_fn() if self.usage_fn is not None else self.replay_usage

    def counter_text(self, label: str = "Running total") -> str:
        u = self.usage()
        return (f"{label}: {self.steps} steps | {u.model_calls} model calls | {u.tokens:,} tokens | "
                f"${u.cost:.3f} spent")

    def _footer(self) -> Text:
        return Text(f" {self.counter_text()} ", style="bold white on blue")

    def _start_live(self) -> None:
        """A footer that stays at the bottom of a terminal while sections
        print above it (not on a file or a pipe -- the checkpoints cover those)."""
        if self._live is None and self.console.is_terminal:
            self._live = Live(console=self.console, get_renderable=self._footer, refresh_per_second=4,
                              transient=True, redirect_stdout=False, redirect_stderr=False)
            self._live.start()

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _checkpoint(self) -> None:
        self._say(Text(self.counter_text(), style="bold blue"), f"_{self.counter_text()}_")

    # --- output helpers: every line goes to the terminal and the Markdown copy ---

    def _section(self, number: int, title: str) -> None:
        self.console.print()
        self.console.print(Rule(Text(f" {number}. {title} ", style="bold cyan"), style="cyan"))
        self.md += ["", f"### {number}. {title}", ""]

    def _say(self, text: Text | str, md: str | None = None) -> None:
        self.console.print(text)
        self.md.append(md if md is not None else (text.plain if isinstance(text, Text) else text))

    def _item(self, label: str, label_style: str, body: str, md_label: str | None = None) -> None:
        """A label and its text; wrapped lines hang under the text, not the label."""
        grid = Table.grid(padding=(0, 1))
        grid.add_column(no_wrap=True)
        grid.add_column(overflow="fold")
        grid.add_row(Text(label, style=label_style), Text(body))
        self.console.print(grid)
        self.md.append(f"- **{md_label or label}** {body}")

    def _detail(self, text: str) -> None:
        self.console.print(Padding(Text(text, overflow="fold"), (0, 0, 0, 4)))
        self.md.append(f"  - {text}")

    # --- a paper ---

    def start_paper(self, index: int, total: int, profile_id: str, title: str, classification) -> None:
        self._start_live()
        self._paper, self._mode, self._decisions, self._handed = profile_id, "search", [], []
        self.console.print()
        self.console.print(Rule(Text(f" Paper {index} of {total}: {profile_id} ", style="bold white on blue"), style="blue"))
        self.md += ["", f"## Paper {index} of {total}: {profile_id}", ""]
        self._say(Text(title, style="bold"), f"**{title}**")
        self._section(1, "Reading the paper (agent judgment)")
        buckets = ", ".join(BUCKETS[b] for b in classification.paper_bucket)
        self._item("Read as:", "bold yellow", buckets)
        self._say(Text("Cues that drove that call:", style="bold"), "- **Cues that drove that call:**")
        for cue in classification.paper_bucket_cues:
            self._detail(cue)
        self._item("Reasoning:", "bold", " ".join(classification.paper_bucket_reasoning.split()))
        self._section(2, "Searching (agent-directed)")
        self._say(Text("Each search and fetch as the model makes it. Call numbers skip its decisions -- those are under Verifying."),
                  "_Each search and fetch as the model makes it. Call numbers skip its decisions -- those are under Verifying._")

    def listener(self, event: str, data: dict) -> None:
        """Trace display hook (src/discover.py)."""
        getattr(self, f"_on_{event}", lambda d: None)(data)

    def _on_heading(self, data: dict) -> None:
        text = data["text"]
        if text.startswith("Phase 1"):
            self._say(Text("Probes -- one search for each other venue type", style="bold magenta"), "**Probes**")
        elif text.startswith("Phase 2"):
            self._say(Text("Main searches -- the paper's own venue type", style="bold magenta"), "**Main searches**")
        elif text.startswith("Hand-off"):
            self._mode = "handoff"
            self._verifying()

    def _on_lines(self, data: dict) -> None:
        for line in data["lines"]:
            plain = line.strip().strip("*")
            if line.startswith(("**LIMIT", "**probe phase budget", "**main phase's first turn", "**WARNING")):
                self._item("Limit:", "bold red", plain.removeprefix("LIMIT: "))
            elif self._mode == "handoff" and line.startswith(("- direct fetch", "- call status set")):
                self._detail(line[2:])

    def _on_reason_group(self, data: dict) -> None:
        reason = data["reason"] or "(no reason stated)"
        self._item(f"Reason for #{data['first']}-#{data['last']}:", "bold green", reason)

    def _on_server_call(self, data: dict) -> None:
        self.steps += 1
        kind = {"web_search": "Search", "web_fetch": "Fetch"}.get(data["tool"], data["tool"])
        self._item(f"#{data['number']} {kind}:", "bold cyan", str(data["target"] or ""))
        if data.get("reason"):
            self._detail(f"why: {data['reason']}")
        self._detail(_short_summary(data["summary"]) if data["summary"] else "result arrives with the next response")

    def _on_late_result(self, data: dict) -> None:
        self._detail(f"#{data['number']} result arrived: {_short_summary(data['summary'])}")

    def _on_decision(self, data: dict) -> None:
        self.steps += 1
        if data["tool"] in ("record_result", "accept_candidate"):
            self._decisions.append(data)
        elif data["tool"] == "finish" and not data["refused"]:
            self._item("Phase done:", "bold magenta", _one_line(data.get("reason_in_call")))

    # --- verifying ---

    def _verifying(self) -> None:
        self._checkpoint()
        self._section(3, "Verifying (code-checked)")
        if not self._decisions:
            self._say("No results were judged.")
        for d in self._decisions:
            args = d["args"]
            why = _one_line(d.get("reason_in_call"))
            if d["tool"] == "accept_candidate":
                name = f"{args.get('name', '?')} -- {args.get('call_page_url', '?')}"
                if d["refused"]:
                    self._item("REFUSED", "bold red", f"{name}: {_refusal_reason(d['reply'])}")
                else:
                    self._item("ACCEPTED", "bold green", name)
                    self._detail(f'deadline quote verified on the page: "{args.get("deadline_quote", "")}"')
            elif d["refused"]:
                self._item("REFUSED", "bold red", f"{args.get('url', '?')}: {_refusal_reason(d['reply'])}")
            elif args.get("verdict") == "live_cfp":
                self._item("LIVE", "bold yellow", f"{args.get('url', '?')}: {why}")
            else:
                verdict = VERDICTS.get(args.get("verdict", ""), args.get("verdict", "?"))
                self._item("REJECTED", "bold red", f"{args.get('url', '?')}: {verdict}")

    def handoff(self, conference_id: str, profile_id: str, outcome: str, name: str) -> None:
        """One accepted candidate after extraction, classify and judge-fit."""
        self._handed.append((conference_id, profile_id, outcome))
        entry = next((c for c in self.tracker.conferences if c.id == conference_id), None)
        if outcome in ("created", "updated") and entry is not None:
            match = entry.matches.get(profile_id)
            if match is not None and match.fit_by_tier:
                facts = entry.extraction
                self._item("VERIFIED", "bold green", f"{entry.name}: complete, call {facts.call_status.value.replace('_', ' ')}, "
                                                     f"deadline {_deadline(entry)} -> scored")
            else:
                self._item("NOT SCORED", "bold red", f"{entry.name}: {_not_scored_reason(entry)}")
        elif outcome == "flagged":
            self._item("FLAGGED", "bold red", f"{name}: clashes with a tracked venue -- recorded for a look, not merged")
        elif outcome == "not_tracked":
            self._item("NOT TRACKED", "bold red", f"{name}: discovery and extraction disagree on the deadline")
        elif outcome == "skipped":
            self._item("SKIPPED", "bold", f"{name}: already tracked for this paper")
        else:
            self._item("FAILED", "bold red", f"{name}: hand-off failed (listed under Run problems)")

    # --- scoring ---

    def paper_done(self) -> None:
        if self._mode != "handoff":  # discovery stopped before the hand-off heading
            self._verifying()
        self._section(4, "Scoring (agent judgment)")
        scored = 0
        for conference_id, profile_id, outcome in self._handed:
            entry = next((c for c in self.tracker.conferences if c.id == conference_id), None)
            if entry is None or outcome not in ("created", "updated"):
                continue
            match = entry.matches.get(profile_id)
            location = entry.extraction.location or "Not stated"
            base = dict(venue=entry.name, location=location, paper=profile_id, deadline=_deadline(entry))
            if match is None or not match.fit_by_tier:
                self.rows.append(Row(bucket=", ".join(SHORT_BUCKETS[t] for t in entry.extraction.venue_tier), **base))
                continue
            scored += 1
            self._say(Text(entry.name, style="bold"), f"**{entry.name}**")
            for tier, fit in match.fit_by_tier.items():
                verdict = "clears 50%" if fit.overall > NEW_FIT_MIN else "below 50%"
                self._item(f"{BUCKETS[tier]}:", "bold yellow", f"overall {fit.overall} ({verdict})")
                self._detail(f"Theme {fit.theme_fit.score}: {_one_line(fit.theme_fit.reasoning)}")
                row = Row(bucket=SHORT_BUCKETS[tier], theme=fit.theme_fit.score, overall=fit.overall, **base)
                if isinstance(fit, StandardFit):
                    self._detail(f"Genre {fit.genre_fit.score}: {_one_line(fit.genre_fit.reasoning)}")
                    self._detail(f"Format {fit.format_fit.score}: {_one_line(fit.format_fit.reasoning)}")
                    row.genre, row.format = fit.genre_fit.score, fit.format_fit.score
                elif isinstance(fit, OtherAvenuesFit):
                    self._detail(f"Why no other venue type: {_one_line(fit.no_other_fit_reasoning)}")
                self.rows.append(row)
        if not scored:
            self._say("No venue was scored for this paper.")
        self._checkpoint()

    # --- results ---

    def _table(self, rows: list[Row]) -> Table:
        return _results_table(rows, self.console.width)

    def _problems(self, problems: list[tuple[str, str]]) -> None:
        """The run's warnings and failures, one tidy line each, after the
        totals; the full text is in the run's log."""
        self.console.print()
        self.console.print(Rule(Text(" Run problems ", style="bold white on red"), style="red"))
        self.md += ["", "## Run problems", ""]
        for source, message in problems:
            short = message if len(message) <= PROBLEM_MAX else message[: PROBLEM_MAX - 3].rstrip() + "..."
            self._item(f"[{source}]", "bold red", short, md_label=f"[{source}]")
        self._say(Text("Full text in the run's log (logs/<stamp>_demo_stdout.log).", style="bold"))

    def finish(self, cost: float | None = None, md_path: Path | None = None, cost_label: str = "this run",
               problems: list[tuple[str, str]] | None = None) -> Path | None:
        self._stop_live()
        rows = sorted(self.rows, key=lambda r: (r.overall is None, -(r.overall or 0), r.venue))
        self.console.print()
        self.console.print(Rule(Text(" Results ", style="bold white on blue"), style="blue"))
        self.md += ["", "## Results", ""]
        if not rows:
            self._say("No venues were found and handed off this run.")
        else:
            self.console.print(self._table(rows))
            self.md += ["| Venue | Location | Paper | Bucket | Deadline | Theme | Genre | Format | Overall | Clears 50% |",
                        "|---|---|---|---|---|---:|---:|---:|---:|---|"]
            for r in rows:
                cells = [f"**{r.venue}**" if r.clears else r.venue, r.location, r.paper, r.bucket, r.deadline,
                         _num(r.theme), _num(r.genre), _num(r.format), _num(r.overall), "**YES**" if r.clears else "no"]
                self.md.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
            cleared = sum(r.clears for r in rows)
            self._say(Text(f"{cleared} of {len(rows)} row(s) clear the {NEW_FIT_MIN}% threshold.", style="bold"))
        if cost is not None:
            self._say(Text(f"Estimated cost of {cost_label}: about ${cost:.2f} (list prices).", style="bold"))
        u = self.usage()
        totals = (f"TOTALS: {self.steps} steps, {u.model_calls} model calls, {u.tokens:,} tokens, "
                  f"${u.cost:.3f} spent (list prices; the Console bill is authoritative)")
        self._say(Text(totals, style="bold white on blue"), f"**{totals}**")
        if problems:
            self._problems(problems)
        if md_path is None:
            return None
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# Cicadas demo\n" + "\n".join(self.md) + "\n", encoding="utf-8")
        self.console.print(Text(f"Backup saved: {md_path}", style="bold"))
        return md_path


class ProblemTap(io.TextIOBase):
    """stderr during a --demo run: every character goes to the run's log in
    full, and the lines are kept for the demo's Run problems section --
    never to the terminal, where a warning would land mid-section and
    displace the live footer (the duplicate "Running total" line)."""

    def __init__(self, log):
        self.log, self._text = log, ""

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        self.log.write(s)
        self._text += s
        return len(s)

    def flush(self) -> None:
        if not self.log.closed:  # the tap outlives the log: it is read after the run
            self.log.flush()

    def problems(self) -> list[tuple[str, str]]:
        return group_problems(self._text.splitlines())


_TAGGED = re.compile(r"\[(\w+)\]\s*(?:WARNING:\s*)?(.*)")


def group_problems(lines: list[str]) -> list[tuple[str, str]]:
    """(source, message) per "[tracker] ..." / "[run] ..." line, with its
    continuation lines joined on; Python tracebacks are left to the log."""
    entries: list[list[str]] = []
    in_traceback = False
    for line in lines:
        if line.startswith("Traceback (most recent call last)"):
            in_traceback = True
            continue
        tagged = _TAGGED.match(line)
        if tagged:
            in_traceback = False
            entries.append([tagged[1], tagged[2]])
        elif line.strip() and not in_traceback:
            if entries:
                entries[-1][1] += " " + line.strip()
            else:
                entries.append(["stderr", line.strip()])
    return [(source, " ".join(message.split())) for source, message in entries]


def _num(value: int | None) -> str:
    return "-" if value is None else str(value)


def _venue_name(name: str) -> str:
    return name if len(name) <= VENUE_MAX else name[: VENUE_MAX - 3].rstrip() + "..."


def _paper_short(profile_id: str) -> str:
    return profile_id.split("-")[0]


def _results_table(rows: list[Row], width: int) -> Table:
    """One column per field on a wide terminal; on a narrow one (large fonts)
    location, deadline and paper move into the venue cell so every score
    column keeps its full name and the venue name wraps by words."""
    wide = width >= WIDE
    table = Table(box=box.HEAVY_HEAD, show_lines=True, header_style="bold", expand=True)
    table.add_column("Venue" if wide else "Venue / location / deadline / paper", ratio=1, overflow="fold")
    if wide:
        for name in ("Location", "Deadline", "Paper"):
            table.add_column(name, max_width=22, overflow="fold")
    table.add_column("Bucket", no_wrap=True)
    for name in ("Theme", "Genre", "Format", "Overall"):
        table.add_column(name, justify="right", no_wrap=True)
    table.add_column("Clears\n50%", justify="center", no_wrap=True)
    for r in rows:
        if wide:
            cells = [_venue_name(r.venue), r.location, r.deadline, _paper_short(r.paper)]
        else:
            cells = [f"{_venue_name(r.venue)}\n{r.location} | {r.deadline}\nfor {_paper_short(r.paper)}"]
        cells += [r.bucket, _num(r.theme), _num(r.genre), _num(r.format), _num(r.overall), "YES" if r.clears else "no"]
        table.add_row(*cells, style="bold white on dark_green" if r.clears else None)
    return table


def backup_path(when: datetime.datetime, suffix: str = "demo") -> Path:
    """runs/<date>_demo.md, never overwriting an earlier backup."""
    path = Path(RUNS_DIR) / f"{when:%Y-%m-%d}_{suffix}.md"
    if path.exists():
        path = Path(RUNS_DIR) / f"{when:%Y-%m-%d}_{when:%H%M%S}_{suffix}.md"
    return path


# --- Replay: saved trace Markdown -> the same events ---------------------------------


_CALL = re.compile(r"^\*\*#(\d+) (\w+)\*\*\s?(.*)$")
_GROUP = re.compile(r"^Reason stated before calls #(\d+)–#(\d+): (.*)$")
_NO_GROUP = re.compile(r"^\(no reason stated before calls #(\d+)–#(\d+)\)$")
_OUTCOME = re.compile(r"^- outcome: \*\*(created|updated)\*\* `([^`]+)`")
_COST = re.compile(r"cost about \$([0-9.]+)")
_USAGE = re.compile(r"^- \S+: (\d+) calls, ([\d,]+) in \(\+([\d,]+) cache write, ([\d,]+) cache read\) / ([\d,]+) out tokens.*about \$([0-9.]+)$")


@dataclass
class ReplayedPaper:
    profile_id: str
    title: str
    events: list[tuple[str, dict]]
    handoffs: list[tuple[str, str, str]]  # (conference id or name, outcome, name)
    cost: float | None
    usage: Usage = field(default_factory=Usage)


def parse_trace(text: str) -> ReplayedPaper:
    lines = text.splitlines()
    head = re.match(r"# Discovery trace: (\S+),", lines[0])
    profile_id = head.group(1) if head else "?"
    title = next((l.split("Paper: ", 1)[1].rsplit(" (`", 1)[0] for l in lines if l.startswith("Paper: ")), profile_id)
    events: list[tuple[str, dict]] = []
    handoffs: list[tuple[str, str, str]] = []
    cost = None
    usage = Usage()
    i, in_details, section, venue = 0, False, "", ""
    while i < len(lines):
        line = lines[i]
        if line.startswith("<details>"):
            in_details = True
        elif in_details:
            in_details = line != "</details>"
        elif line.startswith("## "):
            section = line[3:]
            if not section.startswith(("Turn ", "Stopped", "Candidates", "Usage", "Failures", "Re-check")):
                events.append(("heading", {"text": section}))
        elif section.startswith("Usage"):
            if (u := _USAGE.match(line)):
                n = [int(g.replace(",", "")) for g in u.groups()[:5]]
                usage += Usage(n[0], n[1] + n[2] + n[3] + n[4], float(u.group(6)))
        elif section == "Candidates" or section.startswith("Re-check"):
            pass
        elif section.startswith("Hand-off"):
            if line.startswith("### "):
                venue = line[4:].rsplit(" (", 1)[0]
            m = _OUTCOME.match(line)
            if m:
                handoffs.append((m.group(2), m.group(1), venue))
            elif line.startswith("- outcome: **flagged**"):
                handoffs.append((venue, "flagged", venue))
            elif line.startswith("- outcome: skipped"):
                handoffs.append((venue, "skipped", venue))
            elif line.startswith("- outcome: **not tracked"):
                handoffs.append((venue, "not_tracked", venue))
            elif line.startswith("- **FAILED**") and venue:
                handoffs.append((venue, "failed", venue))
            elif line.startswith(("- direct fetch", "- call status set")):
                events.append(("lines", {"lines": [line]}))
        elif (m := _GROUP.match(line)) or (m2 := _NO_GROUP.match(line)):
            m = m or m2
            events.append(("reason_group", {"first": int(m.group(1)), "last": int(m.group(2)),
                                            "reason": m.group(3) if m.re is _GROUP else None}))
        elif line.startswith(("**LIMIT", "**probe phase budget", "**main phase's first turn", "**WARNING")):
            events.append(("lines", {"lines": [line]}))
        elif (m := _CALL.match(line)):
            number, tool, rest = int(m.group(1)), m.group(2), m.group(3)
            block, j = [], i + 1
            while j < len(lines) and lines[j].startswith("- "):
                block.append(lines[j])
                j += 1
            reason = next((b.split(": ", 1)[1] for b in block if b.startswith("- reason (before the call):")), None)
            if tool in ("web_search", "web_fetch"):
                result = next((b.split(": ", 1)[1] for b in block if b.startswith("- result:")), "")
                late = result.endswith(" (arrived with the next response)")
                events.append(("server_call", {"number": number, "tool": tool, "target": rest.strip("`"),
                                               "reason": reason, "summary": None if late else result}))
                if late:
                    events.append(("late_result", {"number": number, "tool": tool,
                                                   "summary": result.removesuffix(" (arrived with the next response)")}))
            else:
                try:
                    args = json.loads(rest) if rest else {}
                except json.JSONDecodeError:
                    args = {}
                in_call = next((b.split(": ", 1)[1] for b in block if b.startswith("- reason (in the call):")), None)
                reply_line = next((b for b in block if b.startswith(("- code:", "- REFUSED by code:"))), "- code: ")
                args["reason"] = in_call
                events.append(("decision", {"number": number, "tool": tool, "args": args, "reason": reason,
                                            "reason_in_call": in_call, "refused": reply_line.startswith("- REFUSED"),
                                            "reply": reply_line.split(": ", 1)[1] if ": " in reply_line else ""}))
            i = j
            continue
        if (m := _COST.search(line)) and line.startswith("**Summary for"):
            cost = float(m.group(1))
        i += 1
    return ReplayedPaper(profile_id, title, events, handoffs, cost, usage)


def latest_traces(directory: str | Path = TRACES_DIR) -> list[Path]:
    paths = sorted(Path(directory).glob("*_*.md"))
    if not paths:
        return []
    stamp = paths[-1].name.split("_", 1)[0]
    return [p for p in paths if p.name.startswith(stamp + "_")]


def replay(trace_paths: list[Path], tracker: TrackerFile, md_path: Path | None, console: Console | None = None) -> DemoSession:
    session = DemoSession(tracker, console or Console(highlight=False))
    papers = [parse_trace(p.read_text(encoding="utf-8")) for p in trace_paths]
    session._say(Text("Replay: steps update as they happen; tokens and dollars are known only per paper, from each "
                      "trace's usage lines.", style="bold"),
                 "_Replay: steps update as they happen; tokens and dollars are known only per paper, from each trace's usage lines._")
    total_cost = 0.0
    for index, paper in enumerate(papers, 1):
        classification = tracker.profiles.get(paper.profile_id)
        if classification is None:
            session.console.print(f"[bold red]No classification for {paper.profile_id} in the tracker -- skipped[/]")
            continue
        session.start_paper(index, len(papers), paper.profile_id, paper.title, classification)
        for event, data in paper.events:
            session.listener(event, data)
        for conference_id, outcome, name in paper.handoffs:
            session.handoff(conference_id, paper.profile_id, outcome, name)
        session.replay_usage += paper.usage  # known only per paper, from the trace's usage lines
        session.paper_done()
        total_cost += paper.cost or 0.0
    session.finish(total_cost, md_path, "the replayed run (from its traces; this replay made no API calls)")
    return session


def main(argv: list[str] | None = None) -> int:
    from src.tracker import TrackerError, load_tracker

    parser = argparse.ArgumentParser(prog="python -m src.demo", description="Replay saved traces as the demo (no API calls).")
    parser.add_argument("traces", nargs="*", help="trace files (default: the latest run's traces)")
    parser.add_argument("--tracker", default=TRACKER_PATH)
    parser.add_argument("--no-backup", action="store_true", help="don't write runs/<date>_demo_replay.md")
    args = parser.parse_args(argv)
    paths = [Path(p) for p in args.traces] or latest_traces()
    if not paths:
        print("[demo] FAILED: no traces found", file=sys.stderr)
        return 1
    try:
        tracker = load_tracker(args.tracker)
    except TrackerError as exc:
        print(f"[demo] FAILED: {exc}", file=sys.stderr)
        return 1
    md = None if args.no_backup else backup_path(datetime.datetime.now(), "demo_replay")
    Console(highlight=False).print(f"[bold]Replaying {', '.join(p.name for p in paths)}[/]")
    replay(paths, tracker, md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
