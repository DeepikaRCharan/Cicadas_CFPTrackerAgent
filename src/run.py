"""The monthly run, end to end (sending excluded -- see src/notify.py).

Build step 8 (docs/project-brief.md, Workflow). Same code path for the
scheduled run and an on-demand one:

1. Sync decisions.json into the tracker (always first).
2. Per live profile: classify the paper if it's new or revised (cached in
   the tracker's profiles block), then run discovery (src/discover.py).
3. Hand off each accepted candidate, in code: fetch -> extract (light model)
   -> classify the venue -> judge fit (when complete and not skipped) ->
   tracker merge, with the edition year passed in explicitly. The tracker is
   saved atomically after every candidate.
4. Re-check every open tracked venue: re-fetch and re-extract (light model);
   re-judge only when a venue that was incomplete is now complete, or the
   profile changed since it was scored.

    python -m src.run                                   # every live profile, tracker.json
    python -m src.run --profile immune-tolerance        # one profile
    python -m src.run --tracker /tmp/t.json --max-searches 4 --max-turns 6
    python -m src.run --problems-out logs/run_problems.json   # what the workflow runs

Exit codes (the monthly workflow branches on them):

- 0 (EXIT_OK): everything ran.
- 1 (EXIT_CRASHED): the run couldn't start -- tracker, decisions or
  profiles unreadable. Nothing ran, so the workflow sends no digest.
- 3 (EXIT_PARTIAL): something failed -- a candidate, a re-check, or a whole
  paper's classify/discovery. The run keeps going past each failure (one bad
  page or one paper never throws away the rest), then exits 3; the workflow
  still sends the digest, with a "Run problems" section listing each
  failure -- a whole paper's search first, stated plainly, since that digest
  has nothing new for that paper -- and then fails the job. (2 is skipped
  because argparse exits 2 on a usage error.)

Every failure is a WARNING on stderr, an ::error:: annotation on GitHub
Actions, and a line in the trace. --problems-out writes them as JSON for
`python -m src.notify --run-problems` to render.

Each paper's trace (src/discover.py's Trace, plus the hand-off and re-check
here) is saved to traces/, printed to the terminal (one-line summaries only,
no <details> lists), and appended to $GITHUB_STEP_SUMMARY when running on
GitHub Actions, so it shows on the run page. It ends with one line: counts,
this paper's cost, and the tool-call total.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime
import json
import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

from src.config import DECISIONS_PATH, LOGS_DIR, MODEL_DISCOVERY, TRACES_DIR, TRACKER_PATH, WEB_TOOL_TYPES
from src.discover import BUCKET_LABELS, MIN_READABLE_CHARS, Candidate, Limits, Trace, discover, page_key
from src.profile_schema import Profile, load_live_profiles
from src.tracker import (
    DecisionsError,
    TrackerError,
    decide_call_status,
    flag_conflict,
    is_open,
    load_decisions,
    load_tracker,
    lookup_venue,
    record_paper_classification,
    save_tracker,
    sync_decisions,
    upsert_venue,
    warn,
)
from src.tracker_schema import Extraction, ExtractionFacts, OtherAvenuesFit, StandardFit, TrackerFile


EXIT_OK = 0
EXIT_CRASHED = 1
EXIT_PARTIAL = 3


def _annotate(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error title=Cicadas run::{message}")


def readable_text(text: str) -> bool:
    """The search loop's readability rule (discover.MIN_READABLE_CHARS), for
    the hand-off's and re-check's own fetches."""
    return len(text.strip()) >= MIN_READABLE_CHARS


def _preview(text: str, n: int = 60) -> str:
    flat = " ".join(text.split())
    return f'"{flat[:n]}{"..." if len(flat) > n else ""}"'


def write_problems(path: str | Path, crashed: bool, problems: list[str]) -> None:
    """The run's failures, for the digest's Run problems section."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"crashed": crashed, "problems": problems}, indent=2) + "\n", encoding="utf-8")


def _stated(value) -> str:
    if value is None:
        return "not stated"
    return getattr(value, "value", str(value))


