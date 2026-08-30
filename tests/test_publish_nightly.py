"""Tests for the pkg-local Nightly handoff publisher.

Fixtures construct valid source-contract records and handoffs directly; hostile
cases mutate a deep copy before ingestion.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import itertools
import json
import sys
import tarfile
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalogue_assembly as ca
import catalogue_fixtures as tbrp
import nightly_contract as nc
import pfb_pkg
import publish_catalogues as pc
import publish_nightly as pn
import publish_release as pr

_REPO = pc.EXPECTED_SOURCE_REPOSITORY
_RUN_ID = "555000111:1"
_SOURCE_SHA = "a" * 40
_PORTS_SHA = "b" * 40
_TOOLS_SHA = "e" * 40
_MATRIX_SHA = "d" * 40
_EPOCH = 1_800_000_000
_DEPENDENCY_BUILDER = {
    "python": "3.11.15",
    "pip": "26.2.1",
    "setuptools": "75.6.0",
    "wheel": "0.45.1",
    "zstandard": "0.25.0",
    "uv": "0.12.6",
    "uv_lock_sha256": "2d9aa34742bd0a43e69c8cc1216e23130145369b7ac32a5603e5eb42094d00d9",
}

_pkg_counter = itertools.count()


# --------------------------------------------------------------------------- #
# ROUTE/BUILD matrix rows — mirrors tests/test_publish_release.py's ROW_* shape.
# --------------------------------------------------------------------------- #


def _row(
    *,
    freebsd_major: str = "15",
    pfsense_version: str = "2.8",
    variant: str = "CE",
    extra_pkgs: Sequence[str] = (),
    role: str | None = None,
    php_version: str = "8.3",
    py_flavor: str = "py311",
) -> dict[str, object]:
    row: dict[str, object] = {
        "pfsense_version": pfsense_version,
        "channel": variant,
        "freebsd_version": f"{freebsd_major}.0-RELEASE",
        "freebsd_major": freebsd_major,
        "php_version": php_version,
        "py_flavor": py_flavor,
        "variant": variant,
        "status": "active",
        "extra_pkgs": list(extra_pkgs),
    }
    if role is not None:
        row["role"] = role
    return row


ROW_CE15 = _row(
    freebsd_major="15",
    pfsense_version="2.8",
    variant="CE",
    extra_pkgs=["textproc/py-charset-normalizer"],
)
ROW_CE15_NO_EXTRA = _row(freebsd_major="15", pfsense_version="2.8", variant="CE")
# Current Plus (26.03/26.07) and Plus 25.11 share FreeBSD major 16 with distinct
# PHP runtimes (issue #2926): build identity is the exact runtime tuple
# (freebsd_major, php_version, py_flavor), NOT the major alone.
ROW_PLUS16_03 = _row(
    freebsd_major="16",
    pfsense_version="26.03",
    variant="Plus",
    php_version="8.5",
)
ROW_PLUS16_07 = _row(
    freebsd_major="16",
    pfsense_version="26.07",
    variant="Plus",
    php_version="8.5",
)
ROW_PLUS16_25_11 = _row(
    freebsd_major="16",
    pfsense_version="25.11",
    variant="Plus",
    php_version="8.4",
)
ROW_PLUS15_03 = _row(freebsd_major="15", pfsense_version="26.03", variant="Plus")
# Same major + PHP as ROW_CE15 but a distinct Python flavor — the third key
# dimension (issue #2926): identity is the complete tuple, so this row must get
# its OWN leg/artifact, never ROW_CE15's py311 build.
ROW_CE15_PY312 = _row(
    freebsd_major="15", pfsense_version="3.0", variant="CE", py_flavor="py312"
)
ROW_ROUTE_ONLY_17 = _row(
    freebsd_major="17", pfsense_version="17.0", variant="CE", role="route-only"
)
# Same major as ROW_CE15, ALSO declaring the charset extra — two build-role ROUTE
# rows sharing one leg, both declaring the dep (issue #2468 coverage).
ROW_CE15_29 = _row(
    freebsd_major="15",
    pfsense_version="2.9",
    variant="CE",
    extra_pkgs=["textproc/py-charset-normalizer"],
)


# --------------------------------------------------------------------------- #
# Snapshot + genuine .pkg archive fixture builders.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Snapshot:
    pkg_version: str
    source_sha: str
    ports_sha: str


def _snapshot(
    *,
    build_date: date = date(2026, 8, 4),
    source_sha: str = _SOURCE_SHA,
    ports_sha: str = _PORTS_SHA,
) -> _Snapshot:
    return _Snapshot(
        pkg_version=f"{build_date:%Y%m%d}120000.{source_sha[:7]}",
        source_sha=source_sha,
        ports_sha=ports_sha,
    )


def _handoff_input_digest(
    *,
    source_sha: str,
    ports_sha: str,
    matrix_digest: str,
    source_date_epoch: int,
    dependency_builder: dict[str, str],
) -> str:
    payload = json.dumps(
        {
            "matrix_digest": matrix_digest,
            "source_date_epoch": source_date_epoch,
            "dependency_builder": dependency_builder,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return nc.combined_nightly_input_digest(
        source_sha, ports_sha, hashlib.sha256(payload).hexdigest()
    )


def _write_tar_pkg(path: Path, members: list[tuple[str, bytes, int, int]]) -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name, data, mode, mtime in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = mode
            info.mtime = mtime
            tf.addfile(info, io.BytesIO(data))
    path.write_bytes(
        pfb_pkg.zstd_compress(raw.getvalue(), pfb_pkg.PkgError, "zstd unavailable")
    )


def _wrap_canonical_pkg(
    directory: Path, record: dict, *, local_name: str
) -> tuple[Path, str]:
    """A full, validate_project_pkg-shaped canonical .pkg carrying ``record`` as its
    pfb_build_record annotation, under its bare Nightly name (no -<Variant>-<version>
    suffix). Returns (path, sha256 of the bytes)."""
    row = record["matrix_row"]
    version = record["canonical_package_version"]
    epoch = record["source_date_epoch"]
    major = row["freebsd_major"]

    payload = {
        pfb_pkg._INFO_PATH: (
            f"<pfsensepkgs><package><name>pfBlockerNG</name><version>{version}</version></package></pfsensepkgs>"
        ).encode(),
        "/usr/local/pkg/pfblockerng/pfb_stub.py": b"print('ok')\n",
    }
    common = {
        "name": pfb_pkg.CANONICAL_EMITTED_IDENTITY,
        "origin": "net/pfSense-pkg-pfBlockerNG",
        "version": version,
        "abi": f"FreeBSD:{major}:*",
        "arch": f"freebsd:{major}:*",
        "prefix": "/usr/local",
        "annotations": {
            pfb_pkg.PFB_BUILD_RECORD_KEY: json.dumps(
                record, separators=(",", ":"), sort_keys=True
            )
        },
    }
    php_dep = "php" + row["php_version"].replace(".", "")
    python_dep = "python" + row["py_flavor"][2:]
    deps = {
        php_dep: {"origin": f"lang/{php_dep}", "version": "1.0"},
        python_dep: {"origin": f"lang/{python_dep}", "version": "1.0"},
    }
    files = {
        name: {
            "sum": "1$" + hashlib.sha256(data).hexdigest(),
            "perm": "0644",
            "mtime": epoch,
            "size": len(data),
        }
        for name, data in payload.items()
    }
    full = {
        **common,
        "deps": deps,
        "files": files,
        "scripts": {
            "install": "#!/bin/sh\n/usr/local/bin/php -f /etc/rc.packages pfSense-pkg-pfBlockerNG ${2}\n",
            "deinstall": "#!/bin/sh\n/usr/local/bin/php -f /etc/rc.packages pfSense-pkg-pfBlockerNG ${2}\n",
        },
    }
    compact = {**common, "deps": deps}

    members = [
        (
            "+COMPACT_MANIFEST",
            json.dumps(compact, separators=(",", ":")).encode(),
            0o644,
            0,
        ),
        ("+MANIFEST", json.dumps(full, separators=(",", ":")).encode(), 0o644, 0),
    ]
    members.extend((name, data, 0o644, epoch) for name, data in payload.items())
    path = directory / local_name
    _write_tar_pkg(path, members)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _wrap_dependency_pkg(
    directory: Path,
    *,
    name: str,
    version: str,
    abi: str,
    local_name: str,
    origin: str | None = None,
    payload: dict[str, bytes] | None = None,
) -> tuple[Path, str]:
    manifest = {
        "name": name,
        "version": version,
        "abi": abi,
        "origin": origin or f"textproc/{name}",
    }
    compact = json.dumps(manifest, separators=(",", ":")).encode()
    members = [("+COMPACT_MANIFEST", compact, 0o644, 0)]
    # Extra members vary the archive BYTES under an identical manifest identity —
    # how a nightly rebuild of the same name-version ends up byte-distinct.
    members.extend((member, data, 0o644, 0) for member, data in (payload or {}).items())
    path = directory / local_name
    _write_tar_pkg(path, members)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# Leg and handoff fixture assembly.
# --------------------------------------------------------------------------- #


_CHARSET_DEP_SPEC: tuple[str, str] = ("py311-charset-normalizer", "3.4.0")
_CHARSET_ORIGIN = "textproc/py-charset-normalizer"


@dataclass(frozen=True)
class _LegSpec:
    row: dict[str, object]
    # None = derive from row extra_pkgs (issue #2405: count must match).
    dep_specs: Sequence[tuple[str, str]] | None = None
    source_date_epoch: int = _EPOCH
    # Extra archive member(s) so a rebuilt dep of the SAME name-version ends up
    # byte-distinct from a prior leg's — issue #2468's place-if-missing rule.
    dep_payload: dict[str, bytes] | None = None


def _resolved_dep_specs(spec: _LegSpec) -> Sequence[tuple[str, str]]:
    if spec.dep_specs is not None:
        return spec.dep_specs
    raw_extras = spec.row.get("extra_pkgs") or []
    extras = list(raw_extras) if isinstance(raw_extras, (list, tuple)) else []
    if extras == [_CHARSET_ORIGIN]:
        return (_CHARSET_DEP_SPEC,)
    if extras:
        raise AssertionError(f"fixture has no default dep for extra_pkgs={extras!r}")
    return ()


def _make_record(
    snapshot: _Snapshot, row: dict[str, object], epoch: int = _EPOCH
) -> dict[str, object]:
    normalized = pfb_pkg.validate_build_matrix_row(row)
    major_minor = ".".join(str(normalized["pfsense_version"]).split(".")[:2])
    record: dict[str, object] = {
        "schema": 1,
        "channel": "nightly",
        "release_line": "nightly",
        "classification": "nightly",
        "source_tag": None,
        "source_sha": snapshot.source_sha,
        "canonical_package_version": snapshot.pkg_version,
        "native_recipe_identity": "pfSense-pkg-pfBlockerNG-nightly",
        "emitted_identity": "pfSense-pkg-pfBlockerNG",
        "matrix_row": normalized,
        "freebsd_ports_sha": snapshot.ports_sha,
        "route": f"nightly/{str(normalized['variant']).lower()}-{major_minor}",
        "source_date_epoch": epoch,
        "dependency_builder": dict(_DEPENDENCY_BUILDER),
        "build_input_digest": "",
    }
    record["build_input_digest"] = pfb_pkg.build_input_digest(record)
    return pfb_pkg.validate_build_record(record)


def _leg_dir(assets_root: Path, row: dict[str, object]) -> Path:
    """The exact tuple-bearing result-directory name the consumer must derive
    (issue #2926): nightly-result-<major>-php<php_version>-<py_flavor>."""
    return assets_root / (
        f"{pn._LEG_DIR_PREFIX}{row['freebsd_major']}"
        f"-php{row['php_version']}-{row['py_flavor']}"
    )


def _build_leg_result(
    snapshot: Any, spec: _LegSpec, *, assets_root: Path
) -> dict[str, Any]:
    """Mint one Nightly leg's record and package fixtures."""
    major = str(spec.row["freebsd_major"])
    legdir = _leg_dir(assets_root, spec.row)
    legdir.mkdir(parents=True, exist_ok=True)
    record = _make_record(snapshot, spec.row, spec.source_date_epoch)
    canonical_name = f"{pfb_pkg.CANONICAL_EMITTED_IDENTITY}-{snapshot.pkg_version}.pkg"
    _path, digest = _wrap_canonical_pkg(legdir, record, local_name=canonical_name)
    dep_artifacts = []
    for name, version in _resolved_dep_specs(spec):
        dep_name = f"{name}-{version}.pkg"
        _dep_path, dep_digest = _wrap_dependency_pkg(
            legdir,
            name=name,
            version=version,
            abi=f"FreeBSD:{major}:*",
            local_name=dep_name,
            payload=spec.dep_payload,
        )
        dep_artifacts.append(
            {"abi": f"FreeBSD:{major}:*", "name": dep_name, "sha256": dep_digest}
        )
    return {
        "matrix_row": spec.row,
        "record": record,
        "artifact": {
            "abi": f"FreeBSD:{major}:*",
            "name": canonical_name,
            "sha256": digest,
        },
        "dep_artifacts": dep_artifacts,
    }


def _build_handoff(
    snapshot: Any,
    *,
    legs: Sequence[_LegSpec],
    route_rows: Sequence[dict[str, object]],
    assets_root: Path,
    run_id: str = _RUN_ID,
    source_sha: str = _SOURCE_SHA,
    ports_sha: str = _PORTS_SHA,
) -> dict[str, Any]:
    results = [
        _build_leg_result(snapshot, spec, assets_root=assets_root) for spec in legs
    ]
    build_rows = [spec.row for spec in legs]
    matrix_payload = json.dumps(
        {
            "tools_sha": _TOOLS_SHA,
            "matrix_sha": _MATRIX_SHA,
            "build": build_rows,
            "route": list(route_rows),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    matrix_digest = hashlib.sha256(matrix_payload).hexdigest()
    source_date_epoch = legs[0].source_date_epoch
    dependency_builder = dict(_DEPENDENCY_BUILDER)
    input_digest = _handoff_input_digest(
        source_sha=source_sha,
        ports_sha=ports_sha,
        matrix_digest=matrix_digest,
        source_date_epoch=source_date_epoch,
        dependency_builder=dependency_builder,
    )
    return {
        "schema": 1,
        "kind": "nightly-handoff",
        "run_id": run_id,
        "source_ref": "",
        "ports_repo": "",
        "ports_ref": "",
        "pkg_version": snapshot.pkg_version,
        "input_digest": input_digest,
        "source_sha": source_sha,
        "ports_sha": ports_sha,
        "tools_sha": _TOOLS_SHA,
        "matrix_sha": _MATRIX_SHA,
        "matrix_digest": matrix_digest,
        "source_date_epoch": source_date_epoch,
        "dependency_builder": dependency_builder,
        "build_matrix": build_rows,
        "route_matrix": list(route_rows),
        "builds": sorted(
            results, key=lambda item: str(item["matrix_row"]["freebsd_major"])
        ),
    }


def _mutate(handoff: dict[str, Any]) -> dict[str, Any]:
    """A fully independent, mutable deep copy (JSON round-trip)."""
    return json.loads(json.dumps(handoff))


def _run(
    *,
    handoff: dict[str, Any],
    results_dir: Path,
    pkg_repo: Path,
    source_run_id: str = _RUN_ID,
    sign_key: Path | None = None,
) -> pr.PublishReport:
    handoff_path = results_dir.parent / f"handoff-{next(_pkg_counter)}.json"
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    return pn.run(
        handoff_path=handoff_path,
        results_dir=results_dir,
        pkg_repo=pkg_repo,
        source_run_id=source_run_id,
        sign_key=sign_key,
    )


class _TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pub-nightly-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.pkg_repo = self.tmp / "pkg-repo"
        self._dir_counter = itertools.count()

    def new_results_dir(self) -> Path:
        results_dir = self.tmp / f"results-{next(self._dir_counter)}"
        results_dir.mkdir(parents=True)
        return results_dir

    def base_handoff(self) -> tuple[dict[str, Any], Path, Any]:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_dir,
        )
        return handoff, results_dir, snapshot


# --------------------------------------------------------------------------- #
# T1 — happy fan-out: 2 legs, 3 ROUTE rows -> 3 catalogues.
# --------------------------------------------------------------------------- #


class HappyFanOutTests(_TempDirTestCase):
    def test_two_legs_three_route_rows_three_catalogues(self) -> None:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        legs = [
            _LegSpec(row=ROW_CE15, dep_specs=[("py311-charset-normalizer", "3.4.0")]),
            _LegSpec(row=ROW_PLUS16_03),
        ]
        route_rows = [ROW_CE15, ROW_PLUS16_03, ROW_PLUS16_07]
        handoff = _build_handoff(
            snapshot, legs=legs, route_rows=route_rows, assets_root=results_dir
        )

        report = _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)

        self.assertEqual(
            set(report.touched),
            {
                ("nightly", "ce-2.8"),
                ("nightly", "plus-26.03"),
                ("nightly", "plus-26.07"),
            },
        )
        docs = self.pkg_repo / "docs" / "nightly"
        canonical_name = f"pfSense-pkg-pfBlockerNG-{snapshot.pkg_version}.pkg"
        self.assertTrue((docs / "ce-2.8" / canonical_name).is_file())
        self.assertTrue(
            (docs / "ce-2.8" / "py311-charset-normalizer-3.4.0.pkg").is_file()
        )
        self.assertTrue((docs / "plus-26.03" / canonical_name).is_file())
        self.assertTrue((docs / "plus-26.07" / canonical_name).is_file())
        self.assertFalse(
            (docs / "plus-26.03" / "py311-charset-normalizer-3.4.0.pkg").exists()
        )
        self.assertFalse(
            (docs / "plus-26.07" / "py311-charset-normalizer-3.4.0.pkg").exists()
        )
        self.assertEqual(
            (docs / "plus-26.03" / canonical_name).read_bytes(),
            (docs / "plus-26.07" / canonical_name).read_bytes(),
        )
        for varver in ("ce-2.8", "plus-26.03", "plus-26.07"):
            self.assertTrue((docs / varver / "meta.conf").is_file(), varver)
            self.assertTrue((docs / varver / "data.pkg").is_file(), varver)
            self.assertTrue((docs / varver / "packagesite.pkg").is_file(), varver)

    def test_both_freebsd_16_runtime_tuples_route_their_own_artifacts(self) -> None:
        """issue #2926: two build legs share FreeBSD major 16 but differ in the
        PHP runtime (25.11 -> PHP 8.4, current Plus -> PHP 8.5). Each build-role
        ROUTE row must resolve exactly its own tuple's artifact: plus-25.11 only
        the PHP 8.4 artifact, every PHP 8.5 route only the PHP 8.5 artifact."""
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        legs = [
            _LegSpec(row=ROW_PLUS16_25_11),
            _LegSpec(row=ROW_PLUS16_03),
        ]
        route_rows = [ROW_PLUS16_25_11, ROW_PLUS16_03, ROW_PLUS16_07]
        handoff = _build_handoff(
            snapshot, legs=legs, route_rows=route_rows, assets_root=results_dir
        )

        report = _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)

        self.assertEqual(
            set(report.touched),
            {
                ("nightly", "plus-25.11"),
                ("nightly", "plus-26.03"),
                ("nightly", "plus-26.07"),
            },
        )
        sha_by_php = {
            str(entry["matrix_row"]["php_version"]): entry["artifact"]["sha256"]
            for entry in handoff["builds"]
        }
        self.assertEqual(set(sha_by_php), {"8.4", "8.5"})
        self.assertNotEqual(sha_by_php["8.4"], sha_by_php["8.5"])
        docs = self.pkg_repo / "docs" / "nightly"
        canonical_name = f"pfSense-pkg-pfBlockerNG-{snapshot.pkg_version}.pkg"

        def published_sha(varver: str) -> str:
            return hashlib.sha256(
                (docs / varver / canonical_name).read_bytes()
            ).hexdigest()

        self.assertEqual(published_sha("plus-25.11"), sha_by_php["8.4"])
        self.assertNotEqual(published_sha("plus-25.11"), sha_by_php["8.5"])
        self.assertEqual(published_sha("plus-26.03"), sha_by_php["8.5"])
        self.assertEqual(published_sha("plus-26.07"), sha_by_php["8.5"])

    def test_same_major_php_distinct_py_flavors_route_their_own_artifacts(self) -> None:
        """issue #2926: the key is the COMPLETE tuple — same FreeBSD major AND same
        PHP (8.3) but distinct py_flavor (py311 vs py312) are two separate builds,
        each route receiving only its own exact-flavor artifact. A _build_key that
        ignores or hardcodes py_flavor would collapse these legs and fail here."""
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        legs = [_LegSpec(row=ROW_CE15), _LegSpec(row=ROW_CE15_PY312)]
        route_rows = [ROW_CE15, ROW_CE15_PY312]
        handoff = _build_handoff(
            snapshot, legs=legs, route_rows=route_rows, assets_root=results_dir
        )

        report = _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)

        self.assertEqual(
            set(report.touched),
            {("nightly", "ce-2.8"), ("nightly", "ce-3.0")},
        )
        sha_by_flavor = {
            str(entry["matrix_row"]["py_flavor"]): entry["artifact"]["sha256"]
            for entry in handoff["builds"]
        }
        self.assertEqual(set(sha_by_flavor), {"py311", "py312"})
        self.assertNotEqual(sha_by_flavor["py311"], sha_by_flavor["py312"])
        docs = self.pkg_repo / "docs" / "nightly"
        canonical_name = f"pfSense-pkg-pfBlockerNG-{snapshot.pkg_version}.pkg"

        def published_sha(varver: str) -> str:
            return hashlib.sha256(
                (docs / varver / canonical_name).read_bytes()
            ).hexdigest()

        self.assertEqual(published_sha("ce-2.8"), sha_by_flavor["py311"])
        self.assertEqual(published_sha("ce-3.0"), sha_by_flavor["py312"])
        self.assertNotEqual(
            (docs / "ce-2.8" / canonical_name).read_bytes(),
            (docs / "ce-3.0" / canonical_name).read_bytes(),
        )


