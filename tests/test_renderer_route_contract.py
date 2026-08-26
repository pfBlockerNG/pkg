from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RendererRouteContractTests(unittest.TestCase):
    def test_standalone_renderer_preserves_route_only_role(self) -> None:
        script = (ROOT / "scripts" / "render-pkg-site.sh").read_text(encoding="utf-8")
        transform = re.search(r"jq -c \\\n\s+'(\[.*?\])'", script, re.DOTALL)
        self.assertIsNotNone(transform)
        assert transform is not None
        self.assertIn("role", transform.group(1))


if __name__ == "__main__":
    unittest.main()
