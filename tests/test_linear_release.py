import copy
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from helpers import project

from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.linear import ingest_feedback, receive
from sdlc_dispatcher.release_controller import Controller
from sdlc_dispatcher.release_store import configure, latest, projected_stage, save
from sdlc_dispatcher.store import Store

HEAD, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = project(
            root,
            github_repository="owner/repo",
            linear_team_id="team",
            linear_project_id="project",
            required_labels=["user-feedback"],
            linear_intake_mode="feedback",
            automatic_intake=True,
            linear_statuses={
                "awaiting_approval": "review",
                "deploying": "deploying",
                "done": "done",
                "blocked": "blocked",
            },
            production_health_url="https://example.test/healthz",
        )
        self.store = Store(root / "queue.db")
        self.issue = {
            "id": "issue",
            "title": "Fix the UI",
            "description": "UI correction",
            "teamId": "team",
            "projectId": "project",
            "labels": [{"name": "user-feedback"}],
            "state": {"id": "review", "type": "started"},
        }
        self.job_id = ingest_feedback(self.store, self.project, self.issue)
        with self.store.transaction() as db:
            db.execute(
                "UPDATE jobs SET status='published',pr_url=? WHERE id=?",
                ("https://github.com/owner/repo/pull/1", self.job_id),
            )
        configure(
            self.store,
            self.project,
            {
                "actor_ids": ["owner-id"],
                "approval_state": "approve",
                "review_state": "review",
                "deploying_state": "deploying",
                "github_owner": "owner",
                "enabled_at": time.time() - 100,
                "mode": "live",
            },
        )
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO review_cards(job,head,digest,manifest,comment_id,delivered,ready_at) VALUES(?,?,?,?,?,1,?)",
                (self.job_id, HEAD, "evidence", "{}", "comment", time.time() - 10),
            )
        self.github = FakeGitHub(self)
        self.linear = Mock()
        self.linear.issue.side_effect = lambda _: copy.deepcopy(self.issue)
        self.linear.update.side_effect = lambda _, state: self.issue.update(
            state={"id": state, "type": "started"}
        )
        self.evidence = Mock()
        self.controller = Controller(
            self.store,
            self.project,
            self.linear,
            self.github,
            self.evidence,
            health=lambda _: {"status": "ok", "build_sha": self.github.live},
        )
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES(?,?)",
                ("verified-release:sample:owner/repo", BASE),
            )

    def event(self, *, actor="owner-id", state="approve", previous="review", at=None, **extra):
        at = time.time() if at is None else at
        return {
            "type": "Issue",
            "action": "update",
            "actor": {"id": actor, "type": "user"},
            "createdAt": datetime.fromtimestamp(at, timezone.utc).isoformat(),
            "webhookTimestamp": time.time() * 1000,
            "updatedFrom": {"stateId": previous},
            "data": {**self.issue, "stateId": state},
            **extra,
        }

    def send(self, event, delivery="delivery"):
        raw = json.dumps(event).encode()
        signature = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
        return receive(self.store, self.project, raw, signature, delivery, "secret")

    def approve(self):
        self.assertEqual(self.send(self.event()), "release_approved")
        self.issue["state"]["id"] = "approve"
        return latest(self.store, self.job_id)

    def test_signed_owner_transition_binds_commit_and_evidence(self):
        r = self.approve()
        self.assertEqual((r["head"], r["card_digest"], r["actor"]), (HEAD, "evidence", "owner-id"))
        self.assertEqual(
            projected_stage(
                self.store, self.project, self.store.get(self.job_id), "awaiting_approval"
            ),
            "release_approved",
        )

    def test_duplicate_delivery_after_restart_does_not_duplicate_approval(self):
        event = self.event()
        self.send(event)
        self.store = Store(self.store.path)
        self.assertEqual(self.send(event), "release_approved")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM release_requests").fetchone()[0], 1)

    def test_invalid_signature_cannot_authorize(self):
        with self.assertRaises(DispatchError):
            receive(
                self.store,
                self.project,
                json.dumps(self.event()).encode(),
                "invalid",
                "id",
                "secret",
            )
        self.assertIsNone(latest(self.store, self.job_id))

    def test_nonowner_and_integration_and_wrong_transition_cannot_authorize(self):
        for index, event in enumerate(
            [
                self.event(actor="other"),
                self.event(actor=None),
                self.event(previous="testing"),
                self.event(actor={"id": "owner-id"}),
                self.event(actor="owner-id", updatedFrom={}),
            ]
        ):
            try:
                self.send(event, str(index))
            except DispatchError:
                pass
        self.assertIsNone(latest(self.store, self.job_id))
        event = self.event()
        event["actor"]["type"] = "integration"
        self.send(event, "integration")
        self.assertIsNone(latest(self.store, self.job_id))

    def test_no_screenshot_no_approval(self):
        with self.store.transaction() as db:
            db.execute("UPDATE review_cards SET delivered=0")
        self.assertEqual(self.send(self.event()), "release_without_current_review")
        self.assertIsNone(latest(self.store, self.job_id))

    def test_issue_edit_invalidates_approval(self):
        event = self.event()
        event["data"]["description"] = "Another task"
        self.assertEqual(self.send(event), "release_without_current_review")

    def test_stale_event_after_revocation_cannot_reapprove(self):
        at = time.time() - 2
        self.send(self.event(at=at))
        self.send(self.event(at=at + 1, state="review", previous="approve"), "cancel")
        self.assertTrue(latest(self.store, self.job_id)["cancel_requested"])
        self.assertEqual(self.send(self.event(at=at + 0.5), "late"), "release_stale")

    def digitalocean_evidence(self):
        return {
            "health": {"healthy": True, "build_sha": self.github.live},
            "release": {"status": "success", "sha": self.github.live, "ci_run_id": 1},
        }

    def test_digitalocean_promotes_once_without_fly_dispatch(self):
        self.approve()

        def promote(run_id, sha):
            self.github.live = sha

        with (
            patch("sdlc_dispatcher.digitalocean.enabled", return_value=True),
            patch("sdlc_dispatcher.digitalocean.status", side_effect=self.digitalocean_evidence),
            patch("sdlc_dispatcher.digitalocean.deploy", side_effect=promote) as deploy,
        ):
            for _ in range(9):
                self.controller.tick()
        self.assertEqual(latest(self.store, self.job_id)["status"], "done")
        self.assertEqual(self.github.dispatches, 0)
        deploy.assert_called_once_with(1, MERGE)

    def test_digitalocean_lost_response_reconciles_receipt_without_second_deploy(self):
        self.approve()

        def promote(run_id, sha):
            self.github.live = sha
            raise DispatchError("Response lost")

        with (
            patch("sdlc_dispatcher.digitalocean.enabled", return_value=True),
            patch("sdlc_dispatcher.digitalocean.status", side_effect=self.digitalocean_evidence),
            patch("sdlc_dispatcher.digitalocean.deploy", side_effect=promote) as deploy,
        ):
            for _ in range(9):
                self.controller.tick()
                with self.store.transaction() as db:
                    db.execute("UPDATE release_requests SET next_retry=0")
        self.assertEqual(latest(self.store, self.job_id)["status"], "done")
        deploy.assert_called_once()
        self.assertEqual(self.github.dispatches, 0)

    def test_digitalocean_wrong_ci_receipt_never_marks_done(self):
        self.approve()

        def evidence():
            result = self.digitalocean_evidence()
            result["release"]["ci_run_id"] = 999
            return result

        def promote(run_id, sha):
            self.github.live = sha

        with (
            patch("sdlc_dispatcher.digitalocean.enabled", return_value=True),
            patch("sdlc_dispatcher.digitalocean.status", side_effect=evidence),
            patch("sdlc_dispatcher.digitalocean.deploy", side_effect=promote),
        ):
            for _ in range(9):
                self.controller.tick()
        self.assertEqual(latest(self.store, self.job_id)["status"], "local_deploying")
        self.linear.ensure_comment.assert_not_called()

    def test_full_release_through_verified_health_and_done_comment(self):
        self.approve()
        for _ in range(9):
            self.controller.tick()
        self.assertEqual(latest(self.store, self.job_id)["status"], "done")
        self.assertEqual(self.github.merges, 1)
        self.assertEqual(self.github.dispatches, 1)
        self.assertEqual(self.github.approvals, 1)
        self.linear.ensure_comment.assert_called_once()

    def test_lost_merge_response_reconciles_without_second_merge(self):
        self.approve()
        self.github.lose_merge = True
        for _ in range(9):
            self.controller.tick()
            with self.store.transaction() as db:
                db.execute("UPDATE release_requests SET next_retry=0")
        self.assertEqual(latest(self.store, self.job_id)["status"], "done")
        self.assertEqual(self.github.merges, 1)

    def test_low_storage_prevents_merge_and_dispatch(self):
        self.approve()
        self.evidence.preflight.side_effect = DispatchError("Data volume has 0 bytes free")
        self.controller.tick()
        self.assertEqual(self.github.merges, 0)
        self.assertEqual(self.github.dispatches, 0)
        self.assertIn("0 bytes free", latest(self.store, self.job_id)["error"])

    def test_new_owner_approval_retries_recorded_merge_without_merging_twice(self):
        original = self.approve()
        self.github.pr.update(merged=True, merge_commit_sha=MERGE)
        self.github.main = MERGE
        save(self.store, original, "blocked", expected_head=HEAD, merge_sha=MERGE)
        self.issue["state"]["id"] = "review"
        self.assertEqual(self.send(self.event(), "retry-approval"), "release_approved")
        self.issue["state"]["id"] = "approve"
        for _ in range(6):
            self.controller.tick()
        self.assertEqual(latest(self.store, self.job_id)["status"], "done")
        self.assertEqual(self.github.merges, 0)
        self.assertEqual(self.github.dispatches, 1)
        self.assertEqual(self.github.approvals, 1)

    def test_merged_retry_rejects_unrecorded_merge_or_changed_main(self):
        original = self.approve()
        self.github.pr.update(merged=True, merge_commit_sha=MERGE)
        self.github.main = "d" * 40
        save(self.store, original, "blocked", expected_head=HEAD, merge_sha=MERGE)
        self.issue["state"]["id"] = "review"
        self.send(self.event(), "retry-approval")
        self.issue["state"]["id"] = "approve"
        self.controller.tick()
        self.assertEqual(self.github.dispatches, 0)
        self.assertIn("differs", latest(self.store, self.job_id)["error"])

    def test_lost_dispatch_response_finds_existing_run(self):
        self.approve()
        self.github.lose_dispatch = True
        for _ in range(10):
            self.controller.tick()
            with self.store.transaction() as db:
                db.execute("UPDATE release_requests SET next_retry=0")
        self.assertEqual(latest(self.store, self.job_id)["status"], "done")
        self.assertEqual(self.github.dispatches, 1)

    def test_changed_head_is_not_merged(self):
        self.approve()
        self.github.pr["head"]["sha"] = "d" * 40
        self.controller.tick()
        self.assertEqual(self.github.merges, 0)
        self.assertIn("PR changed", latest(self.store, self.job_id)["error"])

    def test_unapproved_main_is_not_merged_or_deployed(self):
        self.approve()
        self.controller.tick()
        self.github.main = "d" * 40
        self.controller.tick()
        self.assertEqual(self.github.merges, 0)
        self.assertIn("not verified live", latest(self.store, self.job_id)["error"])

    def test_main_moved_after_merge_cannot_deploy_unrelated_changes(self):
        r = self.approve()
        self.issue["state"]["id"] = "deploying"
        save(self.store, r, "main_ci", merge_sha=MERGE)
        self.github.main = "d" * 40
        self.controller.tick()
        self.assertEqual(self.github.dispatches, 0)
        self.assertIn("Main changed", latest(self.store, self.job_id)["error"])

    def test_withdrawal_prevents_merge(self):
        self.approve()
        self.issue["state"]["id"] = "review"
        self.send(self.event(state="review", previous="approve"), "cancel")
        self.controller.tick()
        self.assertEqual(self.github.merges, 0)
        self.assertEqual(latest(self.store, self.job_id)["status"], "cancelled")

    def test_observe_mode_never_merges(self):
        self.approve()
        from sdlc_dispatcher.release_store import settings

        config = settings(self.store, self.project)
        config["mode"] = "observe"
        configure(self.store, self.project, config)
        self.controller.tick()
        self.assertEqual(self.github.merges, 0)

    def test_successful_workflow_with_stale_health_is_not_done(self):
        self.approve()
        self.github.stale_health = True
        for _ in range(9):
            self.controller.tick()
        self.assertNotEqual(latest(self.store, self.job_id)["status"], "done")
        self.linear.ensure_comment.assert_not_called()

    def test_tampered_review_digest_cannot_merge(self):
        self.approve()
        with self.store.transaction() as db:
            db.execute("UPDATE review_cards SET digest='changed'")
        self.controller.tick()
        self.assertEqual(self.github.merges, 0)
        self.assertIn("matching delivered", latest(self.store, self.job_id)["error"])

    def test_second_owner_event_does_not_duplicate_active_request(self):
        self.approve()
        self.assertEqual(self.send(self.event(), "second-delivery"), "release_already_queued")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM release_requests").fetchone()[0], 1)

    def test_wrong_astra_publisher_cannot_pass_merge_gate(self):
        self.approve()
        self.controller.tick()
        original = self.github.call

        def provider(method, path, body=None):
            if "/statuses?" in path:
                return [
                    {
                        "context": "dispatcher/astra",
                        "state": "success",
                        "creator": {"login": "other"},
                    }
                ]
            return original(method, path, body)

        self.github.call = provider
        self.controller.tick()
        self.assertEqual(self.github.merges, 0)
        self.assertIn("Required exact-commit checks", latest(self.store, self.job_id)["error"])

    def test_withdrawal_after_merge_prevents_environment_approval(self):
        request = self.approve()
        save(
            self.store,
            request,
            "deploying",
            run_id=2,
            run_url="https://example.test/run",
            merge_sha=MERGE,
        )
        self.github.dispatches = 1
        self.github.main = MERGE
        self.github.pr.update(merged=True, merge_commit_sha=MERGE)
        self.issue["state"]["id"] = "review"
        self.send(self.event(state="review", previous="deploying"), "withdraw")
        self.controller.tick()
        self.assertEqual(self.github.approvals, 0)
        self.assertEqual(latest(self.store, self.job_id)["status"], "blocked")

    def test_observer_does_not_announce_done_before_controller_finishes_receipt(self):
        self.approve()
        self.assertEqual(
            projected_stage(self.store, self.project, self.store.get(self.job_id), "done"),
            "release_approved",
        )

    def test_no_review_state_without_delivered_screenshots(self):
        with self.store.transaction() as db:
            db.execute("UPDATE review_cards SET ready_at=0")
        self.assertEqual(
            projected_stage(
                self.store, self.project, self.store.get(self.job_id), "awaiting_approval"
            ),
            "testing",
        )


