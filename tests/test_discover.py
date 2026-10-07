"""Tests for src/discover.py -- build step 8: the discovery loop. Every
model response is scripted; no API calls.

Run with: python -m unittest tests/test_discover.py -v
"""

import contextlib
import datetime
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.discover import Limits, Trace, build_queries, discover, normalize_for_quote, probe_covered, quote_found
from src.tracker import record_paper_classification, upsert_venue
from src.tracker_schema import TrackerFile, VenueTier
from tests import render_fixtures as fx

TODAY = datetime.date(2026, 10, 1)
PROBE_QUERIES = ["Regulatory T cells call for speakers", "Regulatory T cells policy forum",
                 "Regulatory T cells open call for submissions"]
PAGE_TEXT = "Call for abstracts. Submission deadline: 31 March 2027. " * 10  # readable: > 300 characters
SINGLE_PHASE = dict(probe_reserve=0)  # no probe phase: one conversation, for tests of other rules


# --- scripted response builders ---------------------------------------------

def text(t):
    return NS(type="text", text=t)


def search(query, results=(("ECI 2027 call for abstracts", "https://eci2027.org/call-for-abstracts/"),), sid="s1"):
    return [
        NS(type="server_tool_use", id=sid, name="web_search", input={"query": query}),
        NS(type="web_search_tool_result", tool_use_id=sid,
           content=[NS(type="web_search_result", title=t, url=u, page_age=None) for t, u in results]),
    ]


def failed_search(query, sid="x1", code="max_uses_exceeded"):
    return [
        NS(type="server_tool_use", id=sid, name="web_search", input={"query": query}),
        NS(type="web_search_tool_result", tool_use_id=sid, content=NS(type="web_search_tool_result_error", error_code=code)),
    ]


def fetch(url, sid="f1", page=PAGE_TEXT):
    return [
        NS(type="server_tool_use", id=sid, name="web_fetch", input={"url": url}),
        NS(type="web_fetch_tool_result", tool_use_id=sid,
           content=NS(type="web_fetch_result", url=url,
                      content=NS(title="Call for abstracts", source=NS(type="text", data=page)))),
    ]


def tool(tool_name, tid="t1", **inp):
    return NS(type="tool_use", id=tid, name=tool_name, input=inp)


def response(*blocks, stop="tool_use", searches=None, fetches=None):
    flat = [b for item in blocks for b in (item if isinstance(item, list) else [item])]
    if searches is None:  # billed = searches that returned results
        ok = {b.tool_use_id for b in flat if b.type == "web_search_tool_result" and isinstance(b.content, list)}
        searches = sum(1 for b in flat if b.type == "server_tool_use" and b.name == "web_search" and b.id in ok)
    if fetches is None:
        fetches = sum(1 for b in flat if b.type == "server_tool_use" and b.name == "web_fetch")
    usage = NS(input_tokens=1000, output_tokens=100,
               server_tool_use=NS(web_search_requests=searches, web_fetch_requests=fetches))
    return NS(content=flat, stop_reason=stop, usage=usage)


def finish(reason="done", tid="fin"):
    return response(tool("finish", tid, reason=reason))


ECI = dict(name="ECI 2027", year=2027, call_page_url="https://eci2027.org/call-for-abstracts/",
           dates_page_url=None, deadline_evidence="on_call_page", deadline_quote="Submission deadline: 31 March 2027",
           reason="live call for immunology abstracts")


def probes_turn():
    return response(text("Probes, one per venue type."),
                    *[search(q, sid=f"p{i}") for i, q in enumerate(PROBE_QUERIES)],
                    tool("record_result", url="https://example.org", verdict="irrelevant", reason="no call"))


