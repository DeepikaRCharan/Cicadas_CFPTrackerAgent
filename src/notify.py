"""Email sending: send one rendered email through Resend to NOTIFY_TO.

Build step 7 (docs/project-brief.md, "Build order"). Sends whatever
src/render.py produced, to NOTIFY_TO, nothing more. The renderer's receipt
is applied (tracker.mark_sent) and tracker.json saved only after Resend
accepts the send. Any failure -- a missing RESEND_API_KEY or NOTIFY_TO, a
malformed address, an API rejection -- is printed as FAILED, exits non-zero,
marks nothing, and leaves tracker.json untouched, so those items go out with
the next email instead of being lost (brief, Guardrails > Visible failure).

    python -m src.notify digest                        # render tracker.json, send, mark
    python -m src.notify reminder eci-2027             # week-before reminder for one conference
    python -m src.notify --dry-run digest              # print instead of sending; marks nothing
    python -m src.notify --tracker /tmp/t.json digest  # any tracker file, e.g. a test copy
    python -m src.notify digest --run-problems logs/run_problems.json  # after a partial run (workflow)

Not retried on failure: the next run (or a manual workflow_dispatch) is the
retry, and nothing is lost in between because nothing was marked.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, Mapping

import resend
from dotenv import load_dotenv

from src.config import RESEND_FROM, TRACKER_PATH
from src.profile_schema import load_live_profiles
from src.render import RenderedEmail, render_digest, render_week_before
from src.tracker import TrackerError, load_tracker, mark_sent, save_tracker
from src.tracker_schema import TrackerFile

# One address, plausibly shaped. Resend does the real validation; this only
# catches a placeholder or typo before a call is made.
_ADDRESS = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


class NotifyError(Exception):
    """The email was not sent (or not recorded) -- the run must fail visibly."""


def load_settings(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """RESEND_API_KEY and NOTIFY_TO from the environment, checked before any
    send is attempted."""
    env = os.environ if env is None else env
    missing = [name for name in ("RESEND_API_KEY", "NOTIFY_TO") if not env.get(name, "").strip()]
    if missing:
        raise NotifyError(f"{' and '.join(missing)} not set -- add to .env locally or to the GitHub Actions secrets")
    api_key, to = env["RESEND_API_KEY"].strip(), env["NOTIFY_TO"].strip()
    if not _ADDRESS.match(to):
        raise NotifyError(f"NOTIFY_TO={to!r} is not a single email address")
    return api_key, to


def resend_send(api_key: str, to: str, email: RenderedEmail) -> str:
    """One Resend API call; returns the email id Resend assigned."""
    resend.api_key = api_key
    response = resend.Emails.send(
        {"from": RESEND_FROM, "to": [to], "subject": email.subject, "text": email.body}
    )
    email_id = response.get("id") if isinstance(response, dict) else None
    if not email_id:
        raise NotifyError(f"Resend returned no email id -- treating as not sent (response: {response!r})")
    return email_id


Sender = Callable[[str, str, RenderedEmail], str]


def send_and_mark(
    email: RenderedEmail,
    tracker: TrackerFile,
    tracker_path: str | Path,
    sent_on: datetime.date,
    *,
    env: Mapping[str, str] | None = None,
    sender: Sender = resend_send,
) -> str:
    """Send `email`; only if Resend accepts it, apply its receipt and save
    the tracker. Returns the Resend email id. Raises NotifyError otherwise,
    with tracker.json untouched and nothing marked."""
    api_key, to = load_settings(env)
    try:
        email_id = sender(api_key, to, email)
    except NotifyError:
        raise
    except Exception as exc:  # any SDK, HTTP or network error: not sent
        raise NotifyError(f"send to {to} failed: {type(exc).__name__}: {exc}") from exc

    mark_sent(tracker, email.receipt, sent_on)
    try:
        save_tracker(tracker, tracker_path)
    except Exception as exc:
        raise NotifyError(
            f"email {email_id} WAS sent, but saving {tracker_path} failed, so nothing is recorded as "
            f"reported and these items will be sent again next time: {exc}"
        ) from exc
    return email_id


def load_run_problems(path: str | Path) -> list[str]:
    """src.run's --problems-out file. Missing or unreadable is an error, not
    'no problems' -- and a crashed run's file refuses the digest outright
    (the workflow skips it anyway; this is the backstop)."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        crashed, problems = data["crashed"], data["problems"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise NotifyError(f"can't read run problems from {path}: {type(exc).__name__}: {exc}") from exc
    if crashed:
        raise NotifyError(f"{path} says the run crashed -- no digest for a crashed run")
    if not isinstance(problems, list) or not all(isinstance(p, str) for p in problems):
        raise NotifyError(f"{path}: 'problems' must be a list of strings")
    return problems


def _fail(message: str) -> int:
    print(f"[notify] FAILED: {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error title=Cicadas notify::{message}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.notify", description="Send a Cicadas email.")
    parser.add_argument("--tracker", default=TRACKER_PATH, help="tracker file to render from and mark (default: tracker.json)")
    parser.add_argument("--dry-run", action="store_true", help="print the email instead of sending it; marks nothing")
    kinds = parser.add_subparsers(dest="kind", required=True)
    digest = kinds.add_parser("digest", help="the monthly digest")
    digest.add_argument("--run-problems", metavar="PATH",
                        help="src.run's --problems-out file; its failures go in a Run problems section")
    reminder = kinds.add_parser("reminder", help="the week-before reminder for one conference")
    reminder.add_argument("conference_id")
    args = parser.parse_args(argv)

    load_dotenv()
    today = datetime.date.today()
    try:
        tracker = load_tracker(args.tracker)
        profiles = load_live_profiles()
        if args.kind == "digest":
            problems = load_run_problems(args.run_problems) if args.run_problems else []
            email = render_digest(tracker, profiles, today, problems)
        else:
            email = render_week_before(tracker, args.conference_id, profiles, today)
    except (TrackerError, ValueError, KeyError, NotifyError) as exc:
        return _fail(str(exc))

    receipt = email.receipt
    summary = (
        f"{len(receipt.notifications)} notification(s), {len(receipt.conflicts)} conflict(s), "
        f"snapshots: {receipt.snapshot_profile_ids or 'none'}"
    )
    if args.dry_run:
        print(f"Subject: {email.subject}\n")
        print(email.body)
        print(f"[notify] dry run: nothing sent, nothing marked (would mark {summary})")
        return 0

    try:
        email_id = send_and_mark(email, tracker, args.tracker, today)
    except NotifyError as exc:
        return _fail(str(exc))
    print(f"[notify] sent {email.subject!r} (Resend id {email_id}); marked {summary} in {args.tracker}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