# --------------------------------------------------------------------------- #
# T2 — identical rerun is a NOOP.
# --------------------------------------------------------------------------- #


class NoopTests(_TempDirTestCase):
    def test_identical_rerun_is_noop(self) -> None:
        handoff, results_dir, snapshot = self.base_handoff()
        first = _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertTrue(first.touched)

        catalogue_dir = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        canonical_name = f"pfSense-pkg-pfBlockerNG-{snapshot.pkg_version}.pkg"
        before_mtime = (catalogue_dir / canonical_name).stat().st_mtime_ns
        before_bytes = (catalogue_dir / canonical_name).read_bytes()

        second = _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)

        self.assertEqual(second.touched, ())
        self.assertTrue(second.noop)
        self.assertEqual(
            second.describe(),
            ["NOOP: every destination already matches this run's verified assets"],
        )
        self.assertEqual(
            (catalogue_dir / canonical_name).stat().st_mtime_ns, before_mtime
        )
        self.assertEqual((catalogue_dir / canonical_name).read_bytes(), before_bytes)

    def test_same_bytes_repair_stale_malformed_or_incomplete_descriptors(self) -> None:
        handoff, results_dir, snapshot = self.base_handoff()
        first = _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertTrue(first.touched)
        site_root = self.pkg_repo / "docs"
        catalogue_dir = site_root / "nightly" / "ce-2.8"
        pristine = {path.name: path.read_bytes() for path in catalogue_dir.iterdir()}
        packagesite = pfb_pkg.zstd_decompress(pristine["packagesite.pkg"])
        with tarfile.open(fileobj=io.BytesIO(packagesite)) as archive:
            member = archive.extractfile("packagesite.yaml")
            self.assertIsNotNone(member)
            valid_rows = [json.loads(line) for line in member.read().splitlines()]

        def restore() -> None:
            for name, data in pristine.items():
                path = catalogue_dir / name
                if path.is_symlink():
                    path.unlink()
                path.write_bytes(data)

        def missing_meta() -> None:
            (catalogue_dir / "meta").unlink()

        def malformed_packagesite() -> None:
            _write_tar_pkg(
                catalogue_dir / "packagesite.pkg",
                [("packagesite.yaml", b"\xff\n", 0o644, 0)],
            )

        def stale_packagesite() -> None:
            rows = [dict(row) for row in valid_rows]
            rows[0]["version"] = "3.3.0"
            rows[0]["path"] = "pfSense-pkg-pfBlockerNG-3.3.0.pkg"
            rows[0]["repopath"] = "pfSense-pkg-pfBlockerNG-3.3.0.pkg"
            payload = b"".join(
                json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows
            )
            _write_tar_pkg(
                catalogue_dir / "packagesite.pkg",
                [("packagesite.yaml", payload, 0o644, 0)],
            )

        for label, corrupt in (
            ("missing meta", missing_meta),
            ("malformed packagesite", malformed_packagesite),
            ("stale packagesite", stale_packagesite),
        ):
            with self.subTest(case=label):
                restore()
                corrupt()
                report = _run(
                    handoff=handoff,
                    results_dir=results_dir,
                    pkg_repo=self.pkg_repo,
                )
                self.assertEqual(report.touched, (("nightly", "ce-2.8"),))
                self.assertTrue(
                    pr._catalogue_descriptor_complete(catalogue_dir, root=site_root)
                )

        canonical_name = f"pfSense-pkg-pfBlockerNG-{snapshot.pkg_version}.pkg"
        repaired = pfb_pkg.zstd_decompress(
            (catalogue_dir / "packagesite.pkg").read_bytes()
        )
        with tarfile.open(fileobj=io.BytesIO(repaired)) as archive:
            member = archive.extractfile("packagesite.yaml")
            self.assertIsNotNone(member)
            rows = [json.loads(line) for line in member.read().splitlines()]
        self.assertEqual(
            {
                (row["name"], row["version"], row["path"], row["repopath"])
                for row in rows
            },
            {
                (
                    "pfSense-pkg-pfBlockerNG",
                    snapshot.pkg_version,
                    canonical_name,
                    canonical_name,
                ),
                (
                    "py311-charset-normalizer",
                    "3.4.0",
                    "py311-charset-normalizer-3.4.0.pkg",
                    "py311-charset-normalizer-3.4.0.pkg",
                ),
            },
        )

    def test_symlinked_destination_is_rejected_before_stale_check_or_mutation(
        self,
    ) -> None:
        handoff, results_dir, _snapshot_value = self.base_handoff()
        outside = self.tmp / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.pkg"
        sentinel.write_bytes(b"outside must remain untouched")
        destination = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        destination.parent.mkdir(parents=True)
        destination.symlink_to(outside, target_is_directory=True)
        before = {path.name: path.read_bytes() for path in outside.iterdir()}

        with self.assertRaises(pr.PublishReleaseError):
            _run(
                handoff=handoff,
                results_dir=results_dir,
                pkg_repo=self.pkg_repo,
            )

        self.assertEqual(
            {path.name: path.read_bytes() for path in outside.iterdir()}, before
        )

    def test_dangling_package_symlink_is_rejected_before_copy(self) -> None:
        handoff, results_dir, snapshot = self.base_handoff()
        outside = self.tmp / "outside-payload"
        outside.mkdir()
        escaped = outside / "escaped.pkg"
        destination = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        destination.mkdir(parents=True)
        payload = destination / f"pfSense-pkg-pfBlockerNG-{snapshot.pkg_version}.pkg"
        payload.symlink_to(escaped)
        before = {
            path.relative_to(outside): path.read_bytes()
            for path in outside.rglob("*")
            if path.is_file()
        }

        with self.assertRaises(pr.PublishReleaseError):
            _run(
                handoff=handoff,
                results_dir=results_dir,
                pkg_repo=self.pkg_repo,
            )

        self.assertEqual(
            {
                path.relative_to(outside): path.read_bytes()
                for path in outside.rglob("*")
                if path.is_file()
            },
            before,
        )

    def test_all_destinations_are_checked_before_any_stale_logic(self) -> None:
        newer_results = self.new_results_dir()
        newer_snapshot = _snapshot(build_date=date(2026, 8, 5), source_sha="f" * 40)
        newer_handoff = _build_handoff(
            newer_snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=newer_results,
            source_sha=newer_snapshot.source_sha,
        )
        first = _run(
            handoff=newer_handoff,
            results_dir=newer_results,
            pkg_repo=self.pkg_repo,
        )
        self.assertEqual(first.touched, (("nightly", "ce-2.8"),))

        older_results = self.new_results_dir()
        older_snapshot = _snapshot()
        older_handoff = _build_handoff(
            older_snapshot,
            legs=[_LegSpec(row=ROW_CE15), _LegSpec(row=ROW_PLUS16_03)],
            route_rows=[ROW_CE15, ROW_PLUS16_03],
            assets_root=older_results,
        )
        site_root = self.pkg_repo / "docs"
        safe = site_root / "nightly" / "ce-2.8"
        safe_before = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in safe.iterdir()
        }
        outside = self.tmp / "outside-later-nightly-destination"
        outside.mkdir()
        (outside / "sentinel.pkg").write_bytes(b"outside must remain byte-identical")
        unsafe = site_root / "nightly" / "plus-26.03"
        unsafe.symlink_to(outside, target_is_directory=True)
        outside_before = {
            path.relative_to(outside): path.read_bytes()
            for path in outside.rglob("*")
            if path.is_file()
        }

        with self.assertRaises(pr.PublishReleaseError):
            _run(
                handoff=older_handoff,
                results_dir=older_results,
                pkg_repo=self.pkg_repo,
            )

        self.assertEqual(
            {
                path.relative_to(outside): path.read_bytes()
                for path in outside.rglob("*")
                if path.is_file()
            },
            outside_before,
        )
        self.assertEqual(
            {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in safe.iterdir()
            },
            safe_before,
        )


