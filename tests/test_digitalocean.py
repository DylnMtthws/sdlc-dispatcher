"""Provider activation must be explicit and independent of the developer's host."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sdlc_dispatcher import digitalocean
from sdlc_dispatcher.config import DispatchError


class DigitalOceanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.activation = self.root / ".private/production-active.json"
        self.activation.parent.mkdir()
        root = patch.object(digitalocean, "ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)

    def test_missing_activation_disables_provider(self):
        self.assertFalse(digitalocean.enabled())

    def test_activation_does_not_enable_another_project(self):
        self.activation.write_text(json.dumps({"provider": "digitalocean"}))
        self.assertTrue(digitalocean.enabled())
        self.assertFalse(digitalocean.enabled(SimpleNamespace(github_repository="example/app")))

    def test_malformed_activation_fails_closed_with_safe_error(self):
        for payload in ("not-json", "[]", "null"):
            with self.subTest(payload=payload):
                self.activation.write_text(payload)
                with self.assertRaises(DispatchError):
                    digitalocean.enabled()

    def test_wrong_prepared_commit_cannot_deploy(self):
        with (
            patch.object(digitalocean, "preflight", return_value={"health": {"build_sha": "old"}}),
            patch.object(digitalocean, "operator", return_value={"sha": "unapproved"}) as operator,
        ):
            with self.assertRaisesRegex(DispatchError, "differs from the approved merge"):
                digitalocean.deploy(123, "approved")
        self.assertEqual(operator.call_count, 1)
        self.assertEqual(operator.call_args.args[0], "release_prepare.py")
