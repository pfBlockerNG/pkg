"""Resolve the pkg-local publisher engine used by focused tests."""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


class EngineRootError(RuntimeError):
    """The pkg-local engine root is missing or incomplete."""


def resolve_src_root() -> Path:
    raw = os.environ.get("PFB_SRC")
    root = Path(raw).expanduser() if raw else _REPO_ROOT
    required = (
        root / "scripts" / "pfb_pkg.py",
        root / "scripts" / "catalogue_engine.py",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise EngineRootError(
            f"pkg engine not found at {root} (set PFB_SRC to override); "
            f"missing: {', '.join(missing)}"
        )
    return root
