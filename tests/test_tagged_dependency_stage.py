from __future__ import annotations

import copy
import io
import json
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import publish_catalogues as pc
import pfb_pkg
import publish_release as pr
import tagged_release_handoff as trh
import pytest
from test_publish_release import (
    _LIVE_EPOCH,
    _LIVE_SOURCE_SHA,
    _LIVE_TAG,
    _REPO,
    _dependency_bound_assets,
    _refresh_digests,
    _rewrite_pkg,
    _tree_snapshot,
)

_DESCRIPTORS = {"meta", "meta.conf", "data.pkg", "packagesite.pkg"}
_CANONICAL = "pfSense-pkg-pfBlockerNG-4.0.0.a1.pkg"
_EXPECTED_DEPENDENCIES = {
    "ce-2.8": {"py311-charset-normalizer-3.4.4.pkg"},
    "ce-2.9": {"py311-charset-normalizer-3.4.4.pkg"},
    "plus-26.03": set(),
    "plus-26.07": set(),
}


def _run_exact(assets_dir: Path, handoff: Path, pkg_repo: Path) -> pr.PublishReport:
    return pr.run(
        source_repository=_REPO,
        release_id="2335",
        release_tag=_LIVE_TAG,
        source_sha=_LIVE_SOURCE_SHA,
        destinations='["edge"]',
        source_run_id="2335:1",
        assets_dir=assets_dir,
        pkg_repo=pkg_repo,
        handoff_file=handoff,
    )
def _dependency(packages: dict[str, Path]) -> Path:
    matches = [
        path
        for name, path in packages.items()
        if name.startswith("py311-charset-normalizer-")
    ]
    if len(matches) != 2:
        raise AssertionError(f"expected two dependencies: {matches}")
    return sorted(matches)[0]


def _rewrite_fflags_false(package: Path) -> None:
    raw = pfb_pkg.zstd_decompress(package.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        members = [copy.copy(member) for member in archive.getmembers()]
        data = {}
        for member in archive.getmembers():
            extracted = archive.extractfile(member)
            if extracted is None:
                raise AssertionError(f"cannot read {member.name}")
            data[member.name] = extracted.read()
    manifest = json.loads(data["+MANIFEST"])
    payload_name = next(iter(manifest["files"]))
    manifest["files"][payload_name]["fflags"] = False
    data["+MANIFEST"] = json.dumps(manifest, separators=(",", ":")).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for member in members:
            body = data[member.name]
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    package.write_bytes(
        pfb_pkg.zstd_compress(
            output.getvalue(), pfb_pkg.PkgError, "zstd unavailable"
        )
    )


def _assert_rejected(
    tmp_path: Path,
    mutate: object,
    match: str,
) -> None:
    assets_dir = tmp_path / "assets"
    pkg_repo = tmp_path / "pkg-repo"
    handoff, package_paths = _dependency_bound_assets(assets_dir)
    packages = {package.name: package for package in package_paths}
    mutate(handoff, packages)
    sentinel = pkg_repo / "sentinel" / "keep.bin"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"keep")
    before = _tree_snapshot(pkg_repo)
    with pytest.raises(
        (trh.HandoffError, pfb_pkg.PkgError, pc.PublishError, pr.PublishReleaseError),
        match=match,
    ):
        _run_exact(assets_dir, handoff, pkg_repo)
    assert _tree_snapshot(pkg_repo) == before




def test_live_handoff_stages_exact_catalogue_tree(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    pkg_repo = tmp_path / "pkg-repo"
    handoff, _ = _dependency_bound_assets(assets_dir)

    report = _run_exact(assets_dir, handoff, pkg_repo)

    assert report.touched == tuple(("edge", name) for name in sorted(_EXPECTED_DEPENDENCIES))
    docs = pkg_repo / "docs"
    assert {path.name for path in docs.iterdir()} == {"edge"}
    edge = docs / "edge"
    assert {path.name for path in edge.iterdir()} == set(_EXPECTED_DEPENDENCIES)
    for varver, dependencies in _EXPECTED_DEPENDENCIES.items():
        catalogue = edge / varver
        expected_files = _DESCRIPTORS | {_CANONICAL} | dependencies
        assert {path.name for path in catalogue.iterdir()} == expected_files
        assert all((catalogue / name).is_file() for name in expected_files)
        canonical = pfb_pkg.read_compact_manifest(catalogue / _CANONICAL)
        assert (canonical["name"], canonical["version"]) == (
            pfb_pkg.CANONICAL_EMITTED_IDENTITY,
            "4.0.0.a1",
        )
        for dependency in dependencies:
            manifest = pfb_pkg.read_compact_manifest(catalogue / dependency)
            assert (manifest["name"], manifest["version"]) == (
                "py311-charset-normalizer",
                "3.4.4",
            )


def test_fractional_dependency_member_mtime_is_rejected(tmp_path: Path) -> None:
    def mutate(_handoff: Path, packages: dict[str, Path]) -> None:
        package = _dependency(packages)

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

    _assert_rejected(tmp_path, mutate, "metadata")


def test_boolean_dependency_fflags_is_rejected(tmp_path: Path) -> None:
    def mutate(_handoff: Path, packages: dict[str, Path]) -> None:
        package = _dependency(packages)
        _rewrite_fflags_false(package)
        _refresh_digests(package.parent)
        manifest = pfb_pkg.inspect_pkg(package)["manifest"]
        payload_name = next(iter(manifest["files"]))
        assert type(manifest["files"][payload_name]["fflags"]) is bool

    _assert_rejected(tmp_path, mutate, "metadata")


def test_route_ci_null_is_rejected(tmp_path: Path) -> None:
    def mutate(handoff: Path, _packages: dict[str, Path]) -> None:
        payload = json.loads(handoff.read_text(encoding="utf-8"))
        payload["route_matrix"][0]["ci"] = None
        handoff.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    _assert_rejected(tmp_path, mutate, "ci.*boolean")


def test_duplicate_digest_key_is_rejected(tmp_path: Path) -> None:
    def mutate(handoff: Path, _packages: dict[str, Path]) -> None:
        digest_path = handoff.parent / pr._DIGESTS_FILENAME
        raw = digest_path.read_text(encoding="utf-8")
        name = sorted(json.loads(raw))[0]
        digest_path.write_text(
            json.dumps({name: "0" * 64})[:-1] + "," + raw[1:],
            encoding="utf-8",
        )
        assert digest_path.read_text(encoding="utf-8").count(json.dumps(name)) == 2

    _assert_rejected(tmp_path, mutate, "duplicate")
