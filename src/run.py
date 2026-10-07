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
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

from src.config import DECISIONS_PATH, MODEL_DISCOVERY, TRACES_DIR, TRACKER_PATH, WEB_TOOL_TYPES
from src.discover import Candidate, Limits, Trace, discover, page_key
from src.profile_schema import Profile, load_live_profiles
from src.tracker import (
    DecisionsError,
    TrackerError,
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
from src.tracker_schema import Extraction, TrackerFile


EXIT_OK = 0
EXIT_CRASHED = 1
EXIT_PARTIAL = 3


def _annotate(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error title=Cicadas run::{message}")


def write_problems(path: str | Path, crashed: bool, problems: list[str]) -> None:
    """The run's failures, for the digest's Run problems section."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"crashed": crashed, "problems": problems}, indent=2) + "\n", encoding="utf-8")


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

    def judge(self, profile, facts, page_text, url, venue_tier):
        if not facts.complete:
            return {}
        return self.judge_fit.judge_fit_for_venue(profile, facts, page_text, url, venue_tier, self.today)

    # --- step 3: hand-off --------------------------------------------------------

    def page_text(self, urls: list[str], loop_text: dict[str, str], trace: Trace) -> str:
        """Fetch the pages directly; if a site blocks that (bot walls, 403s),
        fall back to the readable copies the discovery loop already fetched
        through Anthropic's web_fetch. With neither, the error stands."""
        try:
            return self.extract.fetch_text(urls)
        except RuntimeError as exc:
            copies = [loop_text.get(page_key(u)) for u in urls]
            if copies[0] is None:
                raise
            kept = [(u, t) for u, t in zip(urls, copies) if t is not None]
            trace.add(f"- direct fetch failed ({exc}); using the discovery loop's fetched copy of "
                      f"{', '.join(u for u, _ in kept)}")
            if len(kept) == 1:
                return kept[0][1]
            return "\n\n".join(f"=== Page: {u} ===\n{t}" for u, t in kept)

    def hand_off(self, profile: Profile, candidate: Candidate, trace: Trace,
                 loop_text: dict[str, str] | None = None) -> None:
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
                return
            if found.entry is not None and profile.id in found.entry.matches:
                trace.add("- outcome: skipped, already tracked for this paper (covered by re-check)", "")
                return

            urls = [candidate.call_page_url] + ([candidate.dates_page_url] if candidate.dates_page_url else [])
            page_text = self.page_text(urls, loop_text or {}, trace)
            facts = self.extract.extract_facts_from_text(candidate.call_page_url, page_text, self.today)
            if candidate.deadline_evidence == "not_found" and not facts.has_deadline:
                # Discovery found no deadline; extraction calls the call rolling.
                # Two readings disagree on the one fact most likely to misfire a
                # reminder -- never resolved by picking one. Not merged.
                message = (f"{label}: discovery found no deadline, but extraction reads the call as rolling -- "
                           f"not tracked; check {candidate.call_page_url} by hand")
                self.problem(message)
                trace.add(f"- outcome: **not tracked, needs a look**: {message}", "")
                return
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
                      f"call {facts.call_status.value}; fit {_fit_summary(fit)}{logged}", "")
        except Exception as exc:  # one bad candidate never stops the run, but is never silent
            self.fail(trace, f"hand-off of {label}", exc)
            trace.add("")

    # --- step 4: re-check ----------------------------------------------------------

    def recheck(self, profile: Profile, trace: Trace, skip_ids: set[str]) -> None:
        trace.heading("Re-check of tracked venues")
        due = [c for c in self.tracker.conferences
               if profile.id in c.matches and is_open(c.matches[profile.id]) and c.id not in skip_ids]
        if not due:
            trace.add("Nothing open to re-check for this paper.")
        for conference in due:
            if conference.id in self.rechecked:
                trace.add(f"- `{conference.id}`: already re-checked this run")
                continue
            self.rechecked.add(conference.id)
            try:
                urls = [conference.url] + ([conference.dates_url] if conference.dates_url else [])
                page_text = self.extract.fetch_text(urls)
                facts = self.extract.extract_facts_from_text(conference.url, page_text, self.today)
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


def _year_of(conference_id: str) -> int | None:
    from src.tracker import id_year

    return id_year(conference_id)


def _usage_lines() -> list[str]:
    from src.llm import USAGE, usage_cost

    lines, total = [], 0.0
    for model, t in USAGE.items():
        cost = usage_cost(model, t["input_tokens"], t["output_tokens"], t["cache_write_tokens"],
                          t["cache_read_tokens"], t["searches"])
        total += cost
        lines.append(f"- {model}: {t['calls']} calls, {t['input_tokens']:,} in (+{t['cache_write_tokens']:,} cache write, "
                     f"{t['cache_read_tokens']:,} cache read) / {t['output_tokens']:,} out tokens, "
                     f"{t['searches']} searches, {t['fetches']} fetches -- about ${cost:.3f}")
    lines.append(f"- **estimated total so far this run: about ${total:.3f}** (list prices; the Console bill is authoritative)")
    return lines


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

    tracker.last_run = now
    sync_decisions(tracker, decisions, set(profiles))
    run = Run(tracker, tracker_path, profiles, decisions, now)
    run.save()

    for profile_id in selected:
        profile = profiles[profile_id]
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        trace = Trace(f"Discovery trace: {profile_id}, {now:%Y-%m-%d %H:%M} UTC")
        trace_path = Path(args.traces_dir) / f"{stamp}_{profile_id}.md"
        first_failure = len(run.failures)
        try:
            run.ensure_classified(profile)
            result = discover(profile, tracker.profiles[profile_id], tracker, trace=trace, today=run.today,
                              limits=limits, model=args.discovery_model)
            trace.heading("Hand-off: extract, classify, judge fit, merge")
            if not result.candidates:
                trace.add("No candidates accepted.")
            handed: set[str] = set()
            for candidate in result.candidates:
                run.hand_off(profile, candidate, trace, result.fetched_text)
                handed.add(lookup_venue(tracker, candidate.name, candidate.year, candidate.call_page_url).conference_id)
            run.recheck(profile, trace, skip_ids=handed)
        except Exception as exc:
            run.fail_paper(trace, profile_id, exc)
            traceback.print_exc()
        finally:
            trace.heading("Usage and cost")
            trace.add(*_usage_lines())
            if run.failures[first_failure:]:
                trace.heading("Failures")
                trace.add(*(f"- {f}" for f in run.failures[first_failure:]))
            print(f"[run] trace written to {trace.save(trace_path)}")

    run.save()
    print(f"[run] done: {len(tracker.conferences)} tracked venue(s), {len(run.failures)} failure(s) -> {tracker_path}")
    for line in _usage_lines():
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
