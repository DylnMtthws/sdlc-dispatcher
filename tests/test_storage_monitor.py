"""Storage incidents remain failures until the observed condition recovers."""

import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class HelperLoader:
    def create_module(self, spec):
        helper = types.ModuleType(spec.name)
        helper.StorageError = type("StorageError", (Exception,), {})
        return helper

    def exec_module(self, module):
        pass


def monitor_module():
    original = importlib.util.spec_from_file_location
    source = Path(__file__).resolve().parents[1] / "integrations/deck-lab/storage.py"
    spec = original("test_storage_monitor_integration", source)
    module = importlib.util.module_from_spec(spec)

    def helper_spec(name, path):
        if name == "installed_storage":
            return importlib.util.spec_from_loader(name, HelperLoader())
        return original(name, path)

    with (
        patch.dict("sys.modules", {"sync": types.SimpleNamespace(clients=lambda: None)}),
        patch("importlib.util.spec_from_file_location", side_effect=helper_spec),
    ):
        spec.loader.exec_module(module)
    return module


class StorageMonitorTests(unittest.TestCase):
    def test_large_wal_stays_blocked_between_checkpoints_then_recovers(self):
        monitor = monitor_module()
        report = {"wal_bytes": 129 * 1024 * 1024, "free": 2_000_000_000}
        observed = []
        with tempfile.TemporaryDirectory() as temporary:
            monitor.STATE = Path(temporary)
            (monitor.STATE / "storage-config.json").write_text("{}")
            state = monitor.STATE / "storage-monitor.json"
            state.write_text(
                json.dumps({"checkpoint_at": 10000, "backup_at": 10000, "restore_at": 10000})
            )
            monitor.storage_client = lambda: types.SimpleNamespace(remote=lambda action: report)
            monitor.publish_status = lambda config, previous, current, error: observed.append(error)
            with (
                patch.object(monitor.time, "time", return_value=10001),
                patch("sdlc_dispatcher.digitalocean.enabled", return_value=False),
                patch("builtins.print"),
            ):
                monitor.run()
                monitor.run()
                self.assertTrue(all("remains large" in error for error in observed))
                self.assertIn("remains large", json.loads(state.read_text())["error"])
                report["wal_bytes"] = 0
                monitor.run()
                self.assertEqual(observed[-1], "")
                self.assertEqual(json.loads(state.read_text())["error"], "")
