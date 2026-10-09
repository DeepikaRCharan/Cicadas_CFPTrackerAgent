"""A scripted discovery run that exercises every trace path -- grouped and
single reasons, a result arriving with the next response, a refused quote,
the two-refusal cap, both phases, an accepted candidate -- with no API calls.

tests/test_display_hook.py compares its output with a golden copy recorded
from the code before the display hook existed (commit 3eda423), to prove the
hook changes nothing when no listener is attached. To re-record (only if
discovery itself is deliberately changed):

    python -m tests.discovery_scenario > tests/fixtures/discovery_golden.json
"""

from __future__ import annotations

import contextlib
import io
import json
import sys

from src.discover import Limits, Trace, discover
from src.tracker import record_paper_classification
from src.tracker_schema import TrackerFile, VenueTier
from tests import render_fixtures as fx
from tests.test_discover import (
    ECI,
    PROBE_QUERIES,
    TODAY,
    FakeAPI,
    fetch,
    fetch_call,
    fetch_result,
    no_direct_fetch,
    response,
    search,
    text,
    tool,
)

OTHER_URL = "https://other.example/cfp"


def scenario_api() -> FakeAPI:
    bad_quote = dict(ECI, call_page_url=OTHER_URL, name="Other 2027", deadline_quote="Deadline: 1 April 2027")
    return FakeAPI(
        # probes: one reason over three searches, then a decision with no text before it
        response(text("Running the three probes, one per venue type."),
                 *[search(q, sid=f"p{i}") for i, q in enumerate(PROBE_QUERIES)],
                 tool("record_result", "r0", url="https://example.org/old", verdict="stale", reason="a 2019 event")),
        # a fetch whose result only arrives with the next response
        response(text("Fetching the other call page."), fetch_call(OTHER_URL, "late"),
                 tool("record_result", "r1", url=OTHER_URL, verdict="live_cfp", reason="looks open"), fetches=0),
        response(fetch_result(OTHER_URL, "late"), text("**Accepting** it."), tool("accept_candidate", "a1", **bad_quote),
                 fetches=1),
        response(tool("accept_candidate", "a2", **bad_quote)),
        response(tool("accept_candidate", "a3", **bad_quote)),  # capped: not evaluated
        # main phase
        response(text("Main: Foxp3 abstracts."), search("Foxp3 call for abstracts 2027", sid="m1"),
                 tool("check_tracker", "c1", name="ECI 2027", year=2027, url=ECI["call_page_url"])),
        response(text("Reading the ECI page."), fetch(ECI["call_page_url"], sid="f2"),
                 text("Accepting ECI 2027."), tool("accept_candidate", "a4", **ECI)),
        response(tool("finish", "fin", reason="main searches done")),
    )


def run_scenario(listener=None) -> dict:
    profile = fx.load_profiles()[fx.IMMUNE]
    tracker = TrackerFile()
    classification = fx._classification(profile, VenueTier.ACADEMIC_PRACTITIONER, ["a", "b", "c"], "a primary research paper")
    record_paper_classification(tracker, fx.IMMUNE, classification)
    trace = Trace("scenario")
    if listener is not None:
        trace.listener = listener
    api = scenario_api()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = discover(profile, classification, tracker, trace=trace, today=TODAY,
                          limits=Limits(probe_turns=5), call=api, fetch_text=no_direct_fetch)
    return {
        "markdown": trace.markdown(),
        "terminal": trace.terminal(),
        "tool_calls": dict(trace.tool_calls),
        "stdout": out.getvalue(),
        "result": {
            "candidates": [[c.name, c.year, c.call_page_url, c.deadline_quote, c.quote_page] for c in result.candidates],
            "stop_reason": result.stop_reason,
            "turns": result.turns, "searches": result.searches, "fetches": result.fetches,
            "probes_missing": [b.value for b in result.probes_missing],
            "cost": round(result.cost, 9),
            "judged": [[j.tool, j.label, j.outcome, j.reason, j.reply] for j in result.judged],
            "fetched_pages": sorted(result.fetched_text),
        },
        "requests": api.requests,
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(run_scenario(), indent=1, ensure_ascii=False) + "\n")
