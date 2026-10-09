"""Fixture tracker for the email renderer, built through the real step-5
merge functions (src/tracker.py) so the state is what the pipeline would
actually write. No API calls; venue facts are hand-written, modelled on the
ECI 2027 page used in steps 2-4.

Three monthly runs plus one reminder:

- Run 1 (7 Oct 2026), first run: both profiles' snapshots; New = ECI 2027
  (immune-tolerance) and Mongabay, a rolling venue (elephant-corridors).
- Run 2 (7 Nov 2026): ECI's call opens (Update; researcher set "applying"),
  a policy forum scores a Tier Alert, a month-only-deadline symposium is
  New, and ECI turns up again under a different URL (identity conflict).
- Run 3 (7 Dec 2026), a quiet month: same inputs, nothing new.
- Week-before reminder for ECI on 24 Mar 2027, with the symposium in Also
  coming up.

Each run "sends" its digest by applying the receipt with mark_sent, as
step 7 will. Print every rendered email with:

    python -m tests.render_fixtures
"""

from __future__ import annotations

import contextlib
import datetime
import io
from pathlib import Path

from src.decisions_schema import ResearcherDecision
from src.profile_schema import Profile
from src.render import RenderedEmail, render_digest, render_week_before
from src.tracker import mark_sent, record_paper_classification, sync_decisions, upsert_venue
from src.tracker_schema import (
    Extraction,
    NotificationEntry,
    NotificationReason,
    ProfileClassification,
    StandardFit,
    TrackerFile,
    VenueTier,
)

ROOT = Path(__file__).resolve().parent.parent
# The tests' own copies of the profiles they use, so the suite doesn't depend
# on which papers are live in profiles/.
PROFILES = Path(__file__).resolve().parent / "fixtures" / "profiles"
UTC = datetime.timezone.utc
RUN_1 = datetime.datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
RUN_2 = datetime.datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
RUN_3 = datetime.datetime(2026, 12, 7, 9, 0, tzinfo=UTC)
REMINDER_DAY = datetime.date(2027, 3, 24)

IMMUNE = "immune-tolerance"
ELEPHANT = "elephant-corridors"


def fixture_profiles_live():
    """For a test running a command-line main: its load_live_profiles()
    reads the fixture copies, not whatever is live in profiles/ today."""
    from unittest import mock

    return mock.patch("src.config.PROFILES_DIR", str(PROFILES))


def load_profiles() -> dict[str, Profile]:
    return {
        pid: Profile.model_validate_json((PROFILES / f"{pid}.json").read_text(encoding="utf-8"))
        for pid in (IMMUNE, ELEPHANT)
    }


def _classification(profile: Profile, bucket: VenueTier, cues: list[str], reasoning: str) -> ProfileClassification:
    return ProfileClassification(
        paper_bucket=[bucket],
        paper_bucket_cues=cues,
        paper_bucket_reasoning=reasoning,
        profile_hash=profile.content_hash(),
        classified_at=RUN_1,
    )


def _fit(profile: Profile, theme, genre, fmt, tier=VenueTier.ACADEMIC_PRACTITIONER, scored_at=RUN_1):
    return {
        tier: StandardFit(
            theme_fit={"score": theme[0], "reasoning": theme[1]},
            genre_fit={"score": genre[0], "reasoning": genre[1]},
            format_fit={"score": fmt[0], "reasoning": fmt[1]},
            scored_at=scored_at,
            profile_hash=profile.content_hash(),
        )
    }


def eci_extraction(call_status="not_yet_open") -> Extraction:
    return Extraction(
        has_deadline=True,
        deadline="2027-03-31",
        deadline_precision="exact",
        deadline_as_stated="31 March 2027",
        submission_opens="November 2026",
        call_status=call_status,
        submission_type="abstract",
        theme="Immunology",
        location="Florence, Italy",
        access="open",
        format_requirements=None,
        fees=None,
        mode=None,
        venue_tier=["academic_practitioner"],
        venue_tier_reasoning="A scientific congress reviewing research abstracts.",
        complete=True,
        missing_fields=["format_requirements", "fees", "mode"],
    )


