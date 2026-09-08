import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import project

from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.review_contract import validate_review
from sdlc_dispatcher.review_packet import prepare_packet
from sdlc_dispatcher.review_proxy import MAX_BODY, validate_request, verify_event
from sdlc_dispatcher.reviewer import run_review
from sdlc_dispatcher.reviewer_auth import private_file
from sdlc_dispatcher.workspace import File
from sdlc_dispatcher.workspace import changes as workspace_changes


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.body = {
            "model": "gpt-6-astra",
            "stream": True,
            "store": False,
            "reasoning": {"effort": "high"},
        }
        self.headers = {"Authorization": "Bearer capability"}

    def validate(self, body=None, path="/responses", headers=None):
        return validate_request(
            path,
            self.headers if headers is None else headers,
            json.dumps(self.body if body is None else body).encode(),
            "capability",
        )

    def test_exact_astra_high_only(self):
        self.assertEqual(self.validate()["model"], "gpt-6-astra")
        for change in (
            {"model": "auto"},
            {"model": "gpt-6-astra-fast"},
            {"reasoning": {"effort": "low"}},
            {"stream": False},
            {"store": True},
            {"service_tier": "priority"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(self.body | change)

    def test_no_route_or_capability_bypass(self):
        for path in (
            "/responses?x=1",
            "/v1/models",
            "https://chatgpt.com/responses",
            "/responses/../other",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate(path=path)
        for headers in (
            {},
            {"Authorization": "Bearer wrong"},
            self.headers | {"Content-Encoding": "gzip"},
        ):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                self.validate(headers=headers)

    def test_bounded_input_and_invalid_json(self):
        for body in (b"", b"not json", b" " * (MAX_BODY + 1), b"[]"):
            with self.assertRaises(ValueError):
                validate_request("/responses", self.headers, body, "capability")

    def test_provider_model_is_independent_of_requested_model(self):
        self.assertTrue(
            verify_event({"type": "response.completed", "response": {"model": "gpt-6-astra"}})
        )
        for event in (
            {"type": "response.completed"},
            {"type": "response.completed", "response": {"model": "other"}},
            {"type": "response.created", "response": {"model": "other"}},
        ):
            with self.assertRaises(ValueError):
                verify_event(event)

    def test_provider_hosted_tools_and_malformed_reasoning_fail_closed(self):
        for change in (
            {"reasoning": []},
            {"tools": [{"type": "web_search"}]},
            {"tools": [{"type": "mcp", "server_url": "https://example.com"}]},
            {"tools": [{"type": "namespace", "tools": [{"type": "web_search"}]}]},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(self.body | change)
        for event in (
            {"response": [], "type": "response.completed"},
            {"response": "bad"},
            {"type": "response.failed"},
        ):
            with self.assertRaises(ValueError):
                verify_event(event)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)
        (self.source / "app.py").write_text("first\nsecond\n")
        self.review = {
            "schema_version": 1,
            "verdict": "pass",
            "summary": "Reviewed",
            "findings": [],
            "coverage": ["patch"],
            "limitations": [],
            "suggested_debt": [],
        }
        self.finding = {
            "id": "F1",
            "severity": "high",
            "blocking": True,
            "category": "bug",
            "path": "app.py",
            "line": 2,
            "requirement": "preserve behavior",
            "evidence": "observed failure",
            "impact": "broken flow",
            "requested_change": "correct the failure",
        }

    def test_valid_pass_and_supported_block(self):
        validate_review(self.review, self.source)
        self.review.update(verdict="changes_required", findings=[self.finding])
        validate_review(self.review, self.source)

    def test_contradictory_and_unsubstantiated_verdicts_fail(self):
        for changes in (
            {"findings": [self.finding]},
            {"verdict": "changes_required"},
            {"verdict": "needs_human_review"},
            {"schema_version": True},
            {"extra": "not allowed"},
        ):
            with self.subTest(changes=changes), self.assertRaises(DispatchError):
                validate_review(self.review | changes, self.source)

    def test_invalid_citations_and_severity_fail(self):
        for changes in (
            {"path": "../secret"},
            {"path": "missing.py"},
            {"line": 3},
            {"line": 0},
            {"blocking": False},
            {"evidence": ""},
            {"severity": "low"},
        ):
            finding = copy.deepcopy(self.finding) | changes
            with self.subTest(changes=changes), self.assertRaises(DispatchError):
                validate_review(
                    self.review | {"verdict": "changes_required", "findings": [finding]},
                    self.source,
                )

    def test_private_auth_rejects_shared_and_symlink_files(self):
        auth = self.source / "auth.json"
        auth.write_text("not a credential")
        auth.chmod(0o600)
        private_file(auth)
        auth.chmod(0o644)
        with self.assertRaises(DispatchError):
            private_file(auth)
        link = self.source / "link"
        link.symlink_to(auth)
        with self.assertRaises(DispatchError):
            private_file(link)


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = project(self.root)
        self.base = {
            "src/app.py": File(b"before\n"),
            "src/context.py": File(b"context\n"),
            "docs/private.pdf": File(b"unrelated private document"),
        }
        after = self.base | {"src/app.py": File(b"after\n")}
        self.artifact = self.root / "artifact.json"
        self.artifact.write_text(
            json.dumps(
                {
                    "project": self.project.id,
                    "policy": self.project.fingerprint,
                    "base_sha": "a" * 40,
                    "changes": workspace_changes(self.project, self.base, after),
                    "checks": [{"command": ["test"], "exit_code": 0}],
                }
            )
        )
        self.expected = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        self.policy = self.root / "policy.md"
        self.policy.write_text("Operator policy")

    def prepare(self, **kwargs):
        with patch("sdlc_dispatcher.review_packet.export", return_value=("a" * 40, self.base)):
            return prepare_packet(
                project=self.project,
                artifact=self.artifact,
                expected_digest=self.expected,
                issue={"id": "issue"},
                policy=self.policy,
                evidence={},
                images=[],
                limitations=[],
                destination=self.root / "packet",
                **kwargs,
            )

    def test_only_changed_files_and_explicit_context_leave_repository(self):
        self.prepare(context_paths=("src/context.py",))
        source = self.root / "packet/source"
        self.assertEqual(
            sorted(str(p.relative_to(source)) for p in source.rglob("*") if p.is_file()),
            ["src/app.py", "src/context.py"],
        )
        self.assertEqual((source / "src/app.py").read_text(), "after\n")

    def test_changed_only_is_default(self):
        self.prepare()
        self.assertFalse((self.root / "packet/source/src/context.py").exists())
        self.assertFalse((self.root / "packet/source/docs").exists())

    def test_changed_artifact_is_rejected(self):
        self.artifact.write_text(self.artifact.read_text() + " ")
        with self.assertRaisesRegex(DispatchError, "differs"):
            self.prepare()

    def test_missing_or_unsafe_context_is_rejected(self):
        for name in ("src/missing.py", "../private"):
            with self.subTest(name=name), self.assertRaises(DispatchError):
                self.prepare(context_paths=(name,))


@unittest.skipUnless(os.environ.get("DISPATCHER_DOCKER_TESTS") == "1", "opt-in Docker")
class ReviewerDockerTests(unittest.TestCase):
    def setUp(self):
        parent = Path.cwd() / ".dispatcher" / "docker-tests"
        parent.mkdir(exist_ok=True, parents=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packet = self.root / "packet"
        self.packet.mkdir()
        (self.packet / "packet.json").write_text('{"kind":"access_probe","images":[]}')
        (self.packet / "policy.md").write_text("Probe only")
        (self.packet / "review.diff").write_text("")

    def run_reviewer(self, **kwargs):
        return run_review(
            packet=self.packet,
            output=self.root / "result",
            auth_home=self.root,
            image="deck-lab-agent:local",
            **kwargs,
        )

    def test_actual_reviewer_boundary_and_proxy(self):
        result = self.run_reviewer(containment_only=True)
        self.assertEqual(result["cleanup"], "confirmed")
        self.assertEqual(result["containment"]["containment"], "passed")

    def test_timeout_fails_closed_and_cleans_up(self):
        with (
            patch(
                "sdlc_dispatcher.reviewer.access_lease",
                return_value={"access_token": "dummy", "account_id": "dummy"},
            ),
            patch(
                "sdlc_dispatcher.reviewer.codex_command",
                return_value=["python", "-c", "import time; time.sleep(100)"],
            ),
            self.assertRaisesRegex(DispatchError, "timed out"),
        ):
            self.run_reviewer(timeout=10)
        result = json.loads((self.root / "result/metadata.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["cleanup"], "confirmed")