def venue_detail_lines(facts: ExtractionFacts, tier, tier_reasoning: str, fit, profile_id: str) -> list[str]:
    """An accepted venue in the trace: the extraction, the venue bucket(s)
    with classify's reasoning, and every fit dimension with its rationale --
    all stored fields, printed as they are."""
    if not facts.has_deadline:
        deadline = "rolling"
    elif facts.deadline is None:
        deadline = "not stated"
    else:
        deadline = f"{facts.deadline_as_stated} ({facts.deadline_precision.value})"
    call = facts.call_status.value + (f", opens {facts.submission_opens}" if facts.submission_opens else "")
    lines = [
        "- extraction:",
        f"  - submission type: {_stated(facts.submission_type)}; theme: {_stated(facts.theme)}",
        f"  - deadline: {deadline}; call: {call}",
        f"  - location: {_stated(facts.location)}; access: {_stated(facts.access)}; mode: {_stated(facts.mode)}; "
        f"fees: {_stated(facts.fees)}",
        f"  - format: {_stated(facts.format_requirements)}",
        "  - " + ("complete" if facts.complete else "INCOMPLETE")
        + (f"; not stated on the page: {', '.join(facts.missing_fields)}" if facts.missing_fields else ""),
        f"- venue bucket(s): {', '.join(BUCKET_LABELS[t] for t in tier)} -- {tier_reasoning}",
    ]
    if not fit:
        why = "extraction incomplete" if not facts.complete else "skipped by code: call closed, deadline passed or invite-only"
        lines.append(f"- fit for {profile_id}: not scored ({why})")
    for t, f in (fit or {}).items():
        lines += [f"- fit for {profile_id}, {BUCKET_LABELS[t]}: overall {f.overall}/100",
                  f"  - theme_fit {f.theme_fit.score}: {f.theme_fit.reasoning}"]
        if isinstance(f, StandardFit):
            lines += [f"  - genre_fit {f.genre_fit.score}: {f.genre_fit.reasoning}",
                      f"  - format_fit {f.format_fit.score}: {f.format_fit.reasoning}"]
        elif isinstance(f, OtherAvenuesFit):
            lines.append(f"  - why no other venue type fit: {f.no_other_fit_reasoning} (low confidence: theme only)")
    return lines


def _fit_summary(fit_by_tier) -> str:
    if not fit_by_tier:
        return "not scored"
    return ", ".join(f"{tier.value} {fit.overall}" for tier, fit in fit_by_tier.items())


