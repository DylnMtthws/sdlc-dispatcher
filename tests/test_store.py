import concurrent.futures
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from helpers import project

from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.store import Store


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "queue.db")
        self.project = project(self.root)

    def enqueue(self, identity="1"):
        return self.store.ingest(
            self.project,
            "manual",
            identity,
            {"title": "Bug", "description": "Reproduction"},
        )

    def test_duplicate_issue_and_delivery(self):
        job = self.enqueue()
        self.assertEqual(job, self.enqueue())
        report = {"title": "Bug", "description": "Reproduction"}
        self.store.ingest(self.project, "manual", "1", report, "delivery", "digest")
        self.assertEqual(
            job,
            self.store.ingest(self.project, "manual", "1", report, "delivery", "digest"),
        )
        with self.assertRaises(DispatchError):
            self.store.ingest(self.project, "manual", "1", report, "delivery", "different")
        self.assertEqual(len(self.store.jobs()), 1)

    def test_only_one_worker_can_claim(self):
        for number in range(8):
            self.store.approve(self.enqueue(str(number)), self.project)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: self.store.claim(self.project), range(8)))
        self.assertEqual(sum(item is not None for item in claims), 1)

    def test_policy_change_requires_new_approval(self):
        job = self.enqueue()
        self.store.approve(job, self.project)
        changed = replace(self.project, max_daily_runs=10)
        self.assertIsNone(self.store.claim(changed))
        self.assertEqual(self.store.get(job)["status"], "needs_review")

    def test_report_update_cancels_run(self):
        job = self.enqueue()
        self.store.approve(job, self.project)
        self.store.claim(self.project)
        self.store.ingest(
            self.project, "manual", "1", {"title": "Updated", "description": "Changed"}
        )
        self.assertTrue(self.store.cancelled(job))
        self.store.finish(job, "ready", artifact="unsafe")
        row = self.store.get(job)
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["artifact"])

    def test_pause_blocks_claims_and_cancels_running(self):
        job = self.enqueue()
        self.store.approve(job, self.project)
        self.store.pause()
        self.assertIsNone(self.store.claim(self.project))
        self.store.pause(False)
        self.store.claim(self.project)
        self.store.pause()
        self.assertTrue(self.store.cancelled(job))

    def test_daily_and_attempt_limits_survive_restart(self):
        constrained = replace(self.project, max_daily_runs=1, max_attempts=1)
        job = self.enqueue()
        self.store.approve(job, constrained)
        self.store.claim(constrained)
        self.store.finish(job, "failed")
        restarted = Store(self.root / "queue.db")
        with self.assertRaises(DispatchError):
            restarted.approve(job, constrained)
        second = self.enqueue("2")
        restarted.approve(second, constrained)
        self.assertIsNone(restarted.claim(constrained))

    def test_crashed_run_is_not_automatically_reissued(self):
        job = self.enqueue()
        self.store.approve(job, self.project)
        self.store.claim(self.project)
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET deadline=0 WHERE id=?", (job,))
        self.assertIsNone(Store(self.root / "queue.db").claim(self.project))

    def test_approval_never_crosses_projects(self):
        with self.assertRaises(DispatchError):
            self.store.approve(self.enqueue(), replace(self.project, id="different"))

    def test_cancel_queued_job(self):
        job = self.enqueue()
        self.store.approve(job, self.project)
        self.store.cancel(job)
        self.assertIsNone(self.store.claim(self.project))

    def test_rapid_resume_does_not_revive_cancelled_worker(self):
        job = self.enqueue()
        self.store.approve(job, self.project)
        self.store.claim(self.project)
        self.store.pause()
        self.store.pause(False)
        self.assertTrue(self.store.cancelled(job))

    def test_retry_preserves_history_and_grants_only_one_extra_attempt(self):
        project = replace(self.project, max_attempts=1, max_daily_runs=0)
        job = self.enqueue()
        self.store.approve(job, project)
        self.store.claim(project)
        self.store.finish(job, "blocked", "Setup fault")
        self.store.retry(job, project, "Owner requested retry after setup repair")
        restarted = Store(self.root / "queue.db")
        self.assertEqual(restarted.stage(job)["stage"], "queued")
        self.assertEqual(restarted.claim(project)["attempts"], 2)
        restarted.finish(job, "blocked")
        with self.assertRaises(DispatchError):
            restarted.approve(job, project)
        with restarted.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
        self.assertTrue(any(e["action"] == "retry_authorized" for e in restarted.events(job)))

    def test_retry_cannot_interrupt_or_duplicate_active_work(self):
        job = self.enqueue()
        self.store.retry(job, self.project, "Owner requested")
        for status in ("queued", "running", "verifying", "ready", "publishing", "published"):
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job))
            with self.assertRaises(DispatchError):
                self.store.retry(job, self.project, "Owner requested")

    def test_retry_grant_is_bound_to_revision_and_policy(self):
        project = replace(self.project, max_attempts=1, max_daily_runs=0)
        job = self.enqueue()
        self.store.approve(job, project)
        self.store.claim(project)
        self.store.finish(job, "blocked")
        self.store.retry(job, project, "Owner requested")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            self.assertEqual(self.store._attempt_limit(db, row, project), 2)
            self.assertEqual(self.store._attempt_limit(db, row, replace(project, model="other")), 1)
            db.execute("UPDATE jobs SET revision='different' WHERE id=?", (job,))
        self.assertIsNone(self.store.claim(project))

    def test_retry_requires_reason_and_correct_project(self):
        job = self.enqueue()
        for target_project, reason in (
            (self.project, " "),
            (replace(self.project, id="other"), "Retry"),
        ):
            with self.assertRaises(DispatchError):
                self.store.retry(job, target_project, reason)
