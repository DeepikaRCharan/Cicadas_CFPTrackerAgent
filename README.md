# Cicadas

A mini-project deliverable for BUAN 6V99.

## What Cicadas is

Researchers find conference and publication calls by hand. They search, check the same sites again a few weeks later, and sometimes miss a deadline that was posted after they last looked. Cicadas does that search for them. On the 7th of every month it searches the web for calls that fit a given paper, checks each deadline against the venue's own page, and scores the fit. It emails a digest without being asked, and a daily watcher sends a reminder a week before each deadline that matters.

Cicadas tracks venues and notifies. It never registers or pays for anything; the researcher does that.

## How it works

Cicadas runs two ways:

- **On a schedule.** GitHub Actions starts the monthly run on the 7th of each month, and a daily deadline watcher every day to send reminders and close calls whose deadlines have passed.
- **On demand.** The same run can be started any time from the Actions tab, or locally with `python -m src.run` (see Setup and running). It's the same code path as the scheduled run.

A monthly run ([`src/run.py`](src/run.py)) does these steps in this order:

1. **Sync decisions.** Read `decisions.json` and copy the researcher's decisions into `tracker.json`.
2. **Read the paper.** Classify the paper into one of four venue buckets, with cues and reasoning. This runs once per profile and is cached; it runs again only if the profile is revised.
3. **Discover.** Run the agent loop that searches and reads the web and accepts up to two live calls per paper ([`src/discover.py`](src/discover.py)).
4. **Extract.** For each accepted call, fetch the page and pull out the facts: deadline, call status, submission type, theme, location, format, fees and mode ([`src/extract.py`](src/extract.py)). Code, not the model, then decides from the deadline and today's date whether the call is open or closed.
5. **Classify the venue.** Decide which bucket or buckets the venue belongs to, with reasoning ([`src/classify.py`](src/classify.py)).
6. **Judge fit.** Score theme, genre and format fit, each 0–100 with written reasoning ([`src/judge_fit.py`](src/judge_fit.py)). Code skips this for calls that are closed, past their deadline, invite-only or missing a critical fact.
7. **Merge.** Write the venue, facts and scores into `tracker.json`, saving after every venue ([`src/tracker.py`](src/tracker.py)).
8. **Re-check.** Re-fetch and re-extract every venue already tracked as open, and log any deadline change or newly opened call.
9. **Trace.** Write a readable record of every search, fetch, decision and score to `traces/`.
10. **Notify.** Render the digest from tracker fields with a fixed template and send it ([`src/render.py`](src/render.py), [`src/notify.py`](src/notify.py)).

**The order is fixed on purpose.** It's a guardrail. Code sequences the pipeline, so the model can't skip verification, score a venue it never extracted, or write to the tracker on its own. The agency sits inside that frame, in discovery. There, a hand-written tool loop gives Claude four web and decision tools: `web_search`, `web_fetch`, `record_result` (a verdict on each result) and `accept_candidate`. Claude picks each next action based on what it just found: refine a query, fetch a promising page, reject a stale or off-topic result, or accept a live call. Discovery runs in two phases, also sequenced by code:

1. **Probe phase.** One search into each bucket the paper is *not* in: three probes for a paper in one bucket. The classification is a guess made from the paper alone, so the probes let real venues test it. A live call found here can be accepted.
2. **Main phase.** Searches in the paper's own bucket, with whatever budget the probes left.

Both phases share the same 12-turn cap. The probe phase may use at most 4 of those turns, so it can't starve the main searches.

**Scoring is judgment, not thresholds.** Classify and judge-fit read the paper and the venue's page and weigh them against qualitative rubric questions. Does the venue want work on this subject? Does it publish this kind of writing for this kind of reader? Can the paper be submitted in a form it accepts? No dimension is a keyword match or a formula. Code computes only the overall score, as the plain average of the dimensions. Judge-fit uses two separate calls so the venue's bucket can only affect genre fit: the theme and format call is never told the bucket.

**Buckets and rubric.** Every venue is scored on theme, genre and format fit. Theme and format mean the same thing in every bucket; genre fit changes with the bucket ([`src/judge_fit.py`](src/judge_fit.py)):

| Bucket | What genre fit looks at |
|---|---|
| Academic & Practitioner | Research rigor and method: would the venue's reviewers accept the paper's level of evidence? |
| Industry & Technical Media | Voice and register: does the outlet publish writing that sounds like the paper? |
| Corporate & Institutional | Audience: would policymakers or executives find the paper usable? |
| Other Avenues | No genre or format score. Theme fit only, plus a written reason why no other bucket fit. Flagged as low confidence in the digest. |

