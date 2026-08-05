"""Tests for scripts/catalogue_assembly.py — issue #2146 step S3 (four disjoint
channel catalogues assembled atomically from an already-resolved plan).

No intake parsing, no ledger, no git, no network — this pins Plan/CatalogueTarget and
assemble() against the pfBlockerNG source-repo engine loaded from PFB_SRC (see
tests/_srcrepo.py). Fixture .pkg archives are minimal, pure-Python zstd-tar files
carrying only +COMPACT_MANIFEST (mirrors tests/test_publish_catalogues.py's
_wrap_dependency_pkg style, simplified further) — assemble()'s pool/dependency
packages never need the full canonical-package validation path (that only fires for a
manifest carrying a pfb_build_record annotation, which these fixtures omit; see
build-repo-portable.py's _validate_annotated_project_pkg / _canonical_build_record).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import itertools
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalogue_assembly as ca
import publish_catalogues as pc
from _srcrepo import SourceRepoError, resolve_src_root

try:
    _SRC_ROOT = resolve_src_root()
    _ENGINE = pc.load_engine(_SRC_ROOT)
    _ENGINE_SKIP_REASON = ""
except (
    SourceRepoError
) as exc:  # pragma: no cover - environment gap, not a behaviour regression
    _SRC_ROOT = None
    _ENGINE = None
    _ENGINE_SKIP_REASON = str(exc)

_requires_engine = unittest.skipIf(_ENGINE is None, _ENGINE_SKIP_REASON)


# --------------------------------------------------------------------------- #
# Fixture builders — minimal pure-Python zstd-tar .pkg archives (no binary
# fixtures vendored). Only +COMPACT_MANIFEST is written.
# --------------------------------------------------------------------------- #

_pkg_counter = itertools.count()


def _write_tar_pkg(path: Path, data: bytes) -> None:
    pfb_pkg = _ENGINE.pfb_pkg
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        info = tarfile.TarInfo(name="+COMPACT_MANIFEST")
        info.size = len(data)
        info.mtime = 0
        tf.addfile(info, io.BytesIO(data))
    path.write_bytes(
        pfb_pkg.zstd_compress(raw.getvalue(), pfb_pkg.PkgError, "zstd unavailable")
    )


def _make_pkg(
    directory: Path,
    *,
    name: str,
    version: str,
    abi: str = "FreeBSD:15:*",
    origin: str | None = None,
    local_name: str | None = None,
) -> Path:
    """A minimal, valid .pkg: zstd-tar carrying only +COMPACT_MANIFEST."""
    manifest = {
        "name": name,
        "version": version,
        "abi": abi,
        "origin": origin or f"net/{name}",
    }
    path = directory / (local_name or f"pkg-{next(_pkg_counter)}.pkg")
    _write_tar_pkg(path, json.dumps(manifest, separators=(",", ":")).encode())
    return path


def _canonical_pkg(
    directory: Path, *, version: str, abi: str = "FreeBSD:15:*", **kw: object
) -> Path:
    return _make_pkg(
        directory,
        name=_ENGINE.pfb_pkg.CANONICAL_EMITTED_IDENTITY,
        version=version,
        abi=abi,
        **kw,
    )


def _dep_pkg(
    directory: Path,
    *,
    name: str = "py311-charset-normalizer",
    version: str = "3.4.0",
    abi: str = "FreeBSD:15:*",
    **kw: object,
) -> Path:
    return _make_pkg(directory, name=name, version=version, abi=abi, **kw)


def _zero_byte_file(directory: Path, name: str = "zero.pkg") -> Path:
    path = directory / name
    path.write_bytes(b"")
    return path


def _not_zstd_file(directory: Path, name: str = "garbage.pkg") -> Path:
    path = directory / name
    path.write_bytes(b"not a zstd archive at all, just plain garbage bytes")
    return path


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _build_record(*, channel: str = "testing", release_line: str | None = None) -> dict:
    """A genuine, digest-bound build record — mirrors tests/test_publish_catalogues.py's
    _matrix_row()/_record() (build_input_digest always engine-computed, never
    hand-typed). Only the "testing" shape is needed here; this is not a general
    replacement for that module's fixture builder."""
    pfb_pkg = _ENGINE.pfb_pkg
    row = {
        "pfsense_version": "2.8",
        "channel": "CE",
        "freebsd_version": "15.0-RELEASE",
        "freebsd_major": "15",
        "php_version": "8.3",
        "py_flavor": "py311",
        "variant": "CE",
        "status": "active",
        "extra_pkgs": [],
    }
    tag = {"stable": "v4.0.0", "testing": "v4.0.1.b1", "edge": "v4.0.0.b1"}[channel]
    info = pfb_pkg.parse_release_tag(tag, channel)
    native = (
        pfb_pkg.CANONICAL_EMITTED_IDENTITY
        if channel == "stable"
        else f"{pfb_pkg.CANONICAL_EMITTED_IDENTITY}-{channel}"
    )
    record = {
        "schema": 1,
        "channel": channel,
        "release_line": info.release_line if release_line is None else release_line,
        "classification": info.stage,
        "source_tag": tag,
        "source_sha": "a" * 40,
        "canonical_package_version": info.pkg_version,
        "native_recipe_identity": native,
        "emitted_identity": pfb_pkg.CANONICAL_EMITTED_IDENTITY,
        "matrix_row": row,
        "freebsd_ports_sha": "b" * 64,
        "route": f"{channel}/{row['variant'].lower()}-2.8",
        "source_date_epoch": 0,
        "build_input_digest": "",
    }
    record["build_input_digest"] = pfb_pkg.build_input_digest(record)
    return record


