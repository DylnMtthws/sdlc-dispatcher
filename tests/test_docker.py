import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from helpers import project

from sdlc_dispatcher.cli import demo
from sdlc_dispatcher.config import DispatchError
from sdlc_dispatcher.runner import DockerRunner


@unittest.skipUnless(
    os.environ.get("DISPATCHER_DOCKER_TESTS") == "1", "opt-in real Docker integration"
)
class DockerTests(unittest.TestCase):
    def setUp(self):
        # Colima shares the user's workspace, but may not share macOS /private/tmp.
        parent = Path.cwd() / ".dispatcher" / "docker-tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="sdlc-tests-", dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runner = DockerRunner()
        self.image = self.runner.image_id("sdlc-dispatcher-test:local")
        self.project = project(self.root, image="sdlc-dispatcher-test:local")

    def test_full_offline_feedback_to_verified_artifact(self):
        result = demo(self.root / "state", "sdlc-dispatcher-test:local")
        self.assertEqual(result["status"], "ready", result)
        self.assertTrue(Path(result["artifact"]).exists())

    def test_nonroot_readonly_offline_without_host_secrets(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        program = """import os, socket
assert os.getuid() != 0
assert 'DISPATCHER_GITHUB_TOKEN' not in os.environ
assert not os.path.exists('/var/run/docker.sock')
try:
    open('/forbidden', 'w')
except OSError:
    pass
else:
    raise AssertionError('root filesystem is writable')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('verification has network access')
open('/workspace/evidence', 'w').write('isolated')
"""
        code = self.runner.run(
            project=self.project,
            image=self.image,
            workspace=workspace,
            command=["python", "-c", program],
            job="isolation-test",
            phase="check",
            deadline=time.time() + 30,
            cancelled=lambda: False,
            log=self.root / "check.log",
        )
        self.assertEqual(code, 0, (self.root / "check.log").read_text())
        self.assertEqual((workspace / "evidence").read_text(), "isolated")

    def test_timeout_stops_actual_container(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        with self.assertRaises(DispatchError):
            self.runner.run(
                project=self.project,
                image=self.image,
                workspace=workspace,
                command=["python", "-c", "import time; time.sleep(100)"],
                job="timeout-test",
                phase="agent",
                deadline=time.time() + 1,
                cancelled=lambda: False,
                log=self.root / "timeout.log",
            )
        # stop is idempotent, and runner confirmed container removal before returning.
        self.runner.stop("timeout-test")

    def test_agent_cannot_bypass_model_proxy(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        program = """import socket, urllib.request, urllib.error
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('Agent has direct outbound access')
try:
    urllib.request.urlopen('https://example.com', timeout=3)
except urllib.error.URLError as error:
    assert '403' in str(error), str(error)
else:
    raise AssertionError('Proxy allowed a non-model destination')
"""
        code = self.runner.run(
            project=self.project,
            image=self.image,
            workspace=workspace,
            command=["python", "-c", program],
            job="egress-test",
            phase="agent",
            deadline=time.time() + 30,
            cancelled=lambda: False,
            log=self.root / "egress.log",
            network=True,
        )
        self.assertEqual(code, 0, (self.root / "egress.log").read_text())

    def test_cursor_receives_only_its_key_and_cannot_reach_other_origins(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        program = """import os, socket, urllib.request, urllib.error
assert os.environ['CURSOR_API_KEY'] == 'fixture-only'
assert 'CODEX_API_KEY' not in os.environ
assert 'DISPATCHER_LINEAR_API_KEY' not in os.environ
assert 'DISPATCHER_GITHUB_TOKEN' not in os.environ
assert not os.path.exists('/var/run/docker.sock')
assert os.environ['CURSOR_CONFIG_DIR'].startswith('/tmp/')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('Direct egress was possible')
for origin in ('https://api.openai.com', 'https://example.com'):
    try:
        urllib.request.urlopen(origin, timeout=3)
    except urllib.error.URLError as error:
        assert '403' in str(error), str(error)
    else:
        raise AssertionError('Cursor proxy reached another origin')
"""
        code = self.runner.run(
            project=replace(self.project, engine="cursor", model="gpt-5.6-sol-medium"),
            image=self.image,
            workspace=workspace,
            command=["python", "-c", program],
            job="cursor-egress-test",
            phase="agent",
            deadline=time.time() + 30,
            cancelled=lambda: False,
            log=self.root / "cursor-egress.log",
            secret="fixture-only",
            network=True,
        )
        self.assertEqual(code, 0, (self.root / "cursor-egress.log").read_text())

    def test_worker_waits_for_slow_proxy_startup(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        read_text = Path.read_text

        def delayed_proxy(path, *args, **kwargs):
            source = read_text(path, *args, **kwargs)
            if path.name == "egress_proxy.py":
                return "import time; time.sleep(2)\n" + source
            return source

        program = """import urllib.request, urllib.error
try:
    urllib.request.urlopen('https://example.com', timeout=3)
except urllib.error.URLError as error:
    assert '403' in str(error), str(error)
else:
    raise AssertionError('Proxy allowed a non-model destination')
"""
        with patch.object(Path, "read_text", delayed_proxy):
            code = self.runner.run(
                project=self.project,
                image=self.image,
                workspace=workspace,
                command=["python", "-c", program],
                job="slow-proxy-test",
                phase="agent",
                deadline=time.time() + 30,
                cancelled=lambda: False,
                log=self.root / "slow-proxy.log",
                network=True,
            )
        self.assertEqual(code, 0, (self.root / "slow-proxy.log").read_text())