# --------------------------------------------------------------------------- #
# T3 — same version, different bytes already at a destination: fail closed.
# --------------------------------------------------------------------------- #


class ConflictTests(_TempDirTestCase):
    def test_same_version_different_bytes_rejected(self) -> None:
        results_dir_1 = self.new_results_dir()
        snapshot = _snapshot()
        handoff_1 = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_dir_1,
        )
        first = _run(
            handoff=handoff_1, results_dir=results_dir_1, pkg_repo=self.pkg_repo
        )
        self.assertEqual(first.touched, (("nightly", "ce-2.8"),))

        catalogue_dir = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        canonical_name = f"pfSense-pkg-pfBlockerNG-{snapshot.pkg_version}.pkg"
        original_bytes = (catalogue_dir / canonical_name).read_bytes()

        # SAME snapshot (same day/version, same source_sha/ports_sha) but a
        # DIFFERENT source_date_epoch -- genuinely different archive bytes under the
        # identical canonical name, with every OTHER cross-checked field unchanged.
        results_dir_2 = self.new_results_dir()
        handoff_2 = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15, source_date_epoch=_EPOCH + 1)],
            route_rows=[ROW_CE15],
            assets_root=results_dir_2,
        )

        with self.assertRaises(pr.DestinationConflictError) as ctx:
            _run(handoff=handoff_2, results_dir=results_dir_2, pkg_repo=self.pkg_repo)
        self.assertIn(str(catalogue_dir / canonical_name), str(ctx.exception))
        self.assertEqual((catalogue_dir / canonical_name).read_bytes(), original_bytes)


