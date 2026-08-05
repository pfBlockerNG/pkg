"""Durable catalogue ledger — the publisher's committed state of record.

Decides, for one verified run (a ``publish_catalogues.RunResult`` for the tagged
fan-out path, or a Nightly handoff document for the Nightly path), whether the run is
a deterministic no-op, an accepted advance, or a fail-closed rejection — and folds an
accepted advance into a new ledger state under bounded per-(channel, varver)
retention.

No tree assembly, no git commands, no network. Those are issue #2146 steps S3
(assemble the working tree from the ledger) and S4 (commit + fast-forward push to
``main``, race handling). This module only decides and folds an in-memory state
snapshot; the caller owns loading it from ``main`` and persisting the result.

Concurrency seam. There is no lock in this module. Real serialization is S4's
fast-forward push to ``main`` plus the workflow concurrency group. Every function
that reads a ledger returns the generation it read (``Decision.generation_read``),
so a caller whose push is rejected (another run advanced ``main`` first) can
``load_state`` the new ``main``, and re-``decide``/``decide_nightly`` against it,
rather than force-pushing over the winner. ``apply`` additionally re-checks that the
``state`` it is given still has the generation the ``Decision`` was computed
against, so a caller cannot accidentally fold a decision into the wrong snapshot.

The Nightly path reuses ``nightly_provenance.complete`` verbatim for every
acceptance rule (no-op replay, stale-generation rejection, artifact-bytes collision,
version collision for different inputs, missing-artifacts rejection) — see
``decide_nightly``. One consequence of reuse is worth stating plainly: the
``Candidate`` passed to ``complete`` carries *this ledger's own* current Nightly
generation (never the source Nightly run's own generation, which is a different
counter describing the *source* repo's Nightly-workflow state, not this publisher's
ledger). Because of that substitution, ``complete``'s own stale-generation check
cannot detect a second, concurrent publisher run — it only detects that the
*ledger* snapshot this call was decided against is not the ledger snapshot
``complete`` is being asked to fold into. Catching an actually-concurrent publisher
run is, again, entirely S4's job (fast-forward push + concurrency group).

Retention. The keep-count for one (channel, varver) is resolved by
``retention_keep_count`` — a seam, not a policy: today every (channel, varver) gets
``DEFAULT_RETENTION_KEEP``. A later ticket that pins EOL'd varvers (ones we have also
EOL'd) to keep=1 only has to teach that one function a lookup; no ledger reshaping.

Dependencies (#2146 F1). Every canonical ledger entry additionally carries the
dependency assets it shipped with (name/version/sha256/path) — the exact set S1
verified alongside it in the same run. Dependencies have no independent retention
count of their own: retention keeps the UNION of dependencies referenced by the
retained canonical set for one (channel, varver), computed fresh from whichever
canonical entries retention kept — never a separately pruned/aged dependency
history. ``apply`` exposes that union per (channel, varver) directly in its
return value (``ApplyResult.dependency_unions``) so a caller can hand it to
``catalogue_assembly.CatalogueTarget.dependencies`` without re-deriving it from
the ledger's entries.

stdlib-only, Python 3.11. The engine (pfb_pkg.py + nightly_provenance.py, which in
turn pulls in release_version.py) is loaded from a pfBlockerNG source-repo checkout
named by ``PFB_SRC`` (env) or an explicit argument — never vendored, never
reimplemented. ``load_engine`` reuses ``publish_catalogues.load_engine`` for the
``pfb_pkg`` half (identical sys.path setup), and additionally imports
``nightly_provenance`` from the same checkout.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Literal

try:
    from scripts.publish_catalogues import Engine as _PublishEngine
    from scripts.publish_catalogues import EngineError as _PublishEngineError
    from scripts.publish_catalogues import RunResult, VerifiedAsset
    from scripts.publish_catalogues import load_engine as _load_publish_engine
except ImportError:  # script directory is also a direct import root
    from publish_catalogues import Engine as _PublishEngine
    from publish_catalogues import EngineError as _PublishEngineError
    from publish_catalogues import RunResult, VerifiedAsset
    from publish_catalogues import load_engine as _load_publish_engine

__all__ = [
    "DEFAULT_RETENTION_KEEP",
    "ApplyResult",
    "CatalogueStateError",
    "Decision",
    "Engine",
    "EngineError",
    "apply",
    "decide",
    "decide_nightly",
    "empty_catalogue_state",
    "load_engine",
    "load_state",
    "retention_keep_count",
    "save_state",
    "validate_state",
]


class CatalogueStateError(ValueError):
    """A ledger document, or a run's reconciliation against it, is invalid."""


class EngineError(CatalogueStateError):
    """The pfBlockerNG source-repo engine could not be loaded."""


