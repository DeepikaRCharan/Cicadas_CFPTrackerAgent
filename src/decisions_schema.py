"""Pydantic schema for decisions.json.

Shape defined in docs/project-brief.md, "Data model > Researcher decisions
(decisions.json)". The file belongs to the researcher: the agent reads it at
the start of every run and never writes it (CLAUDE.md non-negotiables).
Keying is conference `id` -> one of the three researcher-settable states;
`pending` and `unconfirmed` are agent-only and are a validation error here
(brief, Rules > Ownership).
"""

from __future__ import annotations

from enum import Enum

from pydantic import RootModel


class ResearcherDecision(str, Enum):
    APPLYING = "applying"
    APPLIED = "applied"
    SKIPPED = "skipped"


class DecisionsFile(RootModel[dict[str, ResearcherDecision]]):
    """The whole of decisions.json: conference id -> researcher decision.

    Every other model in this project (src/tracker_schema.py,
    src/profile_schema.py) sets extra="forbid" so a stray field errors
    loudly instead of vanishing. A RootModel has no named fields for that
    setting to police (pydantic rejects `model_config['extra']` on a
    RootModel outright), so the equivalent guarantee here comes from
    ResearcherDecision's closed value set: a value outside
    applying/applied/skipped — including the agent-only "pending" or
    "unconfirmed" — is rejected exactly like an extra field would be.
    """


def find_untracked_ids(decisions: dict[str, ResearcherDecision], tracked_ids: set[str]) -> list[str]:
    """Every decisions.json key that isn't a tracked conference id.

    Brief: a key that isn't a tracked conference id is a WARNING in the run
    log naming that line, and the run continues -- the line is ignored,
    never guessed at. Returns the offending ids (sorted, for a stable
    message); src/tracker.py's sync_decisions turns each into that warning.
    """
    return sorted(set(decisions) - tracked_ids)