# --------------------------------------------------------------------------- #
# T4 — a destination holding a NEWER version rejects an older/absent incoming
# version BEFORE any write.
# --------------------------------------------------------------------------- #


class StaleTests(_TempDirTestCase):
    def test_older_absent_version_rejected_before_any_write(self) -> None:
        newer_alloc = _snapshot(build_date=date(2026, 8, 10))
        results_dir_1 = self.new_results_dir()
        handoff_1 = _build_handoff(
            newer_alloc,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_dir_1,
        )
        first = _run(
            handoff=handoff_1, results_dir=results_dir_1, pkg_repo=self.pkg_repo
        )
        self.assertTrue(first.touched)

        catalogue_dir = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        before = {p.name: p.read_bytes() for p in catalogue_dir.iterdir()}

        older_alloc = _snapshot(build_date=date(2026, 8, 5))
        results_dir_2 = self.new_results_dir()
        handoff_2 = _build_handoff(
            older_alloc,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_dir_2,
        )

        with self.assertRaises(pn.StaleNightlyError) as ctx:
            _run(handoff=handoff_2, results_dir=results_dir_2, pkg_repo=self.pkg_repo)
        self.assertIn("stale", str(ctx.exception).lower())

        after = {p.name: p.read_bytes() for p in catalogue_dir.iterdir()}
        self.assertEqual(after, before)

    def test_published_manifest_missing_version_rejected_cleanly(self) -> None:
        """N3b: a catalogue-resident canonical .pkg whose manifest somehow lacks
        'version' (a corrupt or foreign write -- this publisher's own write
        path always carries one) must reject with a clean PublishNightlyError
        naming the corrupt file, not a raw KeyError out of
        manifest["version"]. The fixture writes the tar directly (bypassing
        _wrap_canonical_pkg, which always injects version) since that is the
        only way to construct this shape."""
        handoff, results_dir, _alloc = self.base_handoff()
        _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)

        catalogue_dir = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        corrupt_name = "pfSense-pkg-pfBlockerNG-99.99.9999.pkg"
        corrupt_path = catalogue_dir / corrupt_name
        manifest = {
            "name": pfb_pkg.CANONICAL_EMITTED_IDENTITY,
            "abi": "FreeBSD:15:*",
        }
        _write_tar_pkg(
            corrupt_path,
            [
                (
                    "+COMPACT_MANIFEST",
                    json.dumps(manifest, separators=(",", ":")).encode(),
                    0o644,
                    0,
                )
            ],
        )

        newer_alloc = _snapshot(build_date=date(2026, 8, 20))
        results_dir_2 = self.new_results_dir()
        handoff_2 = _build_handoff(
            newer_alloc,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_dir_2,
        )

        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=handoff_2, results_dir=results_dir_2, pkg_repo=self.pkg_repo)
        self.assertIn(str(corrupt_path), str(ctx.exception))


# --------------------------------------------------------------------------- #
# T5 — retention: keep+1 canonical generations published sequentially, oldest
# evicted; the charset extra (declared on every CE row, issue #2405) survives.
# --------------------------------------------------------------------------- #


class RetentionTests(_TempDirTestCase):
    def test_retention_evicts_oldest_canonical_dep_survives(self) -> None:
        keep = ca.DEFAULT_RETENTION_KEEP
        catalogue_dir = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        dep_name = "py311-charset-normalizer-3.4.0.pkg"
        first_version = None
        for seq in range(keep + 1):
            snapshot = _snapshot(build_date=date(2026, 8, 1 + seq))
            if seq == 0:
                first_version = snapshot.pkg_version
            results_dir = self.new_results_dir()
            handoff = _build_handoff(
                snapshot,
                legs=[_LegSpec(row=ROW_CE15)],
                route_rows=[ROW_CE15],
                assets_root=results_dir,
            )
            _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)

        remaining = sorted(
            p.name for p in catalogue_dir.glob("pfSense-pkg-pfBlockerNG-*.pkg")
        )
        self.assertEqual(len(remaining), keep)
        self.assertNotIn(f"pfSense-pkg-pfBlockerNG-{first_version}.pkg", remaining)
        self.assertTrue((catalogue_dir / dep_name).is_file())


# --------------------------------------------------------------------------- #
# issue #2468 nightly analogue: dependency identity = filename, place-if-missing,
# never byte-compared or overwritten. Nightly rebuilds its dep every run.
# --------------------------------------------------------------------------- #


