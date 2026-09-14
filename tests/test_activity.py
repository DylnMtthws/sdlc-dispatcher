import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers import project

from sdlc_dispatcher.activity import mark, monitor, reconcile_activity, snapshot
from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.linear_sync import LinearClient, reconcile
from sdlc_dispatcher.store import Store


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = project(
            self.root,
            linear_team_id="team",
            linear_project_id="project",
            linear_statuses={"coding": "working", "testing": "testing", "blocked": "blocked"},
        )
        self.store = Store(self.root / "queue.db")
        self.id = self.store.ingest(
            self.project, "linear", "issue", {"title": "Private input", "description": "Fix"}
        )
        self.store.approve(self.id, self.project)
        self.job = self.store.claim(self.project)
        self.linear = Mock()
        self.linear.issue.return_value = {
            "id": "issue",
            "teamId": "team",
            "projectId": "project",
            "labels": [{"name": "user-feedback"}, {"name": "bug"}],
            "state": {"id": "working", "type": "started"},
        }
        self.clock = patch("sdlc_dispatcher.activity.time.time", return_value=2000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def phase(self, name):
        mark(self.store, self.job, name)

    def sync(self, now):
        self.now.return_value = now
        return reconcile_activity(self.store, self.project, self.linear, now=now)

    def body(self):
        return self.linear.ensure_comment.call_args.args[2]

    def card(self):
        with self.store.connect() as db:
            return dict(db.execute("SELECT * FROM activity_comments").fetchone())

    def test_one_comment_tracks_subphases_and_retry_attempts_across_restart(self):
        self.phase("coding")
        self.assertEqual(self.sync(2000), 1)
        comment_id = self.card()["comment_id"]
        self.phase("checks")
        self.assertEqual(self.sync(2005), 0)  # Coalesce rapid transitions.
        self.store = Store(self.root / "queue.db")
        self.assertEqual(self.sync(2016), 1)
        self.assertIn("independent checks", self.body())
        self.assertEqual(self.card()["comment_id"], comment_id)
        self.store.finish(self.id, "blocked", "Check failed")
        self.sync(2032)
        self.assertIn("pipeline needs attention", self.body())
        self.assertNotIn("Worker supervision is responding", self.body())
        self.store.approve(self.id, self.project)
        self.job = self.store.claim(self.project)
        self.phase("repairing")
        self.sync(2050)
        self.assertIn("Attempt 2", self.body())
        self.assertEqual(self.card()["comment_id"], comment_id)

    def test_heartbeat_is_throttled_and_staleness_does_not_claim_progress(self):
        self.phase("astra")
        poll = monitor(self.store, self.job)
        self.assertFalse(poll())
        self.sync(2000)
        self.now.return_value = 2060
        self.assertFalse(poll())
        self.assertEqual(self.sync(2060), 0)
        self.now.return_value = 2120
        poll()
        self.assertEqual(self.sync(2120), 1)
        self.assertIn("supervision is responding", self.body())
        self.assertEqual(self.sync(2211), 1)
        self.assertIn("heartbeat is overdue", self.body())
        self.store.cancel(self.id)
        self.assertTrue(poll())

    def test_lost_response_uses_same_id_and_activity_failure_does_not_block_status(self):
        self.phase("coding")
        self.linear.ensure_comment.side_effect = DispatchError("Provider body with secret")
        self.assertEqual(self.sync(2000), 0)
        comment_id = self.card()["comment_id"]
        self.assertNotIn("secret", self.card()["error"])
        self.assertEqual(self.sync(2030), 0)
        reconcile(self.store, self.project, self.linear, None, observe_releases=False)
        self.linear.update.assert_called_once_with("issue", "testing")
        self.linear.ensure_comment.side_effect = None
        self.assertEqual(self.sync(2061), 1)
        self.assertEqual(self.card()["comment_id"], comment_id)

    def test_terminal_card_updates_once_without_new_historical_done_comments(self):
        self.phase("coding")
        self.sync(2000)
        self.store.set_stage(self.id, "done")
        self.sync(2020)
        self.assertIn("Deployed and verified", self.body())
        self.assertEqual(self.sync(3000), 0)
        with self.store.transaction() as db:
            db.execute("DELETE FROM activity_comments")
        self.assertEqual(self.sync(3100), 0)

    def test_same_stage_browser_to_astra_is_visible(self):
        self.store.set_stage(self.id, "ai_review")
        self.phase("browser")
        self.sync(2000)
        self.assertIn("Capturing before/after", self.body())
        self.phase("astra")
        self.sync(2020)
        self.assertIn("**Now:** Astra", self.body())

    def test_old_attempt_or_revision_heartbeat_is_not_reused(self):
        self.phase("astra")
        with self.store.transaction() as db:
            db.execute("UPDATE agent_activity SET revision='old'")
        self.sync(2000)
        self.assertIn("has not been reported for this attempt", self.body())

    def test_no_raw_report_audit_or_tool_text_in_activity(self):
        with self.store.transaction() as db:
            self.store.audit(db, self.id, "coding_started", "secret agent transcript")
        self.phase("coding")
        body, _, _ = snapshot(self.store, self.job, self.store.stage(self.id), 2000)
        self.assertNotIn("secret", body)
        self.assertNotIn("Private input", body)

    def test_ineligible_issue_is_not_written(self):
        self.linear.issue.return_value["projectId"] = "foreign"
        self.assertEqual(self.sync(2000), 0)
        self.linear.ensure_comment.assert_not_called()

    def test_archived_issue_is_skipped_without_a_comment_write_or_delivery_error(self):
        self.linear.issue.return_value["archivedAt"] = "2026-09-08T23:18:00Z"
        self.assertEqual(self.sync(2000), 0)
        self.linear.ensure_comment.assert_not_called()
        self.assertEqual(self.card()["error"], "")
        self.assertEqual(self.sync(2030), 0)
        self.assertEqual(self.linear.issue.call_count, 1)

    def test_phase_change_during_network_read_is_not_published(self):
        def read(_):
            self.store.set_stage(self.id, "blocked")
            return {
                "id": "issue",
                "teamId": "team",
                "projectId": "project",
                "labels": [{"name": "user-feedback"}, {"name": "bug"}],
                "state": {"id": "working", "type": "started"},
            }

        self.linear.issue.side_effect = read
        self.assertEqual(self.sync(2000), 0)
        self.linear.ensure_comment.assert_not_called()


class MutableCommentTests(unittest.TestCase):
    def test_edit_existing_comment_and_reconcile_lost_response(self):
        client = LinearClient("read", "write")
        connection = {
            "issue": {
                "comments": {
                    "nodes": [{"id": "card", "body": "old"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        client.call = Mock(
            side_effect=[
                connection,
                {
                    "commentUpdate": {
                        "success": True,
                        "comment": {"id": "card", "body": "new"},
                    }
                },
            ]
        )
        client.ensure_comment("issue", "card", "new", mutable=True)
        self.assertIn("commentUpdate", client.call.call_args.args[0])
        self.assertTrue(client.call.call_args.kwargs["write"])
        connection["issue"]["comments"]["nodes"][0]["body"] = "new"
        client.call = Mock(return_value=connection)
        client.ensure_comment("issue", "card", "new", mutable=True)
        self.assertEqual(client.call.call_count, 1)  # No duplicate update after lost response.

    def test_immutable_final_comments_are_not_edited(self):
        client = LinearClient("read", "write")
        client.call = Mock(
            return_value={
                "issue": {
                    "comments": {
                        "nodes": [{"id": "final", "body": "original"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        )
        client.ensure_comment("issue", "final", "replacement")
        self.assertEqual(client.call.call_count, 1)
