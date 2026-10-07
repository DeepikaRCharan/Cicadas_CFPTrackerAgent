"""Tests for src/scratch.py -- the demo scratch copy. No API calls.

Run with: python -m unittest tests/test_scratch.py -v
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.render import render_digest
from src.scratch import make_scratch
from src.tracker import TrackerError, load_tracker, mark_sent, save_tracker
from tests import render_fixtures as fx


class TestScratch(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        self.source, self.target = root / "tracker.json", root / "scratch" / "tracker.json"
        fx.build(stop_after_first_run=True)
        tracker = fx.build.tracker
        self.profiles = fx.load_profiles()
        with contextlib.redirect_stdout(io.StringIO()):
            mark_sent(tracker, render_digest(tracker, self.profiles, fx.RUN_1.date()).receipt, fx.RUN_1.date())
        save_tracker(tracker, self.source)
        self.before = self.source.read_bytes()

    def tearDown(self):
        self._dir.cleanup()

    def test_copy_brings_back_the_snapshot_and_leaves_source_alone(self):
        pending = make_scratch(self.source, self.target, set(self.profiles))
        self.assertEqual(pending, sorted(self.profiles))
        self.assertEqual(self.source.read_bytes(), self.before)
        copy = load_tracker(self.target)
        self.assertTrue(all(p.snapshot_sent is None for p in copy.profiles.values()))
        self.assertIn("FIRST LOOK:", render_digest(copy, self.profiles, fx.RUN_1.date()).body)
        self.assertNotIn("FIRST LOOK:", render_digest(load_tracker(self.source), self.profiles, fx.RUN_1.date()).body)

    def test_swapped_out_profile_snapshot_untouched(self):
        make_scratch(self.source, self.target, {fx.IMMUNE})
        copy = load_tracker(self.target)
        self.assertIsNone(copy.profiles[fx.IMMUNE].snapshot_sent)
        self.assertIsNotNone(copy.profiles[fx.ELEPHANT].snapshot_sent)

    def test_empty(self):
        make_scratch(self.source, self.target, set(self.profiles), empty=True)
        self.assertEqual(load_tracker(self.target).conferences, [])

    def test_refuses_to_overwrite_source(self):
        with self.assertRaises(TrackerError):
            make_scratch(self.source, self.source, set(self.profiles))
        self.assertEqual(self.source.read_bytes(), self.before)


if __name__ == "__main__":
    unittest.main()