class FakeGitHub:
    def __init__(self, test):
        self.test = test
        self.main = BASE
        self.live = BASE
        self.merges = 0
        self.dispatches = 0
        self.approvals = 0
        self.lose_merge = False
        self.lose_dispatch = False
        self.stale_health = False
        self.pr = {
            "number": 1,
            "node_id": "node",
            "state": "open",
            "draft": True,
            "merged": False,
            "html_url": "https://github.com/owner/repo/pull/1",
            "base": {"ref": "main", "sha": BASE, "repo": {"full_name": "owner/repo"}},
            "head": {
                "sha": HEAD,
                "ref": f"sdlc/sample/{test.job_id}",
                "repo": {"full_name": "owner/repo"},
            },
        }

    def api(self, path):
        assert path == "user"
        return {"login": "owner"}

    def make_ready(self, pr):
        self.pr["draft"] = False

    def run(self):
        return {
            "id": 2,
            "head_sha": MERGE,
            "head_branch": "main",
            "actor": {"login": "owner"},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "html_url": "https://github.com/owner/repo/actions/runs/2",
            "event": "workflow_dispatch",
            "path": ".github/workflows/deploy-production.yml",
            "repository": {"full_name": "owner/repo"},
            "status": "completed" if self.approvals else "waiting",
            "conclusion": "success" if self.approvals else None,
        }

    def call(self, method, path, body=None):
        if path == "/pulls/1":
            return copy.deepcopy(self.pr)
        if path.startswith("/git/ref/heads/"):
            return {"object": {"sha": self.main}}
        if path.startswith("/compare/"):
            base, head = path.split("/compare/")[1].split("...")
            return {"status": "identical" if base == head else "ahead"}
        if "/check-runs" in path:
            return {
                "check_runs": [
                    {"id": i, "name": name, "conclusion": "success", "app": {"id": 15368}}
                    for i, name in enumerate(["test", "review", "container"])
                ]
            }
        if "/statuses?" in path:
            return [
                {"context": "dispatcher/astra", "state": "success", "creator": {"login": "owner"}}
            ]
        if path == "/pulls/1/merge":
            self.merges += 1
            self.main = MERGE
            self.pr.update(merged=True, merge_commit_sha=MERGE)
            if self.lose_merge:
                self.lose_merge = False
                raise DispatchError("Response lost")
            return {"merged": True, "sha": MERGE}
        if path.startswith("/actions/workflows/ci.yml/runs"):
            return {
                "workflow_runs": [
                    {"id": 1, "status": "completed", "conclusion": "success", "run_attempt": 1}
                ]
            }
        if path == "/actions/workflows/deploy-production.yml/dispatches":
            self.dispatches += 1
            if self.lose_dispatch:
                self.lose_dispatch = False
                raise DispatchError("Response lost")
            return None
        if path.startswith("/actions/workflows/deploy-production.yml/runs"):
            return {"workflow_runs": [self.run()] if self.dispatches else []}
        if path == "/actions/runs/2":
            return self.run()
        if path.startswith("/actions/runs/2/jobs"):
            return {
                "jobs": [
                    {
                        "name": "deploy",
                        "status": "completed" if self.approvals else "waiting",
                        "conclusion": "success" if self.approvals else None,
                    }
                ]
            }
        if path == "/actions/runs/2/pending_deployments":
            if method == "GET":
                return [
                    {
                        "environment": {"id": 1, "name": "production"},
                        "current_user_can_approve": True,
                    }
                ]
            self.approvals += 1
            if not self.stale_health:
                self.live = MERGE
            return []
        raise AssertionError((method, path, body))