class FakeAPI:
    """Returns scripted responses in order and records what each request sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, **kwargs):
        first = kwargs["messages"][0]["content"]
        self.requests.append({
            "tools": [t.get("name") for t in kwargs["tools"]],
            "max_uses": {t["name"]: t.get("max_uses") for t in kwargs["tools"] if "type" in t},
            "last_role": kwargs["messages"][-1]["role"],
            "n_messages": len(kwargs["messages"]),
            "opening": first if isinstance(first, str) else "",
        })
        if not self.responses:
            raise AssertionError("loop asked for more responses than scripted")
        return self.responses.pop(0)


class DiscoverTestCase(unittest.TestCase):
    def setUp(self):
        self.profiles = fx.load_profiles()
        self.profile = self.profiles[fx.IMMUNE]
        self.tracker = TrackerFile()
        self.classification = fx._classification(
            self.profile, VenueTier.ACADEMIC_PRACTITIONER, ["a", "b", "c"], "a primary research paper")
        record_paper_classification(self.tracker, fx.IMMUNE, self.classification)
        self.trace = Trace("test trace")

    def run_loop(self, api, limits=None, model=None):
        kwargs = {"model": model} if model else {}
        with contextlib.redirect_stdout(io.StringIO()):
            return discover(self.profile, self.classification, self.tracker, trace=self.trace,
                            today=TODAY, limits=limits or Limits(), call=api, **kwargs)

    def seed_eci(self):
        with contextlib.redirect_stdout(io.StringIO()):
            upsert_venue(self.tracker, name="ECI 2027", year=2027, url=ECI["call_page_url"], extraction=fx.eci_extraction(),
                         profile_id=fx.IMMUNE, live_profile_ids={fx.IMMUNE}, now=fx.RUN_1,
                         fit_by_tier=fx._fit(self.profile, (90, "t"), (80, "g"), (70, "f")))

    def trace_text(self):
        return "\n".join(self.trace.lines)

    def turns_text(self):
        """The trace up to the Candidates section (which repeats code replies)."""
        return self.trace_text().split("## Candidates")[0]


class TestQueries(DiscoverTestCase):
    def test_seed_queries_from_profile(self):
        main, probes = build_queries(self.profile, self.classification, TODAY)
        # main seeds: current year, then next year; probes: no year
        self.assertEqual(main, ["Regulatory T cells call for papers 2026", "Foxp3 call for abstracts 2027"])
        self.assertEqual(set(probes), {VenueTier.INDUSTRY_TECHNICAL_MEDIA, VenueTier.CORPORATE_INSTITUTIONAL,
                                       VenueTier.OTHER_AVENUES})
        self.assertEqual(probes[VenueTier.CORPORATE_INSTITUTIONAL], "Regulatory T cells policy forum")
        for q in probes.values():
            self.assertNotRegex(q, r"\b20\d\d\b")

    def test_probe_counts_when_reworded_but_keeps_phrase(self):
        self.assertTrue(probe_covered(VenueTier.INDUSTRY_TECHNICAL_MEDIA, ["Treg immunology WRITE FOR US 2027"]))
        self.assertFalse(probe_covered(VenueTier.INDUSTRY_TECHNICAL_MEDIA, ["Treg immunology magazine"]))


class TestPhases(DiscoverTestCase):
    def happy_path(self):
        return FakeAPI(
            probes_turn(),
            finish("all probes judged"),
            response(text("Main: Foxp3 abstracts."), search("Foxp3 call for abstracts 2027"),
                     tool("check_tracker", name="ECI 2027", year=2027, url=ECI["call_page_url"])),
            response(fetch(ECI["call_page_url"]),
                     tool("record_result", url=ECI["call_page_url"], verdict="live_cfp", reason="open, deadline 31 March 2027"),
                     tool("accept_candidate", "t2", **ECI)),
            finish("main searches exhausted"),
        )

    def test_probes_then_main_as_separate_conversations(self):
        api = self.happy_path()
        result = self.run_loop(api)
        self.assertIn("PHASE 1 of 2 -- PROBES", api.requests[0]["opening"])
        self.assertNotIn("Foxp3 call for abstracts 2027", api.requests[0]["opening"])  # main seeds held back
        self.assertEqual(api.requests[0]["max_uses"]["web_search"], 3)  # exactly one search per probe
        self.assertIn("PHASE 2 of 2 -- MAIN SEARCHES", api.requests[2]["opening"])
        self.assertEqual(api.requests[2]["n_messages"], 1)  # fresh conversation
        self.assertIn("- searched: Regulatory T cells policy forum", api.requests[2]["opening"])  # phase 1 summary
        self.assertEqual(api.requests[2]["max_uses"]["web_search"], 3)  # what's left of 6
        self.assertEqual([c.name for c in result.candidates], ["ECI 2027"])
        self.assertEqual((result.searches, result.probes_missing), (4, []))
        self.assertTrue(result.stop_reason.startswith("finish: main searches exhausted"))
        trace = self.trace_text()
        for snippet in ("## Phase 1: probes", "## Phase 2: main searches", "web_search** `Foxp3 call for abstracts 2027`",
                        'Reason stated before calls #6–#7: "Main: Foxp3 abstracts."', "code: new: not tracked",
                        "accept_candidate** {", "- reason (in the call): live call for immunology abstracts", "probes phase finished: all probes judged"):
            self.assertIn(snippet, trace)

    def test_finish_before_probes_is_pushed_back_once(self):
        api = FakeAPI(finish("found enough"), probes_turn(), finish("probes done"), finish("main done"))
        result = self.run_loop(api)
        self.assertIn("probes not run yet", self.trace_text())
        self.assertEqual(result.probes_missing, [])

    def test_main_phase_skipped_when_probes_use_every_search(self):
        api = FakeAPI(probes_turn(), finish("probes done"))
        result = self.run_loop(api, Limits(searches=3))  # all three go to the probes
        self.assertEqual(result.stop_reason, "main phase skipped: no searches left after the probes")
        self.assertEqual(len(api.requests), 2)

    def test_missing_probes_warned_when_searches_run_out(self):
        api = FakeAPI(response(search(PROBE_QUERIES[0]), tool("finish", reason="out of searches")))
        result = self.run_loop(api, Limits(searches=1))
        self.assertEqual(len(result.probes_missing), 2)
        self.assertIn("WARNING: probes not run", self.trace_text())

    def test_refused_search_is_not_counted_or_credited_as_probe(self):
        api = FakeAPI(response(failed_search("Regulatory T cells policy forum"),
                               tool("record_result", url="u", verdict="stale", reason="r")),
                      finish("x"),  # pushed back: probes not run
                      finish("x"),  # accepted
                      finish("main done"))
        result = self.run_loop(api)
        self.assertEqual(result.searches, 0)
        self.assertIn(VenueTier.CORPORATE_INSTITUTIONAL, result.probes_missing)
        self.assertIn("search error: max_uses_exceeded", self.trace_text())


class TestDecisions(DiscoverTestCase):
    def test_tool_results_returned_in_one_user_message(self):
        api = FakeAPI(
            response(tool("record_result", "a", url="u1", verdict="stale", reason="2025 edition"),
                     tool("record_result", "b", url="u2", verdict="aggregator", reason="WikiCFP listing")),
            finish(),
        )
        self.run_loop(api, Limits(searches=0))
        self.assertEqual(api.requests[1]["last_role"], "user")
        self.assertEqual(api.requests[1]["n_messages"], 3)  # opening, assistant, one user turn with both results

    def test_live_cfp_refused_until_fetched_readable(self):
        url = ECI["call_page_url"]
        api = FakeAPI(
            response(tool("record_result", url=url, verdict="live_cfp", reason="snippet says abstracts open"),
                     tool("accept_candidate", "t2", **ECI)),
            response(fetch("http://www.eci2027.org/call-for-abstracts"),  # same page, different spelling
                     tool("record_result", url=url, verdict="live_cfp", reason="page confirms the call is open")),
            finish(),
        )
        result = self.run_loop(api, Limits(searches=0, fetches=2))
        trace = self.trace_text()
        self.assertIn("REFUSED by code: live_cfp refused", trace)
        self.assertIn("REFUSED by code: not accepted: the call page", trace)
        self.assertIn("- code: noted", trace)  # after the readable fetch, live_cfp is accepted
        self.assertEqual(result.candidates, [])
        self.assertIn("eci2027.org/call-for-abstracts", result.fetched_text)

    def test_unreadable_fetch_is_not_evidence(self):
        url = "https://www.mdpi.com/journal/ijms/special_issues/X"
        api = FakeAPI(
            response(fetch(url, page="Please enable JavaScript."),
                     tool("record_result", url=url, verdict="live_cfp", reason="snippet shows deadline 31 Oct"),
                     tool("accept_candidate", "t2", **dict(ECI, name="T-Cell Special Issue", call_page_url=url))),
            finish(),
        )
        result = self.run_loop(api, Limits(searches=0))
        trace = self.trace_text()
        self.assertIn("**UNREADABLE**", trace)
        self.assertEqual(self.turns_text().count("came back with no readable text"), 2)
        self.assertEqual(result.candidates, [])

    def test_dates_page_evidence_must_have_been_read(self):
        api = FakeAPI(response(fetch(ECI["call_page_url"]),
                               tool("accept_candidate", **dict(ECI, dates_page_url="https://eci2027.org/dates",
                                                               deadline_evidence="on_dates_page"))),
                      finish())
        result = self.run_loop(api, Limits(searches=0))
        self.assertIn("deadline_evidence is on_dates_page but https://eci2027.org/dates has not been fetched",
                      self.trace_text())
        self.assertEqual(result.candidates, [])

    def test_read_check_ignores_url_case(self):
        fetched = "https://www.keystonesymposia.org/conferences/meeting/j22027"
        cited = dict(ECI, name="T Cells 2027", call_page_url="https://www.keystonesymposia.org/conferences/meeting/J22027")
        api = FakeAPI(response(fetch(fetched),
                               tool("record_result", url=cited["call_page_url"], verdict="live_cfp", reason="open"),
                               tool("accept_candidate", "t2", **cited)),
                      finish())
        result = self.run_loop(api, Limits(searches=0))
        self.assertEqual([c.name for c in result.candidates], ["T Cells 2027"])
        self.assertNotIn("REFUSED", self.trace_text())

    def test_deadline_quote_must_appear_on_a_read_page(self):
        api = FakeAPI(response(fetch(ECI["call_page_url"]),
                               tool("accept_candidate", **dict(ECI, deadline_quote="Deadline: 15 January 2027"))),
                      finish())
        result = self.run_loop(api, Limits(searches=0))
        self.assertEqual(result.candidates, [])
        self.assertIn("does not appear on https://eci2027.org/call-for-abstracts/", self.trace_text())

    def test_deadline_quote_match_ignores_whitespace_and_case(self):
        api = FakeAPI(response(fetch(ECI["call_page_url"]),
                               tool("accept_candidate", **dict(ECI, deadline_quote="  SUBMISSION   deadline:\n31 march 2027 "))),
                      finish())
        result = self.run_loop(api, Limits(searches=0))
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].deadline_quote.split()[0], "SUBMISSION")

    def test_deadline_quote_may_come_from_the_dates_page(self):
        dates = "https://eci2027.org/dates"
        api = FakeAPI(response(fetch(ECI["call_page_url"], "f1", page="Call for abstracts is open now. " * 20),
                               fetch(dates, "f2"),
                               tool("accept_candidate", **dict(ECI, dates_page_url=dates, deadline_evidence="on_dates_page"))),
                      finish())
        result = self.run_loop(api, Limits(searches=0))
        self.assertEqual(len(result.candidates), 1)

    def test_empty_deadline_quote_refused(self):
        api = FakeAPI(response(fetch(ECI["call_page_url"]), tool("accept_candidate", **dict(ECI, deadline_quote=""))),
                      finish())
        result = self.run_loop(api, Limits(searches=0))
        self.assertEqual(result.candidates, [])
        self.assertIn("deadline_quote is empty", self.trace_text())

    def test_aggregator_refused(self):
        api = FakeAPI(
            response(tool("accept_candidate", **dict(ECI, call_page_url="http://www.wikicfp.com/cfp/servlet/event.showcfp?eventid=1"))),
            finish(),
        )
        result = self.run_loop(api, Limits(searches=0))
        self.assertEqual(result.candidates, [])
        self.assertIn("REFUSED by code: http://www.wikicfp.com", self.trace_text())

    def test_blocked_domain_refused_outright(self):
        blocked = "https://immunology.conferenceseries.com/call-for-abstracts.php"
        api = FakeAPI(
            response(fetch(blocked),
                     tool("record_result", url=blocked, verdict="live_cfp", reason="looks open"),
                     tool("accept_candidate", "t2", **dict(ECI, name="Global Summit on Immunology", call_page_url=blocked))),
            finish(),
        )
        result = self.run_loop(api, Limits(searches=0))
        trace = self.trace_text()
        self.assertEqual(self.turns_text().count("is on a blocked domain -- refused outright"), 2)
        self.assertNotIn("follow through", trace)
        self.assertEqual(result.candidates, [])

    def test_blocked_domains_passed_to_web_tools(self):
        api = FakeAPI(finish())
        seen = {}

        def capture(**kwargs):
            seen.update(kwargs)
            return api(**kwargs)
        self.run_loop(capture, Limits(**SINGLE_PHASE))
        for t in seen["tools"]:
            if "type" in t:
                self.assertIn("conferenceseries.com", t["blocked_domains"])

    def test_already_tracked_open_refused(self):
        self.seed_eci()
        api = FakeAPI(
            response(tool("check_tracker", name="ECI 2027", year=2027, url=ECI["call_page_url"]),
                     tool("accept_candidate", "t2", **ECI)),
            finish(),
        )
        result = self.run_loop(api, Limits(searches=0))
        self.assertEqual(result.candidates, [])
        self.assertIn("code: tracked_open", self.trace_text())
        self.assertIn("REFUSED by code: not accepted", self.trace_text())

    def test_conflict_accepted_and_marked(self):
        self.seed_eci()
        self.tracker.conferences[0].matches[fx.IMMUNE].decision = "skipped"
        other = dict(ECI, call_page_url="https://immunology-congress.example/eci2027")
        result = self.run_loop(FakeAPI(response(fetch(other["call_page_url"]), tool("accept_candidate", **other)),
                                       finish()), Limits(searches=0))
        self.assertEqual(len(result.candidates), 1)
        self.assertTrue(result.candidates[0].conflict)

    def test_bad_year_refused(self):
        api = FakeAPI(response(tool("accept_candidate", **dict(ECI, year=27))), finish())
        result = self.run_loop(api, Limits(searches=0))
        self.assertEqual(result.candidates, [])
        self.assertIn("is not an edition year", self.trace_text())


class TestLimits(DiscoverTestCase):
    def test_spent_tool_stays_listed_with_constant_max_uses(self):
        url = ECI["call_page_url"]
        api = FakeAPI(
            response(search("q1", sid="a"), search("q2", sid="b"), tool("record_result", url="u", verdict="stale", reason="r")),
            response(fetch(url), tool("record_result", url=url, verdict="live_cfp", reason="r")),
            finish(),
        )
        self.run_loop(api, Limits(searches=2, fetches=3, **SINGLE_PHASE))
        # used-in-exchange + left = the exchange total: identical every turn, so the cache survives
        for request in api.requests:
            self.assertEqual(request["max_uses"], {"web_search": 2, "web_fetch": 3})
        self.assertIn("LIMIT: search limit (2) reached -- further web_search uses are refused", self.trace_text())

    def test_nudge_restarts_the_exchange_count(self):
        api = FakeAPI(response(search("q1"), text("pausing"), stop="end_turn"), finish())
        self.run_loop(api, Limits(searches=3, fetches=1, **SINGLE_PHASE))
        self.assertEqual(api.requests[0]["max_uses"], {"web_search": 3, "web_fetch": 1})
        self.assertEqual(api.requests[1]["max_uses"], {"web_search": 2, "web_fetch": 1})  # after the nudge: just what's left

    def test_usage_report_is_authoritative_for_searches(self):
        # dynamic filtering: searches ran from server-side code, no separate blocks
        api = FakeAPI(response(text("filtered"), tool("record_result", url="u", verdict="stale", reason="r"), searches=3),
                      finish())
        result = self.run_loop(api, Limits(searches=3, **SINGLE_PHASE))
        self.assertEqual(result.searches, 3)
        self.assertEqual(api.requests[1]["max_uses"]["web_search"], 3)  # spent, still listed

    def test_loop_cost_limit_stops_before_a_turn_that_could_exceed_it(self):
        first = response(tool("record_result", url="u", verdict="stale", reason="r"))
        first.usage = NS(input_tokens=60_000, output_tokens=1_000, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                         server_tool_use=NS(web_search_requests=0, web_fetch_requests=0))  # Sonnet 5: about $0.13
        api = FakeAPI(first)
        result = self.run_loop(api, Limits(searches=0, cost_usd=0.15), model="claude-sonnet-5")
        self.assertEqual(len(api.requests), 1)  # $0.13 spent + a $0.13 turn would pass $0.15: stop now
        self.assertIn("another turn could exceed it", result.stop_reason)
        self.assertLess(result.cost, 0.15)

    def test_projection_uses_budget_left_not_largest_turn(self):
        # one expensive turn (big output), but no searches or fetches left and a small
        # context: the next turn is cheap, so the loop carries on
        first = response(tool("record_result", url="u", verdict="stale", reason="r"))
        first.usage = NS(input_tokens=5_000, output_tokens=10_000, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                         server_tool_use=NS(web_search_requests=0, web_fetch_requests=0))  # Sonnet 5: about $0.11
        api = FakeAPI(first, finish())
        result = self.run_loop(api, Limits(searches=0, fetches=0, cost_usd=0.15), model="claude-sonnet-5")
        self.assertEqual(len(api.requests), 2)
        self.assertTrue(result.stop_reason.startswith("finish:"))

    def test_projection_counts_searches_still_allowed(self):
        first = response(tool("record_result", url="u", verdict="stale", reason="r"))
        first.usage = NS(input_tokens=5_000, output_tokens=10_000, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                         server_tool_use=NS(web_search_requests=0, web_fetch_requests=0))
        api = FakeAPI(first, finish())
        result = self.run_loop(api, Limits(searches=2, fetches=0, cost_usd=0.15, **SINGLE_PHASE), model="claude-sonnet-5")
        self.assertEqual(len(api.requests), 1)  # 2 searches still allowed: ~$0.07 more could pass $0.15
        self.assertIn("2 searches and 0 fetches left", result.stop_reason)

    def test_turn_limit_stops_cleanly(self):
        api = FakeAPI(*[response(tool("record_result", url=f"u{i}", verdict="stale", reason="r")) for i in range(3)])
        result = self.run_loop(api, Limits(turns=3, searches=0))
        self.assertEqual(result.stop_reason, "LIMIT: turn limit (3) reached")

    def test_candidate_limit_stops_cleanly(self):
        second = dict(ECI, name="Treg Symposium 2027", call_page_url="https://treg-symposium.jp/2027")
        api = FakeAPI(response(fetch(ECI["call_page_url"], "f1"), fetch(second["call_page_url"], "f2"),
                               tool("accept_candidate", "a", **ECI), tool("accept_candidate", "b", **second)))
        result = self.run_loop(api, Limits(candidates=2, searches=0))
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.stop_reason, "LIMIT: candidate limit (2) reached")

    def test_pause_turn_resumed_without_extra_user_message(self):
        api = FakeAPI(response(search("Foxp3 call for abstracts"), stop="pause_turn"), finish())
        self.run_loop(api, Limits(searches=1, **SINGLE_PHASE))
        self.assertEqual(api.requests[1]["last_role"], "assistant")

    def test_end_turn_without_finish_nudged_once_then_stops(self):
        api = FakeAPI(response(text("I think that's all."), stop="end_turn"),
                      response(text("Done."), stop="end_turn"))
        result = self.run_loop(api, Limits(searches=0))
        self.assertIn("without calling finish", result.stop_reason)
        self.assertEqual(api.requests[1]["last_role"], "user")

    def test_requests_ask_for_prompt_caching(self):
        seen = {}

        def api(**kwargs):
            seen.update(kwargs)
            return finish()
        self.run_loop(api, Limits(searches=0))
        self.assertEqual(seen["cache_control"], {"type": "ephemeral"})


class TestModels(DiscoverTestCase):
    def test_haiku_gets_basic_web_tool_versions(self):
        seen = {}

        def api(**kwargs):
            seen.update(kwargs)
            return finish()
        self.run_loop(api, Limits(**SINGLE_PHASE), model="claude-haiku-4-5-20251001")
        types = {t["name"]: t["type"] for t in seen["tools"] if "type" in t}
        self.assertEqual(types, {"web_search": "web_search_20250305", "web_fetch": "web_fetch_20250910"})
        self.assertEqual(seen["model"], "claude-haiku-4-5-20251001")

    def test_unconfigured_model_refused(self):
        with self.assertRaises(ValueError):
            discover(self.profile, self.classification, self.tracker, trace=self.trace, today=TODAY,
                     model="claude-unknown", call=lambda **k: None)


class TestTraceFile(DiscoverTestCase):
    def test_trace_saved_as_markdown(self):
        self.run_loop(FakeAPI(finish()), Limits(searches=0))
        with tempfile.TemporaryDirectory() as d:
            path = self.trace.save(Path(d) / "sub" / "t.md")
            body = path.read_text(encoding="utf-8")
        self.assertTrue(body.startswith("# test trace"))
        self.assertIn("## Turn 1", body)
        self.assertIn("## Stopped", body)


class TestTraceLayout(DiscoverTestCase):
    """Step 9 trace layout: numbered calls in order, the model's own stated
    reason (once over a group when one line preceded several calls), one-line
    result summaries, <details> lists only outside the terminal."""

    def run_eci(self):
        api = FakeAPI(
            response(text("Looking for immunology congress calls.\nSearching the **ECI call** first."),
                     search("ECI 2027 call for abstracts", results=[("ECI 2027 call for abstracts", ECI["call_page_url"]),
                                                                    ("Some <b>listing</b>", "https://example.org/x")]),
                     search("immunology congress 2027", sid="s2"),
                     text("Fetching the ECI call page to read the deadline."),
                     fetch(ECI["call_page_url"]),
                     text("**Checking the tracker:**"),
                     tool("check_tracker", "c1", name="ECI 2027", year=2027, url=ECI["call_page_url"])),
            response(text("The page shows an open call."),
                     tool("record_result", "r1", url=ECI["call_page_url"], verdict="live_cfp", reason="open, deadline 31 March 2027"),
                     tool("record_result", "r2", url="https://example.org/x", verdict="aggregator", reason="a listing"),
                     text("Accepting ECI 2027."),
                     tool("accept_candidate", "a1", **ECI)),
            finish("done"))
        self.run_loop(api, Limits(**SINGLE_PHASE))

    def test_one_reason_over_a_group_labelled_with_call_numbers(self):
        self.run_eci()
        lines = self.trace.lines
        header = lines.index('Reason stated before calls #1–#2: "Searching the ECI call first."')
        self.assertEqual(lines[header + 2], "**#1 web_search** `ECI 2027 call for abstracts`")
        self.assertNotIn("- reason (before the call)", lines[header + 3])  # not repeated per call
        self.assertIn('Reason stated before calls #5–#6: "The page shows an open call."', lines)

    def test_single_call_keeps_its_reason_inline(self):
        self.run_eci()
        lines = self.trace.lines
        i = lines.index(f"**#3 web_fetch** {ECI['call_page_url']}")
        self.assertEqual(lines[i + 1], '- reason (before the call): "Fetching the ECI call page to read the deadline."')
        i = next(n for n, line in enumerate(lines) if line.startswith("**#7 accept_candidate**"))
        self.assertEqual(lines[i + 1], '- reason (before the call): "Accepting ECI 2027."')
        self.assertEqual(lines[i + 2], "- reason (in the call): live call for immunology abstracts")

    def test_stray_markdown_emphasis_stripped_from_reasons(self):
        self.run_eci()
        i = next(n for n, line in enumerate(self.trace.lines) if line.startswith("**#4 check_tracker**"))
        self.assertEqual(self.trace.lines[i + 1], '- reason (before the call): "Checking the tracker:"')
        self.assertNotIn('**"', self.turns_text())
        self.assertNotIn("the **ECI", self.turns_text().split("<details>")[0])

    def test_calls_without_any_text_say_so(self):
        api = FakeAPI(response(search("q1"), search("q2", sid="s2"), tool("check_tracker", "c1", name="X", year=2027, url="https://x.org")),
                      response(tool("finish", "f1", reason="done")))
        self.run_loop(api, Limits(**SINGLE_PHASE))
        self.assertIn("(no reason stated before calls #1–#3)", self.trace.lines)
        i = next(n for n, line in enumerate(self.trace.lines) if line.startswith("**#4 finish**"))
        self.assertEqual(self.trace.lines[i + 1], "- reason (before the call): (no reason stated before this call)")

    def test_decision_reply_lands_under_its_own_call_in_order(self):
        self.run_eci()
        text = self.turns_text()
        self.assertIn('**#5 record_result** {"url": "https://eci2027.org/call-for-abstracts/", "verdict": "live_cfp"}\n'
                      "- reason (in the call): open, deadline 31 March 2027\n- code: noted", text)
        self.assertLess(text.index("**#5 record_result**"), text.index("**#6 record_result**"))
        self.assertLess(text.index("**#6 record_result**"), text.index("**#7 accept_candidate**"))
        self.assertNotIn("(not handled)", text)

    def test_one_line_result_summaries_logged_from_results(self):
        self.run_eci()
        text = self.turns_text()
        self.assertIn("- result: 2 result(s); top: ECI 2027 call for abstracts — https://eci2027.org/call-for-abstracts/", text)
        self.assertIn("- result: readable, 559 characters — Call for abstracts — https://eci2027.org/call-for-abstracts/", text)

    def test_details_in_markdown_only(self):
        self.run_eci()
        md, term = self.trace.markdown(), self.trace.terminal()
        self.assertIn("<details><summary>All 2 results</summary>", md)
        self.assertIn("- Some &lt;b&gt;listing&lt;/b&gt; — https://example.org/x", md)
        self.assertIn("<details><summary>Model&#x27;s full text this turn</summary>", md)
        for raw in ("<details>", "</details>", "<summary>", "Some <b>listing", "&lt;"):
            self.assertNotIn(raw, term)
        self.assertIn("- result: 2 result(s); top:", term)
        self.assertEqual(md.count("<details>"), md.count("</details>"))

    def test_candidates_section_with_verified_quote(self):
        self.run_eci()
        cands = self.trace_text().split("## Candidates")[1]
        self.assertIn("- **JUDGED LIVE**: https://eci2027.org/call-for-abstracts/", cands)
        self.assertIn("- **REJECTED (aggregator)**: https://example.org/x", cands)
        self.assertIn("- **ACCEPTED**: ECI 2027 (2027) — https://eci2027.org/call-for-abstracts/", cands)
        self.assertIn('deadline quote verified on https://eci2027.org/call-for-abstracts/: "Submission deadline: 31 March 2027"', cands)

    def test_refused_accept_listed_as_refused(self):
        bad = dict(ECI, deadline_quote="Deadline: 1 April 2027")
        api = FakeAPI(response(search("q"), fetch(ECI["call_page_url"]), tool("accept_candidate", "a1", **bad)),
                      finish("done"))
        self.run_loop(api, Limits(**SINGLE_PHASE))
        cands = self.trace_text().split("## Candidates")[1]
        self.assertIn("- **REFUSED BY CODE (accept_candidate)**: ECI 2027 (2027)", cands)
        self.assertIn("does not appear on", cands)

    def test_tool_call_counts(self):
        self.run_eci()
        self.assertEqual(dict(self.trace.tool_calls), {"web_search": 2, "web_fetch": 1, "check_tracker": 1, "record_result": 2,
                                                       "accept_candidate": 1, "finish": 1})

    def test_header_has_paper_bucket_cues_and_probe_caps(self):
        self.run_eci()
        head = self.trace_text().split("## ")[0]
        self.assertIn("Read as: Academic & Practitioner", head)
        self.assertIn("Cues that drove that call:\n- a\n- b\n- c", head)
        self.assertIn("Probe phase capped at 4 turns, 2 fetches and $0.110 spend; the rest is reserved for the main searches", head)

    def test_prompt_asks_for_a_line_before_every_call(self):
        from src.discover import SYSTEM_PROMPT
        self.assertIn("Before each tool call", SYSTEM_PROMPT)


def fetch_call(url, sid):
    """A web_fetch call whose result doesn't come back in the same response."""
    return NS(type="server_tool_use", id=sid, name="web_fetch", input={"url": url})