The overall score is the simple average of a bucket's dimension scores, computed in code ([`src/tracker_schema.py`](src/tracker_schema.py)). The model never produces it.

Discovery and extraction run on Claude Haiku 4.5. Classify and judge-fit run on Claude Sonnet 5 ([`src/config.py`](src/config.py)).

A separate daily watcher ([`src/watch.py`](src/watch.py)) makes no model calls. It syncs decisions, closes calls two days after their deadline, and sends each week-before reminder once.

## Build vs. adopt

**Raw SDK, no framework.** Cicadas uses the Anthropic Python SDK directly, with a hand-written `while` loop over `messages.create` ([`src/discover.py`](src/discover.py), [`src/llm.py`](src/llm.py)). It uses no LangChain, CrewAI or AutoGen. Every model request goes through one wrapper that logs tokens and keeps a running cost estimate. Code checks every decision the model makes before acting on it. A framework would sit between the code and those checks. Here, the rules that matter are plain Python that the unit tests can drive with scripted model responses. Avoiding a framework was also a course requirement.

**An agent, not a script or a chatbot prompt.** A script can't decide which search result deserves a fetch or whether a page is a live call or last year's. A one-off chatbot prompt can, but only when someone asks, and it forgets the answer afterwards. Cicadas has four things neither has:

- **It runs unprompted.** GitHub Actions starts it on the 7th of each month and runs the deadline watcher daily.
- **It remembers across runs.** `tracker.json` holds every venue found, its facts, scores, emails already sent and the researcher's decisions. Later runs re-check tracked venues instead of rediscovering them, and never send the same email twice.
- **It verifies.** The model must quote the deadline wording, and code checks that the quote appears on a page fetched in this run before the venue is accepted.
- **It acts.** It emails a digest and reminders.

## Repo map

| Path | What it is |
|---|---|
| [`src/discover.py`](src/discover.py) | The agent loop: searches, fetches and decides, with the code's refusals of bad decisions and all search-loop budgets. Also writes the run trace. |
| [`src/run.py`](src/run.py) | The monthly and on-demand run: the fixed pipeline above, re-checks, exit codes. |
| [`src/classify.py`](src/classify.py) | Reads the paper, and each venue page, and picks a bucket with cues and reasoning. |
| [`src/judge_fit.py`](src/judge_fit.py) | Scores theme, genre and format fit in two calls; code skips closed, past-deadline and invite-only calls. |
| [`src/extract.py`](src/extract.py) | Fetches a venue page and pulls out its facts into a validated shape; a missing field stays null. |
| [`src/tracker.py`](src/tracker.py) | Reads and writes `tracker.json`, syncs `decisions.json`, venue identity, open/closed by deadline. |
| [`src/tracker_schema.py`](src/tracker_schema.py) | The shape of `tracker.json`; anything invalid is refused before it's written. |
| [`src/profile_schema.py`](src/profile_schema.py) | The shape of a paper profile, its revision hash, and the two-profile limit. |
| [`src/decisions_schema.py`](src/decisions_schema.py) | The shape of `decisions.json`: `applying`, `applied` or `skipped` only. |
| [`src/watch.py`](src/watch.py) | Daily deadline watcher: closure and week-before reminders, no model calls. |
| [`src/render.py`](src/render.py) | Fixed email templates for the digest and reminders. The model never writes emails. |
| [`src/notify.py`](src/notify.py) | Sends a rendered email through Resend; marks items as sent only after Resend accepts it. |
| [`src/llm.py`](src/llm.py) | The one wrapper every model call goes through; logs usage and estimates cost. |
| [`src/config.py`](src/config.py) | Every constant: models, thresholds, search-loop limits, search vocabulary, blocked and aggregator domains. |
| [`src/demo.py`](src/demo.py) | Display only: streams a run in readable sections, or replays saved traces. |
| [`src/scratch.py`](src/scratch.py) | Makes a scratch copy of the tracker so local runs never touch the real one. |
| [`tests/`](tests/) | Unit tests and saved fixtures (real pages Cicadas fetched, a recorded discovery run). None call an API. |
| [`profiles/`](profiles/) | The live paper profiles (two at most): title, abstract, keywords. |
| `profiles_retired/` | A retired profile, kept for reference; the loader doesn't read it. |
| [`tracker.json`](tracker.json) | The agent's memory. Written only by Cicadas; committed after each scheduled run. |
| [`decisions.json`](decisions.json) | The researcher's decisions. Written only by the researcher. |
| [`traces/`](traces/) | Committed traces from development and monthly runs: every search, fetch, decision, score and cost. |
| [`examples/`](examples/) | A full rehearsal run: its demo output and its complete discovery trace (see Evidence). |
| [`docs/project-brief.md`](docs/project-brief.md) | The spec: data model, every rule, build order and a status log of each change and live run. |
| [`docs/file-map.md`](docs/file-map.md) | A longer map of every file, which build step made it, and whether it's agent logic, code rules or display. |
| [`.github/workflows/`](.github/workflows/) | `monthly.yml` (the monthly run) and `daily-watcher.yml` (the deadline watcher). |
| [`CLAUDE.md`](CLAUDE.md) | The non-negotiables, for Claude Code. |
| [`requirements.txt`](requirements.txt), [`.env.example`](.env.example) | Python packages, and a template for the three secrets. |

