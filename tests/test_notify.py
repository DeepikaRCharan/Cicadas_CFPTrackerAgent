"""Tests for src/notify.py -- build step 7: email sending. The Resend call
is always mocked; nothing here sends a real email.

Run with: python -m unittest tests/test_notify.py -v
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import resend

from src import notify
from src.notify import NotifyError, load_settings, send_and_mark
from src.render import render_digest
from src.tracker import load_tracker, save_tracker, unreported_notifications
from tests import render_fixtures as fx

ENV = {"RESEND_API_KEY": "re_test_key", "NOTIFY_TO": "researcher@example.com"}


class NotifyTestCase(unittest.TestCase):
    """Starts from the fixture's first run, saved but not yet sent: two
    new_fit entries and two pending snapshots."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "tracker.json"
        fx.build(stop_after_first_run=True)
        save_tracker(fx.build.tracker, self.path)
        self.before = self.path.read_bytes()
        self.profiles = fx.load_profiles()
        self.tracker = load_tracker(self.path)
        self.email = render_digest(self.tracker, self.profiles, fx.RUN_1.date())

    def tearDown(self):
        self._dir.cleanup()

    def assert_nothing_marked(self):
        self.assertEqual(self.path.read_bytes(), self.before)
        on_disk = load_tracker(self.path)
        self.assertEqual(len(unreported_notifications(on_disk)), 2)
        self.assertTrue(all(p.snapshot_sent is None for p in on_disk.profiles.values()))
        # nor in memory
        self.assertEqual(len(unreported_notifications(self.tracker)), 2)


class TestSuccessfulSend(NotifyTestCase):
    def test_success_marks_receipt_items_reported(self):
        sender = mock.Mock(return_value="email-123")
        email_id = send_and_mark(self.email, self.tracker, self.path, fx.RUN_1.date(), env=ENV, sender=sender)
        self.assertEqual(email_id, "email-123")
        sender.assert_called_once_with("re_test_key", "researcher@example.com", self.email)

        on_disk = load_tracker(self.path)
        self.assertEqual(unreported_notifications(on_disk), [])
        self.assertEqual({p.snapshot_sent for p in on_disk.profiles.values()}, {fx.RUN_1.date()})
        # the next digest no longer carries them
        after = render_digest(on_disk, self.profiles, fx.RUN_1.date())
        self.assertNotIn("FIRST LOOK:", after.body)
        self.assertIn("Nothing new this month.", after.body)

    def test_resend_called_with_rendered_subject_and_body(self):
        with mock.patch.object(resend.Emails, "send", return_value={"id": "email-456"}) as send:
            send_and_mark(self.email, self.tracker, self.path, fx.RUN_1.date(), env=ENV)
        params = send.call_args.args[0]
        self.assertEqual(params["to"], ["researcher@example.com"])
        self.assertEqual((params["subject"], params["text"]), (self.email.subject, self.email.body))


class TestFailedSend(NotifyTestCase):
    def test_api_error_marks_nothing_and_leaves_file_unchanged(self):
        error = resend.exceptions.ValidationError("Invalid `to` field", "validation_error", 422)
        with mock.patch.object(resend.Emails, "send", side_effect=error):
            with self.assertRaises(NotifyError) as ctx:
                send_and_mark(self.email, self.tracker, self.path, fx.RUN_1.date(), env=ENV)
        self.assertIn("Invalid `to` field", str(ctx.exception))
        self.assert_nothing_marked()

    def test_network_error_marks_nothing(self):
        sender = mock.Mock(side_effect=ConnectionError("connection reset"))
        with self.assertRaises(NotifyError):
            send_and_mark(self.email, self.tracker, self.path, fx.RUN_1.date(), env=ENV, sender=sender)
        self.assert_nothing_marked()

    def test_response_without_id_counts_as_failure(self):
        with mock.patch.object(resend.Emails, "send", return_value={}):
            with self.assertRaises(NotifyError):
                send_and_mark(self.email, self.tracker, self.path, fx.RUN_1.date(), env=ENV)
        self.assert_nothing_marked()

    def test_save_failure_after_send_is_visible_and_file_unchanged(self):
        sender = mock.Mock(return_value="email-789")
        with mock.patch("src.notify.save_tracker", side_effect=OSError("disk full")):
            with self.assertRaises(NotifyError) as ctx:
                send_and_mark(self.email, self.tracker, self.path, fx.RUN_1.date(), env=ENV, sender=sender)
        self.assertIn("WAS sent", str(ctx.exception))
        self.assertEqual(self.path.read_bytes(), self.before)


class TestSettings(NotifyTestCase):
    def test_missing_key_fails_before_any_send(self):
        sender = mock.Mock(return_value="never")
        with self.assertRaises(NotifyError) as ctx:
            send_and_mark(self.email, self.tracker, self.path, fx.RUN_1.date(),
                          env={"NOTIFY_TO": "researcher@example.com"}, sender=sender)
        self.assertIn("RESEND_API_KEY", str(ctx.exception))
        sender.assert_not_called()
        self.assert_nothing_marked()

    def test_missing_notify_to_fails_before_any_send(self):
        sender = mock.Mock()
        with self.assertRaises(NotifyError):
            send_and_mark(self.email, self.tracker, self.path, fx.RUN_1.date(),
                          env={"RESEND_API_KEY": "re_x", "NOTIFY_TO": "  "}, sender=sender)
        sender.assert_not_called()

    def test_bad_address_fails_before_any_send(self):
        for bad in ("you@example", "a@b.com, c@d.com", "not-an-address"):
            with self.assertRaises(NotifyError):
                load_settings({"RESEND_API_KEY": "re_x", "NOTIFY_TO": bad})


class TestCommandLine(NotifyTestCase):
    def run_main(self, *args, env=ENV):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", env, clear=True), mock.patch("src.notify.load_dotenv"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = notify.main(["--tracker", str(self.path), *args])
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_prints_and_marks_nothing(self):
        with mock.patch.object(resend.Emails, "send") as send:
            code, out, _ = self.run_main("--dry-run", "digest", env={})
        self.assertEqual(code, 0)
        self.assertIn("Subject: Cicadas monthly digest: 7 October 2026", out)
        self.assertIn("FIRST LOOK:", out)
        self.assertIn("nothing sent, nothing marked", out)
        send.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_failed_send_exits_non_zero_with_visible_error(self):
        with mock.patch.object(resend.Emails, "send", side_effect=resend.exceptions.ResendError(
                500, "application_error", "Internal server error", "Try again")):
            code, _, err = self.run_main("digest")
        self.assertEqual(code, 1)
        self.assertIn("[notify] FAILED", err)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_missing_key_exits_non_zero(self):
        with mock.patch.object(resend.Emails, "send") as send:
            code, _, err = self.run_main("digest", env={"NOTIFY_TO": "researcher@example.com"})
        self.assertEqual(code, 1)
        self.assertIn("RESEND_API_KEY not set", err)
        send.assert_not_called()

    def test_successful_send_exits_zero_and_marks(self):
        with mock.patch.object(resend.Emails, "send", return_value={"id": "email-abc"}):
            code, out, _ = self.run_main("digest")
        self.assertEqual(code, 0)
        self.assertIn("email-abc", out)
        self.assertEqual(unreported_notifications(load_tracker(self.path)), [])

    def test_unknown_reminder_id_exits_non_zero(self):
        code, _, err = self.run_main("--dry-run", "reminder", "no-such-conf-2027")
        self.assertEqual(code, 1)
        self.assertIn("no-such-conf-2027", err)


if __name__ == "__main__":
    unittest.main()