def fetch_result(url, sid, page=PAGE_TEXT):
    return NS(type="web_fetch_tool_result", tool_use_id=sid,
              content=NS(type="web_fetch_result", url=url, content=NS(title="Call for abstracts", source=NS(type="text", data=page))))


def search_call(query, sid):
    return NS(type="server_tool_use", id=sid, name="web_search", input={"query": query})


def search_result(sid, results=(("ECI 2027 call for abstracts", "https://eci2027.org/call-for-abstracts/"),)):
    return NS(type="web_search_tool_result", tool_use_id=sid,
              content=[NS(type="web_search_result", title=t, url=u, page_age=None) for t, u in results])


class TestResultsInTheNextResponse(DiscoverTestCase):
    """Fix #1: a search or fetch made just before a decision-tool call gets
    its result at the start of the next response."""

    def aeet_case(self):
        url = ECI["call_page_url"]
        return FakeAPI(
            response(search("q"), text("Fetching the call page."), fetch_call(url, "late"),
                     tool("record_result", "r1", url=url, verdict="live_cfp", reason="looks open"), fetches=0),
            response(fetch_result(url, "late"), text("Read it; accepting."), tool("accept_candidate", "a1", **ECI), fetches=1),
            finish("done"))

    def test_late_fetch_result_counts_as_read_so_accept_succeeds(self):
        result = self.run_loop(self.aeet_case(), Limits(**SINGLE_PHASE))
        self.assertEqual([c.name for c in result.candidates], ["ECI 2027"])
        self.assertEqual(result.fetched_text.get("eci2027.org/call-for-abstracts"), PAGE_TEXT)
        text = self.turns_text()
        # before the result arrived, live_cfp was rightly refused (not read yet)
        self.assertIn("REFUSED by code: live_cfp refused", text)
        self.assertIn("- code: accepted as 'eci-2027' (new)", text)

    def test_trace_fills_the_result_in_under_the_original_call(self):
        self.run_loop(self.aeet_case(), Limits(**SINGLE_PHASE))
        lines = self.trace.lines
        i = lines.index(f"**#2 web_fetch** {ECI['call_page_url']}")
        self.assertEqual(lines[i + 1], "- result: readable, 559 characters — Call for abstracts — "
                                       "https://eci2027.org/call-for-abstracts/ (arrived with the next response)")
        self.assertNotIn("waiting for the next one", self.trace_text())

    def test_late_search_result_credits_the_probe_and_shows_results(self):
        api = FakeAPI(
            response(search_call(PROBE_QUERIES[0], "p0"), tool("record_result", "r0", url="u", verdict="stale", reason="r"),
                     searches=0),
            response(search_result("p0"), *[search(q, sid=f"p{i}") for i, q in enumerate(PROBE_QUERIES[1:], 1)],
                     tool("finish", "f1", reason="probes done"), searches=3),
            finish("main done"))
        result = self.run_loop(api)
        self.assertEqual(result.probes_missing, [])
        lines = self.trace.lines
        i = lines.index(f"**#1 web_search** `{PROBE_QUERIES[0]}`")
        self.assertTrue(lines[i + 1].startswith("- result: 1 result(s); top: ECI 2027"), lines[i + 1])
        self.assertTrue(lines[i + 1].endswith("(arrived with the next response)"))
        self.assertIn("<details><summary>All 1 results</summary>", lines[i + 2:i + 5])

    def test_result_that_never_arrives_says_so_at_phase_end(self):
        api = FakeAPI(response(fetch_call(ECI["call_page_url"], "lost"), tool("finish", "f1", reason="done"), fetches=0))
        self.run_loop(api, Limits(**SINGLE_PHASE))
        self.assertIn("- result: (no result returned before this phase ended)", self.trace.lines)
        self.assertNotIn("waiting for the next one", self.trace_text())


