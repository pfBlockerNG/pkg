from __future__ import annotations

import json
from pathlib import Path

import catalogue_engine as engine
import catalogue_fixtures as fixtures
import pfb_pkg
import pytest


def test_build_repo_emits_installable_catalogue_descriptors(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    source = packages / "demo-1.0_1.pkg"
    fixtures.make_pkg(source)
    output = tmp_path / "repo"

    assert engine.build_repo(packages, output) == ["FreeBSD:15:*"]
    assert (output / source.name).read_bytes() == source.read_bytes()
    assert (output / "meta").read_text() == (output / "meta.conf").read_text()
    manifest = pfb_pkg.zstd_decompress((output / "packagesite.pkg").read_bytes())
    assert b'"name":"demo"' in manifest


def test_build_repo_rejects_unsafe_catalogue_name(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    fixtures.make_pkg(packages / "demo-1.0_1.pkg")
    with pytest.raises(engine.BuildRepoError, match="catalog"):
        engine.build_repo(packages, tmp_path / "repo", catalog_name="../escape")


def test_signed_catalogue_carries_verifiable_public_key_and_signature(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    fixtures.make_pkg(packages / "demo-1.0_1.pkg")
    key = fixtures._gen_key(tmp_path / "repo.key")
    output = tmp_path / "repo"
    engine.build_repo(packages, output, sign_key=key)
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