def mongabay_extraction() -> Extraction:
    return Extraction(
        has_deadline=False,
        call_status="open",
        submission_type="pitch for a feature article",
        theme="Conservation and environmental science journalism",
        location=None,
        access="open",
        format_requirements="Pitch of 2-3 paragraphs; features 1,500-2,500 words",
        fees="No fee",
        mode="virtual",
        venue_tier=["industry_technical_media"],
        venue_tier_reasoning="An environmental news outlet taking pitches from contributors.",
        complete=True,
        missing_fields=["location", "submission_opens"],
    )


def forum_extraction() -> Extraction:
    return Extraction(
        has_deadline=True,
        deadline="2027-01-15",
        deadline_precision="exact",
        deadline_as_stated="15 January 2027",
        call_status="open",
        submission_type="two-page policy brief",
        theme="Transboundary wildlife corridors and land-use policy",
        location="Nairobi, Kenya",
        access="members_only",
        format_requirements="Two pages maximum, recommendations first",
        fees="USD 150 registration",
        mode="hybrid",
        venue_tier=["corporate_institutional"],
        venue_tier_reasoning="A policy forum convened for government and NGO decision-makers.",
        complete=True,
        missing_fields=["submission_opens"],
    )


def symposium_extraction() -> Extraction:
    return Extraction(
        has_deadline=True,
        deadline="2027-04-01",
        deadline_precision="month",
        deadline_as_stated="April 2027",
        call_status="unknown",
        submission_type="abstract",
        theme="Regulatory T cell biology",
        location="Kyoto, Japan",
        access=None,
        format_requirements=None,
        fees=None,
        mode="in-person",
        venue_tier=["academic_practitioner"],
        venue_tier_reasoning="A specialist research symposium.",
        complete=True,
        missing_fields=["submission_opens", "access", "format_requirements", "fees"],
    )


def _run(tracker, now, candidates, decisions, profiles):
    tracker.last_run = now
    live = set(profiles)
    sync_decisions(tracker, decisions, live)
    for candidate in candidates:
        upsert_venue(tracker, live_profile_ids=live, now=now, decisions=decisions, **candidate)