class DependencyPlaceIfMissingTests(_TempDirTestCase):
    def test_rebuilt_dependency_different_bytes_publishes_without_conflict(
        self,
    ) -> None:
        """RED CANARY (issue #2468), nightly analogue: Nightly rebuilds its
        dependency every run from whatever source commit triggered it — new archive
        bytes under the identical name-version is expected, not an error. A second
        Nightly run publishing a byte-different rebuild of the same dep must succeed
        without DestinationConflictError, and the already-published dep bytes must
        stay untouched (place-if-missing, never compared, never overwritten)."""
        handoff, results_dir, _snap = self.base_handoff()
        first = _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertTrue(first.touched)

        catalogue_dir = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        dep_path = catalogue_dir / "py311-charset-normalizer-3.4.0.pkg"
        original_dep_bytes = dep_path.read_bytes()

        newer = _snapshot(build_date=date(2026, 8, 5))
        results_dir_2 = self.new_results_dir()
        handoff_2 = _build_handoff(
            newer,
            legs=[
                _LegSpec(
                    row=ROW_CE15,
                    dep_payload={"filler.bin": b"rebuilt-from-new-source-commit"},
                )
            ],
            route_rows=[ROW_CE15],
            assets_root=results_dir_2,
        )

        second = _run(
            handoff=handoff_2, results_dir=results_dir_2, pkg_repo=self.pkg_repo
        )

        self.assertTrue(second.touched)
        canonical_name = f"pfSense-pkg-pfBlockerNG-{newer.pkg_version}.pkg"
        self.assertTrue((catalogue_dir / canonical_name).is_file())
        self.assertEqual(dep_path.read_bytes(), original_dep_bytes)

    def test_dep_already_different_at_one_varver_does_not_trip_identity_check(
        self,
    ) -> None:
        """issue #2468: two build-role ROUTE rows of one major (ce-2.8 + ce-2.9) both
        declare the dep, so one leg's dep artifact reaches two catalogues. When one of
        them already holds a byte-different build of that same name-version, the run
        must still succeed: dependency bytes are deliberately NOT part of the
        multi-destination fan-out identity invariant, which covers the canonical
        package alone."""
        stale_dir = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        stale_dir.mkdir(parents=True)
        stale_path, _digest = _wrap_dependency_pkg(
            stale_dir,
            name=_CHARSET_NAME,
            version="3.4.0",
            abi="FreeBSD:15:*",
            local_name=_CHARSET_PKG,
            payload={"filler.bin": b"an-earlier-nights-build"},
        )
        stale_bytes = stale_path.read_bytes()

        results_dir = self.new_results_dir()
        handoff = _build_handoff(
            _snapshot(),
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15, ROW_CE15_29],
            assets_root=results_dir,
        )
        report = _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)

        self.assertEqual(
            set(report.touched), {("nightly", "ce-2.8"), ("nightly", "ce-2.9")}
        )
        docs = self.pkg_repo / "docs" / "nightly"
        self.assertEqual(
            (docs / "ce-2.8" / _CHARSET_PKG).read_bytes(), stale_bytes
        )  # left as it was
        fresh = docs / "ce-2.9" / _CHARSET_PKG
        self.assertTrue(fresh.is_file())
        self.assertNotEqual(
            fresh.read_bytes(), stale_bytes
        )  # this run's own build, placed where missing

    def test_undeclared_same_name_leftover_replaced_by_this_runs_dependency(
        self,
    ) -> None:
        """A Nightly destination holding a same-name dependency whose origin the row no
        longer declares (the port moved category, issue #2403) must end ONE run
        advertising the dependency this run verified — place-if-missing and eviction
        together may never leave the catalogue with the extra silently absent."""
        dest = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        dest.mkdir(parents=True)
        _wrap_dependency_pkg(
            dest,
            name=_CHARSET_NAME,
            version="3.4.0",
            abi="FreeBSD:15:*",
            local_name=_CHARSET_PKG,
            origin="www/py-charset-normalizer",
            payload={"filler.bin": b"leftover-from-the-other-category"},
        )

        handoff, results_dir, _snap = self.base_handoff()
        report = _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)

        self.assertEqual(report.touched, (("nightly", "ce-2.8"),))
        published = dest / _CHARSET_PKG
        self.assertTrue(
            published.is_file(),
            "this run's verified dependency is missing from the catalogue",
        )
        self.assertEqual(
            pfb_pkg.read_compact_manifest(published)["origin"],
            f"textproc/{_CHARSET_NAME}",
        )
        self.assertIn(_CHARSET_NAME, _packagesite_names(dest))


# --------------------------------------------------------------------------- #
# T6 — handoff integrity rejections.
# --------------------------------------------------------------------------- #


