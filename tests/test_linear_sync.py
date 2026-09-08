import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from helpers import project

from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.linear import ingest_feedback
from sdlc_dispatcher.linear_sync import ReleaseObserver, reconcile
from sdlc_dispatcher.store import Store


class AutomaticFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = project(
            self.root,
            automatic_intake=True,
            linear_intake_mode="feedback",
            linear_team_id="team",
            linear_project_id="project",
            required_labels=["user-feedback"],
            max_daily_runs=0,
        )
        self.store = Store(self.root / "queue.db")
        self.issue = {
            "id": "issue",
            "title": "Feedback",
            "description": "Description",
            "teamId": "team",
            "projectId": "project",
            "state": {"id": "triage", "type": "triage"},
            "labels": [{"name": "user-feedback"}],
        }

    def ingest(self):
        return ingest_feedback(self.store, self.project, self.issue)

    def test_feedback_without_actor_or_approval_queues_and_status_echo_does_not_restart(self):
        job = self.ingest()
        self.assertEqual(self.store.get(job)["status"], "queued")
        self.store.claim(self.project)
        self.issue["state"] = {"id": "working", "type": "started"}
        self.assertEqual(self.ingest(), job)
        self.assertEqual(self.store.get(job)["status"], "running")
        self.assertEqual(self.store.get(job)["attempts"], 1)

    def test_completed_and_foreign_feedback_do_not_queue(self):
        self.issue["state"] = {"id": "done", "type": "completed"}
        self.assertEqual(self.ingest(), "ignored")
        self.issue["state"] = {"id": "triage", "type": "triage"}
        self.issue["projectId"] = "other"
        self.assertEqual(self.ingest(), "ignored")
        self.assertEqual(self.store.jobs(), [])

    def test_status_cancellation_cancels_active_job(self):
        job = self.ingest()
        self.store.claim(self.project)
        self.issue["state"] = {"id": "cancel", "type": "canceled"}
        self.ingest()
        self.assertTrue(self.store.cancelled(job))

    def test_content_edit_queues_only_after_old_candidate_cleanup(self):
        job = self.ingest()
        self.store.claim(self.project)
        self.issue["description"] = "Changed request"
        self.ingest()
        self.assertTrue(self.store.cancelled(job))
        self.assertIsNone(self.store.claim(self.project))
        self.store.finish(job, "blocked")
        self.assertEqual(self.store.get(job)["status"], "cancelled")
        self.assertEqual(self.store.stage(job)["stage"], "queued")
        self.store.resume_revision(job, self.project)
        self.assertEqual(self.store.get(job)["status"], "queued")
        self.assertEqual(self.store.get(job)["attempts"], 0)
        self.assertIsNotNone(self.store.claim(self.project))

    def test_manual_cancel_prevents_pending_edit_requeue(self):
        job = self.ingest()
        self.store.claim(self.project)
        self.issue["description"] = "Changed request"
        self.ingest()
        self.store.cancel(job)
        self.store.finish(job, "blocked")
        self.store.resume_revision(job, self.project)
        self.assertEqual(self.store.get(job)["status"], "cancelled")

    def test_zero_daily_quota_keeps_single_worker_and_attempt_bounds(self):
        job = self.ingest()
        with self.store.transaction() as db:
            db.executemany(
                "INSERT INTO runs VALUES(?,?,?)", [(job, self.project.id, 9999999999)] * 20
            )
        self.assertIsNotNone(self.store.claim(self.project))
        self.assertIsNone(self.store.claim(self.project))
        self.store.finish(job, "blocked")
        self.store.approve(job, self.project)
        self.store.claim(self.project)
        self.store.finish(job, "blocked")
        with self.assertRaises(DispatchError):
            self.store.approve(job, self.project)


class StatusProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = project(
            Path(self.temp.name),
            linear_team_id="team",
            linear_project_id="project",
            linear_statuses={
                "queued": "todo",
                "coding": "progress",
                "blocked": "blocked",
                "cancelled": "cancel",
            },
        )
        self.store = Store(Path(self.temp.name) / "queue.db")
        self.job = self.store.ingest(
            self.project, "linear", "issue", {"title": "Bug", "description": "Fix"}
        )
        self.store.approve(self.job, self.project)
        self.issue = {
            "id": "issue",
            "teamId": "team",
            "projectId": "project",
            "labels": [{"name": "user-feedback"}, {"name": "bug"}],
            "state": {"id": "triage", "type": "triage"},
        }
        self.writes = []
        self.linear = type("FakeLinear", (), {})()
        self.linear.issue = lambda _: self.issue
        self.linear.update = self.update

    def update(self, issue, state):
        self.writes.append(state)
        self.issue["state"] = {"id": state, "type": "started"}

    def test_idempotent_delivery_survives_lost_write_response(self):
        def lost(issue, state):
            self.update(issue, state)
            raise DispatchError("Lost response")

        self.linear.update = lost
        reconcile(self.store, self.project, self.linear, None, now=100)
        self.assertEqual(self.writes, ["todo"])
        self.assertEqual(self.store.stage(self.job)["synced_version"], 0)
        self.linear.update = self.update
        reconcile(self.store, self.project, self.linear, None, now=161)
        self.assertEqual(self.writes, ["todo"])
        self.assertEqual(
            self.store.stage(self.job)["synced_version"], self.store.stage(self.job)["version"]
        )

    def test_worker_advancing_during_network_read_does_not_write_stale_stage(self):
        def advance(_):
            self.store.set_stage(self.job, "coding")
            return self.issue

        self.linear.issue = advance
        reconcile(self.store, self.project, self.linear, None)
        self.assertEqual(self.writes, [])

    def test_completed_pr_skips_history_reads_until_live_build_changes(self):
        self.project = replace(
            self.project,
            linear_statuses={
                **self.project.linear_statuses,
                "done": "done",
                "ready_for_release": "ready-release",
            },
        )
        with self.store.transaction() as db:
            db.execute(
                "UPDATE jobs SET status='published',pr_url='https://github.com/owner/repo/pull/1' WHERE id=?",
                (self.job,),
            )
        self.store.set_stage(self.job, "done")
        with self.store.transaction() as db:
            db.execute("UPDATE pipeline SET release_sha=? WHERE job=?", ("a" * 40, self.job))
        self.issue["state"] = {"id": "done", "type": "completed"}
        observer = Mock()
        observer.releases.return_value = {"live": "a" * 40, "verified": {"a" * 40}}
        reconcile(self.store, self.project, self.linear, observer)
        observer.stage.assert_not_called()
        observer.releases.return_value = {"live": "b" * 40, "verified": {"b" * 40}}
        observer.stage.return_value = ("ready_for_release", "")
        reconcile(self.store, self.project, self.linear, observer)
        observer.stage.assert_called_once()
        self.assertEqual(self.writes, ["ready-release"])

    def test_user_cancellation_is_respected(self):
        self.issue["state"] = {"id": "cancel", "type": "canceled"}
        reconcile(self.store, self.project, self.linear, None)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.store.get(self.job)["status"], "cancelled")


class ReleaseProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = project(
            Path(self.temp.name),
            github_repository="owner/repo",
            production_health_url="https://app.example/healthz",
        )
        self.job = {"id": "job", "pr_url": "https://github.com/owner/repo/pull/1"}
        self.pr = {
            "merged": True,
            "merge_commit_sha": "a" * 40,
            "base": {"ref": "main"},
            "head": {"ref": "sdlc/sample/job", "repo": {"full_name": "owner/repo"}},
        }
        self.run = {
            "id": 9,
            "path": ".github/workflows/deploy-production.yml",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "repository": {"full_name": "owner/repo"},
            "head_sha": "b" * 40,
            "status": "completed",
            "conclusion": "success",
        }
        self.deploy = {"name": "deploy", "status": "completed", "conclusion": "success"}
        self.health = {"status": "ok", "build_sha": "b" * 40}
        self.comparison = "ahead"
        self.github = type("FakeGitHub", (), {})()
        self.github.call = self.call

    def call(self, method, path, body=None):
        if path == "/pulls/1":
            return self.pr
        if "/compare/" in path:
            return {"status": self.comparison}
        if "/jobs?" in path:
            return {"jobs": [self.deploy]}
        return {"workflow_runs": [self.run]}

    def stage(self):
        return ReleaseObserver(self.project, self.github, health=lambda _: self.health).stage(
            self.job
        )[0]

    def test_merge_alone_is_ready_for_release(self):
        self.health["build_sha"] = "c" * 40
        self.assertEqual(self.stage(), "ready_for_release")

    def test_live_success_must_include_the_merged_fix(self):
        self.assertEqual(self.stage(), "done")
        self.comparison = "behind"
        self.assertEqual(self.stage(), "ready_for_release")

    def test_successful_preparation_cannot_mark_done(self):
        self.deploy["conclusion"] = "skipped"
        self.assertEqual(self.stage(), "ready_for_release")

    def test_waiting_for_environment_approval_is_not_deploying(self):
        self.run.update(status="in_progress", conclusion=None)
        self.deploy.update(status="queued", conclusion=None)
        self.assertEqual(self.stage(), "ready_for_release")
        self.deploy["status"] = "in_progress"
        self.assertEqual(self.stage(), "deploying")

    def test_failed_release_is_blocked_and_wrong_workflow_cannot_complete(self):
        self.run["conclusion"] = "failure"
        self.assertEqual(self.stage(), "blocked")
        self.run["conclusion"] = "success"
        self.run["path"] = "other.yml"
        self.assertEqual(self.stage(), "ready_for_release")

    def test_unknown_health_cannot_mark_done(self):
        self.health = {}
        with self.assertRaises(DispatchError):
            self.stage()

    def test_closed_unmerged_pr_is_cancelled(self):
        self.pr.update(merged=False, state="closed")
        self.assertEqual(self.stage(), "cancelled")
