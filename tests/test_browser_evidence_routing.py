import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "browser_evidence_routing",
    Path(__file__).resolve().parents[1] / "integrations/deck-lab/evidence.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BrowserEvidenceRoutingTests(unittest.TestCase):
    def scenarios(self, path, before, after):
        return module.scenarios_for({"changes": [{"path": path, "before": before, "after": after}]})

    def test_home_alignment_css_uses_home_page(self):
        self.assertEqual(
            self.scenarios(
                "styles.css", ".dl-step{display:grid}", ".dl-step{align-items:baseline}"
            ),
            ["home"],
        )

    def test_home_template_changes_route_without_css_tokens(self):
        self.assertEqual(
            self.scenarios("src/sabermetrics/ui/templates/deck_lab/home.html", "old", "new"),
            ["home"],
        )

    def test_unchanged_home_css_does_not_route_unrelated_changes(self):
        self.assertEqual(
            self.scenarios(
                "styles.css", ".dl-step{display:grid}\n.old{}", ".dl-step{display:grid}\n.new{}"
            ),
            [],
        )

    def test_existing_scenarios_are_preserved_alongside_home(self):
        self.assertEqual(
            self.scenarios("test.js", "", "renderSpoiler dragstart dl-step"),
            ["spoiler", "drag", "home"],
        )
