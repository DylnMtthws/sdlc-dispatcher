import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers import project

from sdlc_dispatcher.release_controller import ReviewChanged
from sdlc_dispatcher.release_store import latest
from sdlc_dispatcher.store import Store
from sdlc_dispatcher.workspace import File

integration = Path(__file__).resolve().parents[1] / "integrations/deck-lab"
sys.path.insert(0, str(integration))
spec = importlib.util.spec_from_file_location(
    "release_integration_test", integration / "release.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.path.remove(str(integration))


class EvidenceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "queue.db")
        self.project = project(self.root)
        self.evidence = module.Evidence(self.store, self.project, Mock())
        self.evidence.root = self.root / "release"
        self.evidence.repository = self.root / "source"

    def test_release_initializes_tables_even_when_used_without_service(self):
        self.assertIsNone(latest(self.store, "job"))

    def test_completed_coding_is_not_a_cancelled_release(self):
        self.assertFalse(self.evidence.release_cancelled("published-job"))

    def test_cached_new_head_does_not_bypass_approved_patch_check(self):
        old = self.root / "old"
        (old / "input").mkdir(parents=True)
        (old / "input/packet.json").write_text(json.dumps({"base_sha": "old-base"}))
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO review_cards(job,head,digest,manifest,comment_id) VALUES(?,?,?,?,?)",
                ("job", "approved", "hash", json.dumps({"folder": str(old)}), "comment"),
            )
        folder = self.evidence.root / "job" / "new-head"
        folder.mkdir(parents=True)
        (folder / "accepted.json").write_text("{}")
        self.evidence.fetch = Mock()
        self.evidence.patch_id = Mock(side_effect=[b"original-patch", b"different-patch"])
        with patch.object(module, "original_proof", return_value=({}, old, {"verdict": "pass"})):
            with self.assertRaises(ReviewChanged):
                self.evidence.ensure(
                    {"id": "job"},
                    {"head": {"sha": "new-head"}, "base": {"sha": "new-base"}},
                    approved_head="approved",
                )

    def test_original_receipt_requires_original_parent_and_entire_tree(self):
        original = self.root / "original"
        self.evidence.fetch = Mock()
        self.evidence.files = Mock(return_value={"src/app.py": File(b"fixed")})
        self.evidence.git = Mock(return_value=b"base\n")
        artifact = {"base_sha": "base", "changes": []}
        with patch.object(
            module, "original_proof", return_value=(artifact, original, {"verdict": "pass"})
        ):
            folder, result = self.evidence.ensure(
                {"id": "job"}, {"head": {"sha": "head"}, "base": {"sha": "base"}}
            )
        self.assertEqual(folder, original)
        self.assertEqual(result["verdict"], "pass")