def build(stop_after_first_run: bool = False) -> dict[str, RenderedEmail]:
    """Run the three months and the reminder; return every rendered email,
    in order. Each digest is 'sent' (mark_sent) before the next run.

    stop_after_first_run: stop once run 1's digest is rendered, before it is
    marked sent -- build.tracker is then the unsent first-run state (used
    for the real test send in step 7)."""
    profiles = load_profiles()
    immune, elephant = profiles[IMMUNE], profiles[ELEPHANT]
    tracker = TrackerFile()
    emails: dict[str, RenderedEmail] = {}

    eci = dict(name="ECI 2027", year=2027, url="https://eci2027.org/call-for-abstracts/", profile_id=IMMUNE)
    eci_fit = _fit(
        immune,
        (96, "The congress's immunology theme centrally covers regulatory T cells and self-tolerance, the paper's core subject."),
        (85, "A research congress whose reviewers expect experimental evidence; the paper's gene-transfer and expression data meet that bar."),
        (65, "The call takes abstracts only; the paper would need condensing to an abstract, which is feasible but loses the methods detail."),
    )
    mongabay = dict(
        name="Mongabay contributor pitches", year=None, url="https://news.mongabay.com/submissions/",
        extraction=mongabay_extraction(), profile_id=ELEPHANT,
        fit_by_tier=_fit(
            elephant,
            (88, "Mongabay covers wildlife connectivity and human-wildlife conflict, the paper's central concerns."),
            (72, "The outlet publishes reported features for a general audience; the paper's policy argument fits, though its academic register would need loosening."),
            (80, "A 1,500-2,500 word feature is a reasonable condensation of the paper's argument."),
            tier=VenueTier.INDUSTRY_TECHNICAL_MEDIA,
        ),
    )

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        # --- Run 1: first run -------------------------------------------------
        record_paper_classification(tracker, IMMUNE, _classification(
            immune, VenueTier.ACADEMIC_PRACTITIONER,
            ["Reports experimental results (retroviral gene transfer, expression analysis)",
             "Framed as a mechanism question about regulatory T cell development",
             "Written for immunology researchers, with field-specific terminology throughout"],
            "A primary research paper presenting experimental evidence for a molecular mechanism.",
        ))
        record_paper_classification(tracker, ELEPHANT, _classification(
            elephant, VenueTier.ACADEMIC_PRACTITIONER,
            ["Synthesizes the ecological literature on corridors and connectivity",
             "Dense academic register with field terminology (stochasticity, gene flow)",
             "Ends with policy recommendations grounded in that synthesis"],
            "Reads as academic synthesis with a policy angle; the evidence base is the literature.",
        ))
        _run(tracker, RUN_1, [
            dict(eci, extraction=eci_extraction(), fit_by_tier=eci_fit),
            dict(eci, profile_id=ELEPHANT, extraction=eci_extraction(), fit_by_tier=_fit(
                elephant,
                (8, "An immunology congress; nothing on ecology or conservation."),
                (40, "A research venue, but for laboratory immunology rather than ecological synthesis."),
                (55, "Abstract format is feasible."),
            )),
            mongabay,
        ], {}, profiles)
        emails["1. First run (7 Oct 2026)"] = digest = render_digest(tracker, profiles, RUN_1.date())
        if stop_after_first_run:
            build.tracker = tracker
            return emails
        mark_sent(tracker, digest.receipt, RUN_1.date())

        # --- Run 2: update, tier alert, new month-only venue, conflict --------
        decisions = {"eci-2027": ResearcherDecision.APPLYING}
        _run(tracker, RUN_2, [
            dict(eci, extraction=eci_extraction(call_status="open")),
            dict(name="Africa Wildlife Corridors Policy Forum 2027", year=2027,
                 url="https://corridorsforum.org/2027/call", extraction=forum_extraction(), profile_id=ELEPHANT,
                 fit_by_tier=_fit(
                     elephant,
                     (90, "The forum's theme, transboundary corridors and land-use policy, is exactly the paper's subject."),
                     (30, "The forum wants actionable briefs for officials; the paper is a literature synthesis whose recommendations come last."),
                     (20, "A two-page brief is a large cut from a full paper and would mean rewriting, not condensing."),
                     tier=VenueTier.CORPORATE_INSTITUTIONAL, scored_at=RUN_2,
                 )),
            dict(name="Kyoto Treg Biology Symposium 2027", year=2027, url="https://treg-symposium.jp/2027",
                 extraction=symposium_extraction(), profile_id=IMMUNE, fit_by_tier=_fit(
                     immune,
                     (92, "A symposium dedicated to regulatory T cell biology, the paper's exact subject."),
                     (80, "A specialist research audience that expects the paper's kind of experimental evidence."),
                     (60, "Abstracts only; the paper would need condensing."),
                     scored_at=RUN_2,
                 )),
            dict(eci, url="https://easychair.org/cfp/ECI2027", extraction=eci_extraction(call_status="open")),
        ], decisions, profiles)
        emails["2. Update, Tier Alert, conflict (7 Nov 2026)"] = digest = render_digest(tracker, profiles, RUN_2.date())
        mark_sent(tracker, digest.receipt, RUN_2.date())

        # --- Run 3: quiet month (same inputs, nothing changes) ----------------
        _run(tracker, RUN_3, [
            dict(eci, extraction=eci_extraction(call_status="open")),
            dict(mongabay, fit_by_tier=None),
            dict(eci, url="https://easychair.org/cfp/ECI2027", extraction=eci_extraction(call_status="open")),
        ], decisions, profiles)
        emails["3. Quiet month (7 Dec 2026)"] = digest = render_digest(tracker, profiles, RUN_3.date())
        mark_sent(tracker, digest.receipt, RUN_3.date())

    # --- Week-before reminder: the daily watcher (step 10) logs the entry ----
    eci_entry = next(c for c in tracker.conferences if c.id == "eci-2027")
    eci_entry.matches[IMMUNE].notifications.append(NotificationEntry(
        date=REMINDER_DAY, reason=NotificationReason.WEEK_BEFORE,
        deadline=eci_entry.extraction.deadline, run_at=datetime.datetime(2027, 3, 24, 6, 0, tzinfo=UTC),
    ))
    emails["4. Week-before reminder, ECI 2027 (24 Mar 2027)"] = render_week_before(
        tracker, "eci-2027", profiles, REMINDER_DAY
    )
    build.tracker = tracker  # final state, for tests
    return emails


if __name__ == "__main__":
    for label, email in build().items():
        print("#" * 76)
        print(f"# {label}")
        print(f"# Subject: {email.subject}")
        print("#" * 76)
        print(email.body)
