# Cicadas — Project Brief

Build spec for the CFP tracker agent. This is what Claude Code builds against — not a narrative doc, a reference. If something here conflicts with an older doc or commit in this repo, this file wins.

No dates or week numbers appear below on purpose. Sequencing is by dependency, not calendar — see "Build order" at the end.

## What Cicadas does

Given a researcher's paper, Cicadas periodically finds conference and publication calls-for-papers that fit it, scores that fit against a rubric chosen for the venue's actual type, and tracks the researcher's decision (apply / skip / applied) over time — without the researcher having to go looking. It also supports an on-demand run mid-cycle, which does the same full assessment, not a raw search.

## What counts as agentic here

The instructor's explicit requirement: the agent must exercise real agency, not execute a script in a loop.

The agency lives in two steps: **classify** (what bucket does this paper, and this venue, actually belong to) and **judge fit** (how well do they match, on dimensions that depend on that bucket). Neither is declared or configured anywhere — the agent reads unstructured content and forms the judgment itself, every run, with reasoning logged for both calls.

A rubric the researcher can see, or even wrote, does not make this less agentic — a rubric supplies criteria to reason with, not an answer to look up. What *would* break it: any dimension collapsing into a keyword match or a hardcoded threshold. Keep dimensions qualitative enough that the model has to read and weigh them. Do not "simplify" scoring into a numeric formula, even under time pressure — that's the one shortcut not available.

## Demo tiers

Every piece of scope falls into exactly one of these. If unsure where something goes, ask rather than guessing.

**Must work live, unscripted, in the actual demo:**

- Extract → classify → judge-fit → tracker-write, run end to end against at least one real, currently-live CFP or venue page, for both profiles
- Classify's reasoning output (bucket + textual cues) — must be real model output, not a canned example
- The first-run snapshot (see Workflow) shown for at least one profile
- Tracker read/write correctness — schema-valid, idempotent on a second run

**Can be mocked or triggered manually if automation isn't finished:**

- The monthly GitHub Actions cron itself — trigger via `workflow_dispatch` live in the demo instead of waiting on a real schedule
- Email delivery — if Resend/SendGrid isn't wired up, show the notification content that *would* have been sent (log it, print it, whatever's fastest) rather than a live inbox
- The daily deadline-watcher — demo by manually running it against a tracker entry with a synthetic near-term deadline, rather than waiting on elapsed real days
- Discovery breadth — if open-ended web search across many venue types proves flaky, narrowing the search scope for the demo is fine

**Explicitly deferred — do not spend build time here unless everything above is solid:**

- Multi-bucket (two-tier) classification — schema supports it; a real example isn't required for the demo
- The full classify eval set / stability check / extraction eval as a formal, repeatable suite — one worked example of the reasoning holding up is enough for the demo; build the fuller version only with time to spare
- Staleness-based closure for rolling (no-deadline) entries — noted as a future addition, not v1
- README polish beyond the authorship disclosure requirement below
- Anything resembling a hosted app, multi-user support, or UI beyond the repo itself

## Architecture decisions

- **No agent framework.** Raw Anthropic Python SDK, hand-written tool-calling loop.
- **Model tiering.** A lighter/faster model for extraction (structured parsing, not judgment). The stronger reasoning model for classify and judge-fit, where nuance matters. Classify adds a third LLM call per candidate (extract → classify → judge-fit); the paper-side half of classify runs once per profile and is cached, the venue-side half runs once per candidate.
- **No MCP in v1.** Built-in web search covers discovery. MCP is a plausible later extension (Gmail for inbox monitoring, GitHub for the commit-back step) — not needed now.
- **Storage is the repo itself.** `tracker.json` lives in git, not on the researcher's machine — GitHub Actions runners are ephemeral, so each run pulls the repo, updates the tracker, commits back.
- **Scheduling — GitHub Actions.** Monthly cron for the main loop. A second, daily workflow (no LLM calls at all — plain date logic) for the deadline watcher. Both should support `workflow_dispatch` for manual triggering.
- **Notifications — a transactional email API** (Resend or SendGrid free tier), not raw SMTP.
- **Secrets** — `.env` locally (gitignored), GitHub Actions repository secrets for the scheduled run. Anthropic API key and the email service key both need this.
- **Build tool vs. runtime, kept distinct.** Claude Code writes the code interactively; it is not part of the finished agent. The finished script calls the Anthropic API directly and runs independent of Claude Code being open.

## Data model

### Paper profiles

