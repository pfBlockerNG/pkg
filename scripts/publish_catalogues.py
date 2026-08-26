"""Four-channel staged publisher — intake parsing + per-asset/run verification core.

Scope: parse raw workflow intake, verify downloaded .pkg assets against their
embedded provenance, and apply whole-run cross-asset checks before publication.
The repository-local engine is `pfb_pkg.py` plus `catalogue_engine.py`; no code
is loaded from the source repository.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Literal

import catalogue_engine as catalogue_engine_module
import pfb_pkg as pfb_pkg_module

# --------------------------------------------------------------------------- #
# Errors — one dedicated type per stage, all rooted at PublishError.
# --------------------------------------------------------------------------- #


class PublishError(Exception):
    """Base for every error this module raises."""


class IntakeError(PublishError):
    """Workflow intake is malformed."""


class AssetVerificationError(PublishError):
    """A downloaded package failed verification."""


class RunVerificationError(PublishError):
    """Cross-asset or ROUTE validation failed."""


@dataclass(frozen=True)
class Engine:
    pfb_pkg: ModuleType
    catalogue_engine: ModuleType


_LOCAL_ENGINE = Engine(
    pfb_pkg=pfb_pkg_module,
    catalogue_engine=catalogue_engine_module,
)


def load_engine() -> Engine:
    """Return the repository-local publisher engine."""
    return _LOCAL_ENGINE


# --------------------------------------------------------------------------- #
# Intake — the five raw release-workflow intake fields, parsed and validated.
# --------------------------------------------------------------------------- #

IntakeKind = Literal["tagged", "nightly"]

EXPECTED_SOURCE_REPOSITORY = "pfBlockerNG/pfBlockerNG"

_CHANNEL_ORDER: tuple[str, ...] = ("stable", "testing", "edge")
_NIGHTLY_DESTINATIONS: tuple[str, ...] = ("nightly",)
_MAX_DESTINATIONS_TEXT = 256
_MAX_DESTINATIONS_ELEMENTS = len(_CHANNEL_ORDER) + 1

# Closed tuples accepted from the source dispatch contract. Current releases use
# edge-only, testing+edge, or stable+testing+edge; two historical tuples remain
# for immutable pre-migration republish compatibility.
# Stable-only and stable+edge are never legal; every other unlisted tuple fails closed.
_VALID_TAGGED_DESTINATIONS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("edge",),
        ("testing",),
        ("testing", "edge"),
        ("stable", "testing"),
        ("stable", "testing", "edge"),
    }
)

_RELEASE_ID_RE = re.compile(r"^[1-9][0-9]*$")
_RELEASE_TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:\.[abr][1-9][0-9]*)?$")
_RUN_ID_RE = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")


@dataclass(frozen=True)
class Intake:
    kind: IntakeKind
    source_repository: str
    release_id: str
    release_tag: str
    destinations: tuple[str, ...]
    source_run_id: str

    @property
    def primary_channel(self) -> str:
        """The channel the verified asset must have been built for."""
        return self.destinations[0]


def _parse_destinations(raw: str) -> tuple[str, ...]:
    if not isinstance(raw, str) or len(raw) > _MAX_DESTINATIONS_TEXT:
        raise IntakeError("destinations must be a compact JSON array string")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise IntakeError(f"destinations is not valid JSON: {exc}") from None
    if not isinstance(parsed, list) or not parsed:
        raise IntakeError("destinations must be a non-empty JSON array")
    if len(parsed) > _MAX_DESTINATIONS_ELEMENTS:
        raise IntakeError("destinations array is too large")
    if any(not isinstance(item, str) for item in parsed):
        raise IntakeError("destinations elements must be strings")
    values = tuple(parsed)
    if values == _NIGHTLY_DESTINATIONS:
        return values
    if "nightly" in values:
        raise IntakeError("nightly must not be combined with any other destination")
    if values not in _VALID_TAGGED_DESTINATIONS:
        raise IntakeError(
            f"destinations must be one of {sorted(_VALID_TAGGED_DESTINATIONS)!r}; got {values!r}"
        )
    return values


def parse_intake(
    source_repository: str,
    release_id: str,
    release_tag: str,
    destinations: str,
    source_run_id: str,
) -> Intake:
    """Parse the five raw release-workflow intake fields. Fails closed on anything else.

    Kind is derived, never guessed: ``destinations == ["nightly"]`` is the only nightly
    shape; every other well-formed destinations value is tagged.
    """
    raw_inputs = {
        "source_repository": source_repository,
        "release_id": release_id,
        "release_tag": release_tag,
        "destinations": destinations,
        "source_run_id": source_run_id,
    }
    for name, value in raw_inputs.items():
        if not isinstance(value, str):
            raise IntakeError(f"{name} must be a string")

    if source_repository != EXPECTED_SOURCE_REPOSITORY:
        raise IntakeError(
            f"source_repository must be {EXPECTED_SOURCE_REPOSITORY!r}, got {source_repository!r}"
        )

    if not _RUN_ID_RE.fullmatch(source_run_id):
        raise IntakeError(
            f"source_run_id must be '<digits>:<digits>', got {source_run_id!r}"
        )

    destinations_tuple = _parse_destinations(destinations)

    if destinations_tuple == _NIGHTLY_DESTINATIONS:
        kind: IntakeKind = "nightly"
        if release_id != "" or release_tag != "":
            raise IntakeError(
                "nightly intake requires empty release_id and release_tag"
            )
    else:
        kind = "tagged"
        if release_id == "":
            raise IntakeError("tagged intake requires a non-empty release_id")
        if release_tag == "":
            raise IntakeError("tagged intake requires a non-empty release_tag")
        if not _RELEASE_ID_RE.fullmatch(release_id):
            raise IntakeError(
                f"release_id must be a positive decimal integer string, got {release_id!r}"
            )
        if not _RELEASE_TAG_RE.fullmatch(release_tag):
            raise IntakeError(
                f"release_tag must match vX.Y.Z[.aN|.bN|.rN], got {release_tag!r}"
            )

    return Intake(
        kind=kind,
        source_repository=source_repository,
        release_id=release_id,
        release_tag=release_tag,
        destinations=destinations_tuple,
        source_run_id=source_run_id,
    )


# --------------------------------------------------------------------------- #
# Per-asset verification.
# --------------------------------------------------------------------------- #

AssetClass = Literal["canonical", "dependency"]

_UNSAFE_NAME_CHARS = ("\x00", "\n", "\r")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class VerifiedAsset:
    asset_class: AssetClass
    declared_name: str
    canonical_name: str
    work_path: Path
    sha256: str
    manifest: Mapping[str, object]
    record: Mapping[str, object] | None = None
    # (variant, pfsense_version) from a tagged dependency asset's own suffix (issue
    # #2468 per-suffix routing target) — set only there; None for canonical (uses
    # record.matrix_row) and Nightly dependency (no suffix) assets.
    release_suffix: tuple[str, str] | None = None


def _validate_asset_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise AssetVerificationError("asset name must be a non-empty string")
    if any(char in name for char in _UNSAFE_NAME_CHARS):
        raise AssetVerificationError(
            f"asset name contains control characters: {name!r}"
        )
    if "/" in name or "\\" in name or ".." in name:
        raise AssetVerificationError(f"asset name must be a bare filename: {name!r}")
    if not name.endswith(".pkg"):
        raise AssetVerificationError(f"asset name must end with .pkg: {name!r}")


def verify_asset(
    engine: Engine,
    asset_path: str | Path,
    asset_name: str,
    *,
    intake: Intake,
    expected_sha256: str,
    work_dir: str | Path,
) -> VerifiedAsset:
    """Verify one downloaded .pkg against its provenance and the run's intake.

    Produces a canonical working copy (stripping a tagged Release asset's
    ``-<Variant>-<pfsense_version>`` suffix), pulls the provenance record via the
    engine's ``_canonical_build_record``, runs ``validate_project_pkg``, and applies
    the publisher-level cross-checks against ``intake``. A canonical package with no
    provenance annotation is rejected — the legacy unannotated path is gone.
    """
    _validate_asset_name(asset_name)
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(
        expected_sha256
    ):
        raise AssetVerificationError(
            f"expected_sha256 must be 64 lowercase hex characters, got {expected_sha256!r}"
        )

    asset_path = Path(asset_path)
    try:
        data = asset_path.read_bytes()
    except OSError as exc:
        raise AssetVerificationError(f"{asset_name}: cannot read asset: {exc}") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise AssetVerificationError(
            f"{asset_name}: sha256 mismatch: expected {expected_sha256}, got {digest}"
        )

    pfb_pkg = engine.pfb_pkg
    try:
        manifest = pfb_pkg.read_compact_manifest(asset_path)
    except pfb_pkg.PkgError as exc:
        raise AssetVerificationError(f"{asset_name}: {exc}") from exc

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    if manifest.get("name") == pfb_pkg.CANONICAL_EMITTED_IDENTITY:
        return _verify_canonical_asset(
            engine,
            asset_path,
            asset_name,
            manifest,
            intake=intake,
            work_dir=work_dir,
            digest=digest,
        )
    return _verify_dependency_asset(
        engine,
        asset_path,
        asset_name,
        manifest,
        intake=intake,
        work_dir=work_dir,
        digest=digest,
    )


def _verify_canonical_asset(
    engine: Engine,
    asset_path: Path,
    asset_name: str,
    manifest: Mapping[str, object],
    *,
    intake: Intake,
    work_dir: Path,
    digest: str,
) -> VerifiedAsset:
    pfb_pkg = engine.pfb_pkg
    brp = engine.catalogue_engine
    try:
        record = brp._canonical_build_record(asset_path, manifest)
    except brp.BuildRepoError as exc:
        raise AssetVerificationError(f"{asset_name}: {exc}") from exc
    if record is None:
        raise AssetVerificationError(
            f"{asset_name}: canonical package carries no {pfb_pkg.PFB_BUILD_RECORD_KEY} provenance "
            "annotation; the unannotated legacy path is not accepted by this publisher"
        )

    canonical_name = f"{pfb_pkg.CANONICAL_EMITTED_IDENTITY}-{record['canonical_package_version']}.pkg"
    row = record["matrix_row"]
    if intake.kind == "tagged":
        expected_declared = (
            f"{canonical_name[:-4]}-{row['variant']}-{row['pfsense_version']}.pkg"
        )
    else:
        expected_declared = canonical_name
    if asset_name != expected_declared:
        raise AssetVerificationError(
            f"{asset_name}: declared name does not match the package's canonical identity; "
            f"expected {expected_declared!r}"
        )

    if intake.kind == "tagged" and record.get("source_tag") != intake.release_tag:
        raise AssetVerificationError(
            f"{asset_name}: record source_tag {record.get('source_tag')!r} "
            f"does not match release_tag {intake.release_tag!r}"
        )
    if record["channel"] != intake.primary_channel:
        raise AssetVerificationError(
            f"{asset_name}: record channel {record['channel']!r} cannot serve "
            f"destinations {intake.destinations!r} (primary is {intake.primary_channel!r})"
        )

    work_path = work_dir / canonical_name
    shutil.copyfile(asset_path, work_path)
    try:
        pfb_pkg.validate_project_pkg(work_path, record)
    except pfb_pkg.PkgError as exc:
        raise AssetVerificationError(f"{asset_name}: {exc}") from exc

    return VerifiedAsset(
        asset_class="canonical",
        declared_name=asset_name,
        canonical_name=canonical_name,
        work_path=work_path,
        sha256=digest,
        manifest=manifest,
        record=record,
    )


def _verify_dependency_asset(
    engine: Engine,
    asset_path: Path,
    asset_name: str,
    manifest: Mapping[str, object],
    *,
    intake: Intake,
    work_dir: Path,
    digest: str,
) -> VerifiedAsset:
    """Dependency .pkg (e.g. py311-charset-normalizer) — no provenance annotation, so
    only its own manifest identity is checked. release.yml renames a tagged run's
    dependency assets with the SAME ``-<Variant>-<pfsense_version>`` suffix it applies
    to the canonical package (the "Build the .pkg via build-leg.sh" step's
    ``RENAMED_DEP``); a nightly dependency artifact carries no such suffix. Which exact
    varver a tagged dep lands in is decided later, by that suffix, in
    ``publish_release._build_targets`` (issue #2468) — axis 9 in verify_run only checks
    the ABI against SOME ROUTE row. Here the suffix only has to be well-formed; it is
    returned on the VerifiedAsset (``release_suffix``) for the publisher to route on.
    """
    brp = engine.catalogue_engine
    pfb_pkg = engine.pfb_pkg
    name = manifest.get("name")
    version = manifest.get("version")
    segment_re = brp._PKG_SEGMENT_RE
    if not isinstance(name, str) or not segment_re.fullmatch(name):
        raise AssetVerificationError(
            f"{asset_name}: dependency manifest name is missing or unsafe"
        )
    if not isinstance(version, str) or not segment_re.fullmatch(version):
        raise AssetVerificationError(
            f"{asset_name}: dependency manifest version is missing or unsafe"
        )

    canonical_name = f"{name}-{version}.pkg"
    release_suffix: tuple[str, str] | None = None
    if intake.kind == "tagged":
        prefix = f"{name}-{version}-"
        if not asset_name.startswith(prefix):
            raise AssetVerificationError(
                f"{asset_name}: declared name does not match the package's manifest identity; "
                f"expected a name starting with {prefix!r} and ending with '-<Variant>-<pfsense_version>.pkg'"
            )
        suffix = asset_name[len(prefix) : -len(".pkg")]
        variant, sep, pfsense_version = suffix.rpartition("-")
        if (
            not sep
            or not pfb_pkg._VARIANT.fullmatch(variant)
            or not pfb_pkg._PF_VERSION.fullmatch(pfsense_version)
        ):
            raise AssetVerificationError(
                f"{asset_name}: declared name does not carry a valid -<Variant>-<pfsense_version> Release-asset suffix"
            )
        release_suffix = (variant, pfsense_version)
    elif asset_name != canonical_name:
        raise AssetVerificationError(
            f"{asset_name}: declared name does not match the package's manifest identity; expected {canonical_name!r}"
        )

    work_path = work_dir / canonical_name
    shutil.copyfile(asset_path, work_path)
    return VerifiedAsset(
        asset_class="dependency",
        declared_name=asset_name,
        canonical_name=canonical_name,
        work_path=work_path,
        sha256=digest,
        manifest=manifest,
        record=None,
        release_suffix=release_suffix,
    )


# --------------------------------------------------------------------------- #
# Whole-run verification — axes 1-13. validate_build_record/validate_project_pkg
# already cover channel validity, matrix-row shape, ABI<->freebsd_major,
# identity<->channel, tag<->version<->release_line<->classification, route
# composition, and build_input_digest; this adds only the cross-input checks
# those do not.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunResult:
    intake: Intake
    canonical_assets: tuple[VerifiedAsset, ...]
    dependency_assets: tuple[VerifiedAsset, ...]


def _normalize_route_matrix(
    engine: Engine, route_matrix_rows: Sequence[Mapping[str, object]]
) -> tuple[
    dict[tuple[object, object], dict[str, object]],
    dict[tuple[object, object], dict[str, object]],
]:
    """Validate+partition the pinned ROUTE matrix into build-role / route-only rows.

    Mirrors Nightly handoff contract's own ROUTE-row normalization: "role" is
    popped before validate_build_matrix_row only for "route-only" (that function
    rejects any role other than absent/"build"), then reattached.
    """
    pfb_pkg = engine.pfb_pkg
    build_rows: dict[tuple[object, object], dict[str, object]] = {}
    route_only_rows: dict[tuple[object, object], dict[str, object]] = {}
    for raw_row in route_matrix_rows:
        if not isinstance(raw_row, Mapping):
            raise RunVerificationError("ROUTE matrix row must be an object")
        row = dict(raw_row)
        role = row.get("role")
        ci = row.pop("ci", None)
        if ci is not None and type(ci) is not bool:
            raise RunVerificationError("ROUTE matrix ci must be boolean")
        if role not in (None, "build", "route-only"):
            raise RunVerificationError(f"ROUTE matrix row has invalid role {role!r}")
        if role == "route-only":
            del row["role"]
        try:
            normalized = pfb_pkg.validate_build_matrix_row(row)
        except pfb_pkg.PkgError as exc:
            raise RunVerificationError(f"invalid ROUTE matrix row: {exc}") from exc
        key = (normalized["variant"], normalized["pfsense_version"])
        if key in build_rows or key in route_only_rows:
            raise RunVerificationError(
                f"ROUTE matrix has duplicate version identity {key!r}"
            )
        if role == "route-only":
            route_only_rows[key] = normalized
        else:
            build_rows[key] = normalized
    if not build_rows and not route_only_rows:
        raise RunVerificationError("ROUTE matrix must not be empty")
    return build_rows, route_only_rows


def _canonical_record(asset: VerifiedAsset) -> Mapping[str, object]:
    """Narrow ``VerifiedAsset.record`` (``Mapping | None``) for a canonical asset.

    ``asset_class == "canonical"`` and ``record is not None`` travel together by
    construction in verify_asset, but that pairing is a convention nothing at the type
    level enforces — a future caller (S2-S4) handing verify_run a hand-built
    "canonical" VerifiedAsset with no record would otherwise crash on a bare
    ``TypeError: 'NoneType' object is not subscriptable`` deep inside a set/dict
    comprehension. Routing every canonical-record read through this accessor turns
    that into one explicit, named RunVerificationError instead.
    """
    if asset.asset_class != "canonical" or asset.record is None:
        raise RunVerificationError(
            f"{asset.declared_name}: expected a canonical asset with a record"
        )
    return asset.record


def _require_single_value(assets: Sequence[VerifiedAsset], field_name: str) -> object:
    values = {_canonical_record(asset)[field_name] for asset in assets}
    if len(values) != 1:
        raise RunVerificationError(
            f"canonical assets disagree on {field_name}: {sorted(map(str, values))!r}"
        )
    return next(iter(values))


def verify_run(
    engine: Engine,
    intake: Intake,
    assets: Sequence[VerifiedAsset],
    route_matrix_rows: Sequence[Mapping[str, object]],
) -> RunResult:
    """Whole-run checks over already-``verify_asset``-verified assets (design axes 1-13).

    Raises RunVerificationError on the first violation; never returns a partial result.
    """
    if not assets:
        raise RunVerificationError("run has no verified assets")

    canonical = tuple(asset for asset in assets if asset.asset_class == "canonical")
    dependency = tuple(asset for asset in assets if asset.asset_class == "dependency")
    if not canonical:
        raise RunVerificationError("run has no canonical project package")

    build_rows, route_only_rows = _normalize_route_matrix(engine, route_matrix_rows)

    # Axes 4/6: release_line and canonical_package_version identical across the run.
    _require_single_value(canonical, "release_line")
    _require_single_value(canonical, "canonical_package_version")
    # Axis 5 (source_sha half; source_tag==release_tag is per-asset in verify_asset).
    _require_single_value(canonical, "source_sha")

    # Axis 7: every asset's matrix_row is present in the pinned ROUTE matrix (as a
    # build-role row), matched exactly once.
    matched_keys: set[tuple[object, object]] = set()
    for asset in canonical:
        row = _canonical_record(asset)["matrix_row"]
        key = (row["variant"], row["pfsense_version"])
        if key not in build_rows:
            raise RunVerificationError(
                f"{asset.declared_name}: matrix_row {key!r} is not a build-role ROUTE row"
            )
        if build_rows[key] != row:
            raise RunVerificationError(
                f"{asset.declared_name}: matrix_row does not exactly match the pinned ROUTE row {key!r}"
            )
        if key in matched_keys:
            raise RunVerificationError(f"two assets cover the same ROUTE row {key!r}")
        matched_keys.add(key)
    missing = set(build_rows) - matched_keys
    if missing:
        raise RunVerificationError(
            f"ROUTE build row(s) with no asset: {sorted(map(str, missing))!r}"
        )

    # Axis 9: every dependency asset's ABI matches some ROUTE row (build or route-only).
    route_abis = {
        f"FreeBSD:{row['freebsd_major']}:*"
        for row in {**build_rows, **route_only_rows}.values()
    }
    brp = engine.catalogue_engine
    for asset in dependency:
        if not any(brp._pkg_matches_abi(asset.manifest, abi) for abi in route_abis):
            raise RunVerificationError(
                f"{asset.declared_name}: dependency ABI matches no ROUTE row"
            )
    # Axis 12: source_run_id shape already enforced by parse_intake; recorded via intake.
    # Axis 13: destination legality (record.channel == primary) already enforced per
    # asset in verify_asset for every canonical asset.

    return RunResult(
        intake=intake,
        canonical_assets=canonical,
        dependency_assets=dependency,
    )
