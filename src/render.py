"""Email renderer: pure functions from tracker data to email text.

Build step 6 (docs/project-brief.md, "Email layout"). No sending, no LLM
calls, no file or clock access -- `today` and the live profiles are passed
in, so the same tracker always renders the same email.

The template never computes or infers content. Every value shown is a stored
field, or fixed template wording: a null field renders as NOT_STATED, a
rolling call as "Rolling". The only logic here is the selection and ordering
the brief itself specifies -- unreported notifications, the Still open rule,
"what pulled it down" (the lowest stored dimension score), the 14-day Also
coming up window.

Each render returns a RenderedEmail whose `receipt` lists what the email
carried. Step 7 passes it to src/tracker.py's mark_sent only after the send
succeeds; a failed send leaves everything unreported for the next email.
"""

from __future__ import annotations

import datetime
import textwrap
from dataclasses import dataclass

from src.profile_schema import Profile
from src.tracker import (
    PendingReport,
    ReportReceipt,
    above_new_fit,
    is_reminder_eligible,
    unreported_notifications,
)
from src.tracker_schema import (
    AccessType,
    CallStatus,
    ConferenceEntry,
    DeadlinePrecision,
    DecisionState,
    IdentityConflictReason,
    NotificationReason,
    OtherAvenuesFit,
    ProfileMatch,
    StandardFit,
    TierFit,
    TrackerFile,
    VenueTier,
)

NOT_STATED = "Not stated on venue page"
ROLLING = "Rolling"
NONE_THIS_MONTH = "None this month."
ALSO_COMING_UP_DAYS = 14
WIDTH = 76
RULE = "=" * WIDTH

BUCKET_LABELS = {
    VenueTier.ACADEMIC_PRACTITIONER: "Academic & Practitioner",
    VenueTier.INDUSTRY_TECHNICAL_MEDIA: "Industry & Technical Media",
    VenueTier.CORPORATE_INSTITUTIONAL: "Corporate & Institutional",
    VenueTier.OTHER_AVENUES: "Other Avenues",
}
ACCESS_LABELS = {
    AccessType.OPEN: "Open",
    AccessType.MEMBERS_ONLY: "Members only",
    AccessType.INVITE_ONLY: "Invite only",
}
DECISION_LABELS = {
    DecisionState.PENDING: "Pending (no decision yet)",
    DecisionState.APPLYING: "Applying",
    DecisionState.APPLIED: "Applied",
    DecisionState.SKIPPED: "Skipped",
    DecisionState.UNCONFIRMED: "Unconfirmed (deadline passed, no decision recorded)",
}
UPDATE_LABELS = {
    NotificationReason.SUBMISSIONS_OPENED: "Applications now open",
    NotificationReason.DEADLINE_CHANGED: "Deadline published or changed",
}
CONFLICT_LABELS = {
    IdentityConflictReason.SAME_ID_DIFFERENT_URL: "same ID as a tracked conference, but a different link",
    IdentityConflictReason.SAME_URL_SAME_YEAR_DIFFERENT_NAME: "same link and year as a tracked conference, but a different name",
}
DIGEST_REASONS = frozenset(
    {
        NotificationReason.NEW_FIT,
        NotificationReason.TIER_ALERT,
        NotificationReason.SUBMISSIONS_OPENED,
        NotificationReason.DEADLINE_CHANGED,
    }
)
DECISIONS_FOOTER = (
    'To record a decision, edit decisions.json on GitHub: "<ID>": "applying", "applied" or "skipped".'
)


@dataclass
class RenderedEmail:
    subject: str
    body: str
    receipt: ReportReceipt


# --- Field rendering (stored value or fixed wording, nothing else) ----------


def _stated(value: object) -> str:
    return NOT_STATED if value is None else str(value)


def _date_label(day: datetime.date) -> str:
    return f"{day.day} {day:%B %Y}"


def _deadline(conference: ConferenceEntry) -> str:
    facts = conference.extraction
    if not facts.has_deadline:
        return ROLLING
    if facts.deadline_as_stated is None:
        return NOT_STATED
    if facts.deadline_precision == DeadlinePrecision.MONTH:
        return f"{facts.deadline_as_stated} (exact date not published)"
    return facts.deadline_as_stated


def _call(conference: ConferenceEntry) -> str:
    facts = conference.extraction
    if facts.call_status == CallStatus.OPEN:
        return "Open now"
    if facts.call_status == CallStatus.CLOSED:
        return "Closed"
    if facts.submission_opens is not None:
        return f"Opens {facts.submission_opens}"
    if facts.call_status == CallStatus.NOT_YET_OPEN:
        return f"Not yet open (opening date: {NOT_STATED.lower()})"
    return NOT_STATED


