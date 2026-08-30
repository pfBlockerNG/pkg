"""Validate and publish one immutable Nightly OCI handoff.

The handoff is treated as untrusted input: every identity, matrix, digest,
literal build record, dependency declaration, and downloaded package is
revalidated before the verified per-major builds fan out to compatible ROUTE
rows. Catalogue retention and multi-destination identity use the same local
publisher modules as tagged Releases. This module never runs git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# Same sys.path idiom as publish_release.py — scripts/ is not a package, and running
# this file directly only puts ITS OWN directory on sys.path for free.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import catalogue_assembly as ca
import catalogue_engine
import nightly_contract as nc
import pfb_pkg
import publish_catalogues as pc
import publish_release as pr

_CHANNEL = "nightly"
_LEG_DIR_PREFIX = "nightly-result-"

# Nightly build identity is the exact runtime tuple (issue #2926): the same
# FreeBSD major may be built more than once with different PHP/Python runtimes,
# so every build-keyed map, guard, and result-directory suffix is keyed by the
# complete tuple, never the major alone.
_BuildKey = tuple[str, str, str]


def _build_key(row: Mapping[str, object]) -> _BuildKey:
    return (
        str(row["freebsd_major"]),
        str(row["php_version"]),
        str(row["py_flavor"]),
    )


def _leg_dirname(row: Mapping[str, object]) -> str:
    major, php_version, py_flavor = _build_key(row)
    return f"{_LEG_DIR_PREFIX}{major}-php{php_version}-{py_flavor}"


class PublishNightlyError(Exception):
    """A handoff-shape, routing, or CLI-level failure detected at ingestion."""


class StaleNightlyError(PublishNightlyError):
    """A destination already holds a canonical version NEWER than this run's own, and
    this run's version is not already present there. A stale rerun must never regress
    a catalogue a newer run already advanced past it."""


# Handoff validation at the pkg ingestion boundary.
# --------------------------------------------------------------------------- #

_HANDOFF_FIELDS = frozenset(
    {
        "schema",
        "kind",
        "run_id",
        "source_ref",
        "ports_repo",
        "ports_ref",
        "pkg_version",
        "input_digest",
        "source_sha",
        "ports_sha",
        "tools_sha",
        "matrix_sha",
        "matrix_digest",
        "source_date_epoch",
        "dependency_builder",
        "build_matrix",
        "route_matrix",
        "builds",
    }
)
_BUILD_ENTRY_FIELDS = frozenset({"matrix_row", "record", "artifact", "dep_artifacts"})


@dataclass(frozen=True)
class _ValidatedHandoff:
    pkg_version: str
    source_sha: str
    ports_sha: str
    route_matrix: list[Mapping[str, object]]
    # record is validated independently here, then compared with the record
    # re-derived from the downloaded canonical package in _verify_builds.
    builds: list[dict[str, object]]


def _validate_handoff(handoff: object, *, source_run_id: str) -> _ValidatedHandoff:
    if not isinstance(handoff, dict):
        raise PublishNightlyError("handoff must be a JSON object")
    keys = set(handoff)
    if keys != _HANDOFF_FIELDS:
        missing = sorted(_HANDOFF_FIELDS - keys)
        unknown = sorted(keys - _HANDOFF_FIELDS)
        raise PublishNightlyError(
            f"handoff exact fields required (missing={missing}, unknown={unknown})"
        )
    if type(handoff["schema"]) is not int or handoff["schema"] != 1:
        raise PublishNightlyError(
            f"handoff schema must be 1, got {handoff['schema']!r}"
        )
    if handoff["kind"] != "nightly-handoff":
        raise PublishNightlyError(
            f"handoff kind must be 'nightly-handoff', got {handoff['kind']!r}"
        )
    run_id = handoff["run_id"]
    if run_id != source_run_id:
        raise PublishNightlyError(
            f"handoff run_id {run_id!r} does not match --source-run-id {source_run_id!r} — "
            "this publisher only accepts the handoff produced by its OWN workflow run "
            "(a mismatch is a stale or foreign handoff replay)"
        )

    source_sha = handoff["source_sha"]
    ports_sha = handoff["ports_sha"]
    tools_sha = handoff["tools_sha"]
    matrix_sha = handoff["matrix_sha"]
    for label, value in (
        ("source_sha", source_sha),
        ("ports_sha", ports_sha),
        ("tools_sha", tools_sha),
        ("matrix_sha", matrix_sha),
    ):
        if not isinstance(value, str) or not nc.SHA.fullmatch(value):
            raise PublishNightlyError(
                f"handoff {label} must be lowercase 40- or 64-character hex"
            )

    pkg_version = handoff["pkg_version"]
    try:
        nc.validate_nightly_version(pkg_version, source_sha=source_sha)
    except ValueError as exc:
        raise PublishNightlyError(str(exc)) from exc

    source_date_epoch = handoff["source_date_epoch"]
    if type(source_date_epoch) is not int or source_date_epoch < 0:
        raise PublishNightlyError(
            "handoff source_date_epoch must be a non-negative integer"
        )
    try:
        dependency_builder = pfb_pkg.validate_dependency_builder(
            handoff["dependency_builder"]
        )
    except pfb_pkg.PkgError as exc:
        raise PublishNightlyError(
            f"handoff dependency_builder is invalid: {exc}"
        ) from exc

    build_matrix_raw = handoff["build_matrix"]
    route_matrix = handoff["route_matrix"]
    if not isinstance(build_matrix_raw, list) or not build_matrix_raw:
        raise PublishNightlyError("handoff build_matrix must be a non-empty list")
    if not isinstance(route_matrix, list) or not route_matrix:
        raise PublishNightlyError("handoff route_matrix must be a non-empty list")
    try:
        build_matrix = [
            pfb_pkg.validate_build_matrix_row(row) for row in build_matrix_raw
        ]
    except pfb_pkg.PkgError as exc:
        raise PublishNightlyError(f"handoff build_matrix is invalid: {exc}") from exc
    build_keys = [_build_key(row) for row in build_matrix]
    build_rows = dict(zip(build_keys, build_matrix))
    if len(build_rows) != len(build_matrix):
        duplicates = sorted({key for key in build_keys if build_keys.count(key) > 1})
        raise PublishNightlyError(
            f"handoff build_matrix contains duplicate build tuples {duplicates!r}"
        )
    matrix_digest = handoff["matrix_digest"]
    if not isinstance(matrix_digest, str) or not nc.DIGEST.fullmatch(matrix_digest):
        raise PublishNightlyError(
            "handoff matrix_digest must be lowercase 64-character hex"
        )
    matrix_payload = json.dumps(
        {
            "tools_sha": tools_sha,
            "matrix_sha": matrix_sha,
            "build": build_matrix_raw,
            "route": route_matrix,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if hashlib.sha256(matrix_payload).hexdigest() != matrix_digest:
        raise PublishNightlyError(
            "handoff matrix_digest does not match tools/matrix/build/route inputs"
        )
    dependency_payload = json.dumps(
        {
            "matrix_digest": matrix_digest,
            "source_date_epoch": source_date_epoch,
            "dependency_builder": dependency_builder,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    dependency_digest = hashlib.sha256(dependency_payload).hexdigest()
    expected_input_digest = nc.combined_nightly_input_digest(
        source_sha, ports_sha, dependency_digest
    )
    if handoff["input_digest"] != expected_input_digest:
        raise PublishNightlyError(
            "handoff input_digest does not match source_sha/ports_sha/dependency provenance "
            "(tampered or corrupt handoff)"
        )

    builds_raw = handoff["builds"]
    if not isinstance(builds_raw, list) or not builds_raw:
        raise PublishNightlyError("handoff builds must be a non-empty list")

    normalized_builds: list[dict[str, object]] = []
    build_keys: set[_BuildKey] = set()
    for entry in builds_raw:
        if not isinstance(entry, dict) or set(entry) != _BUILD_ENTRY_FIELDS:
            raise PublishNightlyError(
                f"handoff build entry exact fields required: {sorted(_BUILD_ENTRY_FIELDS)}"
            )
        try:
            matrix_row = pfb_pkg.validate_build_matrix_row(entry["matrix_row"])
        except pfb_pkg.PkgError as exc:
            raise PublishNightlyError(
                f"handoff build matrix_row is invalid: {exc}"
            ) from exc
        major, php_version, _py_flavor = key = _build_key(matrix_row)
        label = f"FreeBSD {major} php{php_version}"
        if key in build_keys:
            raise PublishNightlyError(
                f"handoff builds contain duplicate build tuple {key!r}"
            )
        if build_rows.get(key) != matrix_row:
            raise PublishNightlyError(
                f"handoff build entry for {label} does not match build_matrix"
            )
        record_raw = entry["record"]
        if (
            isinstance(record_raw, Mapping)
            and record_raw.get("dependency_builder") is None
        ):
            raise PublishNightlyError(
                f"handoff build record for {label} dependency_builder "
                "is required at the Nightly boundary"
            )
        try:
            record = pfb_pkg.validate_build_record(
                record_raw, abi=f"FreeBSD:{major}:amd64"
            )
        except pfb_pkg.PkgError as exc:
            raise PublishNightlyError(
                f"handoff build record for {label} is invalid: {exc}"
            ) from exc
        if (
            record["matrix_row"] != matrix_row
            or record["canonical_package_version"] != pkg_version
            or record["source_sha"] != source_sha
            or record["freebsd_ports_sha"] != ports_sha
        ):
            raise PublishNightlyError(
                f"handoff build record for {label} disagrees with handoff provenance"
            )
        if record["source_date_epoch"] != source_date_epoch:
            raise PublishNightlyError(
                f"handoff build record for {label} source_date_epoch "
                "disagrees with handoff provenance"
            )
        if record.get("dependency_builder") != dependency_builder:
            raise PublishNightlyError(
                f"handoff build record for {label} dependency_builder "
                "disagrees with handoff provenance"
            )
        artifact = nc.validate_artifacts([entry["artifact"]])[0]
        if (
            artifact["abi"] != f"FreeBSD:{major}:*"
            or artifact["name"] != f"pfSense-pkg-pfBlockerNG-{pkg_version}.pkg"
        ):
            raise PublishNightlyError(
                f"handoff canonical artifact for {label} has inconsistent identity"
            )
        dep_artifacts = nc.validate_dep_artifacts(
            entry["dep_artifacts"],
            leg_abi=f"FreeBSD:{major}:*",
            canonical_name=artifact["name"],
        )
        expected_deps = len(matrix_row.get("extra_pkgs") or [])
        if len(dep_artifacts) != expected_deps:
            raise PublishNightlyError(
                f"handoff dep_artifacts count must match extra_pkgs (got {len(dep_artifacts)}, expected {expected_deps})"
            )
        build_keys.add(key)
        normalized_builds.append(
            {
                "matrix_row": matrix_row,
                "record": record,
                "artifact": artifact,
                "dep_artifacts": dep_artifacts,
            }
        )
    if build_keys != set(build_rows):
        missing = sorted(set(build_rows) - build_keys)
        raise PublishNightlyError(
            "handoff builds do not cover every build_matrix row "
            f"(missing build tuples {missing!r})"
        )

    return _ValidatedHandoff(
        pkg_version=pkg_version,
        source_sha=source_sha,
        ports_sha=ports_sha,
        route_matrix=route_matrix,
        builds=normalized_builds,
    )


# --------------------------------------------------------------------------- #
# Asset discovery + verification — one leg directory per exact build tuple.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Leg:
    key: _BuildKey
    matrix_row: Mapping[str, object]
    canonical: pc.VerifiedAsset
    dependencies: tuple[pc.VerifiedAsset, ...]


def _verify_builds(
    intake: pc.Intake,
    validated: _ValidatedHandoff,
    results_dir: Path,
    work_dir: Path,
) -> list[_Leg]:
    legs: list[_Leg] = []
    for index, entry in enumerate(validated.builds):
        matrix_row = entry["matrix_row"]
        assert isinstance(matrix_row, Mapping)
        key = _build_key(matrix_row)
        legdir = results_dir / _leg_dirname(matrix_row)
        artifact = entry["artifact"]
        assert isinstance(artifact, Mapping)

        # Reject a hostile name BEFORE it is ever joined onto legdir — the same
        # bare-filename guard verify_asset applies to its own `asset_name` argument,
        # invoked here proactively rather than after a path has already been built
        # from untrusted input.
        pc._validate_asset_name(artifact["name"])
        canonical_path = legdir / artifact["name"]
        if not canonical_path.is_file():
            raise PublishNightlyError(
                f"missing canonical asset for {key!r}: {canonical_path}"
            )
        canonical_asset = pc.verify_asset(
            canonical_path,
            artifact["name"],
            intake=intake,
            expected_sha256=artifact["sha256"],
            work_dir=work_dir / f"leg-{index}-canonical",
        )
        record = pc._canonical_record(canonical_asset)
        if record != entry["record"]:
            raise PublishNightlyError(
                f"{key} canonical asset record does not match the handoff build record"
            )
        if record["canonical_package_version"] != validated.pkg_version:
            raise PublishNightlyError(
                f"{key} canonical asset version {record['canonical_package_version']!r} "
                f"does not match handoff pkg_version {validated.pkg_version!r}"
            )
        if record["source_sha"] != validated.source_sha:
            raise PublishNightlyError(
                f"{key} canonical asset source_sha does not match handoff source_sha"
            )
        if record["freebsd_ports_sha"] != validated.ports_sha:
            raise PublishNightlyError(
                f"{key} canonical asset freebsd_ports_sha does not match handoff ports_sha"
            )
        if record["matrix_row"] != matrix_row:
            raise PublishNightlyError(
                f"{key} canonical asset matrix_row does not match this build entry's matrix_row"
            )

        dependencies: list[pc.VerifiedAsset] = []
        dep_artifacts = entry["dep_artifacts"]
        assert isinstance(dep_artifacts, list)
        for dep_index, dep in enumerate(dep_artifacts):
            pc._validate_asset_name(dep["name"])
            dep_path = legdir / dep["name"]
            if not dep_path.is_file():
                raise PublishNightlyError(
                    f"missing dependency asset {dep['name']!r} for {key}: {dep_path}"
                )
            dependencies.append(
                pc.verify_asset(
                    dep_path,
                    dep["name"],
                    intake=intake,
                    expected_sha256=dep["sha256"],
                    work_dir=work_dir / f"leg-{index}-dep-{dep_index}",
                )
            )

        legs.append(
            _Leg(
                key=key,
                matrix_row=matrix_row,
                canonical=canonical_asset,
                dependencies=tuple(dependencies),
            )
        )
    return legs


# --------------------------------------------------------------------------- #
# Route targeting — fan each leg out to every build-role ROUTE row matching its
# exact runtime tuple. Route-only rows are never targeted (no frozen Nightly
# assets).
# --------------------------------------------------------------------------- #


def _route_targets(
    route_matrix_rows: Sequence[Mapping[str, object]],
    legs: Sequence[_Leg],
) -> dict[str, pr._Target]:
    brp = catalogue_engine
    build_rows, _route_only_rows = pc._normalize_route_matrix(route_matrix_rows)

    targets: dict[str, pr._Target] = {}
    used_keys: set[_BuildKey] = set()
    for row in build_rows.values():
        major, php_version, _py_flavor = key = _build_key(row)
        matches = [leg for leg in legs if leg.key == key]
        if not matches:
            raise PublishNightlyError(
                f"ROUTE build row {row['variant']}/{row['pfsense_version']} "
                f"(FreeBSD {major} php{php_version}) has no built asset"
            )
        if len(matches) > 1:
            raise PublishNightlyError(
                f"ROUTE build row {row['variant']}/{row['pfsense_version']} "
                f"(runtime tuple {key!r}) matches more than one built asset — "
                "forged handoff"
            )
        leg = matches[0]
        varver = brp.catalog_name_from_version(row["pfsense_version"], row["variant"])
        if varver in targets:
            raise PublishNightlyError(
                f"two ROUTE build rows resolve to the same varver {varver!r}"
            )

        # Canonical fan-out is per exact runtime tuple; extra_pkgs deps attach
        # only to ROUTE rows that declare their origin (issue #2383).
        dependencies: list[pc.VerifiedAsset] = []
        for dep in leg.dependencies:
            if not brp._pkg_matches_abi(dep.manifest, f"FreeBSD:{major}:*"):
                raise PublishNightlyError(
                    f"{dep.declared_name!r}: dependency ABI does not match FreeBSD major {major}"
                )
            if not pr._row_declares_dep(row, dep):
                continue
            dependencies.append(dep)

        targets[varver] = pr._Target(
            row=row, canonical=leg.canonical, dependencies=dependencies
        )
        used_keys.add(key)

    unused = {leg.key for leg in legs} - used_keys
    if unused:
        raise PublishNightlyError(
            f"canonical asset(s) for runtime tuple(s) {sorted(unused)!r} serve no ROUTE build row"
        )
    return targets


# --------------------------------------------------------------------------- #
# Stale-version tree check — BEFORE any write, for every target.
# --------------------------------------------------------------------------- #


def _reject_stale(site_root: Path, varver: str, incoming_version: str) -> None:
    catalogue_dir = site_root / _CHANNEL / varver
    if not catalogue_dir.is_dir():
        return  # first publish for this varver — nothing to be stale against
    brp = catalogue_engine
    incoming_name = f"{pfb_pkg.CANONICAL_EMITTED_IDENTITY}-{incoming_version}.pkg"
    present = False
    newest_key = None
    for path in sorted(catalogue_dir.glob("*.pkg")):
        if not path.is_file() or path.name in brp._CATALOG_PKG_FILES:
            continue
        manifest = pfb_pkg.read_compact_manifest(path)
        if manifest.get("name") != pfb_pkg.CANONICAL_EMITTED_IDENTITY:
            continue
        if path.name == incoming_name:
            present = True
        version = manifest.get("version")
        if not isinstance(version, str) or not version:
            raise PublishNightlyError(
                f"corrupt published canonical package manifest (missing 'version'): {path}"
            )
        key = pfb_pkg.pkg_version_sort_key(version)
        if newest_key is None or key > newest_key:
            newest_key = key
    if (
        not present
        and newest_key is not None
        and pfb_pkg.pkg_version_sort_key(incoming_version) < newest_key
    ):
        raise StaleNightlyError(
            f"{_CHANNEL}/{varver}: incoming version {incoming_version!r} is older than the newest already-published "
            "canonical version — stale run cannot replace newer catalogue state"
        )


# --------------------------------------------------------------------------- #
# Publish — mirrors publish_release.publish()'s body over a fixed "nightly" channel.
# --------------------------------------------------------------------------- #


def publish(
    pkg_repo: str | Path,
    targets: Mapping[str, pr._Target],
    incoming_version: str,
    *,
    sign_key: Path | None = None,
) -> pr.PublishReport:
    site_root = Path(pkg_repo) / pr._SITE_SUBDIR

    for varver in sorted(targets):
        pr._require_safe_catalogue_destination(
            site_root / _CHANNEL / varver, root=site_root
        )

    for varver in sorted(targets):
        _reject_stale(site_root, varver, incoming_version)

    expected_public = pr._expected_public_member(sign_key)
    touched: list[tuple[str, str]] = []
    source_index: dict[Path, list[tuple[str, str]]] = {}
    for varver in sorted(targets):
        target = targets[varver]
        asset_map = pr._asset_map(target)
        dest_dir = site_root / _CHANNEL / varver
        # Evict before dropping, for the reason publish_release.publish spells out:
        # place-if-missing would otherwise skip the incoming dependency and then
        # unlink the undeclared leftover holding its name.
        changed = pr._evict_undeclared_deps(dest_dir, row=target.row)
        if pr._drop_assets(dest_dir, asset_map):
            changed = True
        if not changed and not pr._catalogue_descriptor_complete(
            dest_dir, root=site_root
        ):
            changed = True
        if (
            not changed
            and expected_public is not None
            and not pr._catalogue_carries_key(dest_dir, expected_public)
        ):
            changed = True
        # issue #2468: only the canonical asset feeds the fan-out identity index —
        # see publish_release.publish's own comment on this same exclusion.
        source_index.setdefault(target.canonical.work_path.resolve(), []).append(
            (_CHANNEL, varver)
        )
        if changed:
            ca.prune_retained(site_root, _CHANNEL, varver)
            ca.regenerate_catalogue(site_root, _CHANNEL, varver, sign_key=sign_key)
            touched.append((_CHANNEL, varver))

    if source_index:
        ca.verify_multi_destination_identity(site_root, source_index)

    return pr.PublishReport(touched=tuple(touched))


# --------------------------------------------------------------------------- #
# run() — handoff -> verify -> route -> publish. main() is a thin CLI wrapper.
# --------------------------------------------------------------------------- #


def run(
    *,
    handoff_path: str | Path,
    results_dir: str | Path,
    pkg_repo: str | Path,
    source_run_id: str,
    sign_key: Path | None = None,
) -> pr.PublishReport:

    handoff_path = Path(handoff_path)
    try:
        raw = handoff_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PublishNightlyError(f"{handoff_path} is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise PublishNightlyError(f"cannot read {handoff_path}: {exc}") from exc
    try:
        handoff_raw = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PublishNightlyError(f"{handoff_path} is not valid JSON: {exc}") from exc

    validated = _validate_handoff(handoff_raw, source_run_id=source_run_id)
    intake = pc.parse_intake(
        pc.EXPECTED_SOURCE_REPOSITORY, "", "", '["nightly"]', source_run_id
    )

    with tempfile.TemporaryDirectory(prefix="publish-nightly-verify-") as work_dir:
        legs = _verify_builds(intake, validated, Path(results_dir), Path(work_dir))
        targets = _route_targets(validated.route_matrix, legs)
        # publish() reads VerifiedAsset.work_path, which lives under work_dir — must
        # run to completion BEFORE this context manager tears work_dir down.
        return publish(pkg_repo, targets, validated.pkg_version, sign_key=sign_key)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a Nightly handoff's .pkg assets and publish them into the "
            "pfBlockerNG/pkg nightly catalogue tree (never runs git)."
        )
    )
    parser.add_argument(
        "--handoff", required=True, help="the immutable Nightly handoff JSON"
    )
    parser.add_argument(
        "--results-dir",
        required=True,
        help="directory of downloaded nightly-result-<major>/ legs",
    )
    parser.add_argument(
        "--pkg-repo",
        required=True,
        help="the checked-out pfBlockerNG/pkg working tree (site is <pkg-repo>/docs)",
    )
    parser.add_argument(
        "--source-run-id", required=True, help="must equal the handoff's own run_id"
    )
    parser.add_argument(
        "--sign-key",
        default="",
        dest="sign_key",
        help=(
            "ECDSA private key (PEM) to sign the regenerated catalogue with (issue #2675). "
            "Omitted = unsigned, today's behaviour."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        report = run(
            handoff_path=args.handoff,
            results_dir=args.results_dir,
            pkg_repo=args.pkg_repo,
            source_run_id=args.source_run_id,
            sign_key=Path(args.sign_key) if args.sign_key else None,
        )
    except (
        PublishNightlyError,
        pc.PublishError,
        ca.CatalogueAssemblyError,
        pr.PublishReleaseError,
        nc.ContractError,
        pfb_pkg.PkgError,
        catalogue_engine.BuildRepoError,
    ) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    for line in report.describe():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
