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
  "schema_version": 6,
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
            { "date": "2026-10-01", "reason": "new_fit", "deadline": "2026-12-15",
              "run_at": "2026-10-01T09:00:00Z", "reported": false }
          ],
          "notes": ""
        }
      }
    }
  ],
  "identity_conflicts": []
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
- A value outside the three allowed (or a key repeated twice) stops the run with a visible error naming the bad line. Never guessed, never skipped.
- A key that isn't a tracked conference `id` is a **WARNING in the run log** naming that line (stderr, and a GitHub Actions annotation when running there) — not a debug line — and the run continues; the line is ignored, never guessed at.
- Decisions are copied only into **live** profiles' matches. A swapped-out profile's matches are frozen history and are not touched.
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

- **Identity.** `id` = normalized name + year; the URL (normalized, full path and query) is a secondary check. A found conference is merged into an existing entry only when its `id` **and** URL both agree. Same `id` but a different URL, or the same URL and year under a different name, is **flagged** in `identity_conflicts` and logged as a warning — never merged, never created as a second entry. Same URL with a different year is a separate venue (sites reuse one CFP page every year). Domain alone is never enough (hosts like EasyChair carry many conferences) — merging two real conferences silently is worse than an occasional duplicate.
- **Ownership.** Agent creates entries (`pending`), writes `extraction`/`matches`/`notifications`, and makes exactly one status change itself: closing to `unconfirmed`. The researcher sets `applying`/`applied`/`skipped` via `decisions.json` only; the agent copies them in and never infers them. `notes` stays in the schema but is unused in v1.
- **Idempotency.** A re-run updates an existing entry, never duplicates one, and never repeats an email already logged in `notifications`.
- **Reported vs. logged.** Every `notifications` entry starts `reported: false`. The digest selects entries where `reported` is false, and the flag flips to true only after a successful send — so if the email fails after the tracker merge, those entries go out with the next email instead of being lost. `run_at` (the run that logged the entry) is kept as history only, never used to select digest content.
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
5. **Judge fit** — skipped entirely if the call is closed, the deadline has passed, or access is invite-only (a code check). Otherwise, two calls, deliberately kept separate so a wrong venue tier can only ever move `genre_fit`:
   - **Tier-blind call** (once per venue, however many buckets it's assigned) — scores `theme_fit` and `format_fit`. It receives the profile and the venue's page text/facts and nothing about `venue_tier` at all: no bucket name, no lens, not even the list of buckets assigned. Neither dimension can be swayed by which bucket classify picked, because the call has no way to know it.
   - **Tier-aware call** (once per bucket in the venue's `venue_tier`) — scores `genre_fit` only, under that bucket's lens. Other Avenues has no `genre_fit`; its tier-aware call produces `no_other_fit_reasoning` instead.
   
   Code assembles each bucket's fit entry from the one shared `theme_fit`/`format_fit` plus that bucket's own `genre_fit` (or `no_other_fit_reasoning`), then computes `overall` as the simple average — never asked of the model. Same `TierFit`/`StandardFit`/`OtherAvenuesFit` shapes as always; this only changes how the calls that fill them are split.
   
   **Why the split:** the original single-call design (score all three dimensions in one call, told which bucket) worked most of the time, but testing caught it leaking the bucket label into `format_fit`'s reasoning — a deliberately wrong tier moved `format_fit`'s score too, not just `genre_fit`'s, even though format is pure shape/length and has nothing to do with venue type. Splitting into a tier-blind call and a tier-aware call makes that leak structurally impossible instead of trusting prompt wording to keep the model in its lane — it can't leak tier into a dimension it's never told the tier for.
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
- Build step 8 (built; verification incomplete -- see Known limitation below): `src/discover.py` + `src/run.py`. Discovery is a hand-written tool-use loop on the raw SDK (via `call_llm`, which now passes extra parameters through, logs web searches/fetches and cache tokens, and keeps per-model usage totals with an estimated cost). Server tools: Anthropic's `web_search`/`web_fetch`; versions per loop model in `config.WEB_TOOL_TYPES` (`_20260209` with dynamic filtering on Sonnet 5, the default `MODEL_DISCOVERY`; basic `web_search_20250305`/`web_fetch_20250910` on Haiku 4.5, selectable with `--discovery-model`, verified working live). Decision tools, each logged with a reason: `check_tracker`, `record_result` (live_cfp / stale / aggregator / not_a_call / irrelevant), `accept_candidate`, `finish`. Code-enforced rules: `live_cfp` and `accept_candidate` require a page fetched in this run that came back readable (at least 300 characters of text; a JavaScript shell or bot wall doesn't count, and the trace marks it UNREADABLE); a dates-page deadline requires the dates page read too (the read-page check ignores URL case; tracker identity does not); `accept_candidate` requires a `deadline_quote` -- the deadline wording (or the rolling or call-is-open wording) copied from the call or dates page -- which the code checks appears in that page's fetched text, ignoring whitespace and case; aggregator URLs (`AGGREGATOR_DOMAINS`) must be followed through to the venue's own page; `BLOCKED_DOMAINS` (OMICS-group: conferenceseries.com and others) are refused outright and also passed to both web tools as `blocked_domains`; already-tracked venues are refused; the edition year must be a real year (rule: edition year; for a one-off call such as a special issue, its deadline's year; null only for rolling). Two phases, each its own conversation, sequenced by code: (1) probes -- exactly one search per other venue type, pushed back once if it tries to finish with a probe not run; (2) main searches with the remaining budget and a code-written summary of phase 1. Seed queries come from the profile's first two keywords plus `BUCKET_VOCABULARY`, with no year (the model adds one when it refines). Hard limits per profile per run (`config.py`, sized for a $0.25-per-profile ceiling): 12 turns, 5 searches (2 main + 3 probes), 3 fetches of at most 5k tokens, 2 candidates, $0.18 loop spend (checked before each turn against spend so far plus the next turn's cost projected from the budget actually left: the searches and fetches it could still make, capped at the most one turn has been seen to make -- 3 and 2 -- one cached re-read of the conversation per server pass, their results written to the cache, a turn's output, and search fees; no searches left means no search cost in the projection). `max_uses` is set to uses billed in the current exchange plus budget left, because the API counts it across the whole exchange (every turn since the last plain user message), not per request; a spent tool stays listed so the prompt cache survives; budgets count only what the API billed (a refused search is not a use, and doesn't count as a probe). Automatic prompt caching on every loop request. Hand-off, in code, per accepted candidate: fetch (call page + dates page; if a site blocks the direct fetch, the loop's readable copy is used) -> extract (Haiku) -> classify the venue (skipped for a venue already tracked under another paper) -> judge fit when complete -> step-5 merge with the year passed explicitly; saved after every candidate. If discovery found no deadline but extraction calls the call rolling, the venue is not tracked and a WARNING says to check it by hand. Re-check: every open tracked venue is re-fetched and re-extracted; re-judged only when it was incomplete and is now complete, or the profile was revised. `ConferenceEntry` gains `dates_url` (optional, v6-compatible). One failed candidate is a visible WARNING and the run continues. Every run writes a Markdown trace to `traces/` (committed for use in class): searches and results, fetches (readable or not), every decision with its reason and the code's reply, limit stops, hand-off outcomes, re-checks, and cost. `python -m src.run [--tracker] [--profile] [--discovery-model] [--max-*]`. Tests: `tests/test_discover.py` (36, scripted responses) and `tests/test_run.py` (12, fake extract/classify/judge); 161 tests passing in all, no API calls. Live runs, all on immune-tolerance against temp tracker copies (traces in `traces/`, oldest first): (1) Sonnet 5, first design -- $1.33, found the Frontiers 'Tregs as medicine' Research Topic (fit 73), but resent context made it 4x the estimate; (2) Sonnet 5 with caching and lower caps -- $0.19, but stopped after two turns and accepted a probably-closed Research Topic page as 'rolling'; (3) Haiku 4.5 -- $0.20, found Keystone 'T Cells' 2027 (fit 87) but marked live from snippets and ran only one probe; (4) Haiku 4.5 -- $0.23, exposed the `max_uses` exchange semantics, cache breaks from removing spent tools, and an MDPI page that was unreadable to the loop and 403 to the hand-off; (5) Haiku 4.5 with the phase split, readable-fetch rule, blocklist and cost projection -- $0.125; (6) Haiku 4.5 adding the case-insensitive read check, `deadline_quote` and the $0.18 limit -- $0.137; (7) Haiku 4.5 with the cost projection from the budget left -- $0.148.
  - **Verification against four checks, runs 5-7:** (1) fetches before calling something live_cfp -- pass in all three; (2) rejects stale pages -- pass in all three (661/662-day-old webinars, a 2018 congress abstract; published articles correctly rejected as not calls); (3) all three probes as separate searches -- pass in all three; (4) accepts only venues with real deadline evidence or a genuinely rolling call -- **not met in any**. Haiku stays an option (`--discovery-model`); the default loop model is still Sonnet 5.
  - **Known limitation (check 4):** neither verification run accepted a venue, so the deadline-evidence rule has not been shown to accept a good one. Run 5: the one attempted accept (Keystone 'T Cells' 2027) cited a dates page the model itself said hadn't loaded, and was refused on a URL case mismatch (now fixed: the read-page check ignores case). Run 6: the loop reached a live call (The Journal of Immunology 'Highlights of IMMUNOLOGY2026', deadline 31 December 2026 -- open only to IMMUNOLOGY2026 presenters, which run 4 had correctly declined) but was stopped by the projected cost check (spent $0.130 + largest turn $0.064 > $0.18) before calling accept_candidate, so the deadline_quote check never ran live. The projection is conservative: it assumes the next turn could cost as much as the largest so far, even when no searches are left and only cheap decision calls remain. Both-profile runs at the new defaults are not done. Run 7, after replacing the largest-turn guess with the budget-left projection (replaying run 6's numbers, the old check would now have let it continue: $0.130 spent + $0.019 projected < $0.18): the loop ran to the model's own finish with the cost check never firing, but accepted nothing -- the two main searches were the bare seeds and returned only published articles, and of 3 fetches one went on a page the snippet had already ruled out and one failed (url_not_in_prior_context). The binding constraint is now the search/fetch budget and seed quality, not cost.
- Build step 7: `src/notify.py` — sends one rendered email through Resend to `NOTIFY_TO` (from `RESEND_FROM` in `config.py`, Resend's shared onboarding sender), reading `RESEND_API_KEY`/`NOTIFY_TO` from the environment (`.env` locally). Only after Resend accepts the send (an email id comes back) is the renderer's receipt applied with `tracker.mark_sent` and `tracker.json` saved. Any failure — missing key or address, malformed address, API/network error, a response with no id — prints `[notify] FAILED: …` (plus a GitHub Actions `::error::` annotation there), exits 1, marks nothing, and leaves `tracker.json` byte-for-byte unchanged; a save failure after a successful send says the email WAS sent and that its items will go out again. No retries. CLI: `python -m src.notify [--tracker PATH] [--dry-run] digest | reminder <id>`; `--dry-run` prints the email and what it would mark. `load_live_profiles` (max two, id must match filename) added to `src/profile_schema.py`. Renderer fix: the shared snapshot explanation now prints once after all FIRST LOOK blocks; each block keeps its own "Main search" line. `tests/test_notify.py` (14 tests, Resend always mocked): success marks the receipt, failed send marks nothing and leaves the file unchanged, missing key fails before any send, plus CLI exit codes and dry run. All 113 tests passing. Before the keys existed, the real attempt failed visibly (exit 1) on the missing keys before any send. **Real send done (2026-10-01):** the first-run fixture digest, from a temp copy of the fixture tracker (never the real `tracker.json`), was accepted by Resend (id `01a0f997-e671-7950-bc1f-f350a607eac2`) and delivered to `NOTIFY_TO`; the copy then showed 0 unreported notifications and both profiles' `snapshot_sent` stamped, and a re-render of it came out as the quiet-month digest.
- Build step 6: `src/render.py` — pure functions from tracker data to plain-text email (no sending, no LLM, no file or clock access; `today` and the live profiles are passed in). `render_digest`: first-run snapshot (live profiles with `snapshot_sent` null), then New, Tier Alert, Updates, Still open, and a new "Needs a look" section listing identity conflicts — every section always present, "None this month." when empty, and a quiet month opens with "Nothing new this month" and still lists Still open. Selects only unreported notifications (`week_before` entries are left for the reminder). `render_week_before`: one conference plus Also coming up (other reminder-eligible deadlines in the next 14 days); month-only deadlines labelled "exact date not published". Null fields render as "Not stated on venue page", rolling calls as "Rolling"; the only logic is the selection/ordering the brief specifies (Still open rule and deadline sort, lowest stored dimension for "What pulled it down"). Each render returns a `ReportReceipt` (notifications, conflicts, snapshot profile ids); `tracker.mark_sent` applies it after a successful send (wired in step 7). Schema: `IdentityConflict` gains `reported` (default false, so each conflict appears once; still v6, since no v6 tracker.json had been written). `above_new_fit` / `is_reminder_eligible` added to `src/tracker.py` for the daily watcher to reuse. `tests/render_fixtures.py` builds three monthly runs (first run, update + Tier Alert + conflict, quiet month) and a week-before reminder through the real tracker functions; `python -m tests.render_fixtures` prints every email. `tests/test_render.py` (22 tests) covers section order, null/rolling wording, selection rules, the receipt, a never-sent digest re-rendering unchanged, and purity. All 99 tests passing.
- Build step 5: `src/tracker.py` — tracker read/write and the `decisions.json` sync, no LLM calls. Schema v6: `NotificationEntry` gains `run_at` (history) and `reported` (default false; the digest's selector, flipped only after a successful send via `mark_reported`, wired in step 7), `TrackerFile` gains `identity_conflicts`, and duplicate conference ids are rejected. `NEW_FIT_MIN = 50` and `DECISIONS_PATH` added to `config.py`. Identity: id = normalized name + year, merged only when id and normalized URL agree; disagreements flagged, not merged (brief Identity rule updated). `decisions.json`: bad value or duplicate key stops the run naming the line; an untracked id is a run-log WARNING and the run continues (brief rule updated); a deleted line reverts a researcher-set decision to `pending`; frozen profiles' matches are skipped. `new_fit`/`tier_alert` logged at most once per match from stored scores; `submissions_opened`/`deadline_changed` logged on re-check only for matches already sent. `save_tracker` re-validates the whole file and writes atomically (temp file + `os.replace`); `tracker.json` is untouched on any failure; a corrupt `tracker.json` stops the run rather than being overwritten. `tests/test_tracker.py` (27 tests, temp files, no API calls) covers: idempotent second run, same name/different year, same name+year/different URL flagged, `decisions.json` edits (set, change, delete) on the next run, unknown id warning, invalid entry and failed replace leaving `tracker.json` byte-identical, profile swap keeping old entries frozen, and entries staying unreported when the send never happens. `tests/test_schema.py` at 50 tests. All 77 passing.
- Build step 4: `src/judge_fit.py` — judge-fit on Sonnet 5. First version made one call per (profile, bucket) scoring all three dimensions together; testing showed a deliberately wrong tier moved `format_fit` as well as `genre_fit`, so it was restructured into two calls (see brief, Workflow > step 5): a tier-blind call (once per venue) scoring `theme_fit`/`format_fit` with no `venue_tier` information at all, and a tier-aware call (once per bucket) scoring `genre_fit` only (or `no_other_fit_reasoning` for Other Avenues) under that bucket's lens; code assembles the two into each bucket's fit entry. Tool schemas use flat `<dimension>_score`/`<dimension>_reasoning` fields rather than a nested object per dimension — nested objects were occasionally malformed by the model (a JSON object collapsed into a raw string), flat fields fixed it in testing. Code-level skip (closed call / passed deadline / `invite_only`) before any model call, logged visibly. Model never asked for `overall` — `TierFitBase` computes it. Validated against `TIER_FIT_MODEL`; no schema change. `python -m src.judge_fit` runs three live tests against ECI 2027 (real tier via `classify_venue`, sharing one tier-blind call between tests 1 and 3 to isolate the comparison): immune-tolerance under its real tier scored 82 overall (theme 96 / genre 85 / format 65); elephant-corridors under the same real tier scored 11 (clear mismatch reasoning); immune-tolerance rescored under a deliberately wrong tier (`industry_technical_media`) dropped to 59, with `theme_fit` and `format_fit` unchanged (96, 65 — same tier-blind call) and the entire swing confined to `genre_fit` (85→15) as designed.

**Not yet done:**

- Step 8 verification: check 4 above, then both profiles at the new defaults; everything after step 8: GitHub Actions workflows, deadline watcher, README, eval sets

**Repo hygiene to fix, not just build:**

- README currently references `paper_profile.json`; this brief uses `profiles/<id>.json`. Line these up once `profiles/` exists — the authorship disclosure requirement also belongs here and hasn't landed yet either
