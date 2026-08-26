from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pfb_pkg
from test_publish_release import _LIVE_EPOCH, _refresh_digests, _rewrite_pkg
from test_tagged_dependency_stage import _assert_rejected


def test_dependency_identity_epoch_float_is_rejected(tmp_path: Path) -> None:
    def mutate(handoff: Path, _packages: dict[str, Path]) -> None:
        payload = json.loads(handoff.read_text(encoding="utf-8"))
        identity = payload["dependency_packages"]["-CE-2.8.pkg"][
            "textproc/py-charset-normalizer"
        ]
        identity["source_date_epoch"] = float(_LIVE_EPOCH)
        handoff.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        assert type(identity["source_date_epoch"]) is float

    _assert_rejected(tmp_path, mutate, "source_date_epoch")


def test_canonical_fractional_member_mtime_is_rejected(tmp_path: Path) -> None:
    def mutate(_handoff: Path, packages: dict[str, Path]) -> None:
        package = min(
            path
            for name, path in packages.items()
            if name.startswith("pfSense-pkg-")
        )

        def change(
            _compact: dict[str, object],
            _full: dict[str, object],
            payload: dict[str, bytes],
            members: dict[str, tarfile.TarInfo],
        ) -> None:
            payload_name = next(iter(payload))
            members[payload_name].mtime = _LIVE_EPOCH + 0.5

        _rewrite_pkg(package, change)
        _refresh_digests(package.parent)
        inspected = pfb_pkg.inspect_pkg(package)
        member = inspected["member_info"][next(iter(inspected["payload"]))]
        assert member.mtime == _LIVE_EPOCH + 0.5

    _assert_rejected(tmp_path, mutate, "mtime")