Up to **two** live profiles at once, each a file under `profiles/`, each a distinct paper (or a distinct angle on one paper). The loader must refuse to run with more than two — this is a deliberate cost cap, not a bug to fix later.

Each profile file: title, abstract, keywords/topics, preferred format, a short `id`.

**Demo profiles:**

1. The 2025 Nobel Prize–winning immune-tolerance paper (Sakaguchi et al., or another paper from that line of work) — real, published, not the researcher's own. Used as an unambiguous "academic" case.
2. The elephant corridors piece — Gemini-generated, policy/design-summit angle. Used as a genuinely ambiguous, non-academic case.

See–Map–Speak is the researcher's own real work but is **not** one of the two live profiles — referenced in the README only (see Authorship disclosure below).

**Profile swap vs. revision — different operations:**

- *Revising* the same paper (same `id`, updated content) changes `profile_hash`; open tracker entries under it get re-scored on the next run.
- *Swapping* a profile for a different paper (replacing one of the two live slots) does **not** touch `tracker.json`. That profile's already-tracked conferences stay exactly as written — frozen as history, never deleted or auto-closed. They simply stop being updated, since the loader no longer loads that profile file.

### Tracker (`tracker.json`)

One entry per conference/venue. Facts about the venue are stored once, in `extraction`. Everything about fit and the researcher's relationship to it is stored per profile, under `matches`.

```json
{
  "schema_version": 2,
  "last_run": "2026-10-01T09:00:00Z",
  "conferences": [
    {
      "id": "example-stem-education-2027",
      "name": "...", "url": "...",
      "first_seen": "...", "last_checked": "...",
      "extraction": {
        "has_deadline": true, "deadline": "2026-12-15 | null",
        "submission_type": "...", "format_requirements": "...",
        "fees": "...", "mode": "in-person | virtual | hybrid",
        "theme": "...",
        "venue_tier": ["academic_practitioner"],
        "venue_tier_reasoning": "...",
        "complete": true, "missing_fields": []
      },
      "matches": {
        "elephant-corridors": {
          "fit_by_tier": {
            "corporate_institutional": {
              "theme_alignment": { "score": 80, "reasoning": "..." },
              "audience_fit": { "score": 65, "reasoning": "..." },
              "logistical_feasibility": { "score": 60, "reasoning": "..." },
              "overall": 68, "scored_at": "...", "profile_hash": "..."
            }
          },
          "decision": "pending",
          "notifications": [
            { "date": "2026-10-01", "reason": "new_fit", "deadline": "2026-12-15" }
          ],
          "notes": ""
        }
      }
    }
  ]
}
```

A candidate assigned two buckets carries two keys under `fit_by_tier`. Never average across buckets — a validator checking a written entry branches on what `venue_tier` actually contains, rather than assuming a fixed key set.

**Decision states** — set by the agent or the researcher, never both for the same field:

| Decision | Set by | Meaning | Reminders |
|---|---|---|---|
| `pending` | agent | Found and scored; no response yet | Yes — treated as planning to apply |
| `applying` | researcher | Plans to apply | Yes |
| `applied` | researcher | Closed | No |
| `skipped` | researcher | Closed, decided against | No |
| `unconfirmed` | agent | Closed: deadline passed, no confirmation | No |

**Rules:**

- **Identity.** `id` = normalized name + year. A found conference counts as already-tracked if its `id` *or* its URL matches either an existing entry. Domain alone is never enough (hosts like EasyChair carry many conferences) — merging two real conferences silently is worse than an occasional duplicate.
- **Ownership.** Agent creates entries (`pending`), writes `extraction`/`matches`/`notifications`, and makes exactly one status change itself: closing to `unconfirmed`. The researcher sets `applying`/`applied`/`skipped` and writes `notes`.
- **Idempotency.** A re-run updates an existing entry, never duplicates one, and never repeats an email already logged in `notifications`.
- **Per profile.** A candidate is scored against the profile whose search surfaced it. If a second profile's search later finds an already-tracked conference, that adds a second entry under `matches`, not a second conference.
- **Stale scores.** `profile_hash` fingerprints the profile at scoring time. Open entries with a mismatched hash get re-scored on the next run; closed entries keep their score as history.
- **Incomplete extraction.** Missing fields → `complete: false`, listed in `missing_fields`, flagged rather than scored.
- **`has_deadline: false` is a fact, not a gap.** A genuinely rolling/pitch-based outlet gets `has_deadline: false` — that's real information. A deadline that should exist but wasn't findable on the page is a *different* case: `complete: false`, in `missing_fields`. Don't conflate the two — this distinction is what the extraction eval specifically stress-tests.