def costly(resp, input_tokens):
    """Make a scripted response cost more: input tokens at list price."""
    resp.usage.input_tokens = input_tokens
    return resp


class TestMainPhaseReserve(DiscoverTestCase):
    """The probe phase has its own caps, so it can never starve the main phase."""

    def test_probe_phase_ends_at_its_turn_cap_and_main_still_runs(self):
        stuck = [response(tool("record_result", f"r{i}", url=f"https://x{i}.org", verdict="stale", reason="old"))
                 for i in range(4)]
        api = FakeAPI(probes_turn(), *stuck[:3], finish("main done"))
        self.run_loop(api, Limits(probe_turns=4))
        self.assertIn("PHASE 2 of 2 -- MAIN SEARCHES", api.requests[4]["opening"])
        self.assertIn("probe phase budget reached (4 turns) -- the main searches keep their reserved budget", self.trace_text())

    def test_probe_phase_fetches_capped(self):
        api = FakeAPI(probes_turn(), finish("probes done"), finish("main done"))
        self.run_loop(api, Limits(fetches=4, probe_fetches=2))
        self.assertEqual(api.requests[0]["max_uses"]["web_fetch"], 2)
        self.assertEqual(api.requests[2]["max_uses"]["web_fetch"], 4)  # main: everything left

    def test_expensive_probes_stop_at_their_share_and_main_gets_a_turn(self):
        # a probe turn costing about $0.08 leaves no room under the $0.11 share for another
        api = FakeAPI(costly(probes_turn(), 50_000),
                      costly(response(tool("record_result", "r1", url="https://y.org", verdict="stale", reason="old")), 50_000))
        result = self.run_loop(api)
        self.assertIn("probe phase budget reached ($0.110 spend", self.trace_text())
        self.assertEqual(["PHASE 2 of 2" in r["opening"] for r in api.requests], [False, True])
        self.assertTrue(result.stop_reason.startswith("LIMIT: loop cost limit"), result.stop_reason)

    def test_main_phase_never_skipped_for_cost_after_probes(self):
        # one probe turn far over its projection: main still gets its first turn
        api = FakeAPI(costly(probes_turn(), 100_000), finish("main done"))
        result = self.run_loop(api)
        self.assertIn("PHASE 2 of 2", api.requests[1]["opening"])
        self.assertIn("main phase's first turn runs despite the loop cost limit", self.trace_text())
        self.assertTrue(result.stop_reason.startswith("finish: main done"))

    def test_main_phase_cost_limit_still_applies_after_its_first_turn(self):
        api = FakeAPI(costly(probes_turn(), 100_000),
                      costly(response(search("Foxp3 call for abstracts 2027", sid="m1"),
                                      tool("record_result", "r1", url="https://y.org", verdict="stale", reason="old")), 100_000))
        result = self.run_loop(api)
        self.assertTrue(result.stop_reason.startswith("LIMIT: loop cost limit"), result.stop_reason)
        self.assertEqual(len(api.requests), 2)