# --------------------------------------------------------------------------- #
# Engine loading — pfb_pkg reused verbatim via publish_catalogues.load_engine
# (identical sys.path setup); nightly_provenance is the one addition.
# --------------------------------------------------------------------------- #

_REQUIRED_NIGHTLY_PROVENANCE_ATTRS = (
    "ProvenanceError",
    "Candidate",
    "empty_state",
    "validate_state",
    "complete",
)


@dataclass(frozen=True)
class Engine:
    """The loaded engine modules, plus the checkout root they came from."""

    src_root: Path
    pfb_pkg: ModuleType
    nightly_provenance: ModuleType
    build_repo_portable: ModuleType


def load_engine(src_root: str | Path | None = None) -> Engine:
    """Load the source-repo engine from ``src_root`` or the ``PFB_SRC`` env var.

    Never falls back to a guessed path: a missing/incomplete engine is always a
    hard ``EngineError``, quoting exactly what is absent.
    """
    try:
        publish_engine: _PublishEngine = _load_publish_engine(src_root)
    except _PublishEngineError as exc:
        raise EngineError(str(exc)) from exc

    scripts_dir = str(publish_engine.src_root / "scripts")
    import importlib
    import sys

    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        nightly_provenance = importlib.import_module("nightly_provenance")
    except ImportError as exc:
        raise EngineError(
            f"cannot import nightly_provenance from {scripts_dir}: {exc}"
        ) from exc
    missing = [
        name
        for name in _REQUIRED_NIGHTLY_PROVENANCE_ATTRS
        if not hasattr(nightly_provenance, name)
    ]
    if missing:
        raise EngineError(
            f"nightly_provenance engine module is missing required symbol(s): {', '.join(missing)}"
        )
    return Engine(
        src_root=publish_engine.src_root,
        pfb_pkg=publish_engine.pfb_pkg,
        nightly_provenance=nightly_provenance,
        build_repo_portable=publish_engine.build_repo_portable,
    )


def _engine(engine: Engine | None) -> Engine:
    return engine if engine is not None else load_engine()


# --------------------------------------------------------------------------- #
# Ledger schema and strict validation.
# --------------------------------------------------------------------------- #

_SCHEMA = 1
# The four channels pfb_pkg.validate_build_record recognizes (pfb_pkg.py:247). The
# ledger's "channels" object always has exactly these four keys: "stable",
# "testing", and "edge" each map varver -> [asset entry, ...] (this module's own
# generation-tracked history); "nightly" instead holds a nightly_provenance state
# object verbatim (its OWN schema/generation/records, validated by
# nightly_provenance.validate_state, not by this module's asset-entry rules).
_ALL_CHANNELS: tuple[str, ...] = ("stable", "testing", "edge", "nightly")
_TAGGED_CHANNELS: tuple[str, ...] = ("stable", "testing", "edge")
DEFAULT_RETENTION_KEEP = 5

_STATE_FIELDS = {"schema", "generation", "updated_by", "channels"}
_UPDATED_BY_FIELDS = {"source_repository", "source_run_id"}
_ASSET_FIELDS = {
    "name",
    "version",
    "sha256",
    "path",
    "record",
    "dependencies",
    "release_id",
    "release_tag",
    "source_run_id",
}
_DEPENDENCY_FIELDS = {"name", "version", "sha256", "path"}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Mirrors build-repo-portable.py's _CATALOG_NAME_SEGMENT_RE (single varver segment):
# lowercase alphanumeric, '.'/'-' only in the middle, never at either end.
_VARVER_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_ASSET_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.pkg$")
# Mirrors publish_catalogues.py's Intake field shapes (same wire contract).
_RELEASE_ID_RE = re.compile(r"^[1-9][0-9]*$")
_RELEASE_TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:\.[abr][1-9][0-9]*)?$")
_RUN_ID_RE = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")


def _exact_fields(value: Mapping[str, object], expected: set[str], label: str) -> None:
    keys = set(value)
    if keys != expected:
        raise CatalogueStateError(
            f"{label} exact fields required (missing={sorted(expected - keys)}, unknown={sorted(keys - expected)})"
        )


def empty_catalogue_state(*, engine: Engine | None = None) -> dict[str, object]:
    """Return a freshly-initialized, valid, empty ledger."""
    eng = _engine(engine)
    return {
        "schema": _SCHEMA,
        "generation": 0,
        "updated_by": {"source_repository": "", "source_run_id": ""},
        "channels": {
            "stable": {},
            "testing": {},
            "edge": {},
            "nightly": eng.nightly_provenance.empty_state(),
        },
    }