class HandoffIntegrityTests(_TempDirTestCase):
    def test_intake_kind_is_nightly(self) -> None:
        intake = pc.parse_intake(_REPO, "", "", '["nightly"]', _RUN_ID)
        self.assertEqual(intake.kind, "nightly")

    def test_sha256_mismatch_vs_file_bytes_rejected(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        mutated["builds"][0]["artifact"]["sha256"] = "0" * 64
        with self.assertRaises(pc.AssetVerificationError):
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)

    def test_record_version_mismatches_snapshot_rejected(self) -> None:
        results_dir = self.new_results_dir()
        real_alloc = _snapshot(build_date=date(2026, 8, 4))
        forged_alloc = _snapshot(build_date=date(2026, 8, 9))
        handoff = _build_handoff(
            real_alloc,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_dir,
        )
        legdir = _leg_dir(results_dir, ROW_CE15)
        forged_record = _make_record(forged_alloc, ROW_CE15)
        forged_name = f"pfSense-pkg-pfBlockerNG-{forged_alloc.pkg_version}.pkg"
        _path, forged_digest = _wrap_canonical_pkg(
            legdir, forged_record, local_name=forged_name
        )

        mutated = _mutate(handoff)
        mutated["builds"][0]["artifact"] = {
            "abi": "FreeBSD:15:*",
            "name": forged_name,
            "sha256": forged_digest,
        }

        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("artifact", str(ctx.exception))

    def test_run_id_mismatch_rejected(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(
                handoff=handoff,
                results_dir=results_dir,
                pkg_repo=self.pkg_repo,
                source_run_id="some-other-run",
            )
        self.assertIn("run_id", str(ctx.exception))

    def test_kind_wrong_rejected(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        mutated["kind"] = "tagged-handoff"
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("kind", str(ctx.exception))

    def test_schema_wrong_rejected(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        for schema in (2, True):
            with self.subTest(schema=schema):
                mutated = _mutate(handoff)
                mutated["schema"] = schema
                with self.assertRaises(pn.PublishNightlyError) as ctx:
                    _run(
                        handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo
                    )
                self.assertIn("schema", str(ctx.exception))

    def test_top_level_field_missing_rejected(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        del mutated["source_ref"]
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("exact fields", str(ctx.exception))

    def test_top_level_field_extra_rejected(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        mutated["bogus"] = "nope"
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("exact fields", str(ctx.exception))

    def test_dependency_contract_drift_rejected_without_pkg_tree_mutation(
        self,
    ) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        sentinel = self.pkg_repo / "docs" / "sentinel.bin"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b"unchanged")

        epoch_drift = _mutate(handoff)
        epoch_drift["source_date_epoch"] += 1
        epoch_drift["input_digest"] = _handoff_input_digest(
            source_sha=epoch_drift["source_sha"],
            ports_sha=epoch_drift["ports_sha"],
            matrix_digest=epoch_drift["matrix_digest"],
            source_date_epoch=epoch_drift["source_date_epoch"],
            dependency_builder=epoch_drift["dependency_builder"],
        )
        builder_drift = _mutate(handoff)
        builder_drift["dependency_builder"]["uv"] = "0.12.7"
        builder_drift["input_digest"] = _handoff_input_digest(
            source_sha=builder_drift["source_sha"],
            ports_sha=builder_drift["ports_sha"],
            matrix_digest=builder_drift["matrix_digest"],
            source_date_epoch=builder_drift["source_date_epoch"],
            dependency_builder=builder_drift["dependency_builder"],
        )
        legacy_record = _mutate(handoff)
        del legacy_record["builds"][0]["record"]["dependency_builder"]
        old_digest = _mutate(handoff)
        old_payload = "\0".join(
            (
                old_digest["source_sha"],
                old_digest["ports_sha"],
                old_digest["matrix_digest"],
            )
        ).encode("ascii")
        old_digest["input_digest"] = hashlib.sha256(old_payload).hexdigest()
        hostile = (
            ("top-epoch", epoch_drift, "source_date_epoch"),
            ("top-builder", builder_drift, "dependency_builder"),
            ("legacy-record", legacy_record, "dependency_builder"),
            ("old-input-digest", old_digest, "input_digest"),
        )
        before_paths = tuple(
            sorted(path.relative_to(self.pkg_repo) for path in self.pkg_repo.rglob("*"))
        )
        before_bytes = {
            path.relative_to(self.pkg_repo): path.read_bytes()
            for path in self.pkg_repo.rglob("*")
            if path.is_file()
        }

        for label, mutated, responsible_field in hostile:
            with self.subTest(label=label):
                with self.assertRaises(pn.PublishNightlyError) as ctx:
                    _run(
                        handoff=mutated,
                        results_dir=results_dir,
                        pkg_repo=self.pkg_repo,
                    )
                self.assertIn(responsible_field, str(ctx.exception))
                self.assertEqual(
                    tuple(
                        sorted(
                            path.relative_to(self.pkg_repo)
                            for path in self.pkg_repo.rglob("*")
                        )
                    ),
                    before_paths,
                )
                self.assertEqual(
                    {
                        path.relative_to(self.pkg_repo): path.read_bytes()
                        for path in self.pkg_repo.rglob("*")
                        if path.is_file()
                    },
                    before_bytes,
                )

    def test_build_entry_field_missing_rejected(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        del mutated["builds"][0]["dep_artifacts"]
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("build entry", str(ctx.exception))

    def test_build_entry_field_extra_rejected(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        mutated["builds"][0]["bogus"] = 1
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("build entry", str(ctx.exception))

    def test_duplicate_exact_build_tuple_rejected_before_publication(self) -> None:
        """Hostile: two build entries claiming the SAME exact runtime tuple
        (15 / PHP 8.3 / py311 — ce-2.8 + ce-2.9) are a forged handoff. Rejected at
        ingestion, reporting the tuple, before anything is published."""
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15), _LegSpec(row=ROW_CE15_29)],
            route_rows=[ROW_CE15, ROW_CE15_29],
            assets_root=results_dir,
        )
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("duplicate", str(ctx.exception))
        self.assertIn("('15', '8.3', 'py311')", str(ctx.exception))
        self.assertFalse((self.pkg_repo / "docs" / "nightly").exists())

    def test_snapshot_source_sha_mismatch_top_level_rejected(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        mutated["source_sha"] = "f" * 40
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("source SHA", str(ctx.exception))

    def test_snapshot_ports_sha_mismatch_top_level_rejected(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        mutated["ports_sha"] = "f" * 40
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("ports_sha", str(ctx.exception))

    def test_tampered_matrix_digest_input_digest_mismatch_rejected(self) -> None:
        """The publisher recomputes both matrix and combined input digests."""
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        self.assertNotEqual(mutated["matrix_digest"], "d" * 64)
        mutated["matrix_digest"] = "d" * 64
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("matrix_digest", str(ctx.exception))

    def test_matrix_digest_malformed_shape_rejected(self) -> None:
        """N2: matrix_digest shape (lowercase 64-character hex) is validated
        before dependency-bound input_digest reconstruction."""
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        mutated["matrix_digest"] = "not-hex"
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("matrix_digest", str(ctx.exception))

    def test_tools_and_matrix_sha_are_revalidated(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        for field in ("tools_sha", "matrix_sha"):
            with self.subTest(field=field):
                mutated = _mutate(handoff)
                mutated[field] = "not-a-sha"
                with self.assertRaises(pn.PublishNightlyError):
                    _run(
                        handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo
                    )

    def test_build_matrix_must_match_build_entries(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        mutated["build_matrix"][0]["php_version"] = "php999"
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("build_matrix", str(ctx.exception))

    def test_literal_build_record_must_match_verified_payload(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        mutated["builds"][0]["record"]["source_sha"] = "f" * 40
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("record", str(ctx.exception))

    def test_dependency_count_must_match_matrix_extra_packages(self) -> None:
        handoff, results_dir, _alloc = self.base_handoff()
        mutated = _mutate(handoff)
        mutated["builds"][0]["dep_artifacts"] = []
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("extra_pkgs", str(ctx.exception))

    def test_invalid_utf8_handoff_is_a_publisher_error(self) -> None:
        handoff_path = self.tmp / "nightly-handoff.json"
        handoff_path.write_bytes(b"\xff")
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            pn.run(
                handoff_path=handoff_path,
                results_dir=self.tmp,
                pkg_repo=self.pkg_repo,
                source_run_id=_RUN_ID,
            )
        self.assertIn("UTF-8", str(ctx.exception))


# --------------------------------------------------------------------------- #
# T7 — routing rejections.
# --------------------------------------------------------------------------- #


class RoutingTests(_TempDirTestCase):
    def test_route_row_major_has_no_asset_rejected(self) -> None:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15, ROW_PLUS16_03],
            assets_root=results_dir,
        )
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)
        message = str(ctx.exception)
        self.assertIn("no built asset", message)
        # The diagnostic must carry the COMPLETE runtime tuple, py flavor included.
        self.assertIn("runtime tuple ('16', '8.5', 'py311')", message)

    def test_canonical_asset_serving_zero_route_rows_rejected(self) -> None:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15), _LegSpec(row=ROW_PLUS16_03)],
            route_rows=[ROW_CE15],
            assets_root=results_dir,
        )
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("serve no ROUTE build row", str(ctx.exception))

    def test_two_legs_same_tuple_rejected_via_route_targets(self) -> None:
        """The routing-level defensive duplicate-tuple guard, exercised directly
        (bypassing handoff validation, which already has its OWN dup-tuple test
        above) via two hand-built VerifiedAsset/_Leg objects sharing one exact
        runtime tuple."""
        snapshot = _snapshot()
        record_a = _make_record(snapshot, ROW_CE15)
        record_b = _make_record(snapshot, ROW_CE15, _EPOCH + 1)
        asset_a = pc.VerifiedAsset(
            asset_class="canonical",
            declared_name="a.pkg",
            canonical_name="a.pkg",
            work_path=Path("a.pkg"),
            sha256="0" * 64,
            manifest={},
            record=record_a,
        )
        asset_b = pc.VerifiedAsset(
            asset_class="canonical",
            declared_name="b.pkg",
            canonical_name="b.pkg",
            work_path=Path("b.pkg"),
            sha256="1" * 64,
            manifest={},
            record=record_b,
        )
        leg_a = pn._Leg(
            key=pn._build_key(ROW_CE15),
            matrix_row=ROW_CE15,
            canonical=asset_a,
            dependencies=(),
        )
        leg_b = pn._Leg(
            key=pn._build_key(ROW_CE15),
            matrix_row=ROW_CE15,
            canonical=asset_b,
            dependencies=(),
        )

        with self.assertRaises(pn.PublishNightlyError) as ctx:
            pn._route_targets([ROW_CE15], [leg_a, leg_b])
        self.assertIn("more than one built asset", str(ctx.exception))

    def test_dep_abi_mismatch_rejected_via_route_targets(self) -> None:
        """A dependency ABI outside its leg's FreeBSD major is rejected."""
        snapshot = _snapshot()
        record = _make_record(snapshot, ROW_CE15)
        canonical = pc.VerifiedAsset(
            asset_class="canonical",
            declared_name="a.pkg",
            canonical_name="a.pkg",
            work_path=Path("a.pkg"),
            sha256="0" * 64,
            manifest={},
            record=record,
        )
        mismatched_dep = pc.VerifiedAsset(
            asset_class="dependency",
            declared_name="dep.pkg",
            canonical_name="dep.pkg",
            work_path=Path("dep.pkg"),
            sha256="1" * 64,
            manifest={"abi": "FreeBSD:16:*"},
        )
        leg = pn._Leg(
            key=pn._build_key(ROW_CE15),
            matrix_row=ROW_CE15,
            canonical=canonical,
            dependencies=(mismatched_dep,),
        )

        with self.assertRaises(pn.PublishNightlyError) as ctx:
            pn._route_targets([ROW_CE15], [leg])
        self.assertIn("dependency ABI does not match FreeBSD major", str(ctx.exception))


# --------------------------------------------------------------------------- #
# T8 — dependency verification failures.
# --------------------------------------------------------------------------- #


class DependencyTests(_TempDirTestCase):
    def test_dep_file_missing_from_leg_dir_rejected(self) -> None:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[
                _LegSpec(
                    row=ROW_CE15, dep_specs=[("py311-charset-normalizer", "3.4.0")]
                )
            ],
            route_rows=[ROW_CE15],
            assets_root=results_dir,
        )
        (_leg_dir(results_dir, ROW_CE15) / "py311-charset-normalizer-3.4.0.pkg").unlink()
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("missing dependency asset", str(ctx.exception))

    def test_dep_sha_mismatch_rejected(self) -> None:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[
                _LegSpec(
                    row=ROW_CE15, dep_specs=[("py311-charset-normalizer", "3.4.0")]
                )
            ],
            route_rows=[ROW_CE15],
            assets_root=results_dir,
        )
        mutated = _mutate(handoff)
        mutated["builds"][0]["dep_artifacts"][0]["sha256"] = "0" * 64
        with self.assertRaises(pc.AssetVerificationError):
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)

    def test_dep_tagged_style_suffixed_name_rejected(self) -> None:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[
                _LegSpec(
                    row=ROW_CE15, dep_specs=[("py311-charset-normalizer", "3.4.0")]
                )
            ],
            route_rows=[ROW_CE15],
            assets_root=results_dir,
        )
        legdir = _leg_dir(results_dir, ROW_CE15)
        suffixed_name = "py311-charset-normalizer-3.4.0-CE-2.8.pkg"
        (legdir / "py311-charset-normalizer-3.4.0.pkg").rename(legdir / suffixed_name)
        mutated = _mutate(handoff)
        mutated["builds"][0]["dep_artifacts"][0]["name"] = suffixed_name
        with self.assertRaises(pc.AssetVerificationError) as ctx:
            _run(handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("declared name", str(ctx.exception))


# --------------------------------------------------------------------------- #
# T10 — a route-only row is never targeted, and errors on nothing.
# --------------------------------------------------------------------------- #


class RouteOnlyTests(_TempDirTestCase):
    def test_route_only_row_not_targeted_no_error(self) -> None:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15, ROW_ROUTE_ONLY_17],
            assets_root=results_dir,
        )
        report = _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertEqual(report.touched, (("nightly", "ce-2.8"),))
        self.assertFalse((self.pkg_repo / "docs" / "nightly" / "ce-17.0").exists())


# --------------------------------------------------------------------------- #
# Legacy result-directory transition (fix round 1, review finding 1): the
# producer's pre-tuple contract used nightly-result-<major>/. The consumer
# prefers the tuple-bearing directory and falls back to the legacy one ONLY
# while a major carries exactly ONE build tuple; a major with multiple tuples
# never falls back (that would conflate distinct PHP/Python artifacts).
# --------------------------------------------------------------------------- #


class LegacyLayoutTests(_TempDirTestCase):
    def test_single_tuple_major_falls_back_to_legacy_result_dir(self) -> None:
        """An unchanged one-build-per-major producer handoff publishes unchanged
        from the legacy nightly-result-<major>/ directory."""
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_dir,
        )
        _leg_dir(results_dir, ROW_CE15).rename(
            results_dir / f"{pn._LEG_DIR_PREFIX}15"
        )

        report = _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)

        self.assertEqual(report.touched, (("nightly", "ce-2.8"),))
        docs = self.pkg_repo / "docs" / "nightly"
        canonical_name = f"pfSense-pkg-pfBlockerNG-{snapshot.pkg_version}.pkg"
        self.assertTrue((docs / "ce-2.8" / canonical_name).is_file())
        self.assertTrue((docs / "ce-2.8" / _CHARSET_PKG).is_file())

    def test_multi_tuple_major_never_falls_back_to_legacy_dir(self) -> None:
        """Two build tuples share major 16; a legacy nightly-result-16/ directory
        must NOT be consulted for either — falling back there would conflate the
        PHP 8.4 and PHP 8.5 artifacts, the exact ambiguity issue #2926 removes."""
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_PLUS16_25_11), _LegSpec(row=ROW_PLUS16_03)],
            route_rows=[ROW_PLUS16_25_11, ROW_PLUS16_03],
            assets_root=results_dir,
        )
        _leg_dir(results_dir, ROW_PLUS16_25_11).rename(
            results_dir / f"{pn._LEG_DIR_PREFIX}16"
        )

        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)
        message = str(ctx.exception)
        self.assertIn("missing canonical asset", message)
        self.assertIn("('16', '8.4', 'py311')", message)
        self.assertFalse((self.pkg_repo / "docs" / "nightly").exists())


