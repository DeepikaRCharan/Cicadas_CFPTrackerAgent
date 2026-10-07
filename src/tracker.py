"""Tracker read/write: load, merge, and atomically save tracker.json, and
sync decisions.json into it.

Build step 5 (docs/project-brief.md, "Build order"). No LLM calls here -- this
module only moves already-validated shapes (src/tracker_schema.py) in and
out of tracker.json, so every rule below is plain code:

- **Identity** (brief, Rules > Identity). `id` = normalized name + year,
  and the domain alone is never used. The same normalized URL and the same
  year is the same venue, whatever it's called: a different name is merged
  and kept in `aliases`. A conflict is flagged only when the URL or the year
  differs -- same id but a different URL is recorded in `identity_conflicts`
  and logged as a WARNING, never merged, never created as a second entry.
  Same URL but a different year is a new venue (sites reuse one CFP page
  every year).
- **decisions.json** is read, never written. A bad value or a duplicate key
  stops the run with an error naming the line. An id that isn't tracked is a
  WARNING in the run log and the run continues. Decisions are copied into
  live profiles' matches only; swapped-out profiles' matches are frozen.
- **What's been reported.** Every NotificationEntry starts `reported=False`.
  The digest selects unreported entries (`unreported_notifications`) and only
  a successful send flips them (`mark_reported`, wired in step 7).
- **Safe writes.** `save_tracker` re-validates the whole file, writes to a
  temp file in the same directory, and `os.replace`s it in. Any failure
  leaves tracker.json byte-for-byte untouched.

`python -m unittest tests/test_tracker.py -v` exercises all of this with no
API calls.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from pydantic import ValidationError

from src.config import DECISIONS_PATH, NEW_FIT_MIN, TIER_ALERT_THEME_MIN, TRACKER_PATH
from src.decisions_schema import DecisionsFile, ResearcherDecision, find_untracked_ids
from src.tracker_schema import (
    RESEARCHER_SET_DECISIONS,
    CallStatus,
    ConferenceEntry,
    DecisionState,
    Extraction,
    ExtractionFacts,
    IdentityConflict,
    IdentityConflictReason,
    NotificationEntry,
    NotificationReason,
    ProfileClassification,
    ProfileMatch,
    TierFit,
    TrackerFile,
    VenueTier,
)

# A year token: 19xx or 20xx as a whole word. Stripped from names before
# normalizing (so "ECI 2027" + 2027 doesn't become "eci-2027-2027"), which is
# also what makes the trailing "-YYYY" on an id unambiguous to parse back.
_YEAR_TOKEN = re.compile(r"\b(?:19|20)\d{2}\b")
_ID_YEAR_SUFFIX = re.compile(r"-((?:19|20)\d{2})$")

# Query parameters that identify a click, not a page.
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})

# Reasons logged at most once per match, ever.
_ONCE_ONLY_REASONS = frozenset({NotificationReason.NEW_FIT, NotificationReason.TIER_ALERT})


class TrackerError(Exception):
    """tracker.json can't be read or written -- the run must stop."""


class DecisionsError(Exception):
    """decisions.json is malformed -- the run must stop (brief: 'stops the
    run with a visible error naming the bad line')."""


def warn(message: str) -> None:
    """A WARNING in the run log, not a debug line: printed to stderr, and
    also emitted as a GitHub Actions annotation when running there so it
    shows on the run's summary page."""
    print(f"[tracker] WARNING: {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=Cicadas tracker::{message}")


# --- Identity ---------------------------------------------------------------


