"""Pydantic schema for paper profile files (profiles/<id>.json).

Shape defined in docs/project-brief.md, "Data model > Paper profiles". Up to
two profile files may be live at once — the loader refuses to run with more
(a deliberate cost cap), but that check belongs to the loader, not this file.

A profile is written once by the onboarding script and edited by hand for a
revision; nothing in the pipeline mutates it.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        ...,
        min_length=1,
        description="Short, stable identifier; also the filename stem (profiles/<id>.json).",
    )
    title: str = Field(..., min_length=1)
    abstract: str = Field(..., min_length=1)
    keywords: list[str] = Field(
        ...,
        min_length=1,
        description="Keywords/topics used to drive discovery search and classify.",
    )

    def content_hash(self) -> str:
        """Fingerprint of this profile's substantive content.

        Tracker entries store this as `profile_hash` at scoring time — both
        per-match (tracker_schema.TierFitBase.profile_hash) and on the
        top-level paper classification cache
        (tracker_schema.ProfileClassification.profile_hash). A mismatch on a
        later run means the profile was revised and the entry is stale and
        needs re-scoring/re-classifying — this is the mechanism behind
        "Revising the same paper (same id, updated content) changes
        profile_hash" in the brief.

        Deliberately excludes `id`: id is identity and never changes across
        a revision, so including it would tell the hash nothing new.
        """
        payload = json.dumps(
            {
                "title": self.title,
                "abstract": self.abstract,
                "keywords": sorted(self.keywords),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