## Venue buckets and rubric

Four buckets, deliberately few:

1. **Academic & Practitioner**
2. **Industry & Technical Media**
3. **Corporate & Institutional**
4. **Other Avenues** — catch-all

Classify assigns the bucket(s); none are declared on the profile. Almost always exactly one. Two only when a candidate genuinely straddles buckets — capped at two; a third suggests the buckets need rethinking, not that the candidate is unusual.

**Shared dimension, every bucket:** `theme_alignment` — does the call overlap with the paper's actual subject matter.

**Additional dimensions by bucket:**

- **Academic & Practitioner:** `nature_fit` (research-rigor vs. practitioner framing, matched to how the paper itself is framed) · `logistical_feasibility` (from already-extracted facts — deadline runway, mode, fees — not invented fresh)
- **Industry & Technical Media:** `voice_register_match` (does the outlet's actual published tone match the paper's register) · `timeliness` · `submission_model` (CFP-based open call vs. pitch-based)
- **Corporate & Institutional:** `audience_fit` (policymaker/executive audience vs. practitioner) · `logistical_feasibility` (invite-only vs. open, application-gated vs. not)
- **Other Avenues:** `theme_alignment` only, always with reasoning for why nothing else fit. Flag as lower-confidence — a signal to glance at manually, not a fully-trusted score.

Equal weighting within whichever dimension set was selected (simple average). Don't hand-tune weights — there's no run data yet to justify any particular weighting.

**Multi-bucket:** score each assigned bucket independently under `fit_by_tier`, never merged. Tracker and notifications list the conference once, with every bucket and score it qualified under.

## Workflow

**Step 0 — onboarding (once per profile).** Researcher provides paper (PDF/text); a one-time script writes `profiles/<id>.json`.

**Every run, per profile:**

1. **Trigger** — monthly cron, or `workflow_dispatch` on demand. Same code path either way.
2. **Discover** — web search for candidate CFPs/venues matching the profile's topics.
3. **Extract** — per candidate: deadline (or its genuine absence), submission type, format, fees, mode, theme.
4. **Classify** — read the paper's own content (once per profile, cached) and the candidate's content separately; infer bucket(s) for each, with reasoning logged for both calls.
5. **Judge fit** — score against the bucket(s) assigned, using that bucket's dimensions.
6. **Update tracker** — write extraction + fit + reasoning, commit back.
7. **Notify** — first email only if fitness > 50%, one email per conference listing every profile/bucket it fits. Below threshold: silent tracker update, no email, no periodic "still watching" message.

Steps 3–6 repeat per candidate found in step 2.

**First run per profile — required, not optional.** The first email for a newly onboarded profile carries a one-time snapshot, *before* any conference list:

- (a) the bucket Cicadas classified the paper itself into
- (b) 3–4 textual cues that drove that call
- (c) score and reasoning — shown per-dimension, not just as a combined number

This lets the researcher catch a misread immediately rather than trusting it silently for months. Every notification after the first — not just the first — shows the full per-dimension breakdown for each matched conference, not just the combined fitness score.

## Deadline watcher and closure

Separate daily GitHub Actions workflow. Reads `tracker.json` only — **no LLM, no web search**, plain date arithmetic. Reuses the email code. Runs at a different hour than the monthly workflow so they never commit at once.

- **`new_fit`** (monthly run) — first time a match crosses 50%.
- **`deadline_changed`** (monthly run) — an open, `has_deadline: true` match whose deadline changed since last recorded. Doesn't apply to rolling entries.
- **`week_before`** (daily watcher) — only `has_deadline: true` entries, `pending` or `applying`, seven days out, sent once per deadline.
- **Closure** — only `has_deadline: true` entries. Two days after the deadline, any still-open match becomes `unconfirmed`, including ones that never crossed 50% (they were never emailed; their stored fit explains why). Closed entries are never reopened or re-scored, even if rediscovered later.
- **Rolling entries (`has_deadline: false`)** — no week-before reminder, no date-based closure. Stay `pending` indefinitely until the researcher manually sets `applying`/`applied`/`skipped`. Not re-emailed on `new_fit` more than once.

Every email logged in `notifications` (date, reason, deadline) — this is how "already sent for this deadline" gets checked.

## Guardrails