def _access(conference: ConferenceEntry) -> str:
    access = conference.extraction.access
    return NOT_STATED if access is None else ACCESS_LABELS[access]


def _mode(conference: ConferenceEntry) -> str:
    mode = conference.extraction.mode
    return NOT_STATED if mode is None else mode.value.capitalize()


def _buckets(conference: ConferenceEntry) -> str:
    return ", ".join(BUCKET_LABELS[t] for t in conference.extraction.venue_tier)


def _paper(profile_id: str, profiles: dict[str, Profile]) -> str:
    profile = profiles.get(profile_id)
    return f"{profile.title} ({profile_id})" if profile else profile_id


def _decision(conference: ConferenceEntry, profile_ids: list[str]) -> str:
    states = [(pid, conference.matches[pid].decision) for pid in profile_ids if pid in conference.matches]
    if len({state for _, state in states}) == 1:
        return DECISION_LABELS[states[0][1]]
    return "; ".join(f"{pid}: {DECISION_LABELS[state]}" for pid, state in states)


def _field(label: str, value: str, indent: int = 2) -> str:
    prefix = " " * indent + f"{label + ':':<15}"
    return textwrap.fill(value, WIDTH, initial_indent=prefix, subsequent_indent=" " * len(prefix))


def _para(text: str, indent: int) -> str:
    pad = " " * indent
    return textwrap.fill(text, WIDTH, initial_indent=pad, subsequent_indent=pad)


def _bullet(text: str, indent: int) -> str:
    pad = " " * indent
    return textwrap.fill(text, WIDTH, initial_indent=f"{pad}- ", subsequent_indent=f"{pad}  ")


def _dimension(label: str, score: int, reasoning: str) -> list[str]:
    return [f"      {label} {score}/100", _para(reasoning, 8)]


def _fit_lines(tier: VenueTier, fit: TierFit) -> list[str]:
    lines = [f"    {BUCKET_LABELS[tier]}: fitness {fit.overall}/100"]
    lines += _dimension("Theme fit", fit.theme_fit.score, fit.theme_fit.reasoning)
    if isinstance(fit, StandardFit):
        lines += _dimension("Genre fit", fit.genre_fit.score, fit.genre_fit.reasoning)
        lines += _dimension("Format fit", fit.format_fit.score, fit.format_fit.reasoning)
    elif isinstance(fit, OtherAvenuesFit):
        lines.append("      Why no other venue type fit")
        lines.append(_para(fit.no_other_fit_reasoning, 8))
        if fit.low_confidence:
            lines.append("      Low confidence: a theme-only score. Worth a manual look.")
    return lines


def _lowest_dimension(fit: TierFit) -> tuple[str, int, str]:
    dims = [("Theme fit", fit.theme_fit)]
    if isinstance(fit, StandardFit):
        dims += [("Genre fit", fit.genre_fit), ("Format fit", fit.format_fit)]
    label, dim = min(dims, key=lambda d: d[1].score)
    return label, dim.score, dim.reasoning


# --- Digest sections --------------------------------------------------------


def _snapshot(tracker: TrackerFile, profiles: dict[str, Profile]) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    ids: list[str] = []
    for profile_id in profiles:
        classification = tracker.profiles.get(profile_id)
        if classification is None or classification.snapshot_sent is not None:
            continue
        ids.append(profile_id)
        buckets = ", ".join(BUCKET_LABELS[t] for t in classification.paper_bucket)
        lines += [
            _para(f"FIRST LOOK: {_paper(profile_id, profiles)}", 0),
            "",
            _field("Read as", buckets, 0),
            "What drove that call:",
            *(_bullet(cue, 2) for cue in classification.paper_bucket_cues),
            "Reasoning:",
            _para(classification.paper_bucket_reasoning, 2),
            _field("Main search", buckets, 0),
            "",
        ]
    if ids:
        lines += [
            _para(
                "Cicadas will mainly look for the venue types listed as 'Main search' under each paper "
                "above. Every other venue type still gets a light monthly probe.",
                0,
            ),
            _para(
                "Each conference below is scored per dimension (theme, genre, format) "
                "with its reasoning, here and in every email after this one. If a read "
                "of your paper looks wrong, its scores will be too.",
                0,
            ),
            "",
        ]
    return lines, ids


def _group(reports: list[PendingReport], reasons: set[NotificationReason]) -> list[tuple[ConferenceEntry, str, list[PendingReport]]]:
    """Group a section's reports by (conference, profile), in tracker order."""
    grouped: dict[tuple[str, str], tuple[ConferenceEntry, str, list[PendingReport]]] = {}
    for report in reports:
        if report.entry.reason in reasons:
            key = (report.conference.id, report.profile_id)
            grouped.setdefault(key, (report.conference, report.profile_id, []))[2].append(report)
    return list(grouped.values())