class Run:
    def __init__(self, tracker: TrackerFile, tracker_path: Path, profiles: dict[str, Profile],
                 decisions, now: datetime.datetime):
        from src import classify, extract, judge_fit  # model-calling modules: imported only for a real run

        self.classify, self.extract, self.judge_fit = classify, extract, judge_fit
        self.tracker, self.tracker_path, self.profiles = tracker, tracker_path, profiles
        self.decisions, self.now, self.today = decisions, now, now.date()
        self.live = set(profiles)
        self.rechecked: set[str] = set()
        self.failures: list[str] = []  # in the order they happened (each trace lists its own slice)
        self.paper_failures: list[str] = []  # whole-paper failures, also in failures; listed first

    def save(self) -> None:
        save_tracker(self.tracker, self.tracker_path)

    def problem(self, message: str) -> None:
        warn(message)
        _annotate(message)
        self.failures.append(message)

    def fail(self, trace: Trace, what: str, exc: Exception) -> None:
        """One candidate or re-check failed; the run moves on."""
        message = f"{what} failed: {type(exc).__name__}: {exc}"
        self.problem(message)
        trace.add(f"- **FAILED**: {message}")

    def fail_paper(self, trace: Trace, profile_id: str, exc: Exception) -> None:
        """A whole paper's classify/discovery failed; the other paper still runs."""
        message = (f"The whole search for {profile_id} failed, so this digest has nothing new for that paper: "
                   f"{type(exc).__name__}: {exc}")
        self.problem(message)
        self.paper_failures.append(message)
        trace.add(f"- **FAILED**: {message}")

    def problems(self) -> list[str]:
        """For the digest: whole-paper failures first, then the rest in order."""
        return self.paper_failures + [f for f in self.failures if f not in self.paper_failures]

    def exit_code(self) -> int:
        return EXIT_PARTIAL if self.failures else EXIT_OK

    def ensure_classified(self, profile: Profile) -> None:
        cached = self.tracker.profiles.get(profile.id)
        if cached is not None and cached.profile_hash == profile.content_hash():
            return
        print(f"[run] classifying paper {profile.id} ({'revised' if cached else 'new'})")
        record_paper_classification(self.tracker, profile.id, self.classify.classify_paper(profile))
        self.save()

    def call_status(self, facts, trace: Trace, label: str | None = None):
        """Open/closed by code from the deadline and the run date (brief,
        Rules > Call status), never extraction's own reading; noted in the
        trace when it differs."""
        facts, note = decide_call_status(facts, self.today)
        if note:
            trace.add(f"- {f'`{label}`: ' if label else ''}{note}")
        return facts

    def judge(self, profile, facts, page_text, url, venue_tier):
        if not facts.complete:
            return {}
        return self.judge_fit.judge_fit_for_venue(profile, facts, page_text, url, venue_tier, self.today)

    # --- step 3: hand-off --------------------------------------------------------

    def page_text(self, urls: list[str], loop_text: dict[str, str], trace: Trace) -> str:
        """Fetch each page directly. A page whose fetch fails (403, timeout)
        or comes back unreadable by the search loop's own rule (fewer than
        MIN_READABLE_CHARS characters of text: a "JavaScript is disabled"
        shell, a bot wall) is replaced by the readable copy the loop already
        fetched through Anthropic's web_fetch. A call page with neither is an
        error; a dates page with neither is left out, and the trace says so."""
        kept: list[tuple[str, str]] = []
        for i, url in enumerate(urls):
            problem = None
            try:
                text = self.extract.fetch_text([url])
                if not readable_text(text):
                    problem = f"unreadable: {len(text.strip())} characters of text ({_preview(text)})"
            except RuntimeError as exc:
                problem = f"failed: {exc}"
            if problem is None:
                kept.append((url, text))
                continue
            copy = loop_text.get(page_key(url))
            if copy is not None:
                trace.add(f"- direct fetch of {url} {problem}; using the discovery loop's readable copy")
                kept.append((url, copy))
            elif i == 0:
                raise RuntimeError(f"call page {url} {problem}, and the discovery loop has no readable copy")
            else:
                trace.add(f"- direct fetch of {url} {problem}, and no loop copy -- left out")
        if len(kept) == 1:
            return kept[0][1]
        return "\n\n".join(f"=== Page: {u} ===\n{t}" for u, t in kept)

    def hand_off(self, profile: Profile, candidate: Candidate, trace: Trace,
                 loop_text: dict[str, str] | None = None) -> str:
        """Returns the outcome: created, updated, flagged, skipped, not_tracked or failed."""
        label = f"{candidate.name} ({candidate.year if candidate.year is not None else 'rolling'})"
        trace.add(f"### {label}", f"- {candidate.call_page_url}"
                  + (f" + dates page {candidate.dates_page_url}" if candidate.dates_page_url else ""),
                  f"- deadline evidence: {candidate.deadline_evidence}"
                  + (f' -- "{candidate.deadline_quote}"' if candidate.deadline_quote else ""))
        try:
            found = lookup_venue(self.tracker, candidate.name, candidate.year, candidate.call_page_url)
            if found.conflict is not None:
                flag_conflict(self.tracker, name=candidate.name, year=candidate.year, url=candidate.call_page_url,
                              profile_id=profile.id, now=self.now)
                self.save()
                trace.add(f"- outcome: **flagged** ({found.conflict.value} with `{found.entry.id}`), "
                          "no model calls spent", "")
                return "flagged"
            if found.entry is not None and profile.id in found.entry.matches:
                trace.add("- outcome: skipped, already tracked for this paper (covered by re-check)", "")
                return "skipped"

            urls = [candidate.call_page_url] + ([candidate.dates_page_url] if candidate.dates_page_url else [])
            page_text = self.page_text(urls, loop_text or {}, trace)
            facts = self.call_status(self.extract.extract_facts_from_text(candidate.call_page_url, page_text, self.today),
                                     trace)
            if candidate.deadline_evidence == "not_found" and not facts.has_deadline:
                # Discovery found no deadline; extraction calls the call rolling.
                # Two readings disagree on the one fact most likely to misfire a
                # reminder -- never resolved by picking one. Not merged.
                message = (f"{label}: discovery found no deadline, but extraction reads the call as rolling -- "
                           f"not tracked; check {candidate.call_page_url} by hand")
                self.problem(message)
                trace.add(f"- outcome: **not tracked, needs a look**: {message}", "")
                return "not_tracked"
            if found.entry is not None:
                tier = found.entry.extraction.venue_tier
                tier_reasoning = found.entry.extraction.venue_tier_reasoning
            else:
                venue = self.classify.classify_venue(candidate.call_page_url, page_text)
                tier, tier_reasoning = venue.venue_tier, venue.venue_tier_reasoning
            extraction = Extraction(**facts.model_dump(), venue_tier=tier, venue_tier_reasoning=tier_reasoning)
            fit = self.judge(profile, facts, page_text, candidate.call_page_url, tier)
            result = upsert_venue(
                self.tracker, name=candidate.name, year=candidate.year, url=candidate.call_page_url,
                dates_url=candidate.dates_page_url, extraction=extraction, profile_id=profile.id,
                live_profile_ids=self.live, now=self.now, fit_by_tier=fit or None, decisions=self.decisions,
            )
            self.save()
            completeness = "complete" if facts.complete else f"INCOMPLETE (missing {', '.join(facts.missing_fields)})"
            logged = f"; logged {', '.join(r.value for r in result.logged)}" if result.logged else ""
            trace.add(f"- outcome: **{result.status}** `{result.conference_id}`; buckets "
                      f"{', '.join(t.value for t in tier)}; {completeness}; deadline "
                      f"{facts.deadline_as_stated or ('rolling' if not facts.has_deadline else 'not stated')}; "
                      f"call {facts.call_status.value}; fit {_fit_summary(fit)}{logged}")
            trace.add(*venue_detail_lines(facts, tier, tier_reasoning, fit, profile.id), "")
            return result.status
        except Exception as exc:  # one bad candidate never stops the run, but is never silent
            self.fail(trace, f"hand-off of {label}", exc)
            trace.add("")
            return "failed"

    # --- step 4: re-check ----------------------------------------------------------

    def recheck(self, profile: Profile, trace: Trace, skip_ids: set[str]) -> int:
        """Returns how many venues were re-checked."""
        trace.heading("Re-check of tracked venues")
        due = [c for c in self.tracker.conferences
               if profile.id in c.matches and is_open(c.matches[profile.id]) and c.id not in skip_ids]
        if not due:
            trace.add("Nothing open to re-check for this paper.")
        checked = 0
        for conference in due:
            if conference.id in self.rechecked:
                trace.add(f"- `{conference.id}`: already re-checked this run")
                continue
            self.rechecked.add(conference.id)
            checked += 1
            try:
                urls = [conference.url] + ([conference.dates_url] if conference.dates_url else [])
                page_text = self.extract.fetch_text(urls)
                if not readable_text(page_text):
                    # no loop copy on a re-check: never overwrite a tracked entry from a shell page
                    raise RuntimeError(f"page unreadable ({len(page_text.strip())} characters of text: "
                                       f"{_preview(page_text)}) -- tracked entry left unchanged")
                facts = self.call_status(self.extract.extract_facts_from_text(conference.url, page_text, self.today),
                                         trace, conference.id)
                was_complete = conference.extraction.complete
                notes, logged = [], []
                rescored = False
                for pid, match in list(conference.matches.items()):
                    if pid not in self.live or not is_open(match):
                        continue
                    stale = any(f.profile_hash != self.profiles[pid].content_hash() for f in match.fit_by_tier.values())
                    newly_complete = facts.complete and not match.fit_by_tier
                    fit = None
                    if facts.complete and (stale or newly_complete):
                        fit = self.judge(self.profiles[pid], facts, page_text, conference.url, conference.extraction.venue_tier)
                        notes.append(f"re-scored {pid} ({'profile revised' if stale else 'now complete'}): {_fit_summary(fit)}")
                    result = upsert_venue(
                        self.tracker, name=conference.name, year=_year_of(conference.id), url=conference.url,
                        extraction=facts, profile_id=pid, live_profile_ids=self.live, now=self.now,
                        fit_by_tier=fit or None, decisions=self.decisions,
                    )
                    logged += result.logged
                    rescored = rescored or bool(fit)
                self.save()
                change = "now complete" if facts.complete and not was_complete else (
                    "complete" if facts.complete else f"incomplete (missing {', '.join(facts.missing_fields)})")
                trace.add(f"- `{conference.id}`: re-extracted, {change}; call {facts.call_status.value}"
                          + (f"; {'; '.join(notes)}" if notes else "")
                          + (f"; logged {', '.join(r.value for r in logged)}" if logged else ""))
            except Exception as exc:
                self.fail(trace, f"re-check of {conference.id}", exc)
        return checked