class TestProbeNearVariants(unittest.TestCase):
    def test_dropped_filler_word_counts(self):
        self.assertTrue(probe_covered(VenueTier.OTHER_AVENUES, ["Spatial Ecology open call submissions 2026 2027"]))

    def test_plural_and_order_variants_count(self):
        self.assertTrue(probe_covered(VenueTier.CORPORATE_INSTITUTIONAL, ["Spatial ecology policy forums 2027"]))
        self.assertTrue(probe_covered(VenueTier.CORPORATE_INSTITUTIONAL, ["invited submissions spatial ecology"]))
        self.assertTrue(probe_covered(VenueTier.INDUSTRY_TECHNICAL_MEDIA, ["Treg call for speaker"]))
        self.assertTrue(probe_covered(VenueTier.INDUSTRY_TECHNICAL_MEDIA, ["Treg calls for presentations"]))

    def test_missing_content_word_does_not_count(self):
        self.assertFalse(probe_covered(VenueTier.OTHER_AVENUES, ["Spatial Ecology call for submissions"]))  # no "open"
        self.assertFalse(probe_covered(VenueTier.CORPORATE_INSTITUTIONAL, ["spatial ecology policy"]))
        self.assertFalse(probe_covered(VenueTier.INDUSTRY_TECHNICAL_MEDIA, ["Treg immunology magazine"]))
        self.assertFalse(probe_covered(VenueTier.INDUSTRY_TECHNICAL_MEDIA, ["Treg write-ups"]))  # not "write for us"

    def test_main_seed_does_not_credit_a_probe(self):
        self.assertFalse(probe_covered(VenueTier.OTHER_AVENUES, ["Regulatory T cells call for papers 2026"]))


