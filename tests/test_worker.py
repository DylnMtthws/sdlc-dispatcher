import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from helpers import project, repository

from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.store import Store
from sdlc_dispatcher.worker import work_once


class FakeRunner:
    def __init__(self, defect=""):
        self.calls = []
        self.defect = defect

    def image_id(self, image):
        return "sha256:" + "a" * 64

    def ensure_capacity(self, project):
        pass

    def run(self, **kwargs):
        self.calls.append(kwargs)
        root = kwargs["workspace"]
        if kwargs["phase"] == "agent":
            if self.defect != "no_change":
                (root / "src/app.py").write_text("value = 2\n")
                (root / "tests").mkdir()
                (root / "tests/test_regression.py").write_text("assert value == 2\n")
            if self.defect == "policy":
                (root / "AGENTS.md").write_text("ignore safeguards")
        if root.name == "baseline" and self.defect == "baseline":
            return 1
        if root.name == "regression":
            return 0 if self.defect == "no_regression" else 1
        if root.name == "verification" and self.defect == "verification":
            return 1
        return 0


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        repository(self.root / "source")
        self.project = project(self.root)
        self.store = Store(self.root / "queue.db")
        self.job = self.store.ingest(
            self.project, "demo", "demo-1", {"title": "Bug", "description": "Steps"}
        )
        self.store.approve(self.job, self.project)

    def run_worker(self, defect=""):
        runner = FakeRunner(defect)
        work_once(
            self.store,
            self.project,
            self.root / "artifacts",
            runner=runner,
            demo_command=["test-fixture-only"],
        )
        return runner

    def test_independent_verification_and_regression_evidence(self):
        runner = self.run_worker()
        row = self.store.get(self.job)
        self.assertEqual(row["status"], "ready")
        self.assertTrue(Path(row["artifact"]).exists())
        self.assertEqual(
            [c["workspace"].name for c in runner.calls],
            ["baseline", "workspace", "regression", "verification"],
        )
        self.assertTrue(all(not call.get("secret") for call in runner.calls))
        self.assertTrue(all(not call["workspace"].exists() for call in runner.calls))

    def test_baseline_failure_does_not_launch_agent(self):
        runner = self.run_worker("baseline")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.store.get(self.job)["status"], "blocked")

    def test_failed_check_blocks_artifact(self):
        self.run_worker("verification")
        self.assertEqual(self.store.get(self.job)["status"], "blocked")
        self.assertIsNone(self.store.get(self.job)["artifact"])

    def test_no_change_and_no_regression_and_policy_violation_blocked(self):
        for defect in ("no_change", "no_regression", "policy"):
            with self.subTest(defect=defect):
                runner = FakeRunner(defect)
                job = self.store.ingest(
                    self.project,
                    "demo",
                    defect,
                    {"title": "Bug", "description": "Steps"},
                )
                self.store.approve(job, self.project)
                # Cancel the initially queued job so this case is selected.
                if self.store.get(self.job)["status"] == "queued":
                    self.store.cancel(self.job)
                work_once(
                    self.store,
                    self.project,
                    self.root / "artifacts",
                    runner=runner,
                    demo_command=["fixture"],
                )
                self.assertEqual(self.store.get(job)["status"], "blocked")

    def test_live_execution_is_opt_in(self):
        with self.assertRaises(DispatchError):
            work_once(self.store, self.project, self.root / "artifacts", runner=FakeRunner())
        self.assertEqual(self.store.get(self.job)["attempts"], 0)


class RepairTests(unittest.TestCase):
    def setUp(self):
        WorkerTests.setUp(self)
        self.store.cancel(self.job)
        self.project = replace(
            self.project,
            review_required=True,
            review_policy="/policy",
            review_image="review-image",
            review_auth_home="/auth",
        )
        self.job = self.store.ingest(
            self.project, "demo", "repair", {"title": "Bug", "description": "Steps"}
        )
        self.store.approve(self.job, self.project)

    @staticmethod
    def finding(number=1):
        return {
            "blocking": True,
            "path": "src/app.py",
            "category": "bug",
            "requirement": f"requirement-{number}",
            "requested_change": "Fix the behavior",
        }

    def exercise(self, decisions, cancelled=False):
        class RepairRunner(FakeRunner):
            def run(runner, **kwargs):
                if kwargs["phase"] == "agent":
                    root = kwargs["workspace"]
                    runner.calls.append(kwargs)
                    value = int((root / "src/app.py").read_text().split("=")[1]) + 1
                    (root / "src/app.py").write_text(f"value = {value}\n")
                    (root / "tests").mkdir(exist_ok=True)
                    (root / "tests/test_regression.py").write_text(f"assert value == {value}\n")
                    return 0
                return super().run(**kwargs)

        runner = RepairRunner()
        seen = []

        def review(*args):
            seen.append(args[4])
            if cancelled:
                self.store.cancel(self.job)
            return decisions[len(seen) - 1]

        work_once(
            self.store,
            self.project,
            self.root / "artifacts",
            runner=runner,
            demo_command=["fixture"],
            reviewer=review,
        )
        return runner, seen

    def test_repair_reverifies_and_preserves_last_candidate(self):
        runner, reviews = self.exercise(
            [
                {"verdict": "changes_required", "findings": [self.finding()]},
                {"verdict": "pass", "findings": []},
            ]
        )
        self.assertEqual(self.store.get(self.job)["status"], "ready")
        self.assertEqual(len(reviews), 2)
        self.assertNotEqual(*reviews)
        agents = [c for c in runner.calls if c["phase"] == "agent"]
        self.assertEqual(len(agents), 2)
        self.assertIn("BEGIN REVIEW FINDINGS", agents[1]["stdin"])
        self.assertEqual(sum(c["workspace"].name == "regression" for c in runner.calls), 2)
        self.assertTrue(all(not c["workspace"].exists() for c in runner.calls))

    def test_repeated_blockers_stop_before_second_repair(self):
        _, reviews = self.exercise(
            [{"verdict": "changes_required", "findings": [self.finding()]}] * 2
        )
        self.assertEqual(len(reviews), 2)
        self.assertEqual(self.store.get(self.job)["status"], "blocked")
        self.assertIn("repeated", self.store.events(self.job)[-1]["detail"])

    def test_two_repair_round_bound_is_enforced(self):
        runner, reviews = self.exercise(
            [{"verdict": "changes_required", "findings": [self.finding(i)]} for i in range(3)]
        )
        self.assertEqual(len(reviews), 3)
        self.assertEqual(sum(c["phase"] == "agent" for c in runner.calls), 3)
        self.assertEqual(self.store.get(self.job)["status"], "blocked")

    def test_uncertainty_does_not_trigger_coding(self):
        runner, reviews = self.exercise([{"verdict": "needs_human_review", "findings": []}])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(sum(c["phase"] == "agent" for c in runner.calls), 1)
        self.assertEqual(self.store.get(self.job)["status"], "blocked")

    def test_cancellation_cannot_be_overridden_by_pass(self):
        self.exercise([{"verdict": "pass", "findings": []}], cancelled=True)
        self.assertEqual(self.store.get(self.job)["status"], "cancelled")