def normalize_name(name: str) -> str:
    """Lowercase, accents stripped, '&' -> 'and', year tokens removed,
    everything else non-alphanumeric collapsed to single hyphens."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    text = _YEAR_TOKEN.sub(" ", text)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def make_id(name: str, year: int | None) -> str:
    """Conference id: normalized name + year. A rolling outlet with no year
    gets the normalized name alone."""
    base = normalize_name(name)
    if not base:
        raise ValueError(f"name {name!r} normalizes to nothing -- can't build an id")
    return f"{base}-{year}" if year is not None else base


def id_year(conference_id: str) -> int | None:
    match = _ID_YEAR_SUFFIX.search(conference_id)
    return int(match.group(1)) if match else None


def normalize_url(url: str) -> str:
    """Comparable form of a URL: scheme-insensitive, lowercase host without
    'www.' or a default port, no trailing slash, no fragment, no tracking
    parameters. Every other query parameter is kept (sorted) -- on hosts like
    EasyChair (?conf=xyz) the query is what tells conferences apart."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    return host + path + (f"?{urlencode(query)}" if query else "")


# --- Load / save ------------------------------------------------------------


def load_tracker(path: str | Path = TRACKER_PATH) -> TrackerFile:
    """A missing tracker.json is a first run: an empty tracker. One that
    exists but doesn't parse or validate stops the run -- it is never
    silently replaced."""
    path = Path(path)
    if not path.exists():
        print(f"[tracker] {path} not found -- starting from an empty tracker (first run)")
        return TrackerFile()
    try:
        return TrackerFile.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise TrackerError(f"{path} failed schema validation -- not touching it:\n{exc}") from exc


def save_tracker(tracker: TrackerFile, path: str | Path = TRACKER_PATH) -> None:
    """Validate the whole tracker, then write it atomically.

    In-memory edits to pydantic models skip validation, so the full file is
    round-tripped through TrackerFile.model_validate first. Only then is it
    written -- to a temp file in the same directory, fsynced, and swapped in
    with os.replace. On any failure the temp file is removed and tracker.json
    keeps its old bytes."""
    path = Path(path)
    try:
        validated = TrackerFile.model_validate(tracker.model_dump(mode="json"))
    except ValidationError as exc:
        raise TrackerError(f"refusing to write {path} -- tracker failed validation:\n{exc}") from exc
    payload = validated.model_dump_json(indent=2) + "\n"

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# --- decisions.json ---------------------------------------------------------


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise DecisionsError(f"decisions.json: {key!r} appears more than once -- keep one line per conference")
        seen[key] = value
    return seen