`logs/`, `scratch/` and `runs/` are gitignored. They hold raw page text and model responses, scratch trackers, and demo backups.

## Setup and running

### Secrets

Cicadas needs three values: `ANTHROPIC_API_KEY`, `RESEND_API_KEY` and `NOTIFY_TO`. Put them in `.env` for local runs (copy `.env.example`; `.env` is gitignored). For scheduled runs, add them under the repo's Settings > Secrets and variables > Actions.

Email goes through Resend's shared `onboarding@resend.dev` sender, which only delivers to the Resend account's own address. So `NOTIFY_TO` must be the email address of your Resend account. A send to any other address fails the run visibly. The daily watcher's job never receives the Anthropic key.

### Your own copy

To run Cicadas for your own papers:

1. Make a Resend account.
2. Set the three secrets, locally and in GitHub.
3. Replace the files in `profiles/` with your own, two at most. Each file needs an `id` that matches its filename, plus a title, abstract and keywords.
4. Reset `tracker.json` to `{"schema_version": 6, "last_run": null, "profiles": {}, "conferences": [], "identity_conflicts": []}` and `decisions.json` to `{}`.
5. Enable Actions. It's off by default on a fork.

Set a monthly spending cap on your own Anthropic Console as well.

```sh
pip install -r requirements.txt
python -m unittest discover -s tests
```

### Scheduled runs

| Workflow | When (UTC) | Central time | What it does |
|---|---|---|---|
| Monthly run | 7th of each month, 14:17 | 9:17 a.m. CDT / 8:17 a.m. CST | Tests, decisions sync, discovery, re-check, digest, commit tracker and traces |
| Daily deadline watcher | Every day, 12:17 | 7:17 a.m. CDT / 6:17 a.m. CST | Tests, decisions sync, closure, week-before reminders, commit tracker |

The digest goes out every month, even when nothing new turned up, so a missing digest is itself a sign the run failed. Both workflows commit `tracker.json` back as `github-actions[bot]` only when something changed. They share a concurrency group, so they never write the tracker at the same time.

**Run on demand:** Actions tab > pick the workflow > *Run workflow*. It's the same code path as the scheduled run.

**Pause and resume:** Actions tab > pick the workflow > *…* > *Disable workflow*. Enable it again to resume on the next scheduled date. Missed monthly runs aren't replayed. When the watcher is re-enabled, overdue closures catch up and any reminder still inside its window goes out late rather than never.

### Running locally

Every command takes `--tracker`, so you can work on a scratch copy and leave the real tracker alone:

```sh
python -m src.scratch --empty            # scratch/tracker.json, starting from nothing
python -m src.run --demo --tracker scratch/tracker.json --traces-dir scratch/traces
python -m src.notify --tracker scratch/tracker.json --dry-run digest
python -m src.watch --tracker scratch/tracker.json --today 2027-03-24 --dry-run
python -m src.demo                       # replay the latest saved traces, no API calls
```

`src.run` calls the API and costs real money. Use `--profile <id>` to run one paper. Without `--dry-run`, `notify` and `watch` really send email.

### Recording decisions

`decisions.json` belongs to the researcher. Cicadas reads it at the start of every monthly run and every daily watcher run, and never writes it. Edit it on GitHub (pencil icon > commit) with one line per conference, keyed by the ID shown in each digest row:

```json
{
  "csedu-2027": "applying",
  "teaching-and-learning-with-ai-2027": "skipped"
}
```