class TestTrackedClosedByDeadline(DiscoverTestCase):
    """A venue tracked under another paper is 'closed' only once its deadline
    has passed -- not because an old extraction stored call_status closed."""

    def seed_other_paper(self, deadline, deadline_as_stated):
        elephant = self.profiles[fx.ELEPHANT]
        record_paper_classification(self.tracker, fx.ELEPHANT, fx._classification(
            elephant, VenueTier.ACADEMIC_PRACTITIONER, ["a", "b", "c"], "r"))
        ext = fx.eci_extraction(call_status="closed").model_copy(update={"deadline": deadline, "deadline_as_stated": deadline_as_stated})
        with contextlib.redirect_stdout(io.StringIO()):
            upsert_venue(self.tracker, name="ECI 2027", year=2027, url=ECI["call_page_url"], extraction=ext,
                         profile_id=fx.ELEPHANT, live_profile_ids={fx.ELEPHANT, fx.IMMUNE}, now=fx.RUN_1,
                         fit_by_tier=fx._fit(elephant, (10, "t"), (10, "g"), (10, "f")))

    def check(self):
        api = FakeAPI(response(tool("check_tracker", "c1", name="ECI 2027", year=2027, url=ECI["call_page_url"])), finish("done"))
        self.run_loop(api, Limits(**SINGLE_PHASE))
        return self.turns_text()

    def test_stored_closed_with_deadline_ahead_may_be_accepted(self):
        self.seed_other_paper(datetime.date(2026, 10, 1), "1 October 2026")  # TODAY is 2026-10-01: deadline day
        self.assertIn("code: tracked_other_paper", self.check())

    def test_deadline_passed_is_closed(self):
        self.seed_other_paper(datetime.date(2026, 9, 30), "30 September 2026")
        self.assertIn("code: tracked_closed", self.check())


