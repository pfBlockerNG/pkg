from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import catalogue_engine as engine
import pfb_pkg
import pytest

from tests import catalogue_fixtures as fixtures


def test_emit_catalog_writes_installable_descriptors(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    source = packages / "demo-1.0_1.pkg"
    fixtures.make_pkg(source)
    output = tmp_path / "repo"

    assert engine.emit_catalog(output, [source], root=tmp_path) == 1
    assert (output / source.name).read_bytes() == source.read_bytes()
    assert (output / "meta").read_text() == (output / "meta.conf").read_text()
    manifest = pfb_pkg.zstd_decompress((output / "packagesite.pkg").read_bytes())
    assert b'"name":"demo"' in manifest


def test_validate_catalogue_name_rejects_traversal() -> None:
    with pytest.raises(engine.BuildRepoError, match="catalog"):
        engine._validate_catalog_name("../escape")


def test_signed_catalogue_carries_verifiable_public_key_and_signature(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    package = packages / "demo-1.0_1.pkg"
    fixtures.make_pkg(package)
    key = fixtures._gen_key(tmp_path / "repo.key")
    output = tmp_path / "repo"
    engine.emit_catalog(output, [package], root=tmp_path, sign_key=key)
    members = fixtures._sig_members(output / "packagesite.pkg")
    public = members["packagesite.yaml.pub"][len(engine.PKGSIGN_ECDSA_HEAD) :]
    signature = members["packagesite.yaml.sig"][len(engine.PKGSIGN_ECDSA_HEAD) :]
    message = fixtures._pkg_signed_message(
        fixtures._read_member(output / "packagesite.pkg", "packagesite.yaml")
    )
    assert fixtures._openssl_verify(message, signature, public, tmp_path)


def test_catalog_object_preserves_manifest_and_repo_fields() -> None:
    manifest = {"name": "demo", "version": "1.0", "abi": "FreeBSD:15:*", "flatsize": 3}
    obj = engine.catalog_object(
        manifest, pkg_name="demo-1.0.pkg", sum_="2$sum", pkgsize=7
    )
    assert json.loads(json.dumps(obj)) == {
        "name": "demo",
        "version": "1.0",
        "abi": "FreeBSD:15:*",
        "sum": "2$sum",
        "flatsize": 3,
        "path": "demo-1.0.pkg",
        "repopath": "demo-1.0.pkg",
        "pkgsize": 7,
    }


def test_catalog_object_derived_fields_override_untrusted_manifest_values() -> None:
    manifest = {
        "name": "demo",
        "version": "1.0",
        "sum": "attacker",
        "path": "../../outside.pkg",
        "repopath": "../../outside.pkg",
        "pkgsize": 999,
    }
    obj = engine.catalog_object(
        manifest, pkg_name="demo-1.0.pkg", sum_="2$trusted", pkgsize=7
    )
    assert {name: obj[name] for name in ("sum", "path", "repopath", "pkgsize")} == {
        "sum": "2$trusted",
        "path": "demo-1.0.pkg",
        "repopath": "demo-1.0.pkg",
        "pkgsize": 7,
    }


def _emit_demo_catalogue(tmp_path: Path, versions: list[str]) -> Path:
    packages = tmp_path / "packages"
    packages.mkdir()
    sources = []
    for version in versions:
        source = packages / f"demo-{version}.pkg"
        fixtures.make_pkg(source, name="demo", version=version)
        sources.append(source)
    output = tmp_path / "repo"
    engine.emit_catalog(output, sources, root=tmp_path)
    return output


def _catalogue_versions(output: Path) -> tuple[list[str], list[str]]:
    """Versions in the order packagesite.yaml and data list them."""
    packagesite = fixtures._read_member(output / "packagesite.pkg", "packagesite.yaml")
    from_packagesite = [
        json.loads(line)["version"] for line in packagesite.splitlines() if line
    ]
    data = json.loads(fixtures._read_member(output / "data.pkg", "data"))
    return from_packagesite, [row["version"] for row in data["packages"]]


@pytest.mark.parametrize(
    ("versions", "ascending"),
    [
        (["3.3.9", "3.3.10.a1", "3.3.3"], ["3.3.3", "3.3.9", "3.3.10.a1"]),
        (["3.3.3", "3.3.3.a1"], ["3.3.3.a1", "3.3.3"]),
    ],
    ids=["release-below-newer-prerelease", "prerelease-below-its-release"],
)
def test_catalogue_lists_pkg_newest_version_last(
    tmp_path: Path, versions: list[str], ascending: list[str]
) -> None:
    """pkg keeps the LAST duplicate of a package name (issue #3386), so the catalogue
    must list versions in pkg order — string order would hide the newest one."""
    output = _emit_demo_catalogue(tmp_path, versions)

    from_packagesite, from_data = _catalogue_versions(output)
    assert from_packagesite == ascending
    assert from_data == ascending


def test_catalogue_orders_by_name_then_pkg_version(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    wanted = [
        ("alpha", "9"),
        ("alpha", "10"),
        ("demo", "3.3.9"),
        ("demo", "3.3.10.a1"),
    ]
    sources = []
    for name, version in reversed(wanted):
        source = packages / f"{name}-{version}.pkg"
        fixtures.make_pkg(source, name=name, version=version)
        sources.append(source)
    output = tmp_path / "repo"

    engine.emit_catalog(output, sources, root=tmp_path)

    packagesite = fixtures._read_member(output / "packagesite.pkg", "packagesite.yaml")
    rows = [json.loads(line) for line in packagesite.splitlines() if line]
    assert [(row["name"], row["version"]) for row in rows] == wanted


@pytest.mark.parametrize("version", [7, "../escape"], ids=["non-string", "traversal"])
def test_catalogue_rejects_unsafe_manifest_version_before_ordering_it(
    tmp_path: Path, version: Any
) -> None:
    """A hostile manifest version is a clean BuildRepoError from the path-segment guard,
    never a crash out of the version ordering."""
    good = tmp_path / "demo-1.0.pkg"
    hostile = tmp_path / "demo-hostile.pkg"
    fixtures.make_pkg(good, name="demo", version="1.0")
    fixtures.make_pkg(hostile, name="demo", version=version)

    with pytest.raises(engine.BuildRepoError, match="manifest version"):
        engine.emit_catalog(tmp_path / "repo", [good, hostile], root=tmp_path)