def _new_section(groups, profiles) -> list[str]:
    lines: list[str] = []
    for conference, profile_id, _ in groups:
        facts = conference.extraction
        match = conference.matches[profile_id]
        lines += [
            f"* {conference.name}",
            _field("For paper", _paper(profile_id, profiles)),
            _field("Location", _stated(facts.location)),
            _field("Access", _access(conference)),
            _field("Deadline", _deadline(conference)),
            _field("Call", _call(conference)),
            _field("Bucket(s)", _buckets(conference)),
            _field("Mode", _mode(conference)),
            _field("Fees", _stated(facts.fees)),
            _field("Link", conference.url),
            _field("ID", conference.id),
            "  Fitness, per dimension:",
        ]
        for tier, fit in match.fit_by_tier.items():
            lines += _fit_lines(tier, fit)
        lines.append("")
    return lines


def _tier_alert_section(groups, profiles) -> list[str]:
    lines = [_para("Strong topic match, low score: possibly misread venue type, or a format you'd need to adapt.", 0), ""]
    for conference, profile_id, _ in groups:
        lines += [
            f"* {conference.name}",
            _field("For paper", _paper(profile_id, profiles)),
            _field("Location", _stated(conference.extraction.location)),
            _field("Deadline", _deadline(conference)),
            _field("Bucket(s)", _buckets(conference)),
        ]
        for tier, fit in conference.matches[profile_id].fit_by_tier.items():
            label, score, reasoning = _lowest_dimension(fit)
            lines += [
                f"    {BUCKET_LABELS[tier]}: theme fit {fit.theme_fit.score}/100, overall {fit.overall}/100",
                f"      What pulled it down: {label} {score}/100",
                _para(reasoning, 8),
            ]
        lines += [_field("Link", conference.url), _field("ID", conference.id), ""]
    return lines


def _updates_section(reports: list[PendingReport]) -> list[str]:
    by_conference: dict[str, tuple[ConferenceEntry, list[str], list[NotificationReason]]] = {}
    for report in reports:
        if report.entry.reason not in UPDATE_LABELS:
            continue
        conference, pids, reasons = by_conference.setdefault(report.conference.id, (report.conference, [], []))
        if report.profile_id not in pids:
            pids.append(report.profile_id)
        if report.entry.reason not in reasons:
            reasons.append(report.entry.reason)
    lines: list[str] = []
    for conference, pids, reasons in by_conference.values():
        lines += [
            f"* {conference.name}",
            _field("Location", _stated(conference.extraction.location)),
            _field("What changed", "; ".join(UPDATE_LABELS[r] for r in reasons)),
            _field("Deadline", _deadline(conference)),
            _field("Your decision", _decision(conference, pids)),
            _field("Link", conference.url),
            _field("ID", conference.id),
            "",
        ]
    return lines


def _past_deadline(conference: ConferenceEntry, today: datetime.date) -> bool:
    facts = conference.extraction
    if not facts.has_deadline or facts.deadline is None:
        return False
    if facts.deadline_precision == DeadlinePrecision.MONTH:
        return (today.year, today.month) > (facts.deadline.year, facts.deadline.month)
    return facts.deadline < today


def _still_open_match(match: ProfileMatch) -> bool:
    return match.decision == DecisionState.APPLYING or (
        match.decision == DecisionState.PENDING and above_new_fit(match)
    )


def _still_open_section(tracker: TrackerFile, profiles: dict[str, Profile], today: datetime.date) -> list[str]:
    rows: list[tuple[ConferenceEntry, list[str]]] = []
    for conference in tracker.conferences:
        pids = [pid for pid, m in conference.matches.items() if pid in profiles and _still_open_match(m)]
        if pids and not _past_deadline(conference, today):
            rows.append((conference, pids))
    rows.sort(key=lambda r: (r[0].extraction.deadline is None, r[0].extraction.deadline or datetime.date.max, r[0].name))
    lines: list[str] = []
    for conference, pids in rows:
        lines += [
            f"* {conference.name}",
            _field("Location", _stated(conference.extraction.location)),
            _field("Deadline", _deadline(conference)),
            _field("Your decision", _decision(conference, pids)),
            _field("ID", conference.id),
        ]
    return lines


def _needs_a_look_section(conflicts, profiles) -> list[str]:
    lines: list[str] = []
    for conflict in conflicts:
        lines += [
            f"* Found: {conflict.candidate_name}",
            _field("Problem", CONFLICT_LABELS[conflict.reason]),
            _field("Found at", conflict.candidate_url),
            _field("Tracked as", f"{conflict.existing_id} at {conflict.existing_url}"),
            _field("Found by", _paper(conflict.profile_id, profiles)),
            "  Not merged. If these are the same call, nothing to do; if not, the new",
            "  one is not being tracked yet.",
            "",
        ]
    return lines


