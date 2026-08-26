from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


class PublicationCiContractTests(unittest.TestCase):
    def test_pkg_gates_owned_python_and_shell_behavior(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("pull_request:", text)
        self.assertIn("push:", text)
        self.assertIn("pytest==9.1.1", text)
        self.assertIn("zstandard==0.25.0", text)
        self.assertIn("pytest -q tests", text)
        self.assertIn("SHELLSPEC_VERSION: 0.28.1", text)
        self.assertIn("350d3de04ba61505c54eda31a3c2ee912700f1758b1a80a284bc08fd8b6c5992", text)
        self.assertIn("shellspec --shell \"$DASH\"", text)
        self.assertIn("if shellspec --shell \"$DASH\" tests/fixtures/red_spec.sh", text)


if __name__ == "__main__":
    unittest.main()