SER_URL = "https://ser2027.org/call-for-proposals/"
SER_FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "ser2027_call_page.txt").read_text(encoding="utf-8")
SER_QUOTE = ("All proposals must be submitted through the conference submission portal and must be received "
             "no later than 7 October 2026 at 23:59:59 UTC.")
# the same sentence as a fetch tool may render it: a markdown link in the middle, emphasis, a non-breaking space
SER_FETCHED = ("## Proposal Submission Guidelines\n\nAll proposals must be submitted through the "
               "[conference submission portal](https://ser2027.org/portal/) and must be received\nno later than "
               "**7 October 2026** at 23:59:59 UTC. Early submissions are highly encouraged. ") * 3


class TestQuoteVerification(unittest.TestCase):
    def test_long_quote_found_on_the_saved_ser2027_page(self):
        self.assertTrue(quote_found(SER_QUOTE, SER_FIXTURE))
        longer = SER_QUOTE + " Early submissions are highly encouraged, and reviews will be conducted on a rolling basis."
        self.assertTrue(quote_found(longer, SER_FIXTURE))

    def test_link_in_the_middle_of_the_sentence_does_not_block_it(self):
        self.assertTrue(quote_found(SER_QUOTE, SER_FETCHED))
        self.assertTrue(quote_found(SER_QUOTE, SER_FETCHED.replace(
            "[conference submission portal](https://ser2027.org/portal/)",
            '<a href="https://ser2027.org/portal/">conference submission portal</a>')))

    def test_quotes_dashes_and_spacing_unified(self):
        page = "Abstracts due “no later than” 31 March 2027 – 23:59 CET"
        self.assertTrue(quote_found('Abstracts due "no later than" 31 March 2027 - 23:59 CET', page))
        self.assertTrue(quote_found("abstracts  due “no later than”\n31 march 2027 — 23:59 cet", page))

    def test_words_must_still_be_on_the_page(self):
        self.assertFalse(quote_found(SER_QUOTE.replace("7 October", "8 October"), SER_FETCHED))
        self.assertFalse(quote_found("must be received no later than 7 Oct", SER_FETCHED))  # whole words only
        self.assertFalse(quote_found("submitted through the portal and must be received", SER_FETCHED))  # words dropped
        self.assertFalse(quote_found("", SER_FETCHED))
        self.assertFalse(quote_found("**  **", SER_FETCHED))

    def test_normalize_keeps_the_words(self):
        self.assertEqual(normalize_for_quote("See [the  `portal`](https://x.org) – **now**"), "see the portal - now")