def load_decisions(path: str | Path = DECISIONS_PATH) -> dict[str, ResearcherDecision]:
    """Read decisions.json. Never written by the agent. A bad value or a
    duplicate key raises DecisionsError naming the offending line."""
    path = Path(path)
    if not path.exists():
        warn(f"{path} not found -- treating as no researcher decisions")
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise DecisionsError(f"{path} is not valid JSON (line {exc.lineno}): {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise DecisionsError(f"{path} must be a JSON object of conference id -> decision")
    allowed = [d.value for d in ResearcherDecision]
    for key, value in raw.items():
        if value not in allowed:
            raise DecisionsError(
                f"{path}: \"{key}\": {json.dumps(value)} -- must be one of {', '.join(allowed)} "
                "(pending and unconfirmed are set by the agent only)"
            )
    return DecisionsFile.model_validate(raw).root


def sync_decisions(
    tracker: TrackerFile,
    decisions: dict[str, ResearcherDecision],
    live_profile_ids: set[str],
) -> list[str]:
    """Copy researcher decisions into every live profile's match. The first
    action of every run (brief, Workflow > step 1).

    - A line for a tracked conference sets that decision (a researcher
      decision always wins, including over `unconfirmed`).
    - No line means no researcher decision: a match still holding a
      researcher-set state (the line was deleted) goes back to `pending`.
    - Swapped-out profiles' matches are frozen and never touched.
    - An id that isn't tracked is a visible WARNING; the run continues.

    Returns the warning messages, so the caller can surface them again
    (e.g. in the digest)."""
    warnings: list[str] = []
    for bad_id in find_untracked_ids(decisions, {c.id for c in tracker.conferences}):
        message = (
            f"decisions.json has \"{bad_id}\": \"{decisions[bad_id].value}\", but {bad_id!r} "
            "is not a tracked conference id -- ignored (check the ID column in the digest)"
        )
        warn(message)
        warnings.append(message)

    for conference in tracker.conferences:
        researcher = decisions.get(conference.id)
        live = {pid: m for pid, m in conference.matches.items() if pid in live_profile_ids}
        if researcher is not None and conference.matches and not live:
            print(
                f"[tracker] decisions.json {conference.id!r}: only swapped-out profiles match "
                "this conference -- their entries are frozen, nothing changed"
            )
        for profile_id, match in live.items():
            if researcher is not None:
                new = DecisionState(researcher.value)
            elif match.decision in RESEARCHER_SET_DECISIONS:
                new = DecisionState.PENDING
            else:
                continue
            if match.decision != new:
                print(f"[tracker] decision {conference.id} [{profile_id}]: {match.decision.value} -> {new.value}")
                match.decision = new
    return warnings


# --- Merge ------------------------------------------------------------------


@dataclass
class UpsertResult:
    status: str  # "created" | "updated" | "flagged"
    conference_id: str
    logged: list[NotificationReason] = field(default_factory=list)


def record_paper_classification(
    tracker: TrackerFile, profile_id: str, classification: ProfileClassification
) -> None:
    """Store classify's read of the paper. `snapshot_sent` survives a
    re-classification -- a revision never re-triggers the snapshot."""
    previous = tracker.profiles.get(profile_id)
    if previous is not None and previous.snapshot_sent is not None:
        classification = classification.model_copy(update={"snapshot_sent": previous.snapshot_sent})
    tracker.profiles[profile_id] = classification


def _flag(
    tracker: TrackerFile,
    reason: IdentityConflictReason,
    candidate_id: str,
    name: str,
    url: str,
    existing: ConferenceEntry,
    profile_id: str,
    now: datetime.datetime,
) -> UpsertResult:
    for conflict in tracker.identity_conflicts:
        if (
            conflict.candidate_id == candidate_id
            and normalize_url(conflict.candidate_url) == normalize_url(url)
            and conflict.existing_id == existing.id
        ):
            conflict.last_flagged = now
            break
    else:
        tracker.identity_conflicts.append(
            IdentityConflict(
                reason=reason,
                candidate_id=candidate_id,
                candidate_name=name,
                candidate_url=url,
                existing_id=existing.id,
                existing_url=existing.url,
                profile_id=profile_id,
                first_flagged=now,
                last_flagged=now,
            )
        )
    warn(
        f"identity conflict ({reason.value}): candidate {name!r} -> {candidate_id!r} at {url} vs "
        f"tracked {existing.id!r} at {existing.url} -- flagged, not merged"
    )
    return UpsertResult("flagged", existing.id)


def _has_reason(match: ProfileMatch, reason: NotificationReason) -> bool:
    return any(n.reason == reason for n in match.notifications)


def _sent_before(match: ProfileMatch) -> bool:
    """Whether this match has already reached the researcher in an email
    (brief: Updates are for 'conferences already sent before')."""
    return any(n.reported and n.reason in _ONCE_ONLY_REASONS for n in match.notifications)


def _log(
    match: ProfileMatch,
    reason: NotificationReason,
    extraction: Extraction,
    now: datetime.datetime,
    logged: list[NotificationReason],
) -> None:
    if reason in _ONCE_ONLY_REASONS and _has_reason(match, reason):
        return
    match.notifications.append(
        NotificationEntry(date=now.date(), reason=reason, deadline=extraction.deadline, run_at=now)
    )
    logged.append(reason)


def _score_events(match: ProfileMatch, extraction: Extraction, now: datetime.datetime, logged: list) -> None:
    """new_fit and tier_alert, from stored scores only (no LLM call)."""
    fits: list[TierFit] = list(match.fit_by_tier.values())
    if any(f.overall > NEW_FIT_MIN for f in fits):
        _log(match, NotificationReason.NEW_FIT, extraction, now, logged)
    if extraction.call_status != CallStatus.CLOSED and any(
        f.theme_fit.score >= TIER_ALERT_THEME_MIN and f.overall < 50 for f in fits
    ):
        _log(match, NotificationReason.TIER_ALERT, extraction, now, logged)


@dataclass
class VenueLookup:
    """Where a (name, year, url) lands under the identity rules, without
    changing anything. `entry` is the matching tracked venue (or, for a
    conflict, the one it collides with)."""

    conference_id: str
    entry: ConferenceEntry | None
    conflict: IdentityConflictReason | None = None
    alias: str | None = None  # the candidate's name, when it matched by URL + year under a different name


def lookup_venue(tracker: TrackerFile, name: str, year: int | None, url: str) -> VenueLookup:
    """The identity rules (brief, Rules > Identity), read-only: the same
    normalized URL and year is the same venue whatever its name (a different
    name comes back as `alias`); same id but a different URL is a conflict;
    same URL with a different year is a new venue."""
    candidate_id = make_id(name, year)
    candidate_url = normalize_url(url)
    same = next(
        (c for c in tracker.conferences if normalize_url(c.url) == candidate_url and id_year(c.id) == year),
        None,
    )
    if same is not None:
        return VenueLookup(same.id, same, alias=None if same.id == candidate_id else name)
    entry = next((c for c in tracker.conferences if c.id == candidate_id), None)
    if entry is not None:  # same id, so same year -- and the URL differs, or `same` would have matched
        return VenueLookup(candidate_id, entry, IdentityConflictReason.SAME_ID_DIFFERENT_URL)
    return VenueLookup(candidate_id, None)


def _add_alias(entry: ConferenceEntry, name: str) -> None:
    known = {normalize_name(entry.name), *(normalize_name(a) for a in entry.aliases)}
    if normalize_name(name) not in known:
        entry.aliases.append(name)
        print(f"[tracker] {entry.id}: also found as {name!r} (same URL and year) -- kept as an alias")


def flag_conflict(
    tracker: TrackerFile, *, name: str, year: int | None, url: str, profile_id: str, now: datetime.datetime
) -> UpsertResult:
    """Record an identity conflict found before any extraction was done
    (discovery checks identity first, so no model calls are spent on a
    candidate that would only be flagged)."""
    found = lookup_venue(tracker, name, year, url)
    if found.conflict is None:
        raise TrackerError(f"{found.conference_id!r} at {url} is not an identity conflict")
    return _flag(tracker, found.conflict, found.conference_id, name, url, found.entry, profile_id, now)


def upsert_venue(
    tracker: TrackerFile,
    *,
    name: str,
    year: int | None,
    url: str,
    extraction: Extraction | ExtractionFacts,
    profile_id: str,
    live_profile_ids: set[str],
    now: datetime.datetime,
    fit_by_tier: dict[VenueTier, TierFit] | None = None,
    decisions: dict[str, ResearcherDecision] | None = None,
    dates_url: str | None = None,
) -> UpsertResult:
    """Merge one found or re-checked venue into the tracker, for one profile.

    - `extraction` must be a full Extraction (with classify's venue_tier)
      for a new venue. For an existing one, venue_tier is kept as tracked --
      re-checks don't re-classify (brief, Workflow > step 3) -- so plain
      ExtractionFacts is enough.
    - `fit_by_tier` None or {} means "not scored this run": an existing match
      keeps its scores; with no match yet, one is only created when the
      venue is incomplete (flagged, not scored), since an empty fit_by_tier
      is invalid for a complete venue (e.g. judge-fit skipped a closed call).
    - `decisions` lets a brand-new match pick up a decision the researcher
      already set on the conference.
    - Re-running with the same input changes nothing but last_checked."""
    if profile_id not in live_profile_ids:
        raise TrackerError(f"profile {profile_id!r} is not live -- its tracker entries are frozen")
    if profile_id not in tracker.profiles:
        raise TrackerError(f"profile {profile_id!r} has no classification yet -- record_paper_classification first")

    found = lookup_venue(tracker, name, year, url)
    candidate_id = found.conference_id
    logged: list[NotificationReason] = []
    if found.conflict is not None:
        return _flag(tracker, found.conflict, candidate_id, name, url, found.entry, profile_id, now)
    entry = found.entry

    if entry is None:
        if not isinstance(extraction, Extraction):
            raise TrackerError(f"new venue {candidate_id!r} needs a full Extraction including venue_tier")
        entry = ConferenceEntry(
            id=candidate_id, name=name, url=url, dates_url=dates_url,
            first_seen=now, last_checked=now, extraction=extraction,
        )
        tracker.conferences.append(entry)
        status = "created"
        print(f"[tracker] created {candidate_id} ({url})")
    else:
        status = "updated"
        if found.alias is not None:
            _add_alias(entry, found.alias)
        old = entry.extraction
        facts = extraction.model_dump(exclude={"venue_tier", "venue_tier_reasoning"})
        new = Extraction(**facts, venue_tier=old.venue_tier, venue_tier_reasoning=old.venue_tier_reasoning)
        if isinstance(extraction, Extraction) and set(extraction.venue_tier) != set(old.venue_tier):
            print(
                f"[tracker] {candidate_id}: venue_tier kept as {[t.value for t in old.venue_tier]} "
                "-- an existing venue is not re-classified on update"
            )
        entry.extraction = new
        entry.last_checked = now
        if dates_url is not None:
            entry.dates_url = dates_url

        opened = old.call_status != CallStatus.OPEN and new.call_status == CallStatus.OPEN
        deadline_changed = new.deadline is not None and (
            new.deadline != old.deadline or new.deadline_precision != old.deadline_precision
        )
        for pid, match in entry.matches.items():
            if pid not in live_profile_ids or not _sent_before(match):
                continue
            if opened:
                _log(match, NotificationReason.SUBMISSIONS_OPENED, new, now, logged)
            if deadline_changed:
                _log(match, NotificationReason.DEADLINE_CHANGED, new, now, logged)

    match = entry.matches.get(profile_id)
    if fit_by_tier:
        if match is None:
            researcher = (decisions or {}).get(entry.id)
            match = ProfileMatch(decision=DecisionState(researcher.value) if researcher else DecisionState.PENDING)
            entry.matches[profile_id] = match
        match.fit_by_tier = dict(fit_by_tier)
        _score_events(match, entry.extraction, now, logged)
    elif match is None and not entry.extraction.complete:
        researcher = (decisions or {}).get(entry.id)
        entry.matches[profile_id] = ProfileMatch(
            decision=DecisionState(researcher.value) if researcher else DecisionState.PENDING
        )
    elif match is None:
        print(f"[tracker] {entry.id}: not scored for {profile_id} -- venue tracked, no match recorded")

    return UpsertResult(status, entry.id, logged)


# --- Reporting --------------------------------------------------------------


@dataclass
class PendingReport:
    conference: ConferenceEntry
    profile_id: str
    entry: NotificationEntry


def unreported_notifications(tracker: TrackerFile) -> list[PendingReport]:
    """Everything the next email should carry: every NotificationEntry not
    yet marked reported, however many runs ago it was logged."""
    return [
        PendingReport(conference, profile_id, entry)
        for conference in tracker.conferences
        for profile_id, match in conference.matches.items()
        for entry in match.notifications
        if not entry.reported
    ]


def mark_reported(reports: list[PendingReport]) -> None:
    """Call only after the email carrying `reports` was sent successfully
    (wired in step 7), then save_tracker. Never called on a failed send, so
    those entries stay unreported and go out with the next email."""
    for report in reports:
        report.entry.reported = True


@dataclass
class ReportReceipt:
    """Everything one rendered email carried (src/render.py builds it).
    References point into the same in-memory TrackerFile the email was
    rendered from -- apply with mark_sent before reloading it."""

    notifications: list[PendingReport] = field(default_factory=list)
    conflicts: list[IdentityConflict] = field(default_factory=list)
    snapshot_profile_ids: list[str] = field(default_factory=list)


def mark_sent(tracker: TrackerFile, receipt: ReportReceipt, sent_on: datetime.date) -> None:
    """After a successful send only: flag the receipt's notifications and
    conflicts reported and stamp snapshot_sent on its snapshot profiles.
    Then save_tracker."""
    mark_reported(receipt.notifications)
    for conflict in receipt.conflicts:
        conflict.reported = True
    for profile_id in receipt.snapshot_profile_ids:
        tracker.profiles[profile_id].snapshot_sent = sent_on


# --- Call status: decided by code from the deadline ---------------------------


def last_submission_day(facts: ExtractionFacts) -> datetime.date | None:
    """The deadline, or for a month-only deadline the end of that month.
    None when there is no deadline to count from (rolling, or not found)."""
    if not facts.has_deadline or facts.deadline is None:
        return None
    if facts.deadline_precision is not None and facts.deadline_precision.value == "month":
        next_month = facts.deadline.replace(day=28) + datetime.timedelta(days=4)
        return next_month - datetime.timedelta(days=next_month.day)
    return facts.deadline


def deadline_passed(facts: ExtractionFacts, today: datetime.date) -> bool:
    """Only after the last submission day: the deadline day itself is still
    open (no deadline time is stored, so it counts as open until it ends)."""
    last = last_submission_day(facts)
    return last is not None and today > last


def decide_call_status(facts: ExtractionFacts, today: datetime.date) -> tuple[ExtractionFacts, str | None]:
    """Open or closed is decided here, from the deadline and the run date --
    not by extraction. A call is closed only after its deadline has passed:

    - deadline passed -> closed, whatever extraction read;
    - extraction read closed, deadline not passed -> open;
    - extraction read closed, no deadline to judge by (rolling, or not
      found) -> unknown;
    - otherwise extraction's open / not_yet_open / unknown stands (the
      deadline can't say whether a call has opened yet).

    Returns (facts, note); note says what changed, None when nothing did."""
    read = facts.call_status
    if deadline_passed(facts, today):
        decided = CallStatus.CLOSED
    elif read == CallStatus.CLOSED:
        decided = CallStatus.UNKNOWN if last_submission_day(facts) is None else CallStatus.OPEN
    else:
        decided = read
    if decided == read:
        return facts, None
    last = last_submission_day(facts)
    why = (f"its deadline ({facts.deadline_as_stated}) passed on {last.isoformat()}" if decided == CallStatus.CLOSED
           else f"its deadline ({facts.deadline_as_stated}) has not passed (run date {today.isoformat()})" if last
           else "there is no deadline to judge it by")
    return (facts.model_copy(update={"call_status": decided}),
            f"call status set to {decided.value} by code (extraction read {read.value}): {why}")


# --- Selection rules shared by the digest and the daily watcher --------------


def is_open(match: ProfileMatch) -> bool:
    """pending or applying: still re-checked every run (brief, Workflow >
    step 3). Closed matches are history."""
    return match.decision in (DecisionState.PENDING, DecisionState.APPLYING)


def above_new_fit(match: ProfileMatch) -> bool:
    return any(f.overall > NEW_FIT_MIN for f in match.fit_by_tier.values())


def is_reminder_eligible(conference: ConferenceEntry, match: ProfileMatch) -> bool:
    """Brief, Deadline watcher > week_before: has_deadline entries that are
    `applying` (whatever the score) or `pending` and above the threshold."""
    if not conference.extraction.has_deadline:
        return False
    return match.decision == DecisionState.APPLYING or (
        match.decision == DecisionState.PENDING and above_new_fit(match)
    )
