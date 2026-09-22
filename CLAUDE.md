# Cicadas — CFP Tracker Agent

Cicadas watches for conference and publication calls-for-papers that fit a given paper, scores the fit against a bucket-specific rubric, and tracks the researcher's decision over time. It runs on a schedule, not on request — though on-demand runs are also supported.

**Full spec, schema, workflow, and every settled design decision:**
[`docs/project-brief.md`](docs/project-brief.md) — read it before writing or changing any code. This file is intentionally short; don't let anything below substitute for it.

## Non-negotiables

- **No agent framework.** No LangChain, CrewAI, AutoGen. A hand-written loop against the raw Anthropic Python SDK — this is a course requirement, not a style preference.
- **Registration and payment are never automated.** Cicadas tracks and notifies; the researcher acts. Don't build toward this even as a stretch goal.
- **A monthly spend cap is set on the Anthropic Console.** Design around it existing — don't add retry loops or fallback logic that could quietly burn through it.
- **Tracked history is never deleted.** Swapping a paper profile for a different one freezes that profile's old tracker entries; it does not remove them. Only a revision of the *same* paper triggers re-scoring.
- **No silent failures.** A run that errors logs visibly, somewhere check-able. Since notifications only fire above a fitness threshold, a silent failure and "genuinely nothing found" must never look identical.
- **The rubric stays qualitative.** Dimensions must require the model to read and weigh text, not collapse into a keyword-match or numeric threshold. This is what keeps the classify/judge steps agentic rather than scripted — see "What counts as agentic" in the brief before changing any scoring logic.

## On timelines

Ignore any week-by-week pacing, dates, or "Week 1 / Week 2" language you find anywhere in this repo's docs or commit history — those are stale. The actual target is a working, live-demonstrable agent within days. Don't let old scheduling language shape scope or sequencing decisions; ask if timing tradeoffs come up.

## Demo requirements

See "Demo tiers" in `docs/project-brief.md`. In short: classify, judge-fit, and the tracker write/read path must work live and unscripted. Scheduling infrastructure and email delivery can be mocked or triggered manually if unfinished. A short, named list of items can be explicitly deferred — don't silently drop scope without checking it against that list first.

## Current build status

Tracked in the Status section of `docs/project-brief.md`, not duplicated here — update that file, not this one, as pieces land.
