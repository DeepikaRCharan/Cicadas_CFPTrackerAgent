# Cicadas — Project Brief

Build spec for the CFP tracker agent. This is what Claude Code builds against — not a narrative doc, a reference. If something here conflicts with an older doc or commit in this repo, this file wins.

No dates or week numbers appear below on purpose. Sequencing is by dependency, not calendar — see "Build order" at the end.

## What Cicadas does

Given a researcher's paper, Cicadas periodically finds conference and publication calls-for-papers that fit it, scores that fit against a rubric chosen for the venue's actual type, and tracks the researcher's decision (apply / skip / applied) over time — without the researcher having to go looking. It also supports an on-demand run mid-cycle, which does the same full assessment, not a raw search.

## What counts as agentic here

The instructor's explicit requirement: the agent must exercise real agency, not execute a script in a loop.

The agency lives in two steps: **classify** (what bucket does this paper, and this venue, actually belong to) and **judge fit** (how well do they match, with the bucket steering how the kind of writing is judged). Neither is declared or configured anywhere — the agent reads unstructured content and forms the judgment itself, every run, with reasoning logged for both calls.

A rubric the researcher can see, or even wrote, does not make this less agentic — a rubric supplies criteria to reason with, not an answer to look up. What *would* break it: any dimension collapsing into a keyword match or a hardcoded threshold. Keep dimensions qualitative enough that the model has to read and weigh them. Do not "simplify" scoring into a numeric formula, even under time pressure — that's the one shortcut not available.

## Demo tiers

Every piece of scope falls into exactly one of these. If unsure where something goes, ask rather than guessing.

**Must work live, unscripted, in the actual demo:**

- Extract → classify → judge-fit → tracker-write, run end to end against at least one real, currently-live CFP or venue page, for both profiles
- Classify's reasoning output (bucket + textual cues) — must be real model output, not a canned example
- The first-run snapshot (see Workflow) shown for at least one profile
- Tracker read/write correctness — schema-valid, idempotent on a second run
- The researcher decision round-trip: edit one line in `decisions.json` on GitHub, re-run, and the tracker and next email reflect it

**Can be mocked or triggered manually if automation isn't finished:**

- The monthly GitHub Actions cron itself — trigger via `workflow_dispatch` live in the demo instead of waiting on a real schedule
- Email delivery — if Resend isn't wired up, print the rendered email that *would* have been sent rather than showing a live inbox. The renderer itself is not mockable: the printed output must come from the same template the real email uses
- The daily deadline-watcher — demo by manually running it against a tracker entry with a synthetic near-term deadline, rather than waiting on elapsed real days
- Discovery breadth — if open-ended web search across many venue types proves flaky, narrowing the search scope for the demo is fine

**Explicitly deferred — do not spend build time here unless everything above is solid:**

- Multi-bucket (two-tier) classification — schema supports it; a real example isn't required for the demo
- The full classify eval set / stability check / extraction eval as a formal, repeatable suite — one worked example of the reasoning holding up is enough for the demo; build the fuller version only with time to spare
- Staleness-based closure for rolling (no-deadline) entries — noted as a future addition, not v1
- README polish beyond the authorship disclosure and setup sections below
- Anything resembling a hosted app, multi-user support, or UI beyond the repo itself

## Architecture decisions

