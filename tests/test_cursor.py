import io
import json
import os
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from helpers import project, repository
from test_worker import FakeRunner

from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.cursor_entrypoint import available_model, run
from sdlc_dispatcher.egress_proxy import CURSOR_HOSTS, resolve_target
from sdlc_dispatcher.runner import agent_command
from sdlc_dispatcher.store import Store
from sdlc_dispatcher.worker import work_once

MODEL = "grok-4.6[effort=xhigh,fast=false]"
DISPLAY = "Cursor Grok 4.6 Extra High"


class CursorTests(unittest.TestCase):
    def preflight_fixture(
        self, directory, *, actual_effort="xhigh", response_effort="xhigh", error=False
    ):
        def setting(name, value, options):
            return {
                "id": name,
                "type": "select",
                "currentValue": value,
                "options": [{"value": item} for item in options],
            }

        model = setting("model", "grok-4.6", ["grok-4.6"])
        effort = setting("effort", "high", ["high", "xhigh"])
        fast = setting("fast", "true", ["true", "false"])
        responses = [
            {"protocolVersion": 1},
            {"sessionId": "fixture", "configOptions": [model]},
            {"configOptions": [model, effort, fast]},
            {"configOptions": [model, {**effort, "currentValue": response_effort}, fast]},
            {
                "configOptions": [
                    model,
                    {**effort, "currentValue": "xhigh"},
                    {**fast, "currentValue": "false"},
                ]
            },
        ]
        messages = [
            {"jsonrpc": "2.0", "id": i, "result": result} for i, result in enumerate(responses, 1)
        ]
        if error:
            messages[0] = {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -1, "message": "sensitive-fixture"},
            }
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.StringIO("".join(json.dumps(item) + "\n" for item in messages))
        process.stdin = io.StringIO()
        process.poll.return_value = None
        Path(directory, "cli-config.json").write_text(
            json.dumps(
                {
                    "model": {"modelId": "grok-4.6", "displayName": DISPLAY},
                    "selectedModel": {
                        "modelId": "grok-4.6",
                        "parameters": [
                            {"id": "effort", "value": actual_effort},
                            {"id": "fast", "value": "false"},
                        ],
                    },
                }
            )
        )
        return process

    def test_preflight_selects_exact_parameters_without_prompt(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"CURSOR_CONFIG_DIR": directory}),
            patch("sdlc_dispatcher.cursor_entrypoint.subprocess.Popen") as popen,
        ):
            process = self.preflight_fixture(directory)
            popen.return_value = process
            self.assertEqual(available_model(MODEL), DISPLAY)
            requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
            self.assertEqual(
                [r["method"] for r in requests],
                ["initialize", "session/new"] + ["session/set_config_option"] * 3,
            )
            self.assertEqual(
                [r["params"]["value"] for r in requests[2:]], ["grok-4.6", "xhigh", "false"]
            )
            process.terminate.assert_called_once()

    def test_preflight_rejects_unknown_models_and_substitutions(self):
        for model, kwargs in [
            ("unavailable", {}),
            (MODEL, {"actual_effort": "high"}),
            (MODEL, {"response_effort": "high"}),
            ("grok-4.6", {}),
            ("grok-4.6[effort=unknown,fast=false]", {}),
        ]:
            with (
                self.subTest(model=model, kwargs=kwargs),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(os.environ, {"CURSOR_CONFIG_DIR": directory}),
                patch("sdlc_dispatcher.cursor_entrypoint.subprocess.Popen") as popen,
            ):
                process = self.preflight_fixture(directory, **kwargs)
                popen.return_value = process
                with self.assertRaises(RuntimeError):
                    available_model(model)
                self.assertNotIn("session/prompt", process.stdin.getvalue())
                process.terminate.assert_called_once()

    def test_model_preflight_does_not_echo_provider_error(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"CURSOR_CONFIG_DIR": directory}),
            patch("sdlc_dispatcher.cursor_entrypoint.subprocess.Popen") as popen,
        ):
            popen.return_value = self.preflight_fixture(directory, error=True)
            with self.assertRaises(RuntimeError) as error:
                available_model(MODEL)
            self.assertNotIn("sensitive-fixture", str(error.exception))

    def test_auto_and_unknown_engine_are_rejected(self):
        for kwargs in (
            {"engine": "other"},
            {"engine": "cursor"},
            {"engine": "cursor", "model": "auto"},
            {"engine": "cursor", "model": "auto[fast=false]"},
            {"engine": "cursor", "model": "default"},
            {"engine": "cursor", "model": "grok-4.6[effort=high,effort=xhigh]"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(DispatchError):
                project(Path("/tmp/test"), **kwargs)

    def fake_stream(self, model=DISPLAY, result=True):
        events = [{"type": "system", "subtype": "init", "model": model}]
        if result:
            events.append({"type": "result", "subtype": "success", "is_error": False})
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))
        process.wait.return_value = 0
        process.poll.return_value = 0
        return process

    def test_run_requires_selected_model_and_success_event(self):
        for model, result in [
            (DISPLAY, True),
            ("composer-2.5", True),
            (DISPLAY, False),
            (MODEL, True),
        ]:
            with (
                self.subTest(model=model, result=result),
                patch("sdlc_dispatcher.cursor_entrypoint.subprocess.Popen") as popen,
                patch("builtins.print"),
            ):
                popen.return_value = self.fake_stream(model, result)
                if model == DISPLAY and result:
                    run(MODEL, DISPLAY, "fixture report")
                else:
                    with self.assertRaises(RuntimeError):
                        run(MODEL, DISPLAY, "fixture report")
                argv = popen.call_args.args[0]
                self.assertEqual(argv[argv.index("--model") + 1], MODEL)
                self.assertNotIn("--api-key", argv)

    def test_cursor_proxy_only_allows_cursor_origin_and_public_addresses(self):
        for target in (
            "api.openai.com:443",
            "api2.cursor.sh.evil.test:443",
            "api2.cursor.sh:80",
            "agentn.global.api5.cursor.sh.evil.test:443",
            "unlisted.api5.cursor.sh:443",
            "agentn.global.api5.cursor.sh:80",
            "downloads.cursor.com:443",
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                resolve_target(target, CURSOR_HOSTS)
        with patch("socket.getaddrinfo") as dns:
            dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
            with self.assertRaises(ValueError):
                resolve_target("api2.cursor.sh:443", CURSOR_HOSTS)
            dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
            for host in CURSOR_HOSTS:
                self.assertEqual(resolve_target(host + ":443", CURSOR_HOSTS), dns.return_value)
                dns.assert_called_with(host, 443, type=socket.SOCK_STREAM)
            dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
            for host in CURSOR_HOSTS:
                with self.assertRaises(ValueError):
                    resolve_target(host + ":443", CURSOR_HOSTS)

    def test_model_mismatch_is_recorded_and_process_is_stopped(self):
        with (
            patch("sdlc_dispatcher.cursor_entrypoint.subprocess.Popen") as popen,
            patch.dict(os.environ, {"CURSOR_API_KEY": "secret-fixture"}),
            patch("builtins.print") as output,
        ):
            process = self.fake_stream("unexpected secret-fixture")
            process.poll.return_value = None
            popen.return_value = process
            with self.assertRaisesRegex(RuntimeError, "different or unknown"):
                run(MODEL, DISPLAY, "fixture report")
            diagnostic = json.loads(output.call_args_list[0].args[0])
            self.assertEqual(diagnostic["requested"], MODEL)
            self.assertEqual(diagnostic["reported"], "unexpected [REDACTED]")
            process.terminate.assert_called_once()

    def test_cursor_job_keeps_checks_offline_and_records_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository(root / "source")
            registration = project(root, engine="cursor", model=MODEL)
            store = Store(root / "queue.db")
            job = store.ingest(
                registration, "manual", "bug", {"title": "Bug", "description": "Steps"}
            )
            store.approve(job, registration)
            runner = FakeRunner()
            with patch.dict(os.environ, {"DISPATCHER_CURSOR_API_KEY": "cursor-fixture"}):
                work_once(store, registration, root / "artifacts", runner, allow_live=True)
            row = store.get(job)
            self.assertEqual(row["status"], "ready")
            artifact = json.loads(Path(row["artifact"]).read_text())
            self.assertEqual((artifact["engine"], artifact["model"]), ("cursor", MODEL))
            for call in runner.calls:
                if call["phase"] == "agent":
                    self.assertEqual(call["secret"], "cursor-fixture")
                    self.assertEqual(call["command"], agent_command(registration))
                else:
                    self.assertFalse(call.get("secret"))
                    self.assertFalse(call.get("network"))

    def test_changing_engine_invalidates_existing_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registration = project(root)
            store = Store(root / "queue.db")
            job = store.ingest(
                registration, "manual", "bug", {"title": "Bug", "description": "Steps"}
            )
            store.approve(job, registration)
            cursor = replace(registration, engine="cursor", model=MODEL)
            self.assertIsNone(store.claim(cursor))
            self.assertEqual(store.get(job)["attempts"], 0)
