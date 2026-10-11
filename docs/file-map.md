# File map

Every file in the repo: what it does, which build step created it, and what kind of file it is. Build steps are the numbered steps in [`project-brief.md`](project-brief.md) ("Build order"); later additions name the change that brought them in.

**Kinds:**

- **Agent logic**: a model reads text and makes a judgment.
- **Code-enforced rules**: plain code that decides, limits, validates or refuses, with no model involved.
- **Display**: output for people (emails, the demo, traces), with no decisions.
- **Tests**: unit tests and their fixtures. None of them call an API.
- **Config, data, docs**: settings, inputs, records and documentation.

**★ On the agent's decision path. Study these most closely.** These files decide what the agent looks at, what it accepts and how it scores. Start with `src/discover.py`.

## ★ The decision path

| File | What it does | Created | Kind |
|---|---|---|---|
| ★ `src/discover.py` | **The agent's core.** A hand-written tool-use loop in which the model searches and reads the web and decides, step by step, what to look at next. It records a verdict on each result and accepts the live calls it finds. The same file holds the code's refusals of bad decisions (pages not read, deadline quotes not on the page, aggregators, blocked domains, already-tracked venues, two refusals per page) and every budget limit, plus the run trace and its display hook. | Step 8; display hook added 8 Oct | Agent logic + code-enforced rules (+ trace display) |
| ★ `src/classify.py` | Reads the paper, and separately each venue's page, and decides which of the four buckets each belongs in, with cues and reasoning. This is one of the two judgments the brief calls the agent's real agency. | Step 3 | Agent logic |
| ★ `src/judge_fit.py` | Scores how well a paper fits a venue on theme, genre and format, in two separate calls so the venue's bucket can only affect genre. Code skips closed, past-deadline and invite-only calls before any model call. | Step 4 | Agent logic (+ the code skip rule) |
| ★ `src/extract.py` | Fetches a venue page, turns it into text, and has the light model pull out the facts (deadline, call status, submission type, theme, location and so on) into a validated shape, never guessing a missing field. | Step 2 | Agent logic |
| ★ `src/run.py` | The monthly and on-demand run: syncs decisions, then for each paper runs discovery, hands each accepted candidate to extract, classify and judge-fit, re-checks tracked venues, and saves after every step. Code rules here: open or closed is decided from the deadline, an unreadable page falls back to the loop's copy, and failures set the exit codes. | Step 8; later fixes; `--demo` wiring added 8 Oct | Code-enforced rules (orchestration) |
| ★ `src/tracker.py` | Reads and writes `tracker.json` and syncs `decisions.json` into it. Code rules: venue identity (merging, aliases, conflicts), which notifications get logged, call status decided from the deadline, and what counts as open or reminder-eligible. | Step 5; later fixes | Code-enforced rules |
| ★ `src/config.py` | One place for every constant the agent obeys: models, thresholds (50% fit, Tier Alert 75), search, fetch, turn and cost limits, the probe reserve, the search vocabulary per bucket, and the aggregator and blocked domains. | Before step 1 | Config (sets the rules' numbers) |
| ★ `src/llm.py` | The one wrapper every model call goes through. It creates the API client on first use, logs token usage, and keeps the running cost meter. | Before step 1 | Code (model calls and cost) |

## Rules, schemas and the deadline watcher

| File | What it does | Created | Kind |
|---|---|---|---|
| `src/tracker_schema.py` | The shape of `tracker.json`, with the invariants the brief states (deadline vs rolling, equal-weighted overall score, decision ownership, matching keys). Anything that breaks them is refused before it's written. | Step 1 | Code-enforced rules (validation) |
| `src/profile_schema.py` | The shape of a paper profile, its content hash (how a revision is detected), and loading at most two live profiles. | Step 1 | Code-enforced rules (validation) |
| `src/decisions_schema.py` | The shape of `decisions.json`: only `applying`, `applied` or `skipped`, keyed by conference ID. | Schema v4 (before step 2) | Code-enforced rules (validation) |
| `src/watch.py` | The daily deadline watcher, with no model calls. It syncs decisions, closes calls two days after their deadline, and sends each week-before reminder once. | Step 10 (built with step 9) | Code-enforced rules |

## Display and delivery

| File | What it does | Created | Kind |
|---|---|---|---|
| `src/render.py` | Turns tracker fields into the email text: the monthly digest (first-run snapshot, New, Tier Alert, Updates, Still open, Needs a look, Run problems) and the week-before reminder. It's a fixed template, and the model never writes emails. | Step 6 | Display |
| `src/notify.py` | Sends a rendered email through Resend to `NOTIFY_TO`, and marks items reported only after the send succeeds. | Step 7 | Display (delivery) |
| `src/demo.py` | The class demo. It streams a run in four sections with a running counter of steps, tokens and dollars, then a results table, and saves a Markdown backup. It can also replay saved traces. No model calls, no tracker writes. | Added 8 Oct | Display |
| `src/scratch.py` | Makes a scratch copy of the tracker (or an empty one) so local runs and demos never touch the real `tracker.json`. | Step 9 | Display tooling |

## Automation

| File | What it does | Created | Kind |
|---|---|---|---|
| `.github/workflows/monthly.yml` | The monthly GitHub Actions run on the 7th (also runnable by hand). It runs tests, checks secrets, runs discovery, sends the digest, commits the tracker and traces, and fails the job visibly on any problem. | Step 9 | Config (automation) |
| `.github/workflows/daily-watcher.yml` | The daily GitHub Actions run of the deadline watcher, with no Anthropic key in the job. It commits the tracker only when something changed. | Step 9 | Config (automation) |

## Data, settings and docs

| File | What it does | Created | Kind |
|---|---|---|---|
| `tracker.json` | The agent's memory: every venue found, its facts, buckets, scores, notifications and decisions. Written only by the agent, and committed after each run. | Step 9 (empty file); filled by runs | Data |
| `decisions.json` | The researcher's file: one line per conference saying `applying`, `applied` or `skipped`. The agent reads it and never writes it. | Schema v4 (before step 2) | Data |
| `profiles/immune-tolerance.json` | Live paper profile 1: the immune-tolerance (Foxp3) paper, an unambiguous academic case. | Step 3 | Data |
| `profiles/ai-tutoring.json` | Live paper profile 2: Kestin et al. (*Scientific Reports*, 2025), the AI-tutoring RCT, used for the rehearsal run and the live demo. | Added 9 Oct | Data |
| `profiles_retired/elephant-corridors.json` | Retired profile: the elephant-corridors piece (Gemini-generated), the intended ambiguous case. The loader doesn't read this folder; the profile's tracker entries stay frozen as history. | Step 3; retired 11 Oct | Data |
| `requirements.txt` | The Python packages the agent needs. `rich` was added for the demo. | Before step 1 | Config |
| `.env.example` | Template for the three secrets (`ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `NOTIFY_TO`). The real `.env` is gitignored. | Step 1 | Config |
| `.gitignore` | Keeps secrets, logs, scratch copies and demo backups out of git. | Initial commit | Config |
| `CLAUDE.md` | Short orientation for Claude Code: the non-negotiables, pointing to the brief. | Before step 1 | Docs |
| `docs/project-brief.md` | The spec: what Cicadas does, the data model, every settled rule, the build order and the Status log. | Before step 1 | Docs |
| `docs/file-map.md` | This file. | Added 8 Oct | Docs |
| `README.md` | What Cicadas is and how it works, build vs. adopt, repo map, setup and running, evidence, guardrails and cost, authorship disclosure, limitations and next steps. | Initial commit; rewritten in step 11 | Docs |
| `examples/README.md` | A short note on the two example files and the paper behind the `ai-tutoring` profile. | Step 11 | Docs |
| `examples/rehearsal-run.md` | The demo output of the 2026-10-09 rehearsal run (`ai-tutoring`, scratch tracker). | Added 9 Oct | Display (record of an agent run) |
| `examples/rehearsal-trace.md` | The same run's full discovery trace. | Step 11 | Display (record of an agent run) |

## Run traces (`traces/`)

Each file is the readable record of one paper's discovery run. It covers every search, fetch and decision in order, the reasons the model gave, the code's replies, the accepted venues' facts and scores, and the cost. They're written by `src/run.py` and committed for use in class. **Kind: display (records of agent runs).**

| File | Run |
|---|---|
| `traces/20261002T003113Z_immune-tolerance.md` | Step 8 development run 1 (Sonnet 5, first design) |
| `traces/20261002T004501Z_immune-tolerance.md` | Step 8 development run 2 (caching, lower caps) |
| `traces/20261002T011211Z_immune-tolerance.md` | Step 8 development run 3 (Haiku 4.5) |
| `traces/20261002T041527Z_immune-tolerance.md` | Step 8 development run 4 |
| `traces/20261002T143334Z_immune-tolerance.md` | Step 8 development run 5 (two phases, readable-fetch rule) |
| `traces/20261002T151017Z_immune-tolerance.md` | Step 8 development run 6 (deadline quote check) |
| `traces/20261002T163607Z_immune-tolerance.md` | Step 8 development run 7 (cost projection) |
| `traces/20261002T164544Z_elephant-corridors.md` | Step 8 final verification run |
| `traces/20261002T164544Z_immune-tolerance.md` | Step 8 final verification run |
| `traces/20261007T043316Z_elephant-corridors.md` | First monthly run (7 Oct), the one that exposed the late-result bug |
| `traces/20261007T043316Z_immune-tolerance.md` | First monthly run (7 Oct) |
| `traces/20261007T045056Z_elephant-corridors.md` | Second monthly run (7 Oct), after the first round of fixes |
| `traces/20261007T045056Z_immune-tolerance.md` | Second monthly run (7 Oct) |
| `traces/20261007T054110Z_elephant-corridors.md` | Third monthly run (7 Oct): three venues accepted, before discovery was frozen |
| `traces/20261007T054110Z_immune-tolerance.md` | Third monthly run (7 Oct) |

## Tests (`tests/`)

All tests use scripted or saved inputs, and none call an API. Run them with `python -m unittest discover -s tests`. **Kind: tests.**

| File | What it checks | Created |
|---|---|---|
| `tests/test_discover.py` | The discovery loop against scripted model responses: phases, budgets, the probe reserve, every refusal rule, quote matching, results arriving with the next response, and the trace layout. | Step 8 |
| `tests/test_run.py` | The hand-off and re-check with stand-ins for extract, classify and judge-fit: exit codes, call status by code, the unreadable-page fallback, aliases, and trace details. | Step 8 |
| `tests/test_tracker.py` | Tracker reads and writes, decisions sync, identity (merges, aliases, conflicts), call status by code, and that `tracker.json` is never corrupted. | Step 5 |
| `tests/test_schema.py` | The schema invariants in `src/tracker_schema.py` and the profile schema. | Step 1 |
| `tests/test_render.py` | The email templates: section order, wording for missing fields, selection rules, Run problems. | Step 6 |
| `tests/test_notify.py` | Email sending with Resend mocked: a failed send marks nothing, and the command exits non-zero. | Step 7 |
| `tests/test_watch.py` | The deadline watcher: closure dates, reminder windows (exact and month-only), sent once, retried after a failed send. | Step 10 |
| `tests/test_scratch.py` | The scratch tracker copy never touches the real tracker. | Step 9 |
| `tests/test_display_hook.py` | **Proves the display hook is display-only**: with no listener (or with any listener) a scripted discovery run reproduces the pre-hook golden copy byte for byte. | Added 8 Oct |
| `tests/test_demo.py` | The demo: replayed traces give the live hook's events, section order, the results table and the 50% rule, the running counter, and `--demo` end to end. | Added 8 Oct |
| `tests/render_fixtures.py` | Builds a realistic tracker (three monthly runs plus a reminder) through the real tracker code, for the render, notify, watch and demo tests. | Step 6 |
| `tests/discovery_scenario.py` | A scripted discovery run that covers every trace path, used by the display-hook and demo tests. | Added 8 Oct |
| `tests/fixtures/discovery_golden.json` | That scenario's full output, recorded from the code before the display hook existed (commit 3eda423). | Added 8 Oct |
| `tests/fixtures/ser2027_call_page.txt` | SER2027's call page as the hand-off fetched it on 7 Oct, for the deadline-quote tests. | Step 9 follow-up |
| `tests/fixtures/ser2027_loop_fetch.txt` | The same page as the search loop's fetch saw it, with the link in the middle of the deadline sentence that caused four refusals. | Step 9 follow-up |
| `tests/fixtures/springer_js_wall.txt` | The 286-character "JavaScript is disabled" page the hand-off got from Springer. | Step 9 follow-up |
| `tests/fixtures/springer_loop_fetch.txt` | The search loop's readable copy of the Springer collections listing (15 collections). | Step 9 follow-up |