def _section(title: str, lines: list[str], empty: str = NONE_THIS_MONTH) -> list[str]:
    body = lines if lines else [empty, ""]
    if body[-1] != "":
        body = body + [""]
    return [RULE, title.upper(), RULE, "", *body]


# --- Public renders ---------------------------------------------------------


def render_digest(tracker: TrackerFile, profiles: dict[str, Profile], today: datetime.date) -> RenderedEmail:
    """The monthly digest. `profiles` are the live profiles (id -> Profile):
    only they get a first-run snapshot or appear in Still open; swapped-out
    profiles' entries are history."""
    run_day = tracker.last_run.date() if tracker.last_run else today
    reports = unreported_notifications(tracker)
    digest_reports = [r for r in reports if r.entry.reason in DIGEST_REASONS]
    new_groups = _group(digest_reports, {NotificationReason.NEW_FIT})
    alert_groups = _group(digest_reports, {NotificationReason.TIER_ALERT})
    updates = _updates_section(digest_reports)
    conflicts = [c for c in tracker.identity_conflicts if not c.reported]
    snapshot_lines, snapshot_ids = _snapshot(tracker, profiles)

    lines = [f"Cicadas monthly digest: run of {_date_label(run_day)}", ""]
    if not (new_groups or alert_groups or updates):
        lines += [
            _para(
                f"Nothing new this month. Cicadas ran on {_date_label(run_day)} and found no "
                "new fits, tier alerts or updates. Your still-open list is below.",
                0,
            ),
            "",
        ]
    lines += snapshot_lines
    lines += _section("New this month", _new_section(new_groups, profiles))
    lines += _section("Tier Alert", _tier_alert_section(alert_groups, profiles) if alert_groups else [])
    lines += _section("Updates", updates)
    lines += _section("Still open", _still_open_section(tracker, profiles, today), "Nothing open right now.")
    lines += _section("Needs a look", _needs_a_look_section(conflicts, profiles), "Nothing to check.")
    lines.append(_para(DECISIONS_FOOTER, 0))

    receipt = ReportReceipt(notifications=digest_reports, conflicts=conflicts, snapshot_profile_ids=snapshot_ids)
    return RenderedEmail(f"Cicadas monthly digest: {_date_label(run_day)}", "\n".join(lines) + "\n", receipt)


def render_week_before(
    tracker: TrackerFile, conference_id: str, profiles: dict[str, Profile], today: datetime.date
) -> RenderedEmail:
    """The week-before reminder for one conference, with Also coming up:
    every other reminder-eligible deadline in the next 14 days. The receipt
    carries that conference's unreported week_before entries (logged by the
    daily watcher, step 10)."""
    conference = next((c for c in tracker.conferences if c.id == conference_id), None)
    if conference is None:
        raise KeyError(f"no tracked conference {conference_id!r}")
    pids = [pid for pid in conference.matches if pid in profiles]
    facts = conference.extraction

    lines = [
        f"Deadline in one week: {conference.name}",
        "",
        _field("Location", _stated(facts.location), 0),
        _field("Deadline", _deadline(conference), 0),
        _field("Link", conference.url, 0),
        _field("ID", conference.id, 0),
        _field("Your decision", _decision(conference, pids) if pids else NOT_STATED, 0),
        "",
    ]

    horizon = today + datetime.timedelta(days=ALSO_COMING_UP_DAYS)
    upcoming = [
        c
        for c in tracker.conferences
        if c.id != conference_id
        and c.extraction.deadline is not None
        and today <= c.extraction.deadline <= horizon
        and any(pid in profiles and is_reminder_eligible(c, m) for pid, m in c.matches.items())
    ]
    upcoming.sort(key=lambda c: (c.extraction.deadline, c.name))
    also = [
        f"{c.name}: {_stated(c.extraction.location)}; deadline {_deadline(c)}" for c in upcoming
    ] or ["Nothing else in the next 14 days."]
    lines += [f"Also coming up (next {ALSO_COMING_UP_DAYS} days):", *(_bullet(line, 2) for line in also), "", _para(DECISIONS_FOOTER, 0)]

    receipt = ReportReceipt(
        notifications=[
            PendingReport(conference, pid, entry)
            for pid in pids
            for entry in conference.matches[pid].notifications
            if entry.reason == NotificationReason.WEEK_BEFORE and not entry.reported
        ]
    )
    subject = f"Cicadas reminder: {conference.name}, deadline {_deadline(conference)}"
    return RenderedEmail(subject, "\n".join(lines) + "\n", receipt)
