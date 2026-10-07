"""The daily deadline watcher: week-before reminders and closure.

Build step 10 (docs/project-brief.md, "Deadline watcher and closure"). No
LLM and no web search -- plain date arithmetic over decisions.json and
tracker.json, reusing the email code (src/render.py, src/notify.py). Run
daily by .github/workflows/daily-watcher.yml, at a different hour from the
monthly run.

Every run, in order:

1. Sync decisions.json into the tracker -- always first, as in the monthly run.
2. Closure. A has_deadline entry two days past its deadline (month-only:
   two days past the end of that month) has every live, still-open match
   (pending or applying) set to `unconfirmed`, including matches that never
   crossed the threshold. Closed matches are never reopened. Rolling entries
   and swapped-out profiles' matches are never touched, and neither is a
   conference with a decisions.json line: a researcher decision always wins
   (an `applying` past its deadline stays until they change it -- otherwise
   this closure and the next run's decisions sync would undo each other).
3. week_before. Every reminder-eligible match (tracker.is_reminder_eligible:
   applying whatever its score, or pending above the threshold) is logged
   once per deadline, from seven days before it (month-only: seven days
   before the month starts) up to the deadline (month-only: the end of that
   month). It's a window rather than a single day, so a day the watcher
   didn't run (workflow paused, a skipped GitHub schedule) sends the
   reminder late rather than never. A changed deadline gets its own reminder.
4. Send one reminder per conference holding an unreported week_before entry
   for its current deadline -- including one whose send failed on an
   earlier day -- marking it reported only after Resend accepts it.

tracker.json is saved only when something changed, so a day with nothing to
do leaves the file byte-identical and the workflow makes no commit.

    python -m src.watch                                   # tracker.json, today (UTC)
    python -m src.watch --dry-run                         # print changes and emails; save and send nothing
    python -m src.watch --today 2027-03-24 --tracker scratch/tracker.json   # a synthetic date, scratch copy

Exits 0 when everything succeeded, including "nothing due today"; 1 on any
failure. A failed send doesn't stop the other reminders, but fails the run.
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

from src.config import DECISIONS_PATH, TRACKER_PATH
from src.notify import NotifyError, Sender, resend_send, send_and_mark
from src.profile_schema import Profile, load_live_profiles
from src.render import render_week_before
from src.tracker import (
    DecisionsError,
    TrackerError,
    is_open,
    is_reminder_eligible,
    load_decisions,
    load_tracker,
    save_tracker,
    sync_decisions,
)
from src.tracker_schema import (
    ConferenceEntry,
    DeadlinePrecision,
    DecisionState,
    NotificationEntry,
    NotificationReason,
    TrackerFile,
)

REMINDER_DAYS_BEFORE = 7
CLOSURE_DAYS_AFTER = 2


# --- Date rules ---------------------------------------------------------------


def _last_day(day: datetime.date) -> datetime.date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def last_submission_day(conference: ConferenceEntry) -> datetime.date | None:
    """The deadline itself, or for a month-only deadline the end of that
    month. None when there's no deadline to count from (rolling, or
    has_deadline but not found)."""
    facts = conference.extraction
    if not facts.has_deadline or facts.deadline is None:
        return None
    if facts.deadline_precision == DeadlinePrecision.MONTH:
        return _last_day(facts.deadline)
    return facts.deadline


def reminder_window(conference: ConferenceEntry) -> tuple[datetime.date, datetime.date] | None:
    """From seven days before the deadline (month-only: before the month
    starts -- `deadline` is then the 1st) to the last submission day."""
    last = last_submission_day(conference)
    if last is None:
        return None
    return conference.extraction.deadline - datetime.timedelta(days=REMINDER_DAYS_BEFORE), last


def closure_day(conference: ConferenceEntry) -> datetime.date | None:
    last = last_submission_day(conference)
    return None if last is None else last + datetime.timedelta(days=CLOSURE_DAYS_AFTER)


def _in_window(conference: ConferenceEntry, today: datetime.date) -> bool:
    window = reminder_window(conference)
    return window is not None and window[0] <= today <= window[1]


def _week_before_for_deadline(conference: ConferenceEntry, profile_id: str) -> list[NotificationEntry]:
    return [
        n
        for n in conference.matches[profile_id].notifications
        if n.reason == NotificationReason.WEEK_BEFORE and n.deadline == conference.extraction.deadline
    ]


# --- The three steps ----------------------------------------------------------


def close_passed(tracker: TrackerFile, live: set[str], today: datetime.date, researcher_ids: set[str]) -> list[str]:
    """Step 2. `researcher_ids`: conferences with a decisions.json line, left
    alone. Returns one line per match closed."""
    closed: list[str] = []
    for conference in tracker.conferences:
        day = closure_day(conference)
        if day is None or today < day or conference.id in researcher_ids:
            continue
        for profile_id, match in conference.matches.items():
            if profile_id in live and is_open(match):
                closed.append(f"{conference.id} [{profile_id}]: {match.decision.value} -> unconfirmed "
                              f"(deadline {conference.extraction.deadline_as_stated}, closed {day})")
                match.decision = DecisionState.UNCONFIRMED
    return closed


def log_reminders(tracker: TrackerFile, live: set[str], today: datetime.date, now: datetime.datetime) -> list[str]:
    """Step 3. Logs a week_before entry on every eligible live match in its
    window that hasn't had one for this deadline. Returns one line per entry."""
    logged: list[str] = []
    for conference in tracker.conferences:
        if not _in_window(conference, today):
            continue
        for profile_id, match in conference.matches.items():
            if profile_id not in live or not is_reminder_eligible(conference, match):
                continue
            if _week_before_for_deadline(conference, profile_id):
                continue
            match.notifications.append(NotificationEntry(
                date=today, reason=NotificationReason.WEEK_BEFORE,
                deadline=conference.extraction.deadline, run_at=now,
            ))
            logged.append(f"{conference.id} [{profile_id}]: week_before for {conference.extraction.deadline_as_stated}")
    return logged