def _year_of(conference_id: str) -> int | None:
    from src.tracker import id_year

    return id_year(conference_id)


def _usage_snapshot() -> dict[str, dict[str, int]]:
    from src.llm import USAGE

    return copy.deepcopy(USAGE)


def _usage_lines(since: dict[str, dict[str, int]] | None = None, label: str = "this run") -> tuple[list[str], float]:
    """Per-model usage and estimated cost since `since` (a _usage_snapshot),
    or for the whole process. Returns (lines, total USD)."""
    from src.llm import USAGE, usage_cost

    since = since or {}
    lines, total = [], 0.0
    for model, now in USAGE.items():
        before = since.get(model, {})
        t = {k: v - before.get(k, 0) for k, v in now.items()}
        if not t.get("calls"):
            continue
        cost = usage_cost(model, t["input_tokens"], t["output_tokens"], t["cache_write_tokens"],
                          t["cache_read_tokens"], t["searches"])
        total += cost
        lines.append(f"- {model}: {t['calls']} calls, {t['input_tokens']:,} in (+{t['cache_write_tokens']:,} cache write, "
                     f"{t['cache_read_tokens']:,} cache read) / {t['output_tokens']:,} out tokens, "
                     f"{t['searches']} searches, {t['fetches']} fetches -- about ${cost:.3f}")
    lines.append(f"- **estimated total, {label}: about ${total:.3f}** (list prices; the Console bill is authoritative)")
    return lines, total


