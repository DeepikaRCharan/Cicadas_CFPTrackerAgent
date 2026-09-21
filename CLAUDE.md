# CFP Tracker Agent

A small agent that watches for conference calls for papers (CFPs) that fit a researcher's paper, keeps a structured tracker of them, and emails only when something is worth acting on.

Solo mini-project for UT Dallas MSBA, Special Topics: Agentic AI (Fall 2026). Owner: Deepika Ravi. Course deadline Oct 12; personal target Oct 8.

## How to work with me

- I direct, you build. I must be able to explain every piece to my class, so explain what you write in plain language and prefer readable code over clever code.
- Plan before code. For any new piece, propose the approach in a few lines and wait for my approval.
- Small steps. One working thing at a time, then a commit with a meaningful message (what and why). Never bundle unrelated changes.
- Edit surgically. Do not refactor or reorganize anything I didn't ask about.
- Keep it simple. No speculative features, abstractions or config options.
- If something here conflicts with what I ask in the moment, say so and ask.

## Hard rules

- Raw Anthropic Python SDK only. No agent framework (no LangChain, CrewAI, AutoGen). The agent loop is hand-written and commented so the mechanics are visible. This is a course requirement.
- No MCP in v1.
- Secrets never enter git. Read keys from environment variables: `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `NOTIFY_EMAIL`. Locally they come from `.env` (gitignored); in GitHub Actions from repository secrets. Keep an `.env.example` with names only. Check every diff for keys before committing.
- Model names live in one place, `src/config.py`: Haiku 4.5 (`claude-haiku-4-5-20251001`) for extraction, Sonnet 5 (`claude-sonnet-5`) for fit judgment. Never hardcode a model name elsewhere.
- Every LLM call goes through one small wrapper that logs token usage, so cost stays visible.
- Never write bad data silently. Incomplete or malformed extraction is flagged (`complete: false`, `missing_fields`), not scored. A failed run must fail visibly: non-zero exit and a clear log line.
- Out of scope: registering or paying for anything, a web UI, tracking acceptance or rejection outcomes, more than two profiles.

## What it does

Monthly, for each profile file in `profiles/` (maximum two; the loader refuses more):

1. **Discover**: web search (Anthropic's built-in web search tool) for candidate CFPs matching the profile.
2. **Extract**: per candidate, pull deadline, submission type, format requirements, fees, mode and theme (Haiku, structured output).
3. **Judge fit**: three scores from 0 to 100, each with short reasoning: theme/topic alignment; nature fit (academic vs practitioner venue, against how the paper is framed); logistical feasibility (grounded in the extracted facts). Overall is the simple average.
4. **Update** `tracker.json` and commit it back to the repo.
5. **Notify** by email only if overall is above 50.

A second, daily workflow, the deadline watcher, uses no LLM and no web search. It sends week-before reminders and closes lapsed entries. Both workflows share one `concurrency` group and pull before committing, so they never write `tracker.json` at the same time.

## Data model

A profile file follows the `paper_profile.json` structure: `id`, title, abstract, core claims, keywords/topics, preferred format.

`tracker.json` holds one entry per conference. Conference facts are stored once; fit and the researcher's decision are stored per profile under `matches`.

```json
{
  "schema_version": 1,
  "last_run": "2026-10-01T09:00:00Z",
  "conferences": [
    {
      "id": "example-stem-education-2027",
      "name": "…", "url": "…",
      "first_seen": "…", "last_checked": "…",
      "extraction": {
        "deadline": "2026-12-15", "submission_type": "…",
        "format_requirements": "…", "fees": "…",
        "mode": "in-person | virtual | hybrid", "theme": "…",
        "complete": true, "missing_fields": []
      },
      "matches": {
        "<profile-id>": {
          "fit": {
            "theme_alignment": { "score": 80, "reasoning": "…" },
            "nature_fit": { "score": 70, "reasoning": "…" },
            "logistical_feasibility": { "score": 60, "reasoning": "…" },
            "overall": 70, "scored_at": "…", "profile_hash": "…"
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

**Identity.** `id` is the normalized conference name + year. A found conference counts as already tracked if its id matches or its URL matches. Never match on domain alone. A re-run updates an existing entry and never creates a duplicate or repeats an email already logged in `notifications`.

**Decision** records what the researcher does, not what the conference does.

| Decision | Set by | Meaning | Reminders |
|---|---|---|---|
| `pending` | agent | Found and scored; no response yet | Yes (treated as planning to apply) |
| `applying` | researcher | Plans to apply | Yes |
| `applied` | researcher | Closed | No |
| `skipped` | researcher | Closed: decided not to apply | No |
| `unconfirmed` | agent | Closed: deadline passed, no confirmation | No |

The agent creates entries as `pending` and makes exactly one status change: closing an open entry to `unconfirmed`. It never touches `applying`, `applied`, `skipped` or `notes`.

**Notifications** (each one is logged in `notifications` with date, reason, deadline):

- `new_fit`: first time a match scores above 50. One email per conference per run, listing every profile it fits.
- `deadline_changed`: an open match whose deadline differs from the last one recorded.
- `week_before`: sent once per deadline, seven days out, for open matches that cleared the threshold. A `pending` reminder asks whether I'm applying; an `applying` reminder says one week remains. Both link to editing `tracker.json`.

**Closure.** Two days after a deadline, any still-open match becomes `unconfirmed`, including below-threshold ones that were never emailed. Closed entries are never reopened, re-scored or re-emailed.

**Stale scores.** `profile_hash` fingerprints the profile file at scoring time. Open entries whose stored hash differs from the current profile are re-scored on the next monthly run.

## Layout

```
profiles/            # one JSON per profile, max two
src/                 # config, llm wrapper, extract, judge, tracker, notify, discover, watcher, run
tests/               # plain unit tests on fake data (no LLM calls)
tracker.json
.github/workflows/   # monthly.yml (schedule + workflow_dispatch), watcher.yml (daily)
README.md            # graded: keep current as the build lands, not at the end
.env.example
```

Tracker and watcher logic (id matching, idempotency, reminder dates, closure) get unit tests on hand-made entries, so behavior that depends on dates can be verified without waiting for real ones.

## Build order

1. **Thin slice first (current focus).** One hardcoded CFP (URL or pasted text) → extract → validate → write a tracker entry, idempotent on re-run → send one plain email → run the same path from GitHub Actions with commit-back. Prove the risky integrations before adding anything else.
2. Fit judgment against a hand-written profile.
3. Deadline watcher and its tests.
4. Discovery: web search inside the hand-written loop.
5. Monthly schedule, threshold notification, second profile.
6. README, commit-history check, demo rehearsal.