def due_reminders(tracker: TrackerFile, live: set[str], today: datetime.date) -> list[str]:
    """Step 4's selection: conference ids, in tracker order, whose current
    deadline has an unreported week_before entry on an eligible live match
    and is still in its window."""
    due: list[str] = []
    for conference in tracker.conferences:
        if not _in_window(conference, today):
            continue
        if any(
            profile_id in live
            and is_reminder_eligible(conference, match)
            and any(not n.reported for n in _week_before_for_deadline(conference, profile_id))
            for profile_id, match in conference.matches.items()
        ):
            due.append(conference.id)
    return due


# --- A whole run -----------------------------------------------------------------


@dataclass
class WatchResult:
    closed: list[str] = field(default_factory=list)
    logged: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    changed: bool = False
    tracked: int = 0
    open_matches: int = 0

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0


def _fail(message: str) -> None:
    print(f"[watch] FAILED: {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error title=Cicadas watcher::{message}")


def run_watch(
    tracker_path: str | Path,
    decisions_path: str | Path,
    profiles: dict[str, Profile],
    today: datetime.date,
    now: datetime.datetime,
    *,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    sender: Sender = resend_send,
) -> WatchResult:
    """One watcher run against `tracker_path`. Raises TrackerError or
    DecisionsError when either file can't be read -- nothing is changed then."""
    tracker_path = Path(tracker_path)
    if not tracker_path.exists():
        raise TrackerError(f"{tracker_path} not found -- the watcher runs on the committed tracker "
                           "(an empty one is fine); it never starts one itself")
    tracker = load_tracker(tracker_path)
    decisions = load_decisions(decisions_path)
    live = set(profiles)
    before = tracker.model_dump_json()

    sync_decisions(tracker, decisions, live)
    result = WatchResult()
    result.closed = close_passed(tracker, live, today, set(decisions))
    result.logged = log_reminders(tracker, live, today, now)
    result.changed = tracker.model_dump_json() != before
    result.tracked = len(tracker.conferences)
    result.open_matches = sum(1 for c in tracker.conferences for pid, m in c.matches.items() if pid in live and is_open(m))
    for line in result.closed:
        print(f"[watch] {'would close' if dry_run else 'closed'} {line}")
    for line in result.logged:
        print(f"[watch] {'would log' if dry_run else 'logged'} {line}")

    due = due_reminders(tracker, live, today)
    if dry_run:
        for conference_id in due:
            email = render_week_before(tracker, conference_id, profiles, today)
            print(f"[watch] dry run, would send:\nSubject: {email.subject}\n\n{email.body}")
        return result

    if result.changed:
        save_tracker(tracker, tracker_path)
    for conference_id in due:
        email = render_week_before(tracker, conference_id, profiles, today)
        try:
            email_id = send_and_mark(email, tracker, tracker_path, today, env=env, sender=sender)
        except NotifyError as exc:
            message = f"reminder for {conference_id} not sent: {exc}"
            _fail(message)
            result.failures.append(message)
            continue
        result.changed = True
        result.sent.append(conference_id)
        print(f"[watch] sent {email.subject!r} (Resend id {email_id})")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.watch", description="Cicadas daily deadline watcher (no LLM).")
    parser.add_argument("--tracker", default=TRACKER_PATH)
    parser.add_argument("--decisions", default=DECISIONS_PATH)
    parser.add_argument("--today", type=datetime.date.fromisoformat,
                        help="YYYY-MM-DD: run as if it were this date (demo against synthetic deadlines)")
    parser.add_argument("--dry-run", action="store_true", help="print what would change and every email; save and send nothing")
    args = parser.parse_args(argv)
    load_dotenv()

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    today = args.today or now.date()
    try:
        profiles = load_live_profiles()
        result = run_watch(args.tracker, args.decisions, profiles, today, now, dry_run=args.dry_run)
    except (TrackerError, DecisionsError, ValueError) as exc:
        _fail(str(exc))
        return 1

    saved = "dry run, nothing saved" if args.dry_run else ("tracker.json updated" if result.changed else "tracker.json unchanged")
    print(f"[watch] {today}: {result.tracked} tracked venue(s), {result.open_matches} open match(es) after this run; "
          f"{len(result.closed)} closed, {len(result.logged)} reminder(s) logged, {len(result.sent)} sent, "
          f"{len(result.failures)} failure(s); {saved}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