Only `applying`, `applied` and `skipped` are allowed:

- **`applying`:** the venue stays open. Every monthly run re-checks its page, and it gets a week-before reminder and a place in the digest's still-open list whatever its score. The daily watcher won't close it after the deadline; it stays until you change the line.
- **`applied`:** the venue is closed for you. No more re-checks, reminders or still-open listing, and discovery won't pick it up again.
- **`skipped`:** same effect as `applied`: closed, with no re-checks, reminders or still-open listing. The two differ only in the label kept in the tracker, as a record of what you did.
- **Removing a line** puts the venue back to `pending`.
- **Errors:** any other value, or a repeated key, stops the run with an error naming the line.
- **Unknown IDs:** a line whose ID isn't tracked produces a warning, and the line is ignored.
- **No line:** a venue with no line stays `pending`.

## Evidence

**Rehearsal run.** [`examples/rehearsal-run.md`](examples/rehearsal-run.md) is the full output of a live, end-to-end `--demo` run on 2026-10-09 against a scratch tracker. The run used the AI-tutoring profile, built from Kestin et al. (*Scientific Reports*, 2025). [`examples/rehearsal-trace.md`](examples/rehearsal-trace.md) is the same run's complete discovery trace. In order, the demo output shows:

- the paper classified as Academic & Practitioner, with cues
- every search and fetch, with the model's stated reason
- nine results rejected
- two calls accepted, each with its deadline quote verified on the page
- each venue scored

| Venue | Theme | Genre | Format | Overall |
|---|---:|---:|---:|---:|
| CSEDU 2027 | 94 | 85 | 90 | **90** |
| Teaching and Learning with AI 2027 | 62 | 45 | 45 | **51** |

Both venues are about teaching with AI; what separates them is the kind of writing they want. CSEDU accepts complete research papers, and the paper is a randomized controlled trial. Teaching and Learning with AI describes itself as "primarily a conference focused on sharing practices (rather than research)". Its genre score fell to 45, and the 100-word abstract limit pulled format down too. The run took 18 model calls and cost an estimated $0.352 at list prices.

**Tests.** 312 unit tests pass (`python -m unittest discover -s tests`, run for this README). None call an API. Discovery is tested against scripted model responses, and quote matching against real pages saved from earlier runs. Another test proves the demo display hook changes nothing: a recorded discovery run reproduces its pre-hook output byte for byte. Both workflows run the full suite before doing anything else.

**Quote verification.** `accept_candidate` needs a `deadline_quote`: the deadline wording copied from the call page or dates page. Code accepts it only if those words appear, whole and in order, on a page fetched in this run. Before matching, both sides are normalized: markup stripped, quotes and dashes unified, whitespace and case ignored. If the loop's copy of the page was cut short, the code checks a direct fetch of the same page, still word for word. A page refused twice is recorded as refused and not evaluated again, so a stuck quote can't burn the budget.

**Deadline-passed check.** Deadlines are checked three times:

- **Discovery:** `accept_candidate` refuses a call when the latest full date in its verified quote is before today.
- **After extraction:** code decides open or closed from the deadline and the run date. A call is closed only after its deadline day; a month-only deadline runs to the end of the month. This overrides whatever extraction read.
- **Judge-fit:** never runs on a past-deadline call.

The committed `traces/` from the October monthly runs show quote verification on live pages. For example, the code refused SER2027's full-sentence quote and then verified a shorter one ([`traces/20261002T164544Z_elephant-corridors.md`](traces/20261002T164544Z_elephant-corridors.md)). Discovery's deadline-passed refusal was added after those runs, so no committed trace shows it yet; unit tests cover it.

## Guardrails and cost

These are enforced in code:

- **Two live profiles at most.** The loader refuses to run with more ([`src/profile_schema.py`](src/profile_schema.py)).
- **Limits, per paper per run** ([`src/config.py`](src/config.py)):

  | Limit | Value | Probe phase's share |
  |---|---|---|
  | Turns | 12 | up to 4 |
  | Searches | 6 | 3 (one per other bucket); main gets the rest, so up to 3 |
  | Fetches | 4, at most 5,000 tokens each | up to 2 |
  | Search-loop spend | $0.22 estimated; can run one turn over | up to half |
  | Venues accepted | 2 | shared |
  | Live profiles | 2 | — |
  | Total spending | Anthropic Console monthly spending cap: the only hard stop | — |

  The loop stops *before* a turn projected to cross the spend limit. The main phase's first turn always runs, so the loop can overshoot by one turn.