class TestQuoteInTheLoop(DiscoverTestCase):
    def test_accepts_the_full_sentence_against_a_page_with_link_markup(self):
        ser = dict(name="SER2027", year=2027, call_page_url=SER_URL, dates_page_url=None,
                   deadline_evidence="on_call_page", deadline_quote=SER_QUOTE, reason="open symposium call")
        api = FakeAPI(response(fetch(SER_URL, page=SER_FETCHED), tool("accept_candidate", "a1", **ser)), finish("done"))
        result = self.run_loop(api, Limits(**SINGLE_PHASE))
        self.assertEqual([c.name for c in result.candidates], ["SER2027"])


class TestAcceptRefusalCap(DiscoverTestCase):
    def test_two_refusals_then_recorded_and_not_evaluated_again(self):
        bad = dict(ECI, deadline_quote="Deadline: 1 April 2027")
        good = dict(ECI)  # would pass -- but the candidate is already recorded as refused
        api = FakeAPI(response(fetch(ECI["call_page_url"]), tool("accept_candidate", "a1", **bad)),
                      response(tool("accept_candidate", "a2", **bad)),
                      response(tool("accept_candidate", "a3", **good)),
                      finish("done"))
        result = self.run_loop(api, Limits(**SINGLE_PHASE))
        self.assertEqual(result.candidates, [])
        text = self.turns_text()
        self.assertEqual(text.count("does not appear on"), 2)
        self.assertIn("refused 2 times: recorded as refused, and further attempts on this call page won't be evaluated", text)
        self.assertIn("already refused 2 times and is recorded as refused -- not evaluated again", text)
        self.assertEqual([j.outcome for j in result.judged], ["refused", "refused", "refused"])

    def test_refusals_counted_per_call_page(self):
        other = dict(ECI, call_page_url="https://other.org/cfp", name="Other 2027", deadline_quote="nope")
        api = FakeAPI(response(fetch(ECI["call_page_url"]), fetch("https://other.org/cfp", sid="f2"),
                               tool("accept_candidate", "a1", **dict(ECI, deadline_quote="nope")),
                               tool("accept_candidate", "a2", **other),
                               tool("accept_candidate", "a3", **ECI)),
                      finish("done"))
        result = self.run_loop(api, Limits(**SINGLE_PHASE))
        self.assertEqual([c.name for c in result.candidates], ["ECI 2027"])  # one refusal for ECI: still evaluated


class TestFetchLogs(DiscoverTestCase):
    def test_every_loop_fetch_saved_with_its_page_text(self):
        with tempfile.TemporaryDirectory() as d:
            api = FakeAPI(response(fetch(SER_URL, page=SER_FETCHED), fetch("https://x.org/gone", sid="f2"),
                                   tool("check_tracker", "c1", name="SER2027", year=2027, url=SER_URL)),
                          finish("done"))
            api.responses[0].content[3].content = NS(type="web_fetch_tool_result_error", error_code="url_not_accessible")
            with contextlib.redirect_stdout(io.StringIO()):
                discover(self.profile, self.classification, self.tracker, trace=self.trace, today=TODAY,
                         limits=Limits(**SINGLE_PHASE), call=api, log_dir=d)
            files = sorted(Path(d).glob("*_loop_fetch_*.txt"))
            self.assertEqual(len(files), 2)
            first = files[0].read_text(encoding="utf-8")
            self.assertIn(f"URL requested: {SER_URL}", first)
            self.assertIn("[conference submission portal](https://ser2027.org/portal/)", first)  # as the model saw it
            self.assertIn("Fetch error: url_not_accessible", files[1].read_text(encoding="utf-8"))

    def test_no_log_dir_writes_nothing(self):
        logs = Path(__file__).resolve().parent.parent / "logs"
        before = set(logs.glob("*_loop_fetch_*")) if logs.exists() else set()
        api = FakeAPI(response(fetch(SER_URL), tool("finish", "f1", reason="done")))
        self.run_loop(api, Limits(**SINGLE_PHASE))  # log_dir defaults to None
        after = set(logs.glob("*_loop_fetch_*")) if logs.exists() else set()
        self.assertEqual(after, before)


class TestCheckTrackerAlias(DiscoverTestCase):
    def test_name_variant_is_the_tracked_venue_not_a_conflict(self):
        self.seed_eci()
        api = FakeAPI(response(tool("check_tracker", "c1", name="European Congress of Immunology", year=2027,
                                    url=ECI["call_page_url"])), finish("done"))
        self.run_loop(api, Limits(**SINGLE_PHASE))
        text = self.turns_text()
        self.assertIn("code: tracked_open", text)
        self.assertIn("same URL and year, so the same venue; this name is kept as an alias", text)
        self.assertNotIn("conflict", text)


if __name__ == "__main__":
    unittest.main()