# --------------------------------------------------------------------------- #
# T11 — missing results dir / missing canonical file -> a clean rejection, not a
# raw OSError traceback.
# --------------------------------------------------------------------------- #


class MissingFileTests(_TempDirTestCase):
    def test_missing_results_dir_clean_error(self) -> None:
        scratch = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=scratch,
        )
        missing_dir = self.tmp / "does-not-exist"
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=handoff, results_dir=missing_dir, pkg_repo=self.pkg_repo)
        self.assertIn("missing canonical asset", str(ctx.exception))

    def test_missing_canonical_file_clean_error(self) -> None:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_dir,
        )
        canonical_name = f"pfSense-pkg-pfBlockerNG-{snapshot.pkg_version}.pkg"
        (_leg_dir(results_dir, ROW_CE15) / canonical_name).unlink()
        with self.assertRaises(pn.PublishNightlyError) as ctx:
            _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("missing canonical asset", str(ctx.exception))

# --------------------------------------------------------------------------- #
# T12 — hostile artifact names, rejected BEFORE any path join.
# --------------------------------------------------------------------------- #


class HostileNameTests(_TempDirTestCase):
    def test_hostile_canonical_artifact_name_rejected(self) -> None:
        for hostile in ("../x.pkg", "a/b.pkg", "a\\b.pkg"):
            with self.subTest(hostile=hostile):
                results_dir = self.new_results_dir()
                snapshot = _snapshot()
                handoff = _build_handoff(
                    snapshot,
                    legs=[_LegSpec(row=ROW_CE15)],
                    route_rows=[ROW_CE15],
                    assets_root=results_dir,
                )
                mutated = _mutate(handoff)
                mutated["builds"][0]["artifact"]["name"] = hostile
                with self.assertRaises((pc.AssetVerificationError, nc.ContractError)):
                    _run(
                        handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo
                    )

    def test_hostile_dep_artifact_name_rejected(self) -> None:
        for hostile in ("../x.pkg", "a/b.pkg", "a\\b.pkg"):
            with self.subTest(hostile=hostile):
                results_dir = self.new_results_dir()
                snapshot = _snapshot()
                handoff = _build_handoff(
                    snapshot,
                    legs=[
                        _LegSpec(
                            row=ROW_CE15,
                            dep_specs=[("py311-charset-normalizer", "3.4.0")],
                        )
                    ],
                    route_rows=[ROW_CE15],
                    assets_root=results_dir,
                )
                mutated = _mutate(handoff)
                mutated["builds"][0]["dep_artifacts"][0]["name"] = hostile
                with self.assertRaises((pc.AssetVerificationError, nc.ContractError)):
                    _run(
                        handoff=mutated, results_dir=results_dir, pkg_repo=self.pkg_repo
                    )


# --------------------------------------------------------------------------- #
# T13 — main() CLI wrapper.
# --------------------------------------------------------------------------- #


class MainCliTests(_TempDirTestCase):
    def test_main_invalid_json_exit_1_with_error(self) -> None:
        results_dir = self.new_results_dir()
        handoff_path = self.tmp / "bad.json"
        handoff_path.write_text("not json", encoding="utf-8")
        argv = [
            "--handoff",
            str(handoff_path),
            "--results-dir",
            str(results_dir),
            "--pkg-repo",
            str(self.pkg_repo),
            "--source-run-id",
            _RUN_ID,
        ]
        with (
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            code = pn.main(argv)
        self.assertEqual(code, 1)
        self.assertIn("::error::", err.getvalue())

    def test_main_success_prints_updated_and_returns_zero(self) -> None:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_dir,
        )
        handoff_path = self.tmp / "handoff.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        argv = [
            "--handoff",
            str(handoff_path),
            "--results-dir",
            str(results_dir),
            "--pkg-repo",
            str(self.pkg_repo),
            "--source-run-id",
            _RUN_ID,
        ]
        with (
            mock.patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            code = pn.main(argv)
        self.assertEqual(code, 0)
        self.assertIn("updated nightly/ce-2.8", out.getvalue())


# --------------------------------------------------------------------------- #
# --sign-key threading (issue #2675 step 1): run()/main() must reach
# catalogue_assembly.regenerate_catalogue with the caller's key, or with none at
# all when omitted. The signed wire format itself is test_catalogue_assembly.py's
# and test_catalogue_engine.py's own coverage — never re-derived here.
# --------------------------------------------------------------------------- #


class SignKeyThreadingTests(_TempDirTestCase):
    def _capture_sign_key(self) -> tuple[mock._patch, list[Path | None]]:
        seen: list[Path | None] = []
        real_regenerate = pn.ca.regenerate_catalogue

        def capturing_regenerate(
            site_root: str | Path,
            channel: str,
            varver: str,
            *,
            sign_key: Path | None = None,
        ) -> None:
            seen.append(sign_key)
            real_regenerate(site_root, channel, varver)

        return mock.patch.object(
            pn.ca, "regenerate_catalogue", side_effect=capturing_regenerate
        ), seen

    def test_main_sign_key_flag_reaches_regenerate_catalogue(self) -> None:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_dir,
        )
        handoff_path = self.tmp / "handoff.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        # A REAL key: publish() derives its public half up front, so a placeholder
        # would abort the run before the threading this test is about.
        key = tbrp._gen_key(self.tmp / "repo.key")
        argv = [
            "--handoff",
            str(handoff_path),
            "--results-dir",
            str(results_dir),
            "--pkg-repo",
            str(self.pkg_repo),
            "--source-run-id",
            _RUN_ID,
            "--sign-key",
            str(key),
        ]
        patcher, seen = self._capture_sign_key()
        with patcher:
            code = pn.main(argv)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [key])

    def test_main_without_sign_key_flag_passes_none(self) -> None:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_dir,
        )
        handoff_path = self.tmp / "handoff.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        argv = [
            "--handoff",
            str(handoff_path),
            "--results-dir",
            str(results_dir),
            "--pkg-repo",
            str(self.pkg_repo),
            "--source-run-id",
            _RUN_ID,
        ]
        patcher, seen = self._capture_sign_key()
        with patcher:
            code = pn.main(argv)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [None])


# --------------------------------------------------------------------------- #
# T14 — publish() must actually WIRE catalogue_assembly.verify_multi_destination_
# identity over a genuine Plus fan-out (plus-26.03 + plus-26.07, same canonical
# bytes), not merely have access to a function that works in isolation.
# --------------------------------------------------------------------------- #


class IdentityPostConditionTests(_TempDirTestCase):
    def test_multi_destination_divergence_aborts_publish(self) -> None:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_PLUS16_03)],
            route_rows=[ROW_PLUS16_03, ROW_PLUS16_07],
            assets_root=results_dir,
        )
        canonical_name = f"pfSense-pkg-pfBlockerNG-{snapshot.pkg_version}.pkg"
        divergent_alloc = _snapshot(build_date=date(2026, 8, 20))
        divergent_dir = self.tmp / "divergent"
        divergent_dir.mkdir()
        divergent_record = _make_record(divergent_alloc, ROW_PLUS16_03)
        divergent_path, _digest = _wrap_canonical_pkg(
            divergent_dir, divergent_record, local_name="divergent.pkg"
        )
        divergent_bytes = divergent_path.read_bytes()

        real_regenerate = pn.ca.regenerate_catalogue

        def corrupting_regenerate(
            site_root: str | Path,
            channel: str,
            varver: str,
            *,
            sign_key: Path | None = None,
        ) -> None:
            real_regenerate(site_root, channel, varver)
            if varver == "plus-26.07":
                target = Path(site_root) / channel / varver / canonical_name
                target.write_bytes(divergent_bytes)

        with (
            mock.patch.object(
                pn.ca, "regenerate_catalogue", side_effect=corrupting_regenerate
            ),
            self.assertRaises(pn.ca.CatalogueAssemblyError) as ctx,
        ):
            _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)
        self.assertIn("multi-destination identity violation", str(ctx.exception))