- **Refusals of bad decisions** ([`src/discover.py`](src/discover.py)). Code refuses:
  - `live_cfp` or `accept_candidate` for a page not fetched in this run, or one that came back unreadable (under 300 characters)
  - a deadline quote that isn't on the page, or a deadline that has passed
  - aggregator listings (follow through to the venue's own page)
  - blocked domains: OMICS-group and similar sites, which are also hidden from search and fetch results
  - venues already tracked for this paper
  - an implausible edition year
- **Code decides open or closed** from the deadline, and judge-fit is skipped for closed, past-deadline and invite-only calls.
- **Schema validation and safe writes.** Every tracker write is validated and saved atomically. A corrupt tracker stops the run instead of being overwritten.
- **Venue identity.** Venues are matched on name, year and URL. Uncertain matches are flagged for the researcher, never silently merged.
- **Fixed email templates.** The model fills validated fields; it never writes email text.
- **No silent failures.** A failed candidate, re-check or paper is logged as a warning, a GitHub Actions error annotation and a line in the trace. The digest still goes out with a *Run problems* section, and the job turns red. A run that can't start sends no digest and fails.
- **No retries.** A failed email send marks nothing as sent, and those items go out with the next email.

**Cost.** Nothing in code caps a paper's total cost. The $0.22 limit covers only the search loop. Reading the paper and scoring each accepted venue (extract, classify, two judge-fit calls) have no spending check. A paper typically costs about $0.20–$0.40, depending mostly on how many venues are scored and how long their pages are. That figure is observed in live runs, not enforced. The rehearsal paper cost $0.352. The hard stop on total spending is the monthly spending cap set on the Anthropic Console, at the account level.

## Authorship disclosure

| Profile | What it is | How it's used |
|---|---|---|
| Hori, S., Nomura, T., & Sakaguchi, S. (2003). "Control of regulatory T cell development by the transcription factor Foxp3." *Science*, 299(5609), 1057–1061. [doi:10.1126/science.1079490](https://doi.org/10.1126/science.1079490) (`immune-tolerance`) | Real, published research from the line of work recognized by the 2025 Nobel Prize in Physiology or Medicine. Not my work. | A stress test for classify: an unambiguous academic case. |
| Elephant corridors (*Beyond Island Conservation…*) | Generated by Gemini. | An intended ambiguous case for classify. Now retired to `profiles_retired/`. |
| Kestin et al., "AI tutoring outperforms in-class active learning: an RCT introducing a novel research-based design in an authentic educational setting", *Scientific Reports* (2025) | Real, published research. Not my work. | The rehearsal run and the live demo. |
| See–Map–Speak | My own work. | Referenced here only; not a live profile. |

The code was implemented with Claude Code under my design and direction. Claude Code is the build tool, not part of the running agent. The scheduled runs call the Anthropic API directly.

## Known limitations

- **No formal evaluation yet.** There is no answer-key eval set for classify or extraction, and no repeat-run consistency check. The evidence so far is the rehearsal run, the committed traces and the unit tests.
- **Model judgment varies between runs.** The status log in [`docs/project-brief.md`](docs/project-brief.md) records the discovery model judging the same call differently on different runs.
- **Live web results vary.** The same paper can surface different venues from one run to the next, and publisher sites sometimes block or rate-limit fetches.
- **Two profiles at most.** This is a deliberate cost cap.
- **First two, not best two.** Discovery keeps the first two venues it can verify and then stops. They aren't compared against anything else found that month, so a better-fitting call can be missed.
- **Decisions are edited by hand in JSON.** One decision per conference applies to every profile matched to it.
- **Listing pages.** Discovery can accept a listing page (a journal's collections page, say) instead of a call's own page. One URL then carries several deadlines.
- **Rolling calls.** Calls with no deadline are never closed automatically.

## Next steps

- **Rebuild discovery to keep the best two.** Scan widely each month, then keep the top two candidates, instead of stopping at the first two verified.
- **A dashboard** listing tracked venues, with a decision dropdown per venue to replace hand-editing `decisions.json`.
- **A formal eval set.** A small answer-key set for classify and extraction, plus repeat-run checks for stability.
- **Learning from decisions, only with permission.** Cicadas could notice patterns in what gets skipped (every European venue, say) and *propose* a scoring change. It would always ask before changing how anything is scored, never adjust silently.