- **Spending cap** — set on the Anthropic Console. Not a code-level guarantee; a deliberate, named backstop.
- **Validate before writing.** Incomplete or malformed extraction → flagged (`complete: false`), never silently written as if it were good data.
- **Visible failure.** A run that errors logs somewhere check-able. Silent failure and "nothing found this month" must never look the same from the outside.
- **Classify trust — checked, not assumed:**
  - Small eval set (3–5 papers/CFPs with an obvious bucket) run through classify, checked for agreement
  - Stability check — same input, same output, run twice
  - One deliberately ambiguous case — the elephant-corridors-at-a-design-summit scenario is the natural one
- **Extraction trust — checked, not assumed:**
  - 4–5 real venue pages, one per bucket where possible, hand-verified against what Cicadas extracted
  - Stability check — same page, parsed twice, should agree
  - At least one rolling/pitch-based case (`has_deadline: false`) and one case with a real-but-poorly-stated deadline (`complete: false`) — the single fact most likely to silently misfire a reminder if wrong

Per Demo tiers above: the full formal eval suite for both is a "nice to have" for the demo — one solid worked example of each matters more than a complete battery if time is short.

## Interface

No separate app. Onboarding is a one-time script per profile. "On/off" and "run now" both come from GitHub Actions itself:

- Disabling the workflow schedule (Actions tab, Enable/Disable toggle) pauses the search entirely — a vacation, a quiet stretch, whatever the researcher wants. Nothing else needs to change; re-enabling resumes cleanly on the next scheduled date.
- `workflow_dispatch` alongside the schedule trigger, same workflow file, gives a one-click "Run workflow" button for on-demand runs — no separate code path.

A hosted web app for other researchers is explicitly out of scope.

## Authorship disclosure (README requirement)

The README must state, per profile, one line each:

- **See–Map–Speak** — the researcher's own work; referenced, not one of the two live profiles.
- **Elephant corridors** — Gemini-generated; used as a genuine tier-ambiguity stress test.
- **Immune-tolerance paper** — real, published research, not the researcher's own; used purely to exercise classify against an unambiguous academic case.

## Build order (by dependency, not calendar)

1. Tracker schema + profile schema on disk, validated by hand against the JSON shapes above
2. Extraction working end to end against one real, hardcoded venue page
3. Classify working against both demo profiles + a couple of real candidates, reasoning logged and legible
4. Judge-fit wired to classify's bucket output, per-bucket dimensions applied correctly
5. Tracker read/write, idempotent, tested with a second run against unchanged input
6. First-run snapshot output
7. Email sending, tested standalone before wiring into notify logic
8. Web search wired into discovery, full loop running end to end
9. GitHub Actions: monthly workflow + `workflow_dispatch`, commit-back
10. Deadline watcher: daily workflow, week-before + closure logic, tested against a synthetic near-term deadline
11. README (incl. authorship disclosure)
12. Eval sets — classify and extraction — at minimum one worked example each; fuller suite if time allows

If time runs out, stop after whichever numbered item is last solid and demo from there — see Demo tiers for what's allowed to be mocked instead of finished.

## Status

*(Update this section directly as pieces land — don't duplicate it in CLAUDE.md.)*

**Done:**

- Repo created (private), standard Python `.gitignore`, README stub in place (needs alignment — see below)
- Git/Python/Node environment set up and verified
- `src/config.py` — centralizes model choice (Haiku 4.5 for extraction, Sonnet 5 for judging — declared, not yet called from anywhere) and `TRACKER_PATH`. Matches this brief's model-tiering decision exactly.
- `requirements.txt` — `anthropic`, `resend`, `python-dotenv`
- `src/llm.py` — the single `call_llm()` wrapper every model call routes through; reads the API key from env/`.env`; logs input/output token counts per call. This is the "every call goes through one wrapper that logs usage" guardrail, already satisfied at the plumbing level.
- This brief and the CLAUDE.md pointing to it are written; not yet pushed to GitHub as of the last check

**Not yet done — none of the actual pipeline exists yet:**

- Extraction, classify, judge-fit — no logic for any of the three
- `tracker.json` and tracker read/write logic
- `profiles/` folder and both demo profile files
- Email sending (dependency installed, nothing calls it yet)
- GitHub Actions workflows — monthly loop and daily deadline watcher, neither started
- Discovery (web search)
- `tests/`
- Eval sets — classify and extraction

**Repo hygiene to fix, not just build:**

- No `.env.example` — this brief expects one; add it before any more keys get added, so the shape of required secrets is documented without exposing values
- Confirm `.env` is actually covered by the current `.gitignore` rather than assuming the standard template caught it
- README currently references `paper_profile.json`; this brief uses `profiles/<id>.json`. Line these up once `profiles/` exists — the authorship disclosure requirement also belongs here and hasn't landed yet either
