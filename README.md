# AgenticAI_CFPTracker

An agent that scans for conference call-for-papers, judges fit against a
paper's abstract, and emails me proactively when something's worth
applying to - rather than requiring me to go looking myself.

The paper it evaluates against is configurable (`paper_profile.json`).
Currently set up to track fit for my See–Map–Speak white paper.

Status: in progress — mini-project deliverable for BUAN 6V99

## Scheduled runs (GitHub Actions)

| Workflow | When (UTC) | Central time | What it does |
|---|---|---|---|
| `Monthly run` | 7th, 14:17 | 9:17 a.m. CDT / 8:17 a.m. CST | discovery, re-check of tracked venues, digest email |
| `Daily deadline watcher` | daily, 12:17 | 7:17 a.m. CDT / 6:17 a.m. CST | closure, week-before reminders (no LLM) |

Both commit `tracker.json` back (the monthly run also its `traces/`) and
make no commit when nothing changed. **Run now:** Actions tab -> pick the
workflow -> *Run workflow*. **Pause:** Actions tab -> pick the workflow ->
*...* -> *Disable workflow*; enable it again to resume on the next
scheduled date.

Repository secrets (Settings -> Secrets and variables -> Actions):
`ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `NOTIFY_TO` (your Resend account's
email address).

A failed run turns red and GitHub emails you. If only some venues failed,
the digest still arrives, with a *Run problems* section at the bottom; if
the run crashed, no digest is sent.

## Run locally against a scratch tracker

Everything takes `--tracker`, so you can run against a copy and leave the
real `tracker.json` alone. `scratch/` is gitignored. Put the three secrets
in `.env` first (see `.env.example`).

```sh
python -m src.scratch                      # copy tracker.json -> scratch/tracker.json, first-run snapshot pending again
python -m src.notify --tracker scratch/tracker.json --dry-run digest   # print the digest, FIRST LOOK snapshot on top
```

That shows the real first-run snapshot (real model output from a real run)
again, instantly and at no API cost. For a live, unscripted run instead:

```sh
python -m src.scratch --empty              # start from nothing: the run classifies the papers live
python -m src.run --tracker scratch/tracker.json --traces-dir scratch/traces --profile immune-tolerance
python -m src.notify --tracker scratch/tracker.json --dry-run digest
```

The deadline watcher, as if it were another day (synthetic dates):

```sh
python -m src.watch --tracker scratch/tracker.json --today 2027-03-24 --dry-run
```

Drop `--dry-run` from `notify` or `watch` to really send the email to
`NOTIFY_TO`; that marks the scratch copy, never `tracker.json`. A live
`src.run` costs real API credit (about $0.25 per profile).
