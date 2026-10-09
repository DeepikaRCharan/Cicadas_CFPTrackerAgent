"""Tests for src/extract.py's handling of the model's answer. The model call
is scripted; no API calls.

Run with: python -m unittest tests/test_extract.py -v
"""

import contextlib
import datetime
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError

from src import extract
from src.tracker_schema import ExtractionFacts

# The model's record_extraction_facts input for CSEDU 2027 in rehearsal run
# 2026-10-09_052551 (logs/20261009T052640Z_model_response.json): fees and
# submission_opens are null, but missing_fields lists only fees.
CSEDU_INPUT = {
    "has_deadline": True,
    "deadline": "2026-11-17",
    "deadline_precision": "exact",
    "deadline_as_stated": "November 17, 2026",
    "submission_opens": None,
    "call_status": "open",
    "submission_type": "paper (regular or position paper) or abstract",
    "theme": "Computer Supported Education",
    "location": "Rome, Italy",
    "access": "open",
    "format_requirements": "Must be submitted through PRIMORIS platform following provided instructions and templates.",
    "fees": None,
    "mode": "in-person",
    "complete": True,
    "missing_fields": ["fees"],
}


def extract_with(tool_input):
    reply = NS(content=[NS(type="tool_use", name="record_extraction_facts", input=tool_input)], stop_reason="tool_use")
    out = io.StringIO()
    with mock.patch.object(extract, "call_llm", return_value=reply), \
            mock.patch.object(extract, "_write_log", return_value=Path("log.json")), contextlib.redirect_stdout(out):
        facts = extract.extract_facts_from_text("https://csedu.scitevents.org/CallForPapers.aspx", "page text",
                                                datetime.date(2026, 10, 9))
    return facts, out.getvalue()


class TestMissingFieldsComputedByCode(unittest.TestCase):
    def test_csedu_2027_model_list_off_by_one_is_corrected_not_rejected(self):
        with self.assertRaises(ValidationError):  # what lost CSEDU 2027: the model's list, validated as given
            ExtractionFacts.model_validate(CSEDU_INPUT)
        facts, out = extract_with(CSEDU_INPUT)
        self.assertEqual(facts.missing_fields, ["fees", "submission_opens"])
        self.assertTrue(facts.complete)  # only optional fields missing
        self.assertEqual(str(facts.deadline), "2026-11-17")
        self.assertIn("missing_fields set by code to ['fees', 'submission_opens'] (the model listed ['fees'])", out)

    def test_null_deadline_on_a_rolling_call_is_not_missing(self):
        rolling = dict(CSEDU_INPUT, has_deadline=False, deadline=None, deadline_precision=None,
                       deadline_as_stated=None, missing_fields=["deadline"])
        facts, _ = extract_with(rolling)
        self.assertNotIn("deadline", facts.missing_fields)

    def test_correct_list_passes_without_a_note(self):
        facts, out = extract_with(dict(CSEDU_INPUT, missing_fields=["submission_opens", "fees"]))
        self.assertEqual(facts.missing_fields, ["fees", "submission_opens"])
        self.assertNotIn("set by code", out)


class TestCompleteComputedByCode(unittest.TestCase):
    def test_complete_claimed_with_a_required_field_null_is_corrected(self):
        no_theme = dict(CSEDU_INPUT, theme=None)  # model still says complete=True
        with self.assertRaises(ValidationError):  # as given, this would lose the venue
            ExtractionFacts.model_validate(dict(no_theme, missing_fields=["fees", "submission_opens", "theme"]))
        facts, out = extract_with(no_theme)
        self.assertFalse(facts.complete)
        self.assertIn("theme", facts.missing_fields)
        self.assertIn("complete set by code to False (the model said True; required fields missing: ['theme'])", out)

    def test_incomplete_claimed_with_only_optional_gaps_is_corrected(self):
        facts, out = extract_with(dict(CSEDU_INPUT, complete=False))  # fees, submission_opens: optional only
        self.assertTrue(facts.complete)
        self.assertIn("complete set by code to True (the model said False; required fields missing: [])", out)

    def test_null_deadline_while_has_deadline_is_incomplete(self):
        facts, _ = extract_with(dict(CSEDU_INPUT, deadline=None, deadline_precision=None, deadline_as_stated=None))
        self.assertFalse(facts.complete)
        self.assertIn("deadline", facts.missing_fields)

    def test_correct_flag_passes_without_a_note(self):
        _, out = extract_with(CSEDU_INPUT)
        self.assertNotIn("complete set by code", out)


if __name__ == "__main__":
    unittest.main()