def _packagesite_names(catalogue_dir: Path) -> set[str]:
    catalog = catalogue_dir / "packagesite.pkg"
    data = pfb_pkg.zstd_decompress(catalog.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        member = tf.extractfile("packagesite.yaml")
        assert member is not None
        raw = member.read().decode()
    return {json.loads(line)["name"] for line in raw.splitlines() if line}


_CHARSET_PKG = "py311-charset-normalizer-3.4.0.pkg"
_CHARSET_NAME = "py311-charset-normalizer"


class ExtraPkgsEvictionTests(_TempDirTestCase):
    """issue #2402: Nightly dest leftovers are unlinked before regenerate."""

    def _plant_charset(self, dest_dir: Path, *, major: str) -> None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        _wrap_dependency_pkg(
            dest_dir,
            name=_CHARSET_NAME,
            version="3.4.0",
            abi=f"FreeBSD:{major}:*",
            local_name=_CHARSET_PKG,
        )

    def test_stale_plus_extra_evicted_on_new_canonical(self) -> None:
        first = _snapshot(build_date=date(2026, 8, 4))
        results_1 = self.new_results_dir()
        handoff_1 = _build_handoff(
            first,
            legs=[_LegSpec(row=ROW_PLUS16_03)],
            route_rows=[ROW_PLUS16_03],
            assets_root=results_1,
        )
        _run(handoff=handoff_1, results_dir=results_1, pkg_repo=self.pkg_repo)
        dest = self.pkg_repo / "docs" / "nightly" / "plus-26.03"
        self._plant_charset(dest, major="16")

        second = _snapshot(build_date=date(2026, 8, 5))
        results_2 = self.new_results_dir()
        handoff_2 = _build_handoff(
            second,
            legs=[_LegSpec(row=ROW_PLUS16_03)],
            route_rows=[ROW_PLUS16_03],
            assets_root=results_2,
        )
        report = _run(handoff=handoff_2, results_dir=results_2, pkg_repo=self.pkg_repo)
        self.assertFalse(report.noop)
        self.assertFalse((dest / _CHARSET_PKG).exists())
        self.assertTrue(
            (dest / f"pfSense-pkg-pfBlockerNG-{second.pkg_version}.pkg").is_file()
        )
        self.assertNotIn(_CHARSET_NAME, _packagesite_names(dest))

    def test_stale_plus_extra_evicted_on_exact_republish(self) -> None:
        snapshot = _snapshot()
        results_1 = self.new_results_dir()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_PLUS16_03)],
            route_rows=[ROW_PLUS16_03],
            assets_root=results_1,
        )
        first = _run(handoff=handoff, results_dir=results_1, pkg_repo=self.pkg_repo)
        self.assertFalse(first.noop)
        dest = self.pkg_repo / "docs" / "nightly" / "plus-26.03"
        self._plant_charset(dest, major="16")

        results_2 = self.new_results_dir()
        handoff_2 = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_PLUS16_03)],
            route_rows=[ROW_PLUS16_03],
            assets_root=results_2,
        )
        second = _run(handoff=handoff_2, results_dir=results_2, pkg_repo=self.pkg_repo)
        self.assertFalse(second.noop)
        self.assertFalse((dest / _CHARSET_PKG).exists())
        self.assertNotIn(_CHARSET_NAME, _packagesite_names(dest))

    def test_declared_ce_extra_kept_on_new_canonical(self) -> None:
        first = _snapshot(build_date=date(2026, 8, 4))
        results_1 = self.new_results_dir()
        handoff_1 = _build_handoff(
            first,
            legs=[
                _LegSpec(
                    row=ROW_CE15, dep_specs=[("py311-charset-normalizer", "3.4.0")]
                )
            ],
            route_rows=[ROW_CE15],
            assets_root=results_1,
        )
        _run(handoff=handoff_1, results_dir=results_1, pkg_repo=self.pkg_repo)
        dest = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        self.assertTrue((dest / _CHARSET_PKG).is_file())

        second = _snapshot(build_date=date(2026, 8, 5))
        results_2 = self.new_results_dir()
        handoff_2 = _build_handoff(
            second,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_2,
        )
        _run(handoff=handoff_2, results_dir=results_2, pkg_repo=self.pkg_repo)
        self.assertTrue((dest / _CHARSET_PKG).is_file())
        self.assertIn(_CHARSET_NAME, _packagesite_names(dest))

    def test_undeclared_ce_extra_evicted_when_row_drops_extra_pkgs(self) -> None:
        first = _snapshot(build_date=date(2026, 8, 4))
        results_1 = self.new_results_dir()
        handoff_1 = _build_handoff(
            first,
            legs=[
                _LegSpec(
                    row=ROW_CE15, dep_specs=[("py311-charset-normalizer", "3.4.0")]
                )
            ],
            route_rows=[ROW_CE15],
            assets_root=results_1,
        )
        _run(handoff=handoff_1, results_dir=results_1, pkg_repo=self.pkg_repo)
        dest = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        self.assertTrue((dest / _CHARSET_PKG).is_file())

        second = _snapshot(build_date=date(2026, 8, 5))
        results_2 = self.new_results_dir()
        handoff_2 = _build_handoff(
            second,
            legs=[_LegSpec(row=ROW_CE15_NO_EXTRA)],
            route_rows=[ROW_CE15_NO_EXTRA],
            assets_root=results_2,
        )
        _run(handoff=handoff_2, results_dir=results_2, pkg_repo=self.pkg_repo)
        self.assertFalse((dest / _CHARSET_PKG).exists())
        self.assertNotIn(_CHARSET_NAME, _packagesite_names(dest))


class SameMajorDestScopeTests(_TempDirTestCase):
    """issue #2403: one Nightly major fans to CE + Plus; extra follows extra_pkgs."""

    def test_same_major_dep_not_published_to_row_with_empty_extra_pkgs(self) -> None:
        snapshot = _snapshot()
        results_dir = self.new_results_dir()
        handoff = _build_handoff(
            snapshot,
            legs=[
                _LegSpec(
                    row=ROW_CE15, dep_specs=[("py311-charset-normalizer", "3.4.0")]
                )
            ],
            route_rows=[ROW_CE15, ROW_PLUS15_03],
            assets_root=results_dir,
        )
        captured: dict[str, set[str]] = {}
        orig = pr._drop_assets

        def _spy(dest_dir: Path, asset_map: dict) -> bool:
            captured[dest_dir.name] = set(asset_map)
            return orig(dest_dir, asset_map)

        with mock.patch.object(pr, "_drop_assets", side_effect=_spy):
            _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)

        self.assertIn(_CHARSET_PKG, captured["ce-2.8"])
        self.assertNotIn(_CHARSET_PKG, captured["plus-26.03"])
        docs = self.pkg_repo / "docs" / "nightly"
        self.assertTrue((docs / "ce-2.8" / _CHARSET_PKG).is_file())
        self.assertFalse((docs / "plus-26.03" / _CHARSET_PKG).exists())
        self.assertIn(_CHARSET_NAME, _packagesite_names(docs / "ce-2.8"))
        self.assertNotIn(_CHARSET_NAME, _packagesite_names(docs / "plus-26.03"))

    def test_two_same_major_rows_both_declaring_dep_both_receive_it(self) -> None:
        """issue #2468 coverage: two build-role ROUTE rows sharing one FreeBSD major
        (ce-2.8 + ce-2.9, both major 15), BOTH declaring the same extra_pkgs origin,
        both receive the identical dep bytes from the ONE leg that major built —
        _route_targets's existing per-row extra_pkgs gate is unchanged by this issue,
        only the place-if-missing rule for an already-published dep is new."""
        snapshot = _snapshot()
        results_dir = self.new_results_dir()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15, ROW_CE15_29],
            assets_root=results_dir,
        )
        report = _run(handoff=handoff, results_dir=results_dir, pkg_repo=self.pkg_repo)

        self.assertEqual(
            set(report.touched), {("nightly", "ce-2.8"), ("nightly", "ce-2.9")}
        )
        docs = self.pkg_repo / "docs" / "nightly"
        self.assertTrue((docs / "ce-2.8" / _CHARSET_PKG).is_file())
        self.assertTrue((docs / "ce-2.9" / _CHARSET_PKG).is_file())
        self.assertEqual(
            (docs / "ce-2.8" / _CHARSET_PKG).read_bytes(),
            (docs / "ce-2.9" / _CHARSET_PKG).read_bytes(),
        )

    def test_route_targets_continue_filter_required_for_same_major_plus(self) -> None:
        """Nightly dest attach must skip a dest whose row does not declare the extra."""
        source = inspect.getsource(pn._route_targets)
        self.assertIn("_row_declares_dep", source)
        self.assertIn("continue", source)


class ResignUnsignedCatalogueTests(_TempDirTestCase):
    """Nightly's mirror of publish_release's re-sign gate (issue #2675).

    Nightly rebuilds daily, so the unsigned-catalogue window is short here — but the
    gate lives in both publishers because a varver whose package set does not move
    (a retired FreeBSD major still being served) would otherwise never be re-signed.
    """

    def _publish(self, key: Path | None = None) -> pr.PublishReport:
        results_dir = self.new_results_dir()
        snapshot = _snapshot()
        handoff = _build_handoff(
            snapshot,
            legs=[_LegSpec(row=ROW_CE15)],
            route_rows=[ROW_CE15],
            assets_root=results_dir,
        )
        return _run(
            handoff=handoff,
            results_dir=results_dir,
            pkg_repo=self.pkg_repo,
            sign_key=key,
        )

    def test_republish_with_a_key_signs_a_catalogue_that_has_no_signature(self) -> None:
        self.assertEqual(self._publish().touched, (("nightly", "ce-2.8"),))
        catalogue_dir = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        self.assertEqual(tbrp._sig_members(catalogue_dir / "packagesite.pkg"), {})

        key = tbrp._gen_key(self.tmp / "repo.key")
        self.assertEqual(self._publish(key).touched, (("nightly", "ce-2.8"),))
        self.assertEqual(
            sorted(tbrp._sig_members(catalogue_dir / "packagesite.pkg")),
            ["packagesite.yaml.pub", "packagesite.yaml.sig"],
        )

    def test_republish_with_the_same_key_is_a_noop(self) -> None:
        key = tbrp._gen_key(self.tmp / "repo.key")
        self.assertEqual(self._publish(key).touched, (("nightly", "ce-2.8"),))
        self.assertEqual(self._publish(key).touched, ())

    def test_republish_after_key_rotation_resigns_with_the_new_key(self) -> None:
        catalogue_dir = self.pkg_repo / "docs" / "nightly" / "ce-2.8"
        self._publish(tbrp._gen_key(self.tmp / "old.key"))
        first_pub = tbrp._sig_members(catalogue_dir / "packagesite.pkg")[
            "packagesite.yaml.pub"
        ]

        self.assertEqual(
            self._publish(tbrp._gen_key(self.tmp / "new.key")).touched,
            (("nightly", "ce-2.8"),),
        )
        second_pub = tbrp._sig_members(catalogue_dir / "packagesite.pkg")[
            "packagesite.yaml.pub"
        ]
        self.assertNotEqual(first_pub, second_pub)


if __name__ == "__main__":
    unittest.main()