def summary_line(profile_id: str, judged, outcomes: dict[str, int], rechecked: int, tool_calls, cost: float,
                 failures: int) -> str:
    """The trace's final line: counts, this paper's cost, tool-call total."""
    accepted = sum(1 for j in judged if j.tool == "accept_candidate" and j.outcome == "accepted")
    refused = sum(1 for j in judged if j.outcome == "refused")
    rejected = sum(1 for j in judged if j.tool == "record_result" and j.outcome not in ("refused", "live_cfp"))
    live = sum(1 for j in judged if j.tool == "record_result" and j.outcome == "live_cfp")
    handed = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in outcomes.items() if n) or "none"
    calls = ", ".join(f"{n} {name}" for name, n in sorted(tool_calls.items(), key=lambda kv: (-kv[1], kv[0]))) or "none"
    return (f"**Summary for {profile_id}:** {len(judged)} decision(s) on results -- {live} judged live, "
            f"{accepted} accepted, {rejected} rejected, {refused} refused by code; hand-off: {handed}; "
            f"{rechecked} re-checked; {failures} failure(s); {sum(tool_calls.values())} tool calls ({calls}); "
            f"cost about ${cost:.3f}")


def publish_trace(trace: Trace, path: Path) -> None:
    """Save the trace, print it (terminal form: no <details> lists), and on
    GitHub Actions append it to the run page's step summary."""
    trace.save(path)
    text = trace.terminal()
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, "replace").decode(encoding, "replace"))
    print(f"[run] trace written to {path}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(trace.markdown() + "\n\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.run", description="Run Cicadas discovery end to end (no email).")
    parser.add_argument("--tracker", default=TRACKER_PATH)
    parser.add_argument("--decisions", default=DECISIONS_PATH)
    parser.add_argument("--profile", action="append", help="run only this profile id (repeatable); others stay live")
    defaults = Limits()
    parser.add_argument("--max-turns", type=int, default=defaults.turns)
    parser.add_argument("--max-searches", type=int, default=defaults.searches)
    parser.add_argument("--probe-reserve", type=int, default=defaults.probe_reserve)
    parser.add_argument("--max-fetches", type=int, default=defaults.fetches)
    parser.add_argument("--max-candidates", type=int, default=defaults.candidates)
    parser.add_argument("--max-loop-cost", type=float, default=defaults.cost_usd, help="USD; the loop stops between turns once reached")
    parser.add_argument("--traces-dir", default=TRACES_DIR)
    parser.add_argument("--discovery-model", default=MODEL_DISCOVERY, choices=sorted(WEB_TOOL_TYPES),
                        help="model for the discovery loop only (hand-off models are unchanged)")
    parser.add_argument("--problems-out", help="write the run's failures here as JSON (for notify --run-problems)")
    parser.add_argument("--demo", action="store_true",
                        help="display only: stream the run in readable sections (src/demo.py), save runs/<date>_demo.md; "
                             "the pipeline's own console output goes to logs/<stamp>_demo_stdout.log")
    args = parser.parse_args(argv)
    load_dotenv()

    limits = Limits(args.max_turns, args.max_searches, args.probe_reserve, args.max_fetches, args.max_candidates,
                    args.max_loop_cost)
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    tracker_path = Path(args.tracker)
    try:
        tracker = load_tracker(tracker_path)
        decisions = load_decisions(args.decisions)
        profiles = load_live_profiles()
        selected = args.profile or list(profiles)
        unknown = [p for p in selected if p not in profiles]
        if unknown:
            raise ValueError(f"unknown profile id(s): {unknown}; live: {sorted(profiles)}")
    except (TrackerError, DecisionsError, ValueError) as exc:
        print(f"[run] FAILED: {exc}", file=sys.stderr)
        _annotate(str(exc))
        if args.problems_out:
            write_problems(args.problems_out, True, [str(exc)])
        return EXIT_CRASHED

    demo = None
    with contextlib.ExitStack() as quiet:
        if args.demo:  # display only -- nothing below behaves differently
            from rich.console import Console

            from src.demo import DemoSession, backup_path

            demo = DemoSession(tracker, Console(file=sys.stdout, highlight=False))
            log = Path(LOGS_DIR) / f"{now:%Y%m%dT%H%M%SZ}_demo_stdout.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            quiet.enter_context(contextlib.redirect_stdout(quiet.enter_context(log.open("w", encoding="utf-8"))))
        code = _run_papers(args, tracker, tracker_path, decisions, profiles, selected, now, limits, demo)
    if demo is not None:
        demo.finish(_usage_lines()[1], backup_path(now))
    return code


def _run_papers(args, tracker, tracker_path, decisions, profiles, selected, now, limits, demo) -> int:
    tracker.last_run = now
    sync_decisions(tracker, decisions, set(profiles))
    run = Run(tracker, tracker_path, profiles, decisions, now)
    run.save()

    for index, profile_id in enumerate(selected, 1):
        profile = profiles[profile_id]
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        trace = Trace(f"Discovery trace: {profile_id}, {now:%Y-%m-%d %H:%M} UTC")
        trace_path = Path(args.traces_dir) / f"{stamp}_{profile_id}.md"
        first_failure = len(run.failures)
        usage_before = _usage_snapshot()
        judged, rechecked = [], 0
        outcomes = {k: 0 for k in ("created", "updated", "flagged", "skipped", "not_tracked", "failed")}
        started = False
        try:
            run.ensure_classified(profile)
            if demo is not None:
                demo.start_paper(index, len(selected), profile_id, profile.title, tracker.profiles[profile_id])
                trace.listener, started = demo.listener, True
            result = discover(profile, tracker.profiles[profile_id], tracker, trace=trace, today=run.today,
                              limits=limits, model=args.discovery_model, log_dir=LOGS_DIR)
            judged = result.judged
            trace.heading("Hand-off: extract, classify, judge fit, merge")
            if not result.candidates:
                trace.add("No candidates accepted.")
            handed: set[str] = set()
            for candidate in result.candidates:
                outcome = run.hand_off(profile, candidate, trace, result.fetched_text)
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                conference_id = lookup_venue(tracker, candidate.name, candidate.year, candidate.call_page_url).conference_id
                handed.add(conference_id)
                if demo is not None:
                    demo.handoff(conference_id, profile_id, outcome, candidate.name)
            rechecked = run.recheck(profile, trace, skip_ids=handed)
        except Exception as exc:
            run.fail_paper(trace, profile_id, exc)
            traceback.print_exc()
        finally:
            trace.heading("Usage and cost")
            usage, cost = _usage_lines(usage_before, f"this paper ({profile_id})")
            trace.add(*usage)
            failures = run.failures[first_failure:]
            if failures:
                trace.heading("Failures")
                trace.add(*(f"- {f}" for f in failures))
            trace.add("", summary_line(profile_id, judged, outcomes, rechecked, trace.tool_calls, cost, len(failures)))
            trace.listener = None
            publish_trace(trace, trace_path)
            if started:
                demo.paper_done()

    run.save()
    print(f"[run] done: {len(tracker.conferences)} tracked venue(s), {len(run.failures)} failure(s) -> {tracker_path}")
    for line in _usage_lines()[0]:
        print(f"[run] {line[2:]}")
    if args.problems_out:
        write_problems(args.problems_out, False, run.problems())
    code = run.exit_code()
    if code == EXIT_PARTIAL:
        print(f"[run] PARTIAL: {len(run.failures)} failure(s), {len(run.paper_failures)} of them a whole paper's "
              "search -- digest goes out with a Run problems section, and the job fails", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
