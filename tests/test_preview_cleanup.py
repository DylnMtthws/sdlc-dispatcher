"""Preview cleanup must never remove an unrelated Tailscale route."""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from sdlc_dispatcher.config import DispatchError

spec = importlib.util.spec_from_file_location(
    "deck_lab_evidence", Path(__file__).resolve().parents[1] / "integrations/deck-lab/evidence.py"
)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class PreviewCleanupTests(unittest.TestCase):
    def receipt(self):
        digest = "a" * 64
        name = "sdlc-preview-" + digest[:16]
        return {
            "artifact_digest": digest,
            "https_port": 23690,
            "local_port": 41234,
            "containers": [name + "-after-app", name + "-gateway"],
            "networks": [name + "-after"],
        }

    def test_reassigned_route_is_not_removed(self):
        status = {
            "TCP": {"23690": {}},
            "Web": {"host:23690": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}},
        }
        with patch.object(evidence, "command", return_value=json.dumps(status).encode()) as command:
            with self.assertRaisesRegex(DispatchError, "ownership"):
                evidence.stop(self.receipt())
            self.assertEqual(command.call_count, 1)

    def test_unrelated_container_is_not_removed(self):
        receipt = self.receipt()
        receipt["containers"].append("production-app")
        with patch.object(evidence, "command") as command:
            with self.assertRaisesRegex(DispatchError, "identity"):
                evidence.stop(receipt)
            command.assert_not_called()

    def test_owned_route_and_containers_are_removed(self):
        status = {
            "TCP": {"23690": {}},
            "Web": {"host:23690": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:41234"}}}},
        }
        with patch.object(evidence, "command", return_value=json.dumps(status).encode()) as command:
            evidence.stop(self.receipt())
            self.assertEqual(command.call_count, 5)
            self.assertEqual(
                command.call_args_list[1].args[0], ["tailscale", "serve", "--https=23690", "off"]
            )