def _annotated_pkg(
    directory: Path, *, record: dict, local_name: str | None = None
) -> Path:
    """A minimal .pkg carrying a genuine, load_build_record-parseable pfb_build_record
    annotation. Consumed directly by _verify_multi_destination_identity in these
    tests, never by build_repo/validate_project_pkg, so +COMPACT_MANIFEST alone is
    enough (see _make_pkg's docstring for why the fuller archive is unnecessary)."""
    pfb_pkg = _ENGINE.pfb_pkg
    manifest = {
        "name": pfb_pkg.CANONICAL_EMITTED_IDENTITY,
        "version": record["canonical_package_version"],
        "abi": f"FreeBSD:{record['matrix_row']['freebsd_major']}:*",
        "origin": "net/pfSense-pkg-pfBlockerNG",
        "annotations": {
            pfb_pkg.PFB_BUILD_RECORD_KEY: json.dumps(
                record, separators=(",", ":"), sort_keys=True
            )
        },
    }
    path = directory / (local_name or f"pkg-{next(_pkg_counter)}.pkg")
    _write_tar_pkg(path, json.dumps(manifest, separators=(",", ":")).encode())
    return path


def _move_failing_on_call(fail_on_call: int):
    """A shutil.move replacement that performs the REAL move for every call except
    the ``fail_on_call``-th, which raises instead. Lets a test target one exact step
    of a multi-move publish sequence without touching any other call."""
    real_move = shutil.move
    counter = itertools.count(1)

    def _fake(src, dst, *args, **kwargs):
        n = next(counter)
        if n == fail_on_call:
            raise OSError(f"synthetic move failure on call #{n}")
        return real_move(src, dst, *args, **kwargs)

    return _fake


class _TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cat-asm-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


# --------------------------------------------------------------------------- #
# Hostile channel rows.
# --------------------------------------------------------------------------- #


