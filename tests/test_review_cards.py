import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers import project

from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.release_store import setup
from sdlc_dispatcher.review_cards import media, publish_card, ready
from sdlc_dispatcher.store import Store


class ReviewCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "queue.db")
        setup(self.store)
        self.project = project(self.root)
        self.folder = self.root / "review"
        evidence = self.folder / "input/evidence"
        evidence.mkdir(parents=True)
        self.names = [
            "before-chromium-spoiler-1440.png",
            "after-chromium-spoiler-1440.png",
            "before-chromium-spoiler-390.png",
            "after-chromium-spoiler-390.png",
        ]
        for name in self.names:
            (evidence / name).write_bytes(b"fixture-" + name.encode())
        (evidence / "after-chromium-interaction.webm").write_bytes(b"video")
        (self.folder / "input/packet.json").write_text(
            json.dumps(
                {"images": self.names, "evidence": self.names + ["after-chromium-interaction.webm"]}
            )
        )
        (self.folder / "metadata.json").write_text(
            json.dumps({"review_digest": "review-hash", "input_digest": "input-hash"})
        )
        self.job = {
            "id": "job",
            "revision": "revision",
            "external_id": "issue",
            "pr_url": "https://github.com/owner/repo/pull/1",
        }
        self.review = {"summary": "The correction is visible.", "limitations": ["Synthetic data."]}
        self.linear = Mock()

    def publish(self):
        with patch(
            "sdlc_dispatcher.review_cards.upload",
            side_effect=lambda _, path: "https://uploads.linear.app/" + path.name,
        ) as upload:
            result = publish_card(
                self.store, self.project, self.job, "a" * 40, self.folder, self.review, self.linear
            )
            return result, upload.call_count

    def test_before_after_mobile_and_recording_are_embedded(self):
        row, count = self.publish()
        self.assertEqual(count, 5)
        body = self.linear.ensure_comment.call_args.args[2]
        for name in self.names:
            self.assertIn("https://uploads.linear.app/" + name, body)
        self.assertIn("after-chromium-interaction.webm", body)
        self.assertIn("Ready to Deploy", body)
        self.assertEqual(row["ready_at"], 0)
        ready(self.store, self.job, "a" * 40)
        with self.store.connect() as db:
            self.assertGreater(db.execute("SELECT ready_at FROM review_cards").fetchone()[0], 0)

    def test_lost_comment_response_reuses_id_body_and_uploads(self):
        self.linear.ensure_comment.side_effect = [DispatchError("Response lost"), None]
        with self.assertRaises(DispatchError):
            self.publish()
        first = self.linear.ensure_comment.call_args.args
        _, count = self.publish()
        self.assertEqual(count, 0)
        self.assertEqual(self.linear.ensure_comment.call_args.args, first)

    def test_upload_failure_never_marks_ready(self):
        with patch(
            "sdlc_dispatcher.review_cards.upload", side_effect=DispatchError("Upload failed")
        ):
            with self.assertRaises(DispatchError):
                publish_card(
                    self.store,
                    self.project,
                    self.job,
                    "a" * 40,
                    self.folder,
                    self.review,
                    self.linear,
                )
        ready(self.store, self.job, "a" * 40)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT ready_at FROM review_cards").fetchone()[0], 0)
        self.linear.ensure_comment.assert_not_called()

    def test_missing_screenshots_allows_text_review_without_uploads(self):
        (self.folder / "input/packet.json").write_text(json.dumps({"images": [], "evidence": []}))
        self.assertEqual(media(self.folder), [])
        row, count = self.publish()
        self.assertEqual(count, 0)
        self.assertEqual(row["delivered"], 1)
        self.assertIn("no visual captures", row["body"])
        ready(self.store, self.job, "a" * 40)
        with self.store.connect() as db:
            self.assertGreater(db.execute("SELECT ready_at FROM review_cards").fetchone()[0], 0)

    def test_changed_media_does_not_reuse_old_review_card(self):
        self.publish()
        (self.folder / "input/evidence" / self.names[0]).write_bytes(b"changed")
        with self.assertRaisesRegex(DispatchError, "changed after registration"):
            self.publish()