def load_state(path: str | Path, *, engine: Engine | None = None) -> dict[str, object]:
    """Load and strictly validate one ledger document from ``path``."""
    eng = _engine(engine)
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise CatalogueStateError(f"cannot read ledger {p}: {exc}") from exc
    try:
        value = json.loads(
            raw, object_pairs_hook=eng.pfb_pkg._reject_duplicate_json_keys
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CatalogueStateError(f"ledger {p} is not valid JSON: {exc}") from exc
    return validate_state(value, engine=eng)


def save_state(
    path: str | Path, state: Mapping[str, object], *, engine: Engine | None = None
) -> None:
    """Validate ``state`` and write it to ``path`` as pretty, sorted JSON."""
    eng = _engine(engine)
    normalized = validate_state(dict(state), engine=eng)
    Path(path).write_text(
        json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _validate_updated_by(value: object, *, generation: int) -> dict[str, str]:
    if not isinstance(value, dict):
        raise CatalogueStateError("ledger updated_by must be an object")
    _exact_fields(value, _UPDATED_BY_FIELDS, "updated_by")
    source_repository = value["source_repository"]
    source_run_id = value["source_run_id"]
    if not isinstance(source_repository, str) or not isinstance(source_run_id, str):
        raise CatalogueStateError("updated_by fields must be strings")
    if generation == 0:
        if source_repository or source_run_id:
            raise CatalogueStateError(
                "an empty ledger (generation 0) must have an empty updated_by"
            )
    else:
        if not source_repository or not _RUN_ID_RE.fullmatch(source_run_id):
            raise CatalogueStateError(
                "a non-empty ledger must record a valid updated_by"
            )
    return {"source_repository": source_repository, "source_run_id": source_run_id}


def _validate_asset_path(path: object, channel: str, varver: str) -> str:
    if not isinstance(path, str) or not path:
        raise CatalogueStateError("ledger entry path must be a non-empty string")
    if any(ch in path for ch in ("\x00", "\n", "\r", "\\")):
        raise CatalogueStateError(
            f"ledger entry path contains unsafe characters: {path!r}"
        )
    if path.startswith("/"):
        raise CatalogueStateError(
            f"ledger entry path must be relative, not absolute: {path!r}"
        )
    if ".." in path.split("/"):
        raise CatalogueStateError(f"ledger entry path must not contain '..': {path!r}")
    prefix = f"{channel}/{varver}/"
    if not path.startswith(prefix):
        raise CatalogueStateError(
            f"ledger entry path must be under {prefix!r}: {path!r}"
        )
    basename = path[len(prefix) :]
    if not basename or "/" in basename or not _ASSET_FILENAME_RE.fullmatch(basename):
        raise CatalogueStateError(f"ledger entry path has an unsafe filename: {path!r}")
    return path


def _validate_dependency_entry(
    channel: str, varver: str, entry: object
) -> dict[str, object]:
    """Validate one dependency-asset entry (#2146 F1): name/version/sha256/path
    only — no record, no release identity. A dependency is not independently
    versioned by this ledger; it is only ever "what this canonical build shipped
    with", tracked so retention can compute the dependency union (see ``apply``)."""
    if not isinstance(entry, dict):
        raise CatalogueStateError("ledger dependency entry must be an object")
    _exact_fields(entry, _DEPENDENCY_FIELDS, "ledger dependency entry")
    name = entry["name"]
    version = entry["version"]
    sha256 = entry["sha256"]
    if not isinstance(name, str) or not name:
        raise CatalogueStateError(
            "ledger dependency entry name must be a non-empty string"
        )
    if not isinstance(version, str) or not version:
        raise CatalogueStateError(
            "ledger dependency entry version must be a non-empty string"
        )
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise CatalogueStateError(
            "ledger dependency entry sha256 must be 64 lowercase hex characters"
        )
    path = _validate_asset_path(entry["path"], channel, varver)
    return {"name": name, "version": version, "sha256": sha256, "path": path}


def _validate_dependencies(
    channel: str, varver: str, dependencies_raw: object
) -> list[dict[str, object]]:
    if not isinstance(dependencies_raw, list):
        raise CatalogueStateError("ledger entry dependencies must be an array")
    dependencies: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for dep in dependencies_raw:
        validated_dep = _validate_dependency_entry(channel, varver, dep)
        if validated_dep["name"] in seen_names:
            raise CatalogueStateError(
                f"duplicate dependency name in ledger entry: {validated_dep['name']!r}"
            )
        seen_names.add(validated_dep["name"])
        dependencies.append(validated_dep)
    return dependencies


def _validate_asset_entry(
    engine: Engine, channel: str, varver: str, entry: object
) -> dict[str, object]:
    if not isinstance(entry, dict):
        raise CatalogueStateError("ledger entry must be an object")
    _exact_fields(entry, _ASSET_FIELDS, "ledger entry")
    name = entry["name"]
    version = entry["version"]
    sha256 = entry["sha256"]
    record = entry["record"]
    release_id = entry["release_id"]
    release_tag = entry["release_tag"]
    source_run_id = entry["source_run_id"]

    if not isinstance(name, str) or not name:
        raise CatalogueStateError("ledger entry name must be a non-empty string")
    if not isinstance(version, str) or not version:
        raise CatalogueStateError("ledger entry version must be a non-empty string")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise CatalogueStateError(
            "ledger entry sha256 must be 64 lowercase hex characters"
        )
    path = _validate_asset_path(entry["path"], channel, varver)
    if not isinstance(release_id, str) or not _RELEASE_ID_RE.fullmatch(release_id):
        raise CatalogueStateError(
            "ledger entry release_id must be a positive decimal integer string"
        )
    if not isinstance(release_tag, str) or not _RELEASE_TAG_RE.fullmatch(release_tag):
        raise CatalogueStateError("ledger entry release_tag is malformed")
    if not isinstance(source_run_id, str) or not _RUN_ID_RE.fullmatch(source_run_id):
        raise CatalogueStateError(
            "ledger entry source_run_id must be '<digits>:<digits>'"
        )
    if not isinstance(record, dict):
        raise CatalogueStateError("ledger entry record must be an object")
    try:
        validated_record = engine.pfb_pkg.validate_build_record(record)
    except engine.pfb_pkg.PkgError as exc:
        raise CatalogueStateError(f"ledger entry record is invalid: {exc}") from exc
    if (
        validated_record["emitted_identity"] != name
        or validated_record["canonical_package_version"] != version
    ):
        raise CatalogueStateError(
            "ledger entry name/version does not match its own record"
        )
    if validated_record["route"].split("/", 1)[1] != varver:
        raise CatalogueStateError(
            "ledger entry record route does not match its ledger location"
        )
    dependencies = _validate_dependencies(channel, varver, entry["dependencies"])

    return {
        "name": name,
        "version": version,
        "sha256": sha256,
        "path": path,
        "record": validated_record,
        "dependencies": dependencies,
        "release_id": release_id,
        "release_tag": release_tag,
        "source_run_id": source_run_id,
    }


def _validate_varver_entries(
    engine: Engine, channel: str, varver: str, entries: object
) -> list[dict[str, object]]:
    if not isinstance(entries, list):
        raise CatalogueStateError(f"channels.{channel}.{varver} must be an array")
    seen: set[tuple[str, str]] = set()
    normalized: list[dict[str, object]] = []
    for entry in entries:
        asset = _validate_asset_entry(engine, channel, varver, entry)
        key = (asset["name"], asset["version"])
        if key in seen:
            raise CatalogueStateError(
                f"duplicate ledger entry {channel}/{varver}/{asset['name']}@{asset['version']}"
            )
        seen.add(key)
        normalized.append(asset)
    return normalized


def validate_state(state: object, *, engine: Engine | None = None) -> dict[str, object]:
    """Validate and normalize one ledger document without changing its values.

    Fails closed: an unknown key, a wrong schema/generation, a malformed asset
    entry, an unsafe path, or a generation that disagrees with the record count
    are all hard ``CatalogueStateError``.
    """
    eng = _engine(engine)
    if not isinstance(state, dict):
        raise CatalogueStateError("ledger must be an object")
    _exact_fields(state, _STATE_FIELDS, "ledger")
    if state["schema"] != _SCHEMA:
        raise CatalogueStateError(f"ledger schema must be {_SCHEMA}")
    generation = state["generation"]
    if type(generation) is not int or generation < 0:
        raise CatalogueStateError("ledger generation must be a non-negative integer")
    updated_by = _validate_updated_by(state["updated_by"], generation=generation)

    channels_raw = state["channels"]
    if not isinstance(channels_raw, dict):
        raise CatalogueStateError("ledger channels must be an object")
    if set(channels_raw) != set(_ALL_CHANNELS):
        raise CatalogueStateError(
            f"ledger channels must have exactly the four known channels {_ALL_CHANNELS!r}"
        )

    channels: dict[str, object] = {}
    total = 0
    for channel in _TAGGED_CHANNELS:
        varvers_raw = channels_raw[channel]
        if not isinstance(varvers_raw, dict):
            raise CatalogueStateError(f"channels.{channel} must be an object")
        normalized_varvers: dict[str, list[dict[str, object]]] = {}
        for varver, entries in varvers_raw.items():
            if not isinstance(varver, str) or not _VARVER_RE.fullmatch(varver):
                raise CatalogueStateError(
                    f"channels.{channel} has an unsafe varver key {varver!r}"
                )
            normalized_varvers[varver] = _validate_varver_entries(
                eng, channel, varver, entries
            )
            total += len(normalized_varvers[varver])
        channels[channel] = normalized_varvers

    try:
        nightly_state = eng.nightly_provenance.validate_state(channels_raw["nightly"])
    except eng.nightly_provenance.ProvenanceError as exc:
        raise CatalogueStateError(f"ledger channels.nightly is invalid: {exc}") from exc
    channels["nightly"] = nightly_state
    total += len(nightly_state["records"])

    if generation != total:
        raise CatalogueStateError(
            "ledger generation must equal the total record count across every channel"
        )

    return {
        "schema": _SCHEMA,
        "generation": generation,
        "updated_by": updated_by,
        "channels": channels,
    }


# --------------------------------------------------------------------------- #
# Decision — the outcome of reconciling one verified run against the ledger.
# --------------------------------------------------------------------------- #

DecisionKind = Literal["noop", "advance"]


@dataclass(frozen=True)
class Decision:
    """One ``decide``/``decide_nightly`` outcome.

    A NOOP carries no changes: ``apply`` refuses it (nothing to commit). An ADVANCE
    carries exactly one of ``channel_entries`` (the tagged fan-out path) or
    ``nightly_state`` (the Nightly path) — never both, since one run is either
    tagged or Nightly, never both.
    """

    kind: DecisionKind
    generation_read: int
    channel_entries: tuple[tuple[str, str, dict[str, object]], ...] = ()
    nightly_state: Mapping[str, object] | None = None
    updated_by: Mapping[str, str] | None = None


# --------------------------------------------------------------------------- #
# Tagged fan-out path.
# --------------------------------------------------------------------------- #


def _dependency_candidates(
    dependency_assets: Sequence[VerifiedAsset], channel: str, varver: str
) -> list[dict[str, object]]:
    """This run's dependency-entry candidates for one destination channel.

    Every listed destination gets the SAME dependency assets (they fan out with
    the canonical asset, same as its bytes) — only ``path`` differs per channel.
    """
    candidates = [
        {
            "name": dep.manifest["name"],
            "version": dep.manifest["version"],
            "sha256": dep.sha256,
            "path": f"{channel}/{varver}/{dep.canonical_name}",
        }
        for dep in dependency_assets
    ]
    return sorted(candidates, key=lambda d: d["name"])


def _dependency_assets_for_varver(
    engine: Engine, dependency_assets: Sequence[VerifiedAsset], freebsd_major: str
) -> list[VerifiedAsset]:
    """This varver's slice of the run's dependency assets, matched by ABI major —
    the same match ``publish_catalogues.verify_run``'s own axis-9 check already
    performs, reused verbatim (``_pkg_matches_abi``). A dependency built for one
    FreeBSD major never lands in a varver on a different major: the live ROUTE
    matrix's own shape is the proof — ce-2.8/FreeBSD 15 carries
    py311-charset-normalizer via ``extra_pkgs``; the two FreeBSD-16 Plus varvers
    carry none.
    """
    row_abi = f"FreeBSD:{freebsd_major}:*"
    return [
        dep
        for dep in dependency_assets
        if engine.build_repo_portable._pkg_matches_abi(dep.manifest, row_abi)
    ]


def _varver_for_route_row(engine: Engine, row: Mapping[str, object]) -> str:
    """One ROUTE build row's varver, via ``build_repo_portable.catalog_name_from_version``
    — the exact function that routes a built package into a catalog directory, so
    it always agrees with a matching canonical asset's own ``record["route"]``
    (built from ``pfb_pkg._route_for``, the same ``variant.lower()-major.minor``
    formula) for any row/asset pair S1 has already matched.
    """
    return engine.build_repo_portable.catalog_name_from_version(
        row["pfsense_version"], row["variant"]
    )


def decide(
    state: Mapping[str, object], run_result: RunResult, *, engine: Engine | None = None
) -> Decision:
    """Decide the fate of one verified TAGGED run against the current ledger.

    Nightly runs use ``decide_nightly`` — Nightly's acceptance rules are
    ``nightly_provenance.complete``'s domain, not this function's.

    A tagged run carries ONE canonical asset PER varver — the live ROUTE matrix
    has multiple build rows (e.g. ce-2.8, plus-26.03, plus-26.07), and S1's own
    ``verify_run`` legitimately emits a multi-varver ``RunResult`` for one
    dispatch (proved by its own
    ``test_verify_run_multi_varver_with_dependency_matching_build_row``). This
    function runs the single-varver reconciliation once per (asset, varver),
    aggregating every varver's ``channel_entries`` into ONE ``Decision`` — NEVER
    partial: any per-varver divergence raises immediately, before any
    ``Decision`` is constructed, so the whole run rejects and the ledger stays
    untouched; the result is a NOOP only when EVERY varver's EVERY listed
    destination already matches, and an ADVANCE otherwise, carrying only the
    (channel, varver) gaps that actually need filling — a varver already fully
    published alongside a genuinely new one produces an ADVANCE for just the new
    leg, never a NOOP and never a silently dropped leg.

    Fan-out within one varver is routing only: its canonical asset's bytes/
    record are routed unchanged into every listed destination
    (``run_result.intake.destinations``, the closed tagged-tuple set
    ``publish_catalogues.parse_intake`` already enforces). Because every
    destination's candidate entry for that varver is built from the SAME asset,
    Edge-follows-Testing (the design doc's rule that an Edge entry must
    reference the exact same sha256 as the Testing entry when destinations is
    ``("testing", "edge")``) holds by construction within one varver; the
    per-destination divergence check below additionally rejects a STALE,
    already-published entry at any listed channel (including "edge") whose
    bytes or provenance disagree with what this run verified.
    """
    eng = _engine(engine)
    normalized = validate_state(dict(state), engine=eng)
    intake = run_result.intake
    if intake.kind != "tagged":
        raise CatalogueStateError(
            "decide() handles only tagged runs; use decide_nightly for a nightly run"
        )
    if not run_result.canonical_assets:
        raise CatalogueStateError(
            "a tagged run must verify at least one canonical package"
        )

    assets_by_varver: dict[str, VerifiedAsset] = {}
    for asset in run_result.canonical_assets:
        record = asset.record
        if record is None:
            raise CatalogueStateError("canonical asset has no provenance record")
        varver = record["route"].split("/", 1)[1]
        if varver in assets_by_varver:
            raise CatalogueStateError(
                f"two canonical assets claim the same varver {varver!r}"
            )
        assets_by_varver[varver] = asset

    route_varvers = {
        _varver_for_route_row(eng, row) for row in run_result.build_route_rows
    }
    missing_route_rows = set(assets_by_varver) - route_varvers
    if missing_route_rows:
        raise CatalogueStateError(
            "canonical asset varver(s) absent from the run's ROUTE build rows: "
            f"{sorted(missing_route_rows)!r}"
        )
    missing_assets = route_varvers - set(assets_by_varver)
    if missing_assets:
        raise CatalogueStateError(
            f"ROUTE build row(s) with no canonical asset: {sorted(missing_assets)!r}"
        )

    channels = normalized["channels"]
    all_channel_entries: list[tuple[str, str, dict[str, object]]] = []
    for varver in sorted(assets_by_varver):
        asset = assets_by_varver[varver]
        record = asset.record
        name = record["emitted_identity"]
        version = record["canonical_package_version"]
        sha256 = asset.sha256
        major = record["matrix_row"]["freebsd_major"]
        varver_dependency_assets = _dependency_assets_for_varver(
            eng, run_result.dependency_assets, major
        )
        dependencies_by_channel = {
            channel: _dependency_candidates(varver_dependency_assets, channel, varver)
            for channel in intake.destinations
        }

        present_matches: list[str] = []
        for channel in intake.destinations:
            varver_map = channels[channel]
            existing_list = varver_map.get(varver, [])
            existing = next(
                (
                    e
                    for e in existing_list
                    if e["name"] == name and e["version"] == version
                ),
                None,
            )
            if existing is None:
                continue
            if existing["sha256"] != sha256:
                raise CatalogueStateError(
                    f"{channel}/{varver}: {name}@{version} is already published with different bytes "
                    f"(sha256 {existing['sha256']} != {sha256})"
                )
            if (
                existing["record"] != record
                or existing["dependencies"] != dependencies_by_channel[channel]
            ):
                raise CatalogueStateError(
                    f"{channel}/{varver}: {name}@{version} is already published with different provenance"
                )
            present_matches.append(channel)

        if len(present_matches) == len(intake.destinations):
            continue

        filename = asset.canonical_name
        entry_candidate = {
            "name": name,
            "version": version,
            "sha256": sha256,
            "record": dict(record),
        }
        for channel in intake.destinations:
            if channel in present_matches:
                continue
            path = f"{channel}/{varver}/{filename}"
            all_channel_entries.append(
                (
                    channel,
                    varver,
                    {
                        **entry_candidate,
                        "path": path,
                        "dependencies": dependencies_by_channel[channel],
                        "release_id": intake.release_id,
                        "release_tag": intake.release_tag,
                        "source_run_id": intake.source_run_id,
                    },
                )
            )

    if not all_channel_entries:
        return Decision(kind="noop", generation_read=normalized["generation"])

    return Decision(
        kind="advance",
        generation_read=normalized["generation"],
        channel_entries=tuple(all_channel_entries),
        updated_by={
            "source_repository": intake.source_repository,
            "source_run_id": intake.source_run_id,
        },
    )


# --------------------------------------------------------------------------- #
# Nightly path — decide_nightly wraps nightly_provenance.complete verbatim.
# --------------------------------------------------------------------------- #


def decide_nightly(
    state: Mapping[str, object],
    handoff: Mapping[str, object],
    *,
    run_id: str,
    source_repository: str,
    engine: Engine | None = None,
) -> Decision:
    """Decide the fate of one verified Nightly handoff against the ledger.

    Reuses ``nightly_provenance.complete`` for every acceptance rule (no-op
    replay, stale-generation rejection, artifact-bytes collision, version
    collision for different inputs, missing-artifacts rejection). This function's
    only jobs: build the ``Candidate`` from the handoff's own allocation paired
    with THIS LEDGER's current Nightly generation (never the handoff's own
    generation — see the module docstring), independently recompute the expected
    input digest from the handoff's own pinned ``source_sha``/``ports_sha``/
    ``matrix_digest`` rather than trusting the handoff's ``allocation.input_digest``
    field on its own, and translate ``complete``'s output into a ``Decision``.
    This recompute is an INTERNAL CONSISTENCY check, not tamper resistance: all
    four inputs come from the same handoff document, so a consistently-tampered
    handoff defeats it. What it does catch is accidental corruption or a stale/
    partial field — the allocation and the pinned inputs disagreeing with each
    other within one document.
    """
    eng = _engine(engine)
    normalized = validate_state(dict(state), engine=eng)
    np = eng.nightly_provenance

    if not isinstance(handoff, Mapping):
        raise CatalogueStateError("nightly handoff must be an object")
    if handoff.get("kind") != "nightly-handoff":
        raise CatalogueStateError("nightly handoff kind must be 'nightly-handoff'")

    allocation_raw = handoff.get("allocation")
    if not isinstance(allocation_raw, Mapping):
        raise CatalogueStateError("nightly handoff allocation must be an object")
    try:
        allocation = np.NightlyAllocation(**dict(allocation_raw))
    except TypeError as exc:
        raise CatalogueStateError(
            f"nightly handoff allocation is malformed: {exc}"
        ) from exc
    try:
        np.validate_nightly_allocation(allocation)
    except (TypeError, ValueError) as exc:
        raise CatalogueStateError(
            f"nightly handoff allocation is invalid: {exc}"
        ) from exc
    if allocation.outcome != "build":
        raise CatalogueStateError("nightly handoff allocation outcome must be 'build'")

    try:
        expected_input_digest = np.combined_nightly_input_digest(
            handoff.get("source_sha"),
            handoff.get("ports_sha"),
            handoff.get("matrix_digest"),
        )
    except (TypeError, ValueError) as exc:
        raise CatalogueStateError(
            f"nightly handoff pinned inputs are malformed: {exc}"
        ) from exc

    builds = handoff.get("builds", [])
    if not isinstance(builds, list):
        raise CatalogueStateError("nightly handoff builds must be a list")
    artifacts: list[Mapping[str, object]] = []
    for build in builds:
        if not isinstance(build, Mapping) or "artifact" not in build:
            raise CatalogueStateError("nightly handoff build entry is malformed")
        artifacts.append(build["artifact"])

    nightly_state = normalized["channels"]["nightly"]
    candidate = np.Candidate(
        allocation=allocation, generation=int(nightly_state["generation"])
    )
    try:
        result = np.complete(
            nightly_state,
            candidate,
            artifacts,
            run_id=run_id,
            expected_input_digest=expected_input_digest,
        )
    except np.ProvenanceError as exc:
        raise CatalogueStateError(f"nightly completion rejected: {exc}") from exc

    if result["generation"] == nightly_state["generation"]:
        return Decision(kind="noop", generation_read=normalized["generation"])
    return Decision(
        kind="advance",
        generation_read=normalized["generation"],
        nightly_state=result,
        updated_by={"source_repository": source_repository, "source_run_id": run_id},
    )


# --------------------------------------------------------------------------- #
# apply — fold an ADVANCE decision into a new ledger state, with retention.
# --------------------------------------------------------------------------- #


def retention_keep_count(channel: str, varver: str) -> int:
    """Resolve the retained-generation count for one (channel, varver).

    Seam for a later ticket: pinning an EOL'd varver (one we have also EOL'd on
    our side) to keep=1 only has to teach this function a lookup — no ledger
    reshaping. Today every (channel, varver) gets ``DEFAULT_RETENTION_KEEP``.
    """
    return DEFAULT_RETENTION_KEEP


def _prune_retained(
    engine: Engine, entries: Sequence[Mapping[str, object]], keep: int
) -> list[dict[str, object]]:
    """Keep the top ``keep`` generations of one (channel, varver) list, newest
    first by ``pkg_version_sort_key``, restored to ascending order for storage.

    Never touches package bytes: this only prunes ledger entries (path +
    checksum), never deletes or mutates the mirrored .pkg a retained entry
    points at — that is S3's tree-assembly job, driven off the returned set.
    """
    sort_key = engine.pfb_pkg.pkg_version_sort_key
    ordered = sorted(entries, key=lambda e: sort_key(e["version"]), reverse=True)
    return sorted(
        (dict(e) for e in ordered[:keep]), key=lambda e: sort_key(e["version"])
    )


def _dependency_union(
    entries: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Union of every dependency referenced by any of ``entries`` — the RETAINED
    canonical set for one (channel, varver), after retention has already pruned
    it (#2146 F1). Deduped by full identity (name, version, sha256, path): a
    retained older canonical entry's dependency survives here alongside the
    newest run's, even when the two disagree on dependency version — that union
    IS the whole dependency-retention design, no separate count exists.
    """
    seen: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for entry in entries:
        for dep in entry["dependencies"]:
            key = (dep["name"], dep["version"], dep["sha256"], dep["path"])
            seen[key] = dict(dep)
    return tuple(sorted(seen.values(), key=lambda d: (d["name"], d["version"])))


@dataclass(frozen=True)
class ApplyResult:
    """``apply``'s full outcome.

    ``state`` is the new ledger — persist it exactly like ``apply`` used to
    return directly. ``dependency_unions`` is a pre-computed
    ``{(channel, varver): (dependency entry, ...)}`` map covering every tagged
    (channel, varver) in the resulting ledger, each value already the retained-
    set union described in the module docstring — the exact
    ``catalogue_assembly.CatalogueTarget.dependencies`` input, so a caller never
    re-derives it from ``state`` itself.
    """

    state: dict[str, object]
    dependency_unions: Mapping[tuple[str, str], tuple[dict[str, object], ...]]


def apply(
    state: Mapping[str, object],
    decision: Decision,
    *,
    engine: Engine | None = None,
    keep_count_for: Callable[[str, str], int] | None = None,
) -> ApplyResult:
    """Fold an ADVANCE decision into a new ledger state. Never mutates ``state``.

    Refuses a NOOP decision (nothing to commit) and a decision computed against a
    generation that no longer matches ``state`` (re-``load_state`` and re-decide).
    """
    eng = _engine(engine)
    normalized = validate_state(dict(state), engine=eng)
    if decision.kind != "advance":
        raise CatalogueStateError(
            "apply() requires an ADVANCE decision; a NOOP writes nothing"
        )
    if decision.generation_read != normalized["generation"]:
        raise CatalogueStateError(
            "decision was computed against a stale ledger generation; re-load_state and re-decide"
        )
    if bool(decision.channel_entries) == bool(decision.nightly_state is not None):
        raise CatalogueStateError(
            "an ADVANCE decision must carry exactly one of channel_entries or nightly_state"
        )

    keep_resolver = (
        keep_count_for if keep_count_for is not None else retention_keep_count
    )
    channels: dict[str, object] = {
        chan: dict(normalized["channels"][chan]) for chan in _TAGGED_CHANNELS
    }
    channels["nightly"] = normalized["channels"]["nightly"]

    if decision.nightly_state is not None:
        try:
            channels["nightly"] = eng.nightly_provenance.validate_state(
                dict(decision.nightly_state)
            )
        except eng.nightly_provenance.ProvenanceError as exc:
            raise CatalogueStateError(
                f"decision nightly_state is invalid: {exc}"
            ) from exc

    for channel, varver, entry in decision.channel_entries:
        if channel not in _TAGGED_CHANNELS:
            raise CatalogueStateError(
                f"decision channel_entries has an unknown channel {channel!r}"
            )
        varver_map = dict(channels[channel])
        existing_list = [
            e
            for e in varver_map.get(varver, [])
            if not (e["name"] == entry["name"] and e["version"] == entry["version"])
        ]
        existing_list.append(dict(entry))
        keep = keep_resolver(channel, varver)
        if type(keep) is not int or keep < 1:
            raise CatalogueStateError(
                f"retention keep-count for {channel}/{varver} must be a positive integer"
            )
        varver_map[varver] = _prune_retained(eng, existing_list, keep)
        channels[channel] = varver_map

    total = sum(len(v) for chan in _TAGGED_CHANNELS for v in channels[chan].values())
    total += len(channels["nightly"]["records"])

    new_updated_by = (
        dict(decision.updated_by)
        if decision.updated_by is not None
        else dict(normalized["updated_by"])
    )
    new_state = {
        "schema": _SCHEMA,
        "generation": total,
        "updated_by": new_updated_by,
        "channels": channels,
    }
    validated_new_state = validate_state(new_state, engine=eng)
    dependency_unions = {
        (channel, varver): _dependency_union(entries)
        for channel in _TAGGED_CHANNELS
        for varver, entries in validated_new_state["channels"][channel].items()
    }
    return ApplyResult(state=validated_new_state, dependency_unions=dependency_unions)