- **No agent framework.** Raw Anthropic Python SDK, hand-written tool-calling loop.
- **Model tiering.** A lighter/faster model for extraction (structured parsing, not judgment). The stronger reasoning model for classify and judge-fit, where nuance matters. Classify adds a third LLM call per candidate (extract → classify → judge-fit); the paper-side half of classify runs once per profile and is cached, the venue-side half runs once per candidate.
- **No MCP in v1.** Built-in web search covers discovery. MCP is a plausible later extension (Gmail for inbox monitoring, GitHub for the commit-back step) — not needed now.
- **Storage is the repo itself.** `tracker.json` lives in git, not on the researcher's machine — GitHub Actions runners are ephemeral, so each run pulls the repo, updates the tracker, commits back.
- **Scheduling — GitHub Actions.** The monthly run fires on the **7th of every month**, whatever the weekday: discovery, re-check of tracked open venues, then the digest. A second, daily workflow (no LLM calls at all — plain date logic) for the deadline watcher. Both support `workflow_dispatch` for manual triggering. No daily or weekly web scanning: discovery and re-checks are monthly only, which keeps LLM spend to one run a month.
- **Notifications — Resend free tier**, sending from Resend's default `onboarding@resend.dev` sender, not raw SMTP and no custom domain. That sender can only deliver to the email address tied to the Resend account, so each user runs their own Resend account and sets `NOTIFY_TO` to that account's address. A send to any other address is rejected (403) — it must fail the run visibly, never be swallowed.
- **Secrets** — `.env` locally (gitignored), GitHub Actions repository secrets for the scheduled run. Three values: `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `NOTIFY_TO`. No email address is hardcoded anywhere in the code; the recipient comes only from `NOTIFY_TO`.
- **Handing Cicadas to another user** (e.g. the course instructor) — they get their own copy of the repo, set their own three secrets (so API usage bills to them and email lands in their inbox), put their own paper(s) in `profiles/`, start from an empty `tracker.json` and an empty `decisions.json`, and enable Actions (off by default on a fork). Nothing is shared with the original repo at runtime. This is single-user handoff, not multi-user deployment — the latter stays out of scope.
- **Build tool vs. runtime, kept distinct.** Claude Code writes the code interactively; it is not part of the finished agent. The finished script calls the Anthropic API directly and runs independent of Claude Code being open.

## Data model

### Paper profiles

Up to **two** live profiles at once, each a file under `profiles/`, each a distinct paper (or a distinct angle on one paper). The loader must refuse to run with more than two — this is a deliberate cost cap, not a bug to fix later.

Each profile file: title, abstract, keywords/topics, a short `id`. **No preferred format** — what the paper is, and what form it could be submitted in, is for the agent to read from the paper itself, not for the researcher to declare.

**Demo profiles:**

1. The 2025 Nobel Prize–winning immune-tolerance paper (Sakaguchi et al., or another paper from that line of work) — real, published, not the researcher's own. Used as an unambiguous "academic" case.
2. The elephant corridors piece — Gemini-generated, policy/design-summit angle. Intended as the ambiguous case; classify read it as Academic & Practitioner (its abstract is dense academic synthesis), and that honest result stands. Coverage of the non-academic buckets comes from the classify eval set, not from forcing this profile.

See–Map–Speak is the researcher's own real work but is **not** one of the two live profiles — referenced in the README only (see Authorship disclosure below).

**Profile swap vs. revision — different operations:**

- *Revising* the same paper (same `id`, updated content) changes `profile_hash`; open tracker entries under it get re-scored on the next run.
- *Swapping* a profile for a different paper (replacing one of the two live slots) does **not** touch `tracker.json`. That profile's already-tracked conferences stay exactly as written — frozen as history, never deleted or auto-closed. They simply stop being updated, since the loader no longer loads that profile file.

### Tracker (`tracker.json`)

One entry per conference/venue. Facts about the venue are stored once, in `extraction`. Everything about fit and the researcher's relationship to it is stored per profile, under `matches`.

```json
{
  "schema_version": 3,
  "last_run": "2026-10-01T09:00:00Z",
  "profiles": {
    "elephant-corridors": {
      "paper_bucket": ["academic_practitioner"],
      "paper_bucket_cues": ["...", "...", "..."],
      "paper_bucket_reasoning": "...",
      "profile_hash": "...", "classified_at": "...",
      "snapshot_sent": "2026-10-01 | null"
    }
  },
  "conferences": [
    {
      "id": "example-stem-education-2027",
      "name": "...", "url": "...",
      "first_seen": "...", "last_checked": "...",
      "extraction": {
        "has_deadline": true,
        "deadline": "2026-12-15 | null",
        "deadline_precision": "exact | month | null",
        "deadline_as_stated": "15 December 2026 | null",
        "submission_opens": "November 2026 | null",
        "call_status": "not_yet_open | open | closed | unknown",
        "submission_type": "...", "theme": "...",
        "location": "Florence, Italy | null",
        "access": "open | invite_only | members_only | null",
        "format_requirements": "... | null",
        "fees": "... | null", "mode": "in-person | virtual | hybrid | null",
        "venue_tier": ["academic_practitioner"],
        "venue_tier_reasoning": "...",
        "complete": true, "missing_fields": []
      },
      "matches": {
        "elephant-corridors": {
          "fit_by_tier": {
            "academic_practitioner": {
              "theme_fit": { "score": 80, "reasoning": "..." },
              "genre_fit": { "score": 65, "reasoning": "..." },
              "format_fit": { "score": 60, "reasoning": "..." },
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

**`profiles` block.** Classify's read of the paper itself (bucket, 3–4 cues, reasoning) is agent output, so it lives here in the tracker, not in the researcher-authored `profiles/<id>.json`. It's the cache for "read the paper once per profile," and it's what the first-run snapshot renders from. `snapshot_sent` is how the notifier knows the snapshot already went out — null means the next email for this profile carries it. A changed `profile_hash` (revision) re-classifies the paper but does not resend the snapshot. A swapped-out profile's block stays, frozen, like its tracker entries.

A candidate assigned two buckets carries two keys under `fit_by_tier`. Never average across buckets — a validator checking a written entry branches on what `venue_tier` actually contains, rather than assuming a fixed key set.

### Researcher decisions (`decisions.json`)

A small file at the repo root that **only the researcher edits** — the agent reads it, never writes it. One line per conference the researcher has a view on, keyed by conference `id` (shown in every digest row):

```json
{
  "eci-2027": "applying",
  "example-summit-2027": "skipped"
}
```

- Allowed values: `applying`, `applied`, `skipped` — the researcher's three states only. `pending` and `unconfirmed` are agent-only and are an error here.
- Read at the start of **every** run, monthly and daily watcher, before anything else. Its values are copied into `matches.<profile>.decision` for every profile under that conference.
- A key that isn't a tracked conference `id`, or a value outside the three allowed, stops the run with a visible error naming the bad line. Never guessed, never skipped.
- No line for a conference means no researcher decision: it stays `pending` (or `unconfirmed` once the agent closes it).
- A researcher decision always wins over the agent's: setting `applied` or `skipped` on an `unconfirmed` entry is allowed and simply records the truth.
- Known limitation: one decision per conference, applied to every profile matched to it. Per-profile decisions are not in v1.
- Edited on GitHub's website (pencil icon → commit). Keeping it separate from `tracker.json` means the researcher never touches the agent's file, so a hand-edit can't collide with the agent's monthly commit or break the tracker schema.

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
- **Ownership.** Agent creates entries (`pending`), writes `extraction`/`matches`/`notifications`, and makes exactly one status change itself: closing to `unconfirmed`. The researcher sets `applying`/`applied`/`skipped` via `decisions.json` only; the agent copies them in and never infers them. `notes` stays in the schema but is unused in v1.
- **Idempotency.** A re-run updates an existing entry, never duplicates one, and never repeats an email already logged in `notifications`.
- **Per profile.** A candidate is scored against the profile whose search surfaced it. If a second profile's search later finds an already-tracked conference, that adds a second entry under `matches`, not a second conference.
- **Stale scores.** `profile_hash` fingerprints the profile at scoring time. Open entries with a mismatched hash get re-scored on the next run; closed entries keep their score as history.
- **Critical vs. optional fields.** Critical: the deadline (when `has_deadline: true`), `theme`, `submission_type`. Optional: `location`, `access`, `mode`, `fees`, `format_requirements`, `submission_opens`. Any field not stated on the page is null and listed in `missing_fields` — never guessed. `complete: false` only when a **critical** field is missing; a venue missing only optional fields is still scored. Incomplete venues are flagged rather than scored.
- **Deadline precision.** `deadline_as_stated` is always the page's own wording. An exact date → `deadline_precision: exact`. A month only ("February 2027") → `deadline_precision: month`, with `deadline` set to the first day of that month. A later run that finds the exact date upgrades it and counts as `deadline_changed`.
- **Who fills what.** Extraction (light model) fills the fact fields only. `venue_tier` and `venue_tier_reasoning` are written by classify (stronger model), never by extraction.
- **Call status.** `call_status` describes the venue's call, not the researcher's submission — it records what the page says now, judged against today's date (passed to extraction): `not_yet_open`, `open`, `closed`, or `unknown`. `submission_opens` keeps the page's wording for when it opens.
- **`has_deadline: false` is a fact, not a gap.** A genuinely rolling/pitch-based outlet gets `has_deadline: false` — that's real information. A deadline that should exist but wasn't findable on the page is a *different* case: `complete: false`, in `missing_fields`. Don't conflate the two — this distinction is what the extraction eval specifically stress-tests.

## Venue buckets and rubric

Four buckets, deliberately few:

1. **Academic & Practitioner**
2. **Industry & Technical Media**
3. **Corporate & Institutional**
4. **Other Avenues** — catch-all

Classify assigns the bucket(s); none are declared on the profile. Almost always exactly one. Two only when a candidate genuinely straddles buckets — capped at two; a third suggests the buckets need rethinking, not that the candidate is unusual.

**Three dimensions, same names in every bucket.** Whatever the writing — empirical research, business process, economics, social observation — fit comes down to three questions, and a venue can fail any one while passing the other two:

- **`theme_fit`** — *Does the venue want work on this paper's subject?* The paper's core subject, not words it merely mentions, against the venue's stated theme, topics, tracks and categories. Central overlap (fits a named track) scores high; a loose connection scores low.
- **`genre_fit`** — *Does the venue publish this type of writing, for this kind of reader?* A genre is a type of text with its own conventions, register and expected audience — a research article, a trade feature, a policy brief. The bucket tells the model what to look at:
  - Academic & Practitioner — research rigor and method: would their reviewers accept this level of evidence?
  - Industry & Technical Media — voice and register: does the outlet publish writing that sounds like this?
  - Corporate & Institutional — audience: would policymakers or executives find it usable?
- **`format_fit`** — *Can the paper be submitted in a form and length the venue accepts?* Size and shape only, not content: abstract, full paper, article, pitch, talk proposal, length limits. Judged by the model from the paper itself, noting any adaptation needed ("would need condensing to an abstract").

**Other Avenues:** `theme_fit` only, plus required `no_other_fit_reasoning` — why nothing else fit. Flagged lower-confidence: a signal to glance at manually, not a fully-trusted score.

**Not scored, on purpose.** Deadline, location, access, fees and mode are facts, shown as columns in the email; the researcher judges them better than a score can, and scoring them double-counted them and penalised information the page simply didn't state. A venue whose call is closed, whose deadline has passed, or whose `access` is `invite_only` is not scored at all — code checks that before judge-fit runs. `members_only` is shown, not skipped, since the researcher may be a member.

**Rules for every dimension:** read both texts and cite them, not general knowledge about the field; unknown is not bad — say so in the reasoning rather than scoring it down or assuming the best; score 0–100 (roughly: 80+ strong, 50–79 workable with caveats, under 50 poor); the reasoning explains the number rather than restating it.

**Why one shape instead of per-bucket dimension sets:** a wrong venue tier now only changes the lens on `genre_fit`, instead of swapping the whole rubric — tier errors do less damage.

Equal weighting across the dimensions (simple average). Don't hand-tune weights — there's no run data yet to justify any particular weighting.

**Multi-bucket:** score each assigned bucket independently under `fit_by_tier`, never merged. Tracker and notifications list the conference once, with every bucket and score it qualified under.

## Workflow

**Step 0 — onboarding (once per profile).** Researcher provides paper (PDF/text); a one-time script writes `profiles/<id>.json`.

**Every run, per profile:**

1. **Trigger** — the 7th of every month, or `workflow_dispatch` on demand. Same code path either way. The first action of every run is syncing `decisions.json` into the tracker (see Researcher decisions).
2. **Discover** — web search built from the paper's topics **and** its classification. This replaces the researcher declaring a preferred venue type: the agent reads the paper and decides where to look.
   - **Main searches** (most queries): the paper's topics combined with the vocabulary of the paper's own bucket(s) — e.g. Academic: "call for papers", "call for abstracts"; Industry: "call for speakers", "call for presentations", "write for us"; Corporate & Institutional: "policy forum", "expert panel", "submissions invited".
   - **Probe searches** (one query per remaining bucket, every run): the same topics with each other bucket's vocabulary. Classification is a prediction made from the paper alone; the probe lets real venues test it. New venues appear monthly, so the probe runs monthly — the paper doesn't need to change for it to find something new. A probe-found venue is judged exactly like any other; if the paper really doesn't suit that venue type, `genre_fit` is low and it stays silent.
   - Found candidates are de-duplicated against the tracker: already-tracked open venues go to the re-check instead; closed ones are ignored.
3. **Extract** — per candidate: deadline (or its genuine absence) and its precision, submission status and opening, submission type, theme, location, format, fees, mode.
   - **Re-check tracked venues** — every open (`pending` or `applying`) tracked venue is re-fetched and re-extracted (light model only; no re-classify or re-score unless the profile changed). A change to `call_status` → `open` logs `submissions_opened`; a changed or newly-exact deadline logs `deadline_changed`.
4. **Classify** — read the paper's own content (once per profile, cached) and the candidate's content separately; infer bucket(s) for each, with reasoning logged for both calls.
5. **Judge fit** — skipped entirely if the call is closed, the deadline has passed, or access is invite-only (a code check). Otherwise score each assigned bucket on `theme_fit`, `genre_fit` and `format_fit`, with the bucket steering how `genre_fit` is judged. The model scores each dimension with reasoning; it is never asked for `overall`, which code computes as the simple average.
6. **Update tracker** — write extraction + fit + reasoning, commit back.
7. **Notify** — one digest email per run covering all profiles, always sent, even in a month with nothing new (it then says so, and still carries the Still open list). A conference enters the digest's New section only if fitness > 50%; below that, the tracker updates silently and it never appears — with one exception, the Tier Alert section (strong theme fit, low overall). Because the digest always arrives on the 7th, a missing email is itself a visible sign the run failed.

Steps 3–6 repeat per candidate found in step 2.

**First run per profile — required, not optional.** The first email for a newly onboarded profile carries a one-time snapshot, *before* any conference list:

- (a) the bucket Cicadas classified the paper itself into
- (b) 3–4 textual cues that drove that call
- (b2) where Cicadas will mainly look: *"Cicadas will mainly look for these kinds of venues: …"* — the paper's bucket(s) as search targets, with a note that every other venue type still gets a light monthly probe
- (c) score and reasoning — shown per-dimension, not just as a combined number

This lets the researcher catch a misread immediately rather than trusting it silently for months. Every notification after the first — not just the first — shows the full per-dimension breakdown for each matched conference, not just the combined fitness score.

**Email layout — fixed in code, never composed by the LLM.** The LLM's job ends at writing schema-validated fields into `tracker.json`. A deterministic template renders every email from those fields, so the layout is identical run after run and only the reasoning text varies. Any null field renders as "Not stated on venue page" — the honest wording comes from the template, never from the model. If a column needs a field the schema doesn't have, add it to the schema first; the template never computes or infers content.

**Monthly digest (sent on the 7th) — four sections, in this order,** with the first-run snapshot block above them when a profile is new:

*1. New this month* — conferences whose `new_fit` is logged this run. Shown here **once only**, with full scores and rationale; never again with rationale.

| Column | Source field |
|---|---|
| Conference | `name` |
| Location | `extraction.location` |
| Access | `extraction.access` (open / members only) |
| Deadline | `extraction.deadline_as_stated`, or "Rolling" when `has_deadline: false` |
| Call | `extraction.call_status` + `extraction.submission_opens`, rendered as "Opens November 2026" / "Open now" / "Closed" / "Not stated on venue page" |
| Bucket(s) | `extraction.venue_tier` |
| Fitness | `fit_by_tier.<bucket>.overall` (one row per bucket if multi-bucket) |
| Per-dimension scores | `fit_by_tier.<bucket>.<dimension>` — score + reasoning |
| Mode · Fees | `extraction.mode`, `extraction.fees` |
| Link | `url` |
| ID | `id` — what the researcher types into `decisions.json` |

*2. Tier Alert* — shown with the subtitle *"Strong topic match, low score: possibly misread venue type, or a format you'd need to adapt."* Conferences scored this run with **`theme_fit` ≥ 75 but `overall` < 50**, call not closed. These would otherwise be silent. The signature usually means a wrong venue tier (the genre-fit lens was wrong), a genuine genre mismatch, or a format mismatch the researcher could fix (e.g. the venue takes abstracts only). Shown **once only**, logged as `tier_alert`. The 75 is a named constant (`TIER_ALERT_THEME_MIN`) in `config.py`, computed purely from stored scores — no extra LLM call.

| Column | Source field |
|---|---|
| Conference | `name` |
| Location | `extraction.location` |
| Deadline | `extraction.deadline_as_stated` |
| Bucket(s) | `extraction.venue_tier` |
| Theme fit · Overall | `fit_by_tier.<bucket>.theme_fit.score`, `overall` |
| What pulled it down | the lowest-scoring dimension, with its reasoning |
| Link · ID | `url`, `id` |

*3. Updates* — conferences already sent before that logged `submissions_opened` or `deadline_changed` this run. No rationale, no scores.

| Column | Source field |
|---|---|
| Conference | `name` |
| Location | `extraction.location` |
| What changed | this run's notification `reason` (applications now open / deadline published or changed) |
| Deadline | `extraction.deadline_as_stated` |
| Your decision | `matches.<profile>.decision` |
| Link · ID | `url`, `id` |

*4. Still open* — every tracked venue that is either above the fitness threshold and `pending`, or `applying` whatever its score, and not past its deadline, sorted by deadline. One line each, no LLM involved.

| Column | Source field |
|---|---|
| Conference | `name` |
| Location | `extraction.location` |
| Deadline | `extraction.deadline_as_stated` |
| Your decision | `matches.<profile>.decision` |
| ID | `id` |

**Week-before reminder (daily watcher)** — one email per conference, sent on its own day. It names the conference (name, location, deadline, link, ID, current decision) and adds an **Also coming up** list: every other reminder-eligible deadline (same rule as `week_before`) in the next 14 days (name, location, deadline). Several reminders in one week is expected; each conference is still reminded exactly once.

## Deadline watcher and closure

Separate daily GitHub Actions workflow. Reads `decisions.json` then `tracker.json` — **no LLM, no web search**, plain date arithmetic. Reuses the email code. Runs at a different hour than the monthly workflow so they never commit at once.

- **`new_fit`** (monthly run) — first time a match crosses 50%.
- **`tier_alert`** (monthly run) — first time a match scores `theme_fit` ≥ 75 with `overall` < 50 and the call not closed. Shown once, in the digest's Tier Alert section.
- **`submissions_opened`** (monthly run) — a re-check finds `call_status` changed to `open` on an already-sent venue.
- **`deadline_changed`** (monthly run) — an open, `has_deadline: true` match whose deadline or its precision changed since last recorded (including month → exact date). Doesn't apply to rolling entries.
- **`week_before`** (daily watcher) — only `has_deadline: true` entries that are either `applying` (whatever the score — the researcher's choice overrides it) or `pending` and above the 50% threshold; sent once per deadline: seven days before an exact date; for a month-only deadline, seven days before that month starts, labelled "exact date not published."
- **Closure** — only `has_deadline: true` entries. Two days after the deadline (for a month-only deadline, two days after that month ends), any still-open match becomes `unconfirmed`, including ones that never crossed 50% (they were never emailed; their stored fit explains why). Closed entries are never reopened or re-scored, even if rediscovered later.
- **Rolling entries (`has_deadline: false`)** — no week-before reminder, no date-based closure. Stay `pending` indefinitely until the researcher sets `applying`/`applied`/`skipped` in `decisions.json`. Not re-emailed on `new_fit` more than once.

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

## Pinned for later (not v1)

- **Learning from the researcher's decisions** — e.g. noticing that every European venue gets skipped and proposing location as a scoring factor. Must ask the researcher before changing how anything is scored; never adjusts silently.
- **GitHub Issues as the decision interface** — one Issue per conference, decision set by label, synced the same way as `decisions.json`. Both only ever set `decision`, so switching changes nothing underneath.
- **A dashboard UI** with a decision dropdown per venue.

## Authorship disclosure (README requirement)

The README must state, per profile, one line each:

- **See–Map–Speak** — the researcher's own work; referenced, not one of the two live profiles.
- **Elephant corridors** — Gemini-generated; used as a genuine tier-ambiguity stress test.
- **Immune-tolerance paper** — real, published research, not the researcher's own; used purely to exercise classify against an unambiguous academic case.

## Setup section (README requirement)

The README must include a short setup section that lets someone else run their own copy: create a Resend account; set the three secrets (`ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `NOTIFY_TO` = the Resend account's email) locally in `.env` and as GitHub Actions secrets; replace the files in `profiles/` with their own paper(s), max two; reset `tracker.json` to empty and `decisions.json` to `{}`; enable Actions. Also note that a monthly spend cap on their own Anthropic Console is recommended.

## Build order (by dependency, not calendar)

1. Tracker schema (including the `profiles` block) + profile schema on disk, validated by hand against the JSON shapes above; confirm every email-table column maps to a schema field
2. Extraction working end to end against one real, hardcoded venue page (https://eci2027.org/call-for-abstracts/ — exact deadline, month-only opening, no fees or mode stated, and footer links to an unrelated congress)
3. Classify working against both demo profiles + a couple of real candidates, reasoning logged and legible
4. Judge-fit wired to classify's bucket output — the three dimensions, bucket-specific lens on `genre_fit`, closed/passed/invite-only calls skipped by code; include a deliberate mismatch test (elephant-corridors × ECI 2027 should score low with clear reasoning)
5. Tracker read/write, idempotent, tested with a second run against unchanged input; `decisions.json` sync, including the bad-key and bad-value errors
6. Email template renderer — pure function from tracker fields to email body: digest (snapshot, New, Tier Alert, Updates, Still open) and week-before reminder (with Also coming up); tested by printing output, no sending
7. Email sending, tested standalone before wiring into notify logic — sends whatever the renderer produced to `NOTIFY_TO`, nothing more; confirm a rejected send surfaces as a visible failure
8. Discovery: main searches for the paper's bucket(s) plus one probe query per other bucket, de-duplication against the tracker, re-check of tracked venues, full loop running end to end
9. GitHub Actions: monthly workflow on the 7th + `workflow_dispatch`, commit-back
10. Deadline watcher: daily workflow, week-before (exact and month-only) + Also coming up + closure logic, tested against synthetic near-term deadlines
11. README (incl. authorship disclosure and setup section)
12. Eval sets — classify and extraction — at minimum one worked example each; fuller suite if time allows

If time runs out, stop after whichever numbered item is last solid and demo from there — see Demo tiers for what's allowed to be mocked instead of finished.

## Status

*(Update this section directly as pieces land — don't duplicate it in CLAUDE.md.)*

**Done:**

- Repo created (private), standard Python `.gitignore` (confirmed to cover `.env`), README stub in place (needs alignment — see below)
- Git/Python/Node environment set up and verified
- `src/config.py` — centralizes model choice (Haiku 4.5 for extraction, Sonnet 5 for judging) and `TRACKER_PATH`
- `requirements.txt` — `anthropic`, `resend`, `python-dotenv`, `pydantic`, `requests`
- `src/llm.py` — the single `call_llm()` wrapper every model call routes through; logs input/output token counts per call
- `.env.example` with `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `NOTIFY_TO`; `decisions.json` as `{}`
- Build step 1: schemas (`src/tracker_schema.py`, `src/profile_schema.py`, `src/decisions_schema.py`), `extra="forbid"`, with `tests/test_schema.py` (stdlib `unittest`) passing
- Build step 2: `src/extract.py` — fetch, HTML-to-text, Haiku extraction into `ExtractionFacts`, today's date passed in, logs to gitignored `logs/`; verified against the ECI 2027 page
- Build step 3: `src/classify.py` — paper and venue classify on Sonnet 5; both demo profiles in `profiles/`; `preferred_format` excluded from classify; list-coercion safety net logs a warning when it fires
- Schema v5: one fit shape (`theme_fit`, `genre_fit`, `format_fit`) replacing the per-bucket models, Other Avenues keeping `theme_fit` + `no_other_fit_reasoning`; `access` (open / invite_only / members_only, nullable) added to `ExtractionFacts` and to the extraction prompt/tool schema; `tier_alert` added to `NotificationReason` and `TIER_ALERT_THEME_MIN = 75` added to `config.py`; `preferred_format` confirmed absent from the profile schema and both profile files; `tests/test_schema.py` at 46 tests, all passing; re-verified against the ECI 2027 page live (`access` correctly comes back null with `missing_fields: ["access", ...]`)
- Extraction tweak: a page publishing an open call inviting submissions (call for abstracts/papers/speakers, or similarly public submission instructions) now counts as stating `access: open`, even without the word "open" itself — the one deliberate exception to "never infer" (`src/extract.py`, prompt + tool schema). Null stays reserved for pages that give no indication either way. Re-verified live against the ECI 2027 page: `access` now comes back `"open"`, no longer null/`missing_fields`.
- Build step 4: `src/judge_fit.py` — judge-fit on Sonnet 5, one call per (profile, bucket): `theme_fit`/`genre_fit`/`format_fit` for the three standard buckets (bucket-specific lens on `genre_fit`), `theme_fit` + `no_other_fit_reasoning` for Other Avenues. Code-level skip (closed call / passed deadline / `invite_only`) before any model call, logged visibly. Model never asked for `overall` — `TierFitBase` computes it. Validated against `TIER_FIT_MODEL`. `python -m src.judge_fit` runs three live tests against ECI 2027 (real tier via `classify_venue`): immune-tolerance under its real tier scored 84 overall (theme 95 / genre 88 / format 70); elephant-corridors under the same real tier scored 12 (clear mismatch reasoning); immune-tolerance rescored under a deliberately wrong tier (`industry_technical_media`) dropped to 40, with the swing concentrated in `genre_fit` (88→15) and `format_fit` (70→20) while `theme_fit` barely moved (95→85) — the intended effect of one shared dimension shape.

**Not yet done:**

- Everything after step 4: tracker read/write, email renderer and sending, discovery and re-check, GitHub Actions workflows, deadline watcher, README, eval sets

**Repo hygiene to fix, not just build:**

- Resend account not yet created
- README currently references `paper_profile.json`; this brief uses `profiles/<id>.json`. Line these up once `profiles/` exists — the authorship disclosure requirement also belongs here and hasn't landed yet either
