import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

from helpers import project

from sdlc_dispatcher.blocked_comments import blocked_body
from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.linear_sync import LinearClient, reconcile
from sdlc_dispatcher.store import Store


class BlockedCommentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = project(
            self.root,
            linear_team_id="team",
            linear_project_id="project",
            linear_statuses={"blocked": "blocked", "queued": "todo"},
        )
        self.store = Store(self.root / "queue.db")
        self.job_id = self.store.ingest(
            self.project, "linear", "issue", {"title": "Bug", "description": "Fix"}
        )
        self.store.approve(self.job_id, self.project)
        self.store.claim(self.project)
        self.issue = {
            "id": "issue",
            "teamId": "team",
            "projectId": "project",
            "labels": [{"name": "user-feedback"}, {"name": "bug"}],
            "state": {"id": "blocked", "type": "started"},
        }
        self.linear = Mock()
        self.linear.issue.return_value = self.issue

    def review(self, verdict="needs_human_review"):
        self.store.finish(self.job_id, "verifying")
        result = {
            "schema_version": 1,
            "verdict": verdict,
            "summary": "Actual dragging was not demonstrated.",
            "findings": [
                {
                    "id": "R1",
                    "severity": "medium",
                    "blocking": False,
                    "category": "validation",
                    "path": "app.js",
                    "line": 12,
                    "requirement": "Artwork follows the pointer.",
                    "evidence": "Screenshots show idle layouts.",
                    "impact": "The fix remains unverified.",
                    "requested_change": "Capture active drags in Safari.",
                }
            ],
            "coverage": [],
            "limitations": ["No Safari evidence."],
            "suggested_debt": [],
        }
        raw = json.dumps(result).encode()
        (self.root / "review.json").write_bytes(raw)
        metadata = self.root / "metadata.json"
        metadata.write_text(json.dumps({"review_digest": hashlib.sha256(raw).hexdigest()}))
        self.store.record_review(
            self.job_id,
            "digest",
            metadata,
            hashlib.sha256(metadata.read_bytes()).hexdigest(),
            verdict,
        )

    def block(self, reason="Independent review needs human judgment; no automatic repair"):
        self.store.finish(self.job_id, "blocked", reason)

    def body(self):
        return blocked_body(self.store, self.store.get(self.job_id))

    def test_backfills_already_synced_block_once_with_actionable_review(self):
        self.review()
        self.block()
        with self.store.transaction() as db:
            db.execute("UPDATE pipeline SET synced_version=version WHERE job=?", (self.job_id,))
        reconcile(self.store, self.project, self.linear, None)
        reconcile(self.store, self.project, self.linear, None)
        self.linear.ensure_comment.assert_called_once()
        self.assertEqual(uuid.UUID(self.linear.ensure_comment.call_args.args[1]).version, 4)
        self.linear.update.assert_not_called()
        body = self.linear.ensure_comment.call_args.args[2]
        for text in [
            "needs_human_review",
            "Capture active drags in Safari",
            "No Safari evidence",
            "app.js:12",
        ]:
            self.assertIn(text, body)

    def test_comment_failure_retries_same_id_and_body_before_status(self):
        self.block("Baseline checks failed")
        self.issue["state"]["id"] = "working"
        self.linear.ensure_comment.side_effect = [DispatchError("Lost response"), None]
        reconcile(self.store, self.project, self.linear, None, now=100)
        self.linear.update.assert_not_called()
        self.assertEqual(self.store.stage(self.job_id)["next_retry"], 160)
        reconcile(self.store, self.project, self.linear, None, now=120)
        self.assertEqual(self.linear.ensure_comment.call_count, 1)
        reconcile(self.store, self.project, self.linear, None, now=161)
        self.assertEqual(
            self.linear.ensure_comment.call_args_list[0],
            self.linear.ensure_comment.call_args_list[1],
        )
        self.linear.update.assert_called_once_with("issue", "blocked")

    def test_new_attempt_gets_new_comment_without_old_review(self):
        self.review()
        self.block()
        reconcile(self.store, self.project, self.linear, None)
        old_id = self.linear.ensure_comment.call_args.args[1]
        self.store.approve(self.job_id, self.project)
        self.store.claim(self.project)
        self.block("Baseline checks failed")
        reconcile(self.store, self.project, self.linear, None)
        self.assertNotEqual(old_id, self.linear.ensure_comment.call_args.args[1])
        self.assertIn("No completed Astra review", self.linear.ensure_comment.call_args.args[2])
        self.assertNotIn("Actual dragging", self.linear.ensure_comment.call_args.args[2])

    def test_passing_review_does_not_get_blame_for_publication_failure(self):
        self.review("pass")
        with self.store.transaction() as db:
            self.store.audit(
                db, self.job_id, "publication_needs_attention", "Reconcile an ambiguous write"
            )
        body = self.body()
        self.assertIn("Astra review: pass", body)
        self.assertIn("blocker occurred elsewhere", body)
        self.assertIn("reconcile any existing GitHub branch/PR", body)

    def test_tampered_review_is_not_posted_and_reason_is_redacted(self):
        self.review()
        (self.root / "review.json").write_text("untrusted replacement")
        self.block("Failed for private@example.com")
        body = self.body()
        self.assertIn("could not be verified", body)
        self.assertNotIn("untrusted replacement", body)
        self.assertNotIn("private@example.com", body)

    def test_terminal_and_foreign_issues_receive_no_comments(self):
        self.block()
        self.issue["state"]["type"] = "canceled"
        reconcile(self.store, self.project, self.linear, None)
        self.linear.ensure_comment.assert_not_called()
        self.issue["state"]["type"] = "started"
        self.issue["projectId"] = "foreign"
        self.store.set_stage(self.job_id, "blocked")
        reconcile(self.store, self.project, self.linear, None)
        self.linear.ensure_comment.assert_not_called()

    def test_stage_change_during_read_prevents_stale_comment(self):
        self.block()

        def advance(_):
            self.store.set_stage(self.job_id, "queued")
            return self.issue

        self.linear.issue.side_effect = advance
        reconcile(self.store, self.project, self.linear, None)
        self.linear.ensure_comment.assert_not_called()


class CommentClientTests(unittest.TestCase):
    def test_lost_create_response_is_reconciled_by_paginated_id_lookup(self):
        client = LinearClient("read", "write")

        def page(ids, more=False):
            return {
                "issue": {
                    "comments": {
                        "nodes": [{"id": x} for x in ids],
                        "pageInfo": {"hasNextPage": more, "endCursor": "cursor"},
                    }
                }
            }

        client.call = Mock(
            side_effect=[
                page([]),
                DispatchError("Lost response"),
                page(["other"], True),
                page(["stable-id"]),
            ]
        )
        with self.assertRaises(DispatchError):
            client.ensure_comment("issue", "stable-id", "Reason")
        client.ensure_comment("issue", "stable-id", "Reason")
        writes = [call for call in client.call.call_args_list if call.kwargs.get("write")]
        self.assertEqual(len(writes), 1)
        self.assertEqual(
            writes[0].args[1]["input"],
            {
                "id": "stable-id",
                "issueId": "issue",
                "body": "Reason",
            },
        )
        self.assertEqual(client.call.call_args.args[1]["after"], "cursor")

    def test_unconfirmed_comment_is_an_error(self):
        client = LinearClient("read", "write")
        client.call = Mock(
            side_effect=[
                {"issue": {"comments": {"nodes": [], "pageInfo": {"hasNextPage": False}}}},
                {"commentCreate": {"success": False}},
            ]
        )
        with self.assertRaises(DispatchError):
            client.ensure_comment("issue", "stable-id", "Reason")
