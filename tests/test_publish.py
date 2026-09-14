import json
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from helpers import project

from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.http import ProviderError
from sdlc_dispatcher.publish import publish, publish_ready
from sdlc_dispatcher.store import Store
from sdlc_dispatcher.workspace import write_artifact


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.branch = False
        self.pr = None
        self.lose_response = False
        self.base_sha = "a" * 40

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/git/ref/heads/main":
            return {"object": {"sha": self.base_sha}}
        if path == "/git/commits/" + "a" * 40:
            return {"tree": {"sha": "base-tree"}}
        if path == "/git/trees":
            return {"sha": "candidate-tree"}
        if path.startswith("/git/ref/heads/sdlc/"):
            if not self.branch:
                raise ProviderError(404)
            return {"object": {"sha": "commit"}}
        if path == "/git/commits" and method == "POST":
            return {"sha": "commit"}
        if path == "/git/refs":
            self.branch = True
            return {"object": {"sha": "commit"}}
        if path == "/git/commits/commit":
            return {"tree": {"sha": "candidate-tree"}, "parents": [{"sha": "a" * 40}]}
        if path.startswith("/pulls?"):
            return [self.pr] if self.pr else []
        if path == "/pulls":
            self.pr = {"html_url": "https://github.com/owner/repository/pull/1"}
            if self.lose_response:
                self.lose_response = False
                raise DispatchError("Response lost")
            return self.pr
        raise AssertionError((method, path))


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = project(self.root, github_repository="owner/repository")
        self.store = Store(self.root / "queue.db")
        self.job = self.store.ingest(
            self.project,
            "manual",
            "1",
            {"title": "PRIVATE NAME", "description": "PRIVATE EMAIL"},
        )
        self.store.approve(self.job, self.project)
        self.store.claim(self.project)
        self.artifact = {
            "engine": "codex",
            "revision": self.store.get(self.job)["revision"],
            "base_sha": "a" * 40,
            "image": "sha256:trusted",
            "changes": [
                {
                    "path": "src/app.py",
                    "before": "old",
                    "after": "new",
                    "mode": "100644",
                }
            ],
        }
        path, digest = write_artifact(self.root, self.artifact)
        self.store.finish(
            self.job, "ready", artifact=path, artifact_digest=digest, base_sha="a" * 40
        )
        self.client = FakeGitHub()

    def test_publishes_draft_without_private_report_or_deployment(self):
        url = publish(self.store, self.project, self.job, client=self.client)
        self.assertTrue(url.endswith("/pull/1"))
        creation = [body for method, path, body in self.client.calls if path == "/pulls"][0]
        self.assertTrue(creation["draft"])
        self.assertNotIn("PRIVATE", json.dumps(creation))
        self.assertFalse(
            any("merge" in path or "deploy" in path for _, path, _ in self.client.calls)
        )
        count = len(self.client.calls)
        self.assertEqual(publish(self.store, self.project, self.job, client=self.client), url)
        self.assertEqual(len(self.client.calls), count)

    def test_automatic_publication_reconciles_after_lease_and_retains_diagnostic(self):
        configured = replace(
            self.project,
            automatic_publication=True,
            review_required=True,
            review_policy="/policy",
            review_image="review",
            review_auth_home="/auth",
            publish_command=["trusted-publisher"],
        )

        def partial_write(*args, **kwargs):
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE jobs SET status='publishing',deadline=700 WHERE id=?", (self.job,)
                )
            raise subprocess.CalledProcessError(
                1, args[0], stderr=b'{"error":"Provider transport error"}'
            )

        with patch("sdlc_dispatcher.publish.subprocess.run", side_effect=partial_write):
            publish_ready(self.store, configured, self.job, now=100)
        self.assertEqual(self.store.events(self.job)[-1]["action"], "publication_retry_scheduled")
        self.assertIn("Provider transport error", self.store.events(self.job)[-1]["detail"])
        with patch("sdlc_dispatcher.publish.subprocess.run") as run:
            publish_ready(self.store, configured, self.job, now=200)
            run.assert_not_called()
            publish_ready(self.store, configured, self.job, now=701)
            self.assertEqual(run.call_args.args[0], ["trusted-publisher", self.job, "--reconcile"])

    def test_publication_recovery_is_bounded_and_does_not_stall_coding(self):
        configured = replace(
            self.project,
            automatic_publication=True,
            review_required=True,
            review_policy="/policy",
            review_image="review",
            review_auth_home="/auth",
            publish_command=["trusted-publisher"],
        )
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET status='publishing',deadline=0 WHERE id=?", (self.job,))
        with patch(
            "sdlc_dispatcher.publish.subprocess.run",
            side_effect=subprocess.CalledProcessError(
                1, ["publish"], stderr=b'{"error":"Offline"}'
            ),
        ) as run:
            for now in [100, 161, 222, 283]:
                publish_ready(self.store, configured, self.job, now=now)
            self.assertEqual(run.call_count, 3)
        self.assertEqual(self.store.events(self.job)[-1]["action"], "publication_needs_attention")
        another = self.store.ingest(
            self.project, "manual", "another", {"title": "Bug", "description": "Fix"}
        )
        self.store.approve(another, self.project)
        self.assertEqual(self.store.claim(self.project)["id"], another)

    def test_lost_pr_response_reconciles_without_duplicate_creation(self):
        self.client.lose_response = True
        with self.assertRaises(DispatchError):
            publish(self.store, self.project, self.job, client=self.client)
        self.assertEqual(self.store.get(self.job)["status"], "publishing")
        with self.assertRaises(DispatchError):
            publish(self.store, self.project, self.job, reconcile=True, client=self.client)
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 1, self.job))
        publish(self.store, self.project, self.job, reconcile=True, client=self.client)
        self.assertEqual(
            sum(method == "POST" and path == "/pulls" for method, path, _ in self.client.calls),
            1,
        )

    def test_tampered_artifact_prevents_provider_calls(self):
        Path(self.store.get(self.job)["artifact"]).write_text("tampered")
        with self.assertRaises(DispatchError):
            publish(self.store, self.project, self.job, client=self.client)
        self.assertFalse(self.client.calls)

    def test_moved_base_prevents_writes(self):
        self.client.base_sha = "b" * 40
        with self.assertRaises(DispatchError):
            publish(self.store, self.project, self.job, client=self.client)
        self.assertTrue(all(method == "GET" for method, _, _ in self.client.calls))
        self.assertEqual(self.store.get(self.job)["status"], "blocked")
        self.store.approve(self.job, self.project)
        self.assertEqual(self.store.get(self.job)["status"], "queued")

    def test_existing_pr_reconciles_even_if_base_later_moves(self):
        self.client.lose_response = True
        with self.assertRaises(DispatchError):
            publish(self.store, self.project, self.job, client=self.client)
        self.client.base_sha = "b" * 40
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET deadline=0 WHERE id=?", (self.job,))
        result = publish(self.store, self.project, self.job, reconcile=True, client=self.client)
        self.assertTrue(result.endswith("/pull/1"))

    def test_pause_prevents_publication(self):
        self.store.pause()
        with self.assertRaises(DispatchError):
            publish(self.store, self.project, self.job, client=self.client)
        self.assertFalse(self.client.calls)
