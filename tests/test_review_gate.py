import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import project, repository

from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.privacy import model_report
from sdlc_dispatcher.review_contract import PROMPT, SCHEMA
from sdlc_dispatcher.review_gate import publication_review, sha
from sdlc_dispatcher.review_packet import prepare_packet
from sdlc_dispatcher.store import Store
from sdlc_dispatcher.worker import previous_review_feedback
from sdlc_dispatcher.workspace import File, changes, export, snapshot, write_artifact


class ReviewGateTests(unittest.TestCase):
    def test_model_report_omits_contact_and_token_values(self):
        report = {
            "title": "Mana icons",
            "description": "User: example@example.test key ghp_" + "a" * 30,
        }
        redacted = model_report(report)
        self.assertNotIn("example@example.test", redacted["description"])
        self.assertNotIn("ghp_", redacted["description"])
        self.assertEqual(redacted["title"], report["title"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        repository(self.root / "source")
        policy = self.root / "policy.md"
        policy.write_text("Trusted policy")
        self.project = project(
            self.root,
            review_required=True,
            review_policy=str(policy),
            review_image="review",
            review_auth_home=str(self.root / "auth"),
        )
        self.store = Store(self.root / "queue.db")
        self.job = self.store.ingest(
            self.project,
            "manual",
            "issue",
            {"title": "Private feedback", "description": "Private identity"},
        )
        self.store.approve(self.job, self.project)
        self.store.claim(self.project)
        base_sha, before = export(self.project)
        after = before | {"src/app.py": File(b"value = 2\n")}
        self.artifact = {
            "engine": "cursor",
            "model": "test",
            "project": self.project.id,
            "policy": self.project.fingerprint,
            "base_sha": base_sha,
            "revision": self.store.get(self.job)["revision"],
            "changes": changes(self.project, before, after),
            "checks": [{"command": ["test"], "exit_code": 0}],
        }
        path, self.digest = write_artifact(self.root, self.artifact)
        packet = self.root / "packet"
        prepare_packet(
            project=self.project,
            artifact=Path(path),
            expected_digest=self.digest,
            issue={"revision": self.artifact["revision"]},
            policy=policy,
            evidence={},
            images=[],
            limitations=[],
            destination=packet,
        )
        self.folder = self.root / "review"
        shutil.copytree(packet, self.folder / "input")
        (self.folder / "input/schema.json").write_text(json.dumps(SCHEMA))
        (self.folder / "input/prompt.txt").write_text(PROMPT)
        self.review = {
            "schema_version": 1,
            "verdict": "pass",
            "summary": "Pass",
            "findings": [],
            "coverage": ["patch"],
            "limitations": ["No browser"],
            "suggested_debt": [],
        }
        self.store.finish(self.job, "verifying")
        self.record()
        self.store.finish(
            self.job, "ready", artifact=path, artifact_digest=self.digest, base_sha=base_sha
        )

    def record(self):
        (self.folder / "review.json").write_text(json.dumps(self.review))
        hashes = {
            name: sha(item.data)
            for name, item in sorted(snapshot(self.folder / "input", 1_000_000).items())
        }
        metadata = {
            "status": "completed",
            "cleanup": "confirmed",
            "kind": "candidate_review",
            "model_requested": "gpt-6-astra",
            "model_reported": "gpt-6-astra",
            "reasoning_effort": "high",
            "input_hashes": hashes,
            "input_digest": sha(json.dumps(hashes, sort_keys=True).encode()),
            "review_digest": sha((self.folder / "review.json").read_bytes()),
            "verdict": self.review["verdict"],
        }
        path = self.folder / "metadata.json"
        path.write_text(json.dumps(metadata))
        self.store.record_review(
            self.job, self.digest, path, sha(path.read_bytes()), self.review["verdict"]
        )

    def validate(self):
        return publication_review(self.store, self.project, self.store.get(self.job), self.artifact)

    def test_current_review_passes(self):
        self.assertEqual(self.validate()["review"]["verdict"], "pass")

    def test_retry_receives_verified_feedback_only_for_same_report_revision(self):
        job = self.store.get(self.job)
        self.assertIn("BEGIN PRIOR REVIEW", previous_review_feedback(self.store, job))
        self.assertEqual(previous_review_feedback(self.store, {**job, "revision": "changed"}), "")
        (self.folder / "review.json").write_text("tampered")
        self.assertEqual(previous_review_feedback(self.store, job), "")

    def test_missing_review_blocks(self):
        with self.store.transaction() as db:
            db.execute("DELETE FROM reviews")
        with self.assertRaisesRegex(DispatchError, "required"):
            self.validate()

    def test_changed_metadata_source_result_and_policy_each_block(self):
        paths = [
            self.folder / "metadata.json",
            self.folder / "input/source/src/app.py",
            self.folder / "review.json",
            Path(self.project.review_policy),
        ]
        for path in paths:
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.subTest(path=path), self.assertRaises(DispatchError):
                self.validate()
            path.write_bytes(original)

    def test_changed_issue_cannot_reuse_review(self):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET revision='different' WHERE id=?", (self.job,))
        with self.assertRaisesRegex(DispatchError, "Issue changed"):
            self.validate()

    def test_blocking_review_is_not_publishable_even_if_worker_marks_ready(self):
        self.review.update(verdict="needs_human_review")
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET status='verifying' WHERE id=?", (self.job,))
        self.record()
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET status='ready' WHERE id=?", (self.job,))
        with self.assertRaisesRegex(DispatchError, "prevent publication"):
            self.validate()

    def test_different_candidate_does_not_inherit_pass(self):
        self.artifact["changes"][0]["after"] = "different\n"
        with self.assertRaisesRegex(DispatchError, "source differs"):
            self.validate()