class ChannelValidationTests(_TempDirTestCase):
    @_requires_engine
    def test_channel_empty_rejected(self) -> None:
        self._assert_channel_rejected("")

    @_requires_engine
    def test_channel_release_rejected(self) -> None:
        self._assert_channel_rejected("release")

    @_requires_engine
    def test_channel_devel_rejected(self) -> None:
        self._assert_channel_rejected("devel")

    @_requires_engine
    def test_channel_uppercase_rejected(self) -> None:
        self._assert_channel_rejected("Stable")

    @_requires_engine
    def test_channel_path_traversal_rejected(self) -> None:
        self._assert_channel_rejected("nightly/../etc")

    @_requires_engine
    def test_channel_dot_rejected(self) -> None:
        self._assert_channel_rejected(".")

    @_requires_engine
    def test_channel_dotdot_rejected(self) -> None:
        self._assert_channel_rejected("..")

    def _assert_channel_rejected(self, channel: str) -> None:
        pkg = _canonical_pkg(self.tmp, version="4.0.0")
        target = ca.CatalogueTarget(channel=channel, varver="ce-2.8", pool=(pkg,))
        plan = ca.Plan(targets=(target,))
        with self.assertRaises(ca.CatalogueAssemblyError) as ctx:
            ca.assemble(plan, self.tmp / "out", _ENGINE)
        self.assertIn("unknown channel", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Hostile varver rows.
# --------------------------------------------------------------------------- #


class VarverValidationTests(_TempDirTestCase):
    @_requires_engine
    def test_varver_empty_rejected(self) -> None:
        self._assert_varver_rejected("")

    @_requires_engine
    def test_varver_extra_segment_rejected(self) -> None:
        self._assert_varver_rejected("ce-2.8/extra")

    @_requires_engine
    def test_varver_traversal_prefix_rejected(self) -> None:
        self._assert_varver_rejected("../ce-2.8")

    @_requires_engine
    def test_varver_leading_slash_rejected(self) -> None:
        self._assert_varver_rejected("/ce-2.8")

    @_requires_engine
    def test_varver_meta_rejected(self) -> None:
        self._assert_varver_rejected("meta")

    @_requires_engine
    def test_varver_data_pkg_rejected(self) -> None:
        self._assert_varver_rejected("data.pkg")

    @_requires_engine
    def test_varver_packagesite_pkg_rejected(self) -> None:
        self._assert_varver_rejected("packagesite.pkg")

    @_requires_engine
    def test_varver_leading_hyphen_rejected(self) -> None:
        self._assert_varver_rejected("-2.8")

    @_requires_engine
    def test_varver_trailing_hyphen_rejected(self) -> None:
        self._assert_varver_rejected("ce-")

    @_requires_engine
    def test_varver_nul_byte_rejected(self) -> None:
        self._assert_varver_rejected("ce\x002.8")

    @_requires_engine
    def test_varver_newline_rejected(self) -> None:
        self._assert_varver_rejected("ce\n2.8")

    @_requires_engine
    def test_varver_too_long_rejected(self) -> None:
        self._assert_varver_rejected("a" * 300)

    def _assert_varver_rejected(self, varver: str) -> None:
        pkg = _canonical_pkg(self.tmp, version="4.0.0")
        target = ca.CatalogueTarget(channel="stable", varver=varver, pool=(pkg,))
        plan = ca.Plan(targets=(target,))
        with self.assertRaises(ca.CatalogueAssemblyError):
            ca.assemble(plan, self.tmp / "out", _ENGINE)


# --------------------------------------------------------------------------- #
# Plan-structure hostile rows: empty plan/pool, duplicate keys, missing paths.
# --------------------------------------------------------------------------- #


class PlanStructureValidationTests(_TempDirTestCase):
    @_requires_engine
    def test_empty_plan_rejected(self) -> None:
        plan = ca.Plan(targets=())
        with self.assertRaises(ca.CatalogueAssemblyError) as ctx:
            ca.assemble(plan, self.tmp / "out", _ENGINE)
        self.assertIn("no catalogue targets", str(ctx.exception))

    @_requires_engine
    def test_empty_pool_rejected(self) -> None:
        target = ca.CatalogueTarget(channel="stable", varver="ce-2.8", pool=())
        plan = ca.Plan(targets=(target,))
        with self.assertRaises(ca.CatalogueAssemblyError) as ctx:
            ca.assemble(plan, self.tmp / "out", _ENGINE)
        self.assertIn("empty pool", str(ctx.exception))

    @_requires_engine
    def test_duplicate_catalogue_key_rejected(self) -> None:
        pkg_a = _canonical_pkg(self.tmp, version="4.0.0", local_name="a.pkg")
        pkg_b = _canonical_pkg(self.tmp, version="4.0.0", local_name="b.pkg")
        target_a = ca.CatalogueTarget(channel="stable", varver="ce-2.8", pool=(pkg_a,))
        target_b = ca.CatalogueTarget(channel="stable", varver="ce-2.8", pool=(pkg_b,))
        plan = ca.Plan(targets=(target_a, target_b))
        with self.assertRaises(ca.CatalogueAssemblyError) as ctx:
            ca.assemble(plan, self.tmp / "out", _ENGINE)
        self.assertIn("duplicate catalogue target", str(ctx.exception))

    @_requires_engine
    def test_missing_pool_path_rejected(self) -> None:
        missing = self.tmp / "does-not-exist.pkg"
        target = ca.CatalogueTarget(channel="stable", varver="ce-2.8", pool=(missing,))
        plan = ca.Plan(targets=(target,))
        with self.assertRaises(ca.CatalogueAssemblyError) as ctx:
            ca.assemble(plan, self.tmp / "out", _ENGINE)
        self.assertIn("does not exist", str(ctx.exception))

    @_requires_engine
    def test_directory_instead_of_file_rejected(self) -> None:
        directory = self.tmp / "a-directory.pkg"
        directory.mkdir()
        target = ca.CatalogueTarget(
            channel="stable", varver="ce-2.8", pool=(directory,)
        )
        plan = ca.Plan(targets=(target,))
        with self.assertRaises(ca.CatalogueAssemblyError) as ctx:
            ca.assemble(plan, self.tmp / "out", _ENGINE)
        self.assertIn("does not exist", str(ctx.exception))

    @_requires_engine
    def test_missing_dependency_path_rejected(self) -> None:
        pkg = _canonical_pkg(self.tmp, version="4.0.0")
        missing_dep = self.tmp / "missing-dep.pkg"
        target = ca.CatalogueTarget(
            channel="stable", varver="ce-2.8", pool=(pkg,), dependencies=(missing_dep,)
        )
        plan = ca.Plan(targets=(target,))
        with self.assertRaises(ca.CatalogueAssemblyError) as ctx:
            ca.assemble(plan, self.tmp / "out", _ENGINE)
        self.assertIn("does not exist", str(ctx.exception))


# --------------------------------------------------------------------------- #
# out_dir hostile row.
# --------------------------------------------------------------------------- #


class OutputValidationTests(_TempDirTestCase):
    @_requires_engine
    def test_out_dir_existing_file_rejected(self) -> None:
        out = self.tmp / "out"
        out.write_bytes(b"not a directory")
        pkg = _canonical_pkg(self.tmp, version="4.0.0")
        target = ca.CatalogueTarget(channel="stable", varver="ce-2.8", pool=(pkg,))
        plan = ca.Plan(targets=(target,))
        with self.assertRaises(ca.CatalogueAssemblyError) as ctx:
            ca.assemble(plan, out, _ENGINE)
        self.assertIn("not a directory", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Hostile pool content — these are the engine's own checks (build_repo /
# _emit_catalog_from_paths / _check_collisions), propagated unwrapped.
# --------------------------------------------------------------------------- #


class PoolContentHostileTests(_TempDirTestCase):
    @_requires_engine
    def test_zero_byte_file_rejected(self) -> None:
        bad = _zero_byte_file(self.tmp)
        target = ca.CatalogueTarget(channel="stable", varver="ce-2.8", pool=(bad,))
        plan = ca.Plan(targets=(target,))
        with self.assertRaises(_ENGINE.pfb_pkg.PkgError):
            ca.assemble(plan, self.tmp / "out", _ENGINE)

    @_requires_engine
    def test_non_zstd_file_rejected(self) -> None:
        bad = _not_zstd_file(self.tmp)
        target = ca.CatalogueTarget(channel="stable", varver="ce-2.8", pool=(bad,))
        plan = ca.Plan(targets=(target,))
        with self.assertRaises(_ENGINE.pfb_pkg.PkgError):
            ca.assemble(plan, self.tmp / "out", _ENGINE)

    @_requires_engine
    def test_concrete_abi_rejected(self) -> None:
        pkg = _canonical_pkg(self.tmp, version="4.0.0", abi="FreeBSD:15:amd64")
        target = ca.CatalogueTarget(channel="stable", varver="ce-2.8", pool=(pkg,))
        plan = ca.Plan(targets=(target,))
        with self.assertRaises(_ENGINE.build_repo_portable.BuildRepoError):
            ca.assemble(plan, self.tmp / "out", _ENGINE)

    @_requires_engine
    def test_mixed_abi_majors_rejected(self) -> None:
        pkg_a = _canonical_pkg(self.tmp, version="4.0.0", abi="FreeBSD:15:*")
        pkg_b = _dep_pkg(self.tmp, name="py311-foo", version="1.0", abi="FreeBSD:16:*")
        target = ca.CatalogueTarget(
            channel="stable", varver="ce-2.8", pool=(pkg_a,), dependencies=(pkg_b,)
        )
        plan = ca.Plan(targets=(target,))
        with self.assertRaises(_ENGINE.build_repo_portable.BuildRepoError):
            ca.assemble(plan, self.tmp / "out", _ENGINE)

    @_requires_engine
    def test_same_name_version_different_bytes_rejected(self) -> None:
        pkg_a = _canonical_pkg(
            self.tmp,
            version="4.0.0",
            origin="net/pfSense-pkg-pfBlockerNG",
            local_name="a.pkg",
        )
        pkg_b = _canonical_pkg(
            self.tmp,
            version="4.0.0",
            origin="net/pfSense-pkg-pfBlockerNG-DIFFERENT",
            local_name="b.pkg",
        )
        target = ca.CatalogueTarget(
            channel="stable", varver="ce-2.8", pool=(pkg_a, pkg_b)
        )
        plan = ca.Plan(targets=(target,))
        with self.assertRaises(_ENGINE.build_repo_portable.BuildRepoError):
            ca.assemble(plan, self.tmp / "out", _ENGINE)


# --------------------------------------------------------------------------- #
# Basic functional coverage: each channel alone, each varver alone.
# --------------------------------------------------------------------------- #


class BasicAssemblyTests(_TempDirTestCase):
    @_requires_engine
    def test_channel_stable_alone(self) -> None:
        self._assert_single_channel_catalogue("stable")

    @_requires_engine
    def test_channel_testing_alone(self) -> None:
        self._assert_single_channel_catalogue("testing")

    @_requires_engine
    def test_channel_edge_alone(self) -> None:
        self._assert_single_channel_catalogue("edge")

    @_requires_engine
    def test_channel_nightly_alone(self) -> None:
        self._assert_single_channel_catalogue("nightly")

    def _assert_single_channel_catalogue(self, channel: str) -> None:
        pkg = _canonical_pkg(self.tmp, version="4.0.0")
        target = ca.CatalogueTarget(channel=channel, varver="ce-2.8", pool=(pkg,))
        plan = ca.Plan(targets=(target,))
        out = self.tmp / "out"
        ca.assemble(plan, out, _ENGINE)
        catalogue_dir = out / channel / "ce-2.8"
        self.assertTrue((catalogue_dir / "pfSense-pkg-pfBlockerNG-4.0.0.pkg").is_file())
        self.assertTrue((catalogue_dir / "meta.conf").is_file())
        self.assertTrue((catalogue_dir / "packagesite.pkg").is_file())
        self.assertTrue((catalogue_dir / "data.pkg").is_file())
        for other in ca._KNOWN_CHANNELS - {channel}:
            self.assertFalse((out / other).exists())

    @_requires_engine
    def test_varver_ce_2_8_alone(self) -> None:
        self._assert_single_varver("ce-2.8")

    @_requires_engine
    def test_varver_plus_26_03_alone(self) -> None:
        self._assert_single_varver("plus-26.03", major="16")

    @_requires_engine
    def test_varver_plus_26_07_alone(self) -> None:
        self._assert_single_varver("plus-26.07", major="16")

    def _assert_single_varver(self, varver: str, *, major: str = "15") -> None:
        pkg = _canonical_pkg(self.tmp, version="4.0.0", abi=f"FreeBSD:{major}:*")
        target = ca.CatalogueTarget(channel="stable", varver=varver, pool=(pkg,))
        plan = ca.Plan(targets=(target,))
        out = self.tmp / "out"
        ca.assemble(plan, out, _ENGINE)
        self.assertTrue(
            (out / "stable" / varver / "pfSense-pkg-pfBlockerNG-4.0.0.pkg").is_file()
        )


# --------------------------------------------------------------------------- #
# Tagged destination tuples — the closed set from release_version.py:134, plus
# nightly. Each: the listed catalogues exist, the unlisted ones do not.
# --------------------------------------------------------------------------- #


class DestinationTupleTests(_TempDirTestCase):
    @_requires_engine
    def test_destinations_edge(self) -> None:
        self._assert_destination_tuple(("edge",))

    @_requires_engine
    def test_destinations_testing(self) -> None:
        self._assert_destination_tuple(("testing",))

    @_requires_engine
    def test_destinations_testing_edge(self) -> None:
        self._assert_destination_tuple(("testing", "edge"))

    @_requires_engine
    def test_destinations_stable_testing(self) -> None:
        self._assert_destination_tuple(("stable", "testing"))

    @_requires_engine
    def test_destinations_stable_testing_edge(self) -> None:
        self._assert_destination_tuple(("stable", "testing", "edge"))

    @_requires_engine
    def test_destinations_nightly(self) -> None:
        self._assert_destination_tuple(("nightly",))

    def _assert_destination_tuple(self, destinations: tuple[str, ...]) -> None:
        targets = tuple(
            ca.CatalogueTarget(
                channel=channel,
                varver="ce-2.8",
                pool=(
                    _canonical_pkg(
                        self.tmp, version="4.0.0", local_name=f"{channel}.pkg"
                    ),
                ),
            )
            for channel in destinations
        )
        plan = ca.Plan(targets=targets)
        out = self.tmp / "out"
        ca.assemble(plan, out, _ENGINE)
        for channel in destinations:
            self.assertTrue((out / channel / "ce-2.8").is_dir())
        for channel in ca._KNOWN_CHANNELS - set(destinations):
            self.assertFalse((out / channel).exists())


# --------------------------------------------------------------------------- #
# Fan-out / multi-destination byte+checksum+provenance identity — the ticket's
# "multi-destination fixture proves byte/checksum/provenance identity" criterion,
# both the positive path (through assemble()) and the negative path (a direct,
# white-box call proving the post-condition itself is load-bearing).
# --------------------------------------------------------------------------- #


class FanOutIdentityTests(_TempDirTestCase):
    @_requires_engine
    def test_shared_freebsd_major_fanout_identical_bytes(self) -> None:
        # A NO_ARCH asset with wildcard ABI FreeBSD:16:* legitimately lands in BOTH
        # plus-26.03 and plus-26.07 (both FreeBSD major 16) — same physical file,
        # staged into two catalogues.
        shared = _canonical_pkg(self.tmp, version="4.0.0", abi="FreeBSD:16:*")
        target_a = ca.CatalogueTarget(
            channel="stable", varver="plus-26.03", pool=(shared,)
        )
        target_b = ca.CatalogueTarget(
            channel="stable", varver="plus-26.07", pool=(shared,)
        )
        plan = ca.Plan(targets=(target_a, target_b))
        out = self.tmp / "out"
        ca.assemble(plan, out, _ENGINE)
        path_a = out / "stable" / "plus-26.03" / "pfSense-pkg-pfBlockerNG-4.0.0.pkg"
        path_b = out / "stable" / "plus-26.07" / "pfSense-pkg-pfBlockerNG-4.0.0.pkg"
        self.assertTrue(path_a.is_file())
        self.assertTrue(path_b.is_file())
        data_a, data_b = path_a.read_bytes(), path_b.read_bytes()
        self.assertEqual(data_a, data_b)
        self.assertEqual(
            hashlib.sha256(data_a).hexdigest(), hashlib.sha256(data_b).hexdigest()
        )

    @_requires_engine
    def test_multi_channel_fanout_identical_bytes_sha_and_record(self) -> None:
        shared = _canonical_pkg(self.tmp, version="4.0.0", abi="FreeBSD:15:*")
        channels = ("stable", "testing", "edge")
        targets = tuple(
            ca.CatalogueTarget(channel=channel, varver="ce-2.8", pool=(shared,))
            for channel in channels
        )
        plan = ca.Plan(targets=targets)
        out = self.tmp / "out"
        ca.assemble(plan, out, _ENGINE)
        paths = [
            out / channel / "ce-2.8" / "pfSense-pkg-pfBlockerNG-4.0.0.pkg"
            for channel in channels
        ]
        for p in paths:
            self.assertTrue(p.is_file())
        datas = [p.read_bytes() for p in paths]
        shas = [hashlib.sha256(d).hexdigest() for d in datas]
        self.assertTrue(all(d == datas[0] for d in datas))
        self.assertTrue(all(s == shas[0] for s in shas))
        records = [
            _ENGINE.build_repo_portable._canonical_build_record(
                p, _ENGINE.pfb_pkg.read_compact_manifest(p)
            )
            for p in paths
        ]
        self.assertTrue(all(r == records[0] for r in records))

    @_requires_engine
    def test_multi_destination_divergence_detected(self) -> None:
        """A direct call proving _verify_multi_destination_identity is a real,
        load-bearing post-condition — not merely something the happy path implies."""
        source = _canonical_pkg(self.tmp, version="4.0.0")
        divergent = _canonical_pkg(
            self.tmp, version="4.0.0", origin="net/pfSense-pkg-pfBlockerNG-EVIL"
        )
        tree = self.tmp / "tree"
        (tree / "stable" / "ce-2.8").mkdir(parents=True)
        (tree / "testing" / "ce-2.8").mkdir(parents=True)
        canonical_name = "pfSense-pkg-pfBlockerNG-4.0.0.pkg"
        shutil.copy2(source, tree / "stable" / "ce-2.8" / canonical_name)
        shutil.copy2(divergent, tree / "testing" / "ce-2.8" / canonical_name)
        index = {source.resolve(): [("stable", "ce-2.8"), ("testing", "ce-2.8")]}
        with self.assertRaises(ca.CatalogueAssemblyError) as ctx:
            ca._verify_multi_destination_identity(_ENGINE, tree, index)
        self.assertIn("multi-destination identity violation", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Dependency packages: present, absent, and one wired to only one catalogue even
# though its ABI would nominally match another too.
# --------------------------------------------------------------------------- #


class DependencyTests(_TempDirTestCase):
    @_requires_engine
    def test_dependency_present_included(self) -> None:
        pkg = _canonical_pkg(self.tmp, version="4.0.0")
        dep = _dep_pkg(self.tmp)
        target = ca.CatalogueTarget(
            channel="stable", varver="ce-2.8", pool=(pkg,), dependencies=(dep,)
        )
        plan = ca.Plan(targets=(target,))
        out = self.tmp / "out"
        ca.assemble(plan, out, _ENGINE)
        self.assertTrue(
            (out / "stable" / "ce-2.8" / "py311-charset-normalizer-3.4.0.pkg").is_file()
        )

    @_requires_engine
    def test_dependency_absent_pool_only(self) -> None:
        pkg = _canonical_pkg(self.tmp, version="4.0.0")
        target = ca.CatalogueTarget(channel="stable", varver="ce-2.8", pool=(pkg,))
        plan = ca.Plan(targets=(target,))
        out = self.tmp / "out"
        ca.assemble(plan, out, _ENGINE)
        present = sorted(
            p.name
            for p in (out / "stable" / "ce-2.8").glob("*.pkg")
            if p.name not in {"packagesite.pkg", "data.pkg"}
        )
        self.assertEqual(present, ["pfSense-pkg-pfBlockerNG-4.0.0.pkg"])

    @_requires_engine
    def test_dependency_not_wired_to_other_catalogue_stays_out(self) -> None:
        # dep's ABI (major 15) would nominally match BOTH catalogues below, but the
        # plan wires it only into catalogue A's dependencies — assemble() trusts the
        # plan, it never independently fans a dep out by ABI match.
        pkg_a = _canonical_pkg(
            self.tmp, version="4.0.0", abi="FreeBSD:15:*", local_name="a.pkg"
        )
        pkg_b = _canonical_pkg(
            self.tmp, version="4.0.0", abi="FreeBSD:15:*", local_name="b.pkg"
        )
        dep = _dep_pkg(self.tmp, abi="FreeBSD:15:*")
        target_a = ca.CatalogueTarget(
            channel="stable", varver="ce-2.8", pool=(pkg_a,), dependencies=(dep,)
        )
        target_b = ca.CatalogueTarget(channel="testing", varver="ce-2.8", pool=(pkg_b,))
        plan = ca.Plan(targets=(target_a, target_b))
        out = self.tmp / "out"
        ca.assemble(plan, out, _ENGINE)
        self.assertTrue(
            (out / "stable" / "ce-2.8" / "py311-charset-normalizer-3.4.0.pkg").is_file()
        )
        self.assertFalse(
            (out / "testing" / "ce-2.8" / "py311-charset-normalizer-3.4.0.pkg").exists()
        )


# --------------------------------------------------------------------------- #
# Retention already applied: the pool's exact version set is emitted, nothing
# pruned or added by this module.
# --------------------------------------------------------------------------- #


class RetentionAppliedTests(_TempDirTestCase):
    @_requires_engine
    def test_pool_multiple_versions_all_emitted(self) -> None:
        versions = ["4.0.0", "3.9.5", "3.9.4"]
        pkgs = tuple(
            _canonical_pkg(self.tmp, version=v, local_name=f"v{v}.pkg")
            for v in versions
        )
        target = ca.CatalogueTarget(channel="stable", varver="ce-2.8", pool=pkgs)
        plan = ca.Plan(targets=(target,))
        out = self.tmp / "out"
        ca.assemble(plan, out, _ENGINE)
        catalogue_dir = out / "stable" / "ce-2.8"
        present = sorted(
            p.name
            for p in catalogue_dir.glob("*.pkg")
            if p.name not in {"packagesite.pkg", "data.pkg"}
        )
        expected = sorted(f"pfSense-pkg-pfBlockerNG-{v}.pkg" for v in versions)
        self.assertEqual(present, expected)


# --------------------------------------------------------------------------- #
# Atomicity: a mid-assembly failure must leave a pre-existing out_dir tree
# byte-identical, and must not leak any half-built catalogue into it.
# --------------------------------------------------------------------------- #


class AtomicityTests(_TempDirTestCase):
    @_requires_engine
    def test_failure_on_last_catalogue_leaves_prior_tree_untouched(self) -> None:
        out = self.tmp / "out"
        # Pre-populate out_dir with a complete prior tree for an UNRELATED catalogue,
        # simulating a previous successful run.
        good_pkg = _canonical_pkg(self.tmp, version="1.0.0")
        seed_plan = ca.Plan(
            targets=(
                ca.CatalogueTarget(
                    channel="nightly", varver="ce-2.8", pool=(good_pkg,)
                ),
            )
        )
        ca.assemble(seed_plan, out, _ENGINE)
        before = _tree_snapshot(out)
        self.assertTrue(before)  # sanity: seed run actually wrote something

        # A run whose SECOND (last) catalogue is invalid (concrete ABI) — the first
        # catalogue in this run would otherwise have succeeded on its own.
        first_pkg = _canonical_pkg(self.tmp, version="2.0.0", local_name="first.pkg")
        bad_pkg = _canonical_pkg(
            self.tmp, version="3.0.0", abi="FreeBSD:15:amd64", local_name="bad.pkg"
        )
        failing_plan = ca.Plan(
            targets=(
                ca.CatalogueTarget(
                    channel="stable", varver="ce-2.8", pool=(first_pkg,)
                ),
                ca.CatalogueTarget(channel="testing", varver="ce-2.8", pool=(bad_pkg,)),
            )
        )
        with self.assertRaises(_ENGINE.build_repo_portable.BuildRepoError):
            ca.assemble(failing_plan, out, _ENGINE)

        after = _tree_snapshot(out)
        self.assertEqual(before, after)
        # Neither half of the failed run leaked into out_dir.
        self.assertFalse((out / "stable").exists())
        self.assertFalse((out / "testing").exists())


# --------------------------------------------------------------------------- #
# Fix round 1 (gate F1/F2): publish is a RECOVERABLE multi-step swap, not a bare
# per-target rmtree+move. Each of the three failure shapes the gate reproduced
# against the unmutated code gets its own test, plus steady-state re-publish
# coverage (F2) — normal replace is the common case the original 52-test suite
# never exercised at all.
# --------------------------------------------------------------------------- #


class PublishRecoveryTests(_TempDirTestCase):
    @_requires_engine
    def test_leaked_fresh_catalogue_rolled_back_on_later_failure(self) -> None:
        """Shape 1: a two-target plan where the SECOND (fresh, no prior content)
        target's publish move fails — the first target must not survive in out_dir."""
        out = self.tmp / "out"
        seed_pkg = _canonical_pkg(self.tmp, version="0.1.0", local_name="seed.pkg")
        ca.assemble(
            ca.Plan(
                targets=(
                    ca.CatalogueTarget(
                        channel="nightly", varver="ce-2.8", pool=(seed_pkg,)
                    ),
                )
            ),
            out,
            _ENGINE,
        )
        before = _tree_snapshot(out)

        first_pkg = _canonical_pkg(self.tmp, version="1.0.0", local_name="first.pkg")
        second_pkg = _canonical_pkg(self.tmp, version="2.0.0", local_name="second.pkg")
        plan = ca.Plan(
            targets=(
                ca.CatalogueTarget(
                    channel="stable", varver="ce-2.8", pool=(first_pkg,)
                ),
                ca.CatalogueTarget(
                    channel="testing", varver="ce-2.8", pool=(second_pkg,)
                ),
            )
        )
        # Both targets are FRESH (no prior out_dir content) -> each publish is exactly
        # one shutil.move call (no backup swap). Fail the second one.
        with (
            mock.patch(
                "catalogue_assembly.shutil.move", side_effect=_move_failing_on_call(2)
            ),
            self.assertRaises(OSError),
        ):
            ca.assemble(plan, out, _ENGINE)

        after = _tree_snapshot(out)
        self.assertEqual(before, after)
        # The catalogue content itself is gone (an empty out/stable/ scaffold
        # directory left behind by dest.parent.mkdir() is harmless bookkeeping, not
        # leaked content — _tree_snapshot only tracks files, which is what the
        # byte-identical guarantee above is actually about).
        self.assertFalse((out / "stable" / "ce-2.8").exists())
        self.assertFalse((out / "testing" / "ce-2.8").exists())

    @_requires_engine
    def test_replaced_catalogue_rolled_back_on_later_failure(self) -> None:
        """Shape 2: both targets REPLACE existing content; the second target's
        publish move fails — the first target's successful replace must be undone,
        not left holding its new (post-failure) version."""
        out = self.tmp / "out"
        old_stable = _canonical_pkg(
            self.tmp, version="1.0.0", local_name="old-stable.pkg"
        )
        old_testing = _canonical_pkg(
            self.tmp, version="1.0.0", local_name="old-testing.pkg"
        )
        ca.assemble(
            ca.Plan(
                targets=(
                    ca.CatalogueTarget(
                        channel="stable", varver="ce-2.8", pool=(old_stable,)
                    ),
                    ca.CatalogueTarget(
                        channel="testing", varver="ce-2.8", pool=(old_testing,)
                    ),
                )
            ),
            out,
            _ENGINE,
        )
        before = _tree_snapshot(out)
        self.assertIn(
            "pfSense-pkg-pfBlockerNG-1.0.0.pkg", {Path(k).name for k in before}
        )

        new_stable = _canonical_pkg(
            self.tmp, version="2.0.0", local_name="new-stable.pkg"
        )
        new_testing = _canonical_pkg(
            self.tmp, version="2.0.0", local_name="new-testing.pkg"
        )
        plan = ca.Plan(
            targets=(
                ca.CatalogueTarget(
                    channel="stable", varver="ce-2.8", pool=(new_stable,)
                ),
                ca.CatalogueTarget(
                    channel="testing", varver="ce-2.8", pool=(new_testing,)
                ),
            )
        )
        # Both targets REPLACE existing content -> each publish is TWO shutil.move
        # calls (aside-to-backup, then new-into-place): calls 1,2 for target 1;
        # calls 3,4 for target 2. Fail the second target's REPLACE move (call 4).
        with (
            mock.patch(
                "catalogue_assembly.shutil.move", side_effect=_move_failing_on_call(4)
            ),
            self.assertRaises(OSError),
        ):
            ca.assemble(plan, out, _ENGINE)

        after = _tree_snapshot(out)
        self.assertEqual(before, after)
        self.assertTrue(
            (out / "stable" / "ce-2.8" / "pfSense-pkg-pfBlockerNG-1.0.0.pkg").is_file()
        )
        self.assertFalse(
            (out / "stable" / "ce-2.8" / "pfSense-pkg-pfBlockerNG-2.0.0.pkg").exists()
        )

    @_requires_engine
    def test_catalogue_restored_when_its_own_replace_move_fails(self) -> None:
        """Shape 3 (worst case): the failing move is the replace move ITSELF, after
        its own aside-to-backup move already succeeded — the prior catalogue must
        come back, not be left gone with nothing in its place."""
        out = self.tmp / "out"
        old_pkg = _canonical_pkg(self.tmp, version="1.0.0", local_name="old.pkg")
        ca.assemble(
            ca.Plan(
                targets=(
                    ca.CatalogueTarget(
                        channel="testing", varver="ce-2.8", pool=(old_pkg,)
                    ),
                )
            ),
            out,
            _ENGINE,
        )
        before = _tree_snapshot(out)

        new_pkg = _canonical_pkg(self.tmp, version="2.0.0", local_name="new.pkg")
        plan = ca.Plan(
            targets=(
                ca.CatalogueTarget(channel="testing", varver="ce-2.8", pool=(new_pkg,)),
            )
        )
        # dest exists -> call 1 = aside-to-backup (must succeed), call 2 = new-into-
        # place (the replace move itself) -> fail exactly there.
        with (
            mock.patch(
                "catalogue_assembly.shutil.move", side_effect=_move_failing_on_call(2)
            ),
            self.assertRaises(OSError),
        ):
            ca.assemble(plan, out, _ENGINE)

        after = _tree_snapshot(out)
        self.assertEqual(before, after)
        self.assertTrue(
            (out / "testing" / "ce-2.8" / "pfSense-pkg-pfBlockerNG-1.0.0.pkg").is_file()
        )


class SteadyStateReplaceTests(_TempDirTestCase):
    @_requires_engine
    def test_replace_existing_catalogue_clean(self) -> None:
        out = self.tmp / "out"
        old_pkg = _canonical_pkg(self.tmp, version="1.0.0", local_name="old.pkg")
        ca.assemble(
            ca.Plan(
                targets=(
                    ca.CatalogueTarget(
                        channel="stable", varver="ce-2.8", pool=(old_pkg,)
                    ),
                )
            ),
            out,
            _ENGINE,
        )
        self.assertTrue(
            (out / "stable" / "ce-2.8" / "pfSense-pkg-pfBlockerNG-1.0.0.pkg").is_file()
        )

        new_pkg = _canonical_pkg(self.tmp, version="2.0.0", local_name="new.pkg")
        ca.assemble(
            ca.Plan(
                targets=(
                    ca.CatalogueTarget(
                        channel="stable", varver="ce-2.8", pool=(new_pkg,)
                    ),
                )
            ),
            out,
            _ENGINE,
        )

        catalogue_dir = out / "stable" / "ce-2.8"
        self.assertFalse((catalogue_dir / "pfSense-pkg-pfBlockerNG-1.0.0.pkg").exists())
        self.assertTrue((catalogue_dir / "pfSense-pkg-pfBlockerNG-2.0.0.pkg").is_file())
        # No nesting: the varver directory must not contain a copy of itself.
        self.assertFalse((catalogue_dir / "ce-2.8").exists())
        self.assertFalse((catalogue_dir / "stable").exists())
        # No leftover backup litter after a clean success.
        self.assertFalse(
            (out / "stable" / ".ce-2.8.catalogue-assembly-backup").exists()
        )

    @_requires_engine
    def test_stale_backup_clobber_is_announced(self) -> None:
        """A stale backup is the last-known-good copy of an interrupted run.

        Clobbering it silently leaves an operator no trail back to the content that
        was destroyed, so the destruction is announced on stderr before it happens.
        """
        out = self.tmp / "out"
        first = _canonical_pkg(self.tmp, version="1.0.0", local_name="first.pkg")
        ca.assemble(
            ca.Plan(
                targets=(
                    ca.CatalogueTarget(
                        channel="stable", varver="ce-2.8", pool=(first,)
                    ),
                )
            ),
            out,
            _ENGINE,
        )
        stale = out / "stable" / ".ce-2.8.catalogue-assembly-backup"
        stale.mkdir()
        (stale / "pfSense-pkg-pfBlockerNG-0.9.0.pkg").write_bytes(b"last known good")

        second = _canonical_pkg(self.tmp, version="2.0.0", local_name="second.pkg")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ca.assemble(
                ca.Plan(
                    targets=(
                        ca.CatalogueTarget(
                            channel="stable", varver="ce-2.8", pool=(second,)
                        ),
                    )
                ),
                out,
                _ENGINE,
            )
        self.assertIn(str(stale), stderr.getvalue())
        self.assertIn("stale", stderr.getvalue().lower())
        self.assertFalse(stale.exists())

    @_requires_engine
    def test_replace_leaves_other_channels_untouched(self) -> None:
        out = self.tmp / "out"
        stable_pkg = _canonical_pkg(self.tmp, version="1.0.0", local_name="stable.pkg")
        testing_pkg = _canonical_pkg(
            self.tmp, version="1.0.0", local_name="testing.pkg"
        )
        ca.assemble(
            ca.Plan(
                targets=(
                    ca.CatalogueTarget(
                        channel="stable", varver="ce-2.8", pool=(stable_pkg,)
                    ),
                    ca.CatalogueTarget(
                        channel="testing", varver="ce-2.8", pool=(testing_pkg,)
                    ),
                )
            ),
            out,
            _ENGINE,
        )
        testing_before = _tree_snapshot(out / "testing")

        new_stable_pkg = _canonical_pkg(
            self.tmp, version="2.0.0", local_name="stable2.pkg"
        )
        ca.assemble(
            ca.Plan(
                targets=(
                    ca.CatalogueTarget(
                        channel="stable", varver="ce-2.8", pool=(new_stable_pkg,)
                    ),
                )
            ),
            out,
            _ENGINE,
        )

        testing_after = _tree_snapshot(out / "testing")
        self.assertEqual(testing_before, testing_after)
        self.assertTrue(
            (out / "stable" / "ce-2.8" / "pfSense-pkg-pfBlockerNG-2.0.0.pkg").is_file()
        )


# --------------------------------------------------------------------------- #
# F3: the "record" axis of _verify_multi_destination_identity has zero
# regression protection in the original suite (no fixture carries a real
# pfb_build_record annotation, so record is None==None everywhere). This test
# isolates that axis: both destination files are BYTE-IDENTICAL (same physical
# source copied twice — the data/sha256 axes agree), and only the record
# returned for one destination is made to diverge (injected via mock on top of a
# GENUINE, load_build_record-parseable annotation), so only the record
# comparison can catch it.
# --------------------------------------------------------------------------- #


class RecordIdentityTests(_TempDirTestCase):
    @_requires_engine
    def test_multi_destination_record_divergence_detected(self) -> None:
        record = _build_record()
        source = _annotated_pkg(self.tmp, record=record)
        tree = self.tmp / "tree"
        (tree / "stable" / "ce-2.8").mkdir(parents=True)
        (tree / "testing" / "ce-2.8").mkdir(parents=True)
        canonical_name = (
            f"pfSense-pkg-pfBlockerNG-{record['canonical_package_version']}.pkg"
        )
        dest_a = tree / "stable" / "ce-2.8" / canonical_name
        dest_b = tree / "testing" / "ce-2.8" / canonical_name
        shutil.copy2(source, dest_a)
        shutil.copy2(source, dest_b)
        self.assertEqual(dest_a.read_bytes(), dest_b.read_bytes())  # bytes/sha256 agree

        real_fn = _ENGINE.build_repo_portable._canonical_build_record

        def _fake(path, manifest):
            real = real_fn(path, manifest)
            if real is not None and Path(path) == dest_b:
                return dict(real, release_line=f"{real['release_line']}-INJECTED")
            return real

        index = {source.resolve(): [("stable", "ce-2.8"), ("testing", "ce-2.8")]}
        with (
            mock.patch.object(
                _ENGINE.build_repo_portable,
                "_canonical_build_record",
                side_effect=_fake,
            ),
            self.assertRaises(ca.CatalogueAssemblyError) as ctx,
        ):
            ca._verify_multi_destination_identity(_ENGINE, tree, index)
        self.assertIn("multi-destination identity violation", str(ctx.exception))


# --------------------------------------------------------------------------- #
# F4: two protections _stage/_build_source_index document in their own
# docstrings but the original suite never tested directly.
# --------------------------------------------------------------------------- #


class StagingProtectionTests(_TempDirTestCase):
    @_requires_engine
    def test_stage_does_not_collide_same_named_files_from_different_dirs(self) -> None:
        dir_a = self.tmp / "a"
        dir_b = self.tmp / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        pkg_a = _canonical_pkg(dir_a, version="1.0.0", local_name="same.pkg")
        pkg_b = _canonical_pkg(dir_b, version="2.0.0", local_name="same.pkg")
        target = ca.CatalogueTarget(
            channel="stable", varver="ce-2.8", pool=(pkg_a, pkg_b)
        )
        out = self.tmp / "out"
        ca.assemble(ca.Plan(targets=(target,)), out, _ENGINE)
        present = sorted(
            p.name
            for p in (out / "stable" / "ce-2.8").glob("*.pkg")
            if p.name not in {"packagesite.pkg", "data.pkg"}
        )
        self.assertEqual(
            present,
            ["pfSense-pkg-pfBlockerNG-1.0.0.pkg", "pfSense-pkg-pfBlockerNG-2.0.0.pkg"],
        )

    @_requires_engine
    def test_stage_forces_pkg_suffix_for_non_pkg_named_source(self) -> None:
        odd = _canonical_pkg(
            self.tmp, version="4.0.0", local_name="asset-without-pkg-suffix.bin"
        )
        target = ca.CatalogueTarget(channel="stable", varver="ce-2.8", pool=(odd,))
        out = self.tmp / "out"
        ca.assemble(ca.Plan(targets=(target,)), out, _ENGINE)
        self.assertTrue(
            (out / "stable" / "ce-2.8" / "pfSense-pkg-pfBlockerNG-4.0.0.pkg").is_file()
        )


class SourceIndexResolutionTests(_TempDirTestCase):
    @_requires_engine
    def test_build_source_index_resolves_aliased_paths_to_one_key(self) -> None:
        real_dir = self.tmp / "real"
        real_dir.mkdir()
        pkg = _canonical_pkg(real_dir, version="4.0.0")
        alias_dir = self.tmp / "alias"
        alias_dir.symlink_to(real_dir)
        aliased_path = alias_dir / pkg.name
        self.assertNotEqual(str(aliased_path), str(pkg))
        self.assertEqual(aliased_path.resolve(), pkg.resolve())

        target_a = ca.CatalogueTarget(channel="stable", varver="ce-2.8", pool=(pkg,))
        target_b = ca.CatalogueTarget(
            channel="testing", varver="ce-2.8", pool=(aliased_path,)
        )
        plan = ca.Plan(targets=(target_a, target_b))

        index = ca._build_source_index(plan)
        self.assertEqual(len(index), 1)
        destinations = next(iter(index.values()))
        self.assertEqual(
            set(destinations), {("stable", "ce-2.8"), ("testing", "ce-2.8")}
        )


if __name__ == "__main__":
    unittest.main()
