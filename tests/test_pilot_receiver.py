import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "pilot_receiver",
    Path(__file__).resolve().parents[1] / "integrations/deck-lab/receiver.py",
)
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


class PilotReceiverTests(unittest.TestCase):
    def test_secret_requires_private_regular_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "secret"
            self.assertEqual(receiver.read_secret(path), "")
            path.write_text("fixture-only")
            path.chmod(0o600)
            self.assertEqual(receiver.read_secret(path), "fixture-only")
            path.chmod(0o644)
            with self.assertRaises(RuntimeError):
                receiver.read_secret(path)
            link = Path(temporary) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                receiver.read_secret(link)

    def test_rate_limit_expires_and_does_not_trust_forwarded_ips(self):
        calls = []

        def app(environ, start):
            calls.append(environ)
            start("200 OK", [])
            return [b"ok"]

        limited = receiver.RateLimit(app, limit=2)
        statuses = []

        def start(status, headers):
            statuses.append(status)

        with patch.object(receiver.time, "monotonic", side_effect=[0, 1, 2, 61]):
            for ip in ["one", "two", "three", "four"]:
                limited({"HTTP_X_FORWARDED_FOR": ip}, start)
        self.assertEqual(statuses, ["200 OK", "200 OK", "429 Too Many Requests", "200 OK"])
        self.assertEqual(len(calls), 3)

    def test_secret_entry_saves_without_echo_and_restarts_receiver(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "secrets" / "secret"
            with (
                patch.object(receiver, "SECRET", path),
                patch.object(receiver.os, "isatty", return_value=True),
                patch.object(receiver.getpass, "getpass", return_value="fixture-only"),
                patch.object(receiver.subprocess, "run") as run,
                patch("builtins.print") as output,
            ):
                run.return_value.returncode = 0
                receiver.set_secret()
            self.assertEqual(path.read_text(), "fixture-only")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("fixture-only", repr(output.call_args_list))
            self.assertEqual(
                run.call_args.args[0],
                ["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{receiver.LABEL}"],
            )
