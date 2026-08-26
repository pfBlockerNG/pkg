"""Tagged-release publisher — CLI entry point (issue #2146 step R2).

Wires the already-gated S1/S3 pieces into a working publisher: parse the tagged-run
intake, verify every downloaded ``.pkg`` asset (``publish_catalogues.verify_asset`` /
``verify_run``), then for each destination channel x each varver this run's canonical
assets target, drop the verified assets into ``<pkg>/docs/<channel>/<varver>/``, prune
per retention, and regenerate that catalogue (``catalogue_assembly.prune_retained`` /
``regenerate_catalogue``) — finally checking the byte/checksum/provenance identity of
any asset fanned out to more than one destination
(``catalogue_assembly.verify_multi_destination_identity``).

Never edits ``publish_catalogues.py`` / ``catalogue_assembly.py`` (both gated) — this
module only calls their public contracts. Never runs git: the caller (the release
workflow) owns staging exactly the touched directories, committing, and pushing —
this module only reports which ``(channel, varver)`` directories it touched.

Dependency identity (issue #2468): a dependency ``.pkg``'s identity is its filename
alone (``<name>-<version>.pkg``, e.g. ``py311-charset-normalizer-3.4.0.pkg``) — a
tagged Release asset's ``-<Variant>-<pfsense_version>`` suffix (``VerifiedAsset.
release_suffix``) routes it to the varver of ITS OWN suffix row (``_build_targets``:
that row must be one of this run's own canonical targets, must declare the dep's
origin in ``extra_pkgs``, and its ABI must match — never by ABI-matching every
same-major declaring row). A dependency already present at a destination is left
exactly as-is — never read, never byte-compared, never overwritten — and one missing
is copied in (``_drop_assets``). The canonical package keeps the strict rule below.
Because a dependency is place-if-missing, it never feeds the multi-destination
identity check (``_asset_map``/``publish``'s ``source_index``) — fan-out byte
identity stays a canonical-package invariant only; a dep already differing at one
destination but freshly placed at another is expected, not a divergence.

No-op behaviour: a ``(channel, varver)`` target whose canonical asset is already
present, byte-identical, at the destination (dependency assets are irrelevant to this
check — see above) AND whose ``meta``/``meta.conf``/``data.pkg``/``packagesite.pkg``
descriptor content exactly describes every on-disk payload package is left untouched
entirely — no copy, no prune, no regenerate. There is no ledger; "already published"
is read straight off the files already on disk. An incomplete, unreadable, stale, or
mismatched descriptor is regenerated even when the ``.pkg`` payload is unchanged. A
CANONICAL file sharing an incoming asset's canonical name but carrying DIFFERENT
bytes is never overwritten: same name/version with different bytes, source, or
provenance raises ``DestinationConflictError`` instead.

Nightly intake is not handled here: ``run()`` accepts only ``kind == "tagged"`` intake
and fails closed otherwise.

Asset checksums: ``verify_asset`` requires an independently-sourced ``expected_sha256``
per asset (never recomputed from the same download it is meant to check). This CLI
reads that from a ``digests.json`` sidecar inside ``--assets-dir`` — ``{"<filename>":
"<sha256 hex>", ...}`` — that the caller (the release workflow) is expected to populate
from the GitHub Releases API's own per-asset ``digest`` field before downloading.

All verification code is imported from this pkg checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

# Direct execution already places scripts/ on sys.path; the explicit insert also
# supports focused tests importing this module.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import catalogue_assembly as ca
import catalogue_engine
import catalogue_sig_only as cso
import pfb_pkg
import publish_catalogues as pc
import tagged_release_handoff as trh

_SITE_SUBDIR = "docs"
_DIGESTS_FILENAME = "digests.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PY_FLAVOR_ORIGIN = re.compile(r"^(py)(?:\d+)?-")
_DESCRIPTOR_ERRORS = (
    *cso._READ_ERRORS,
    pfb_pkg.PkgError,
    RecursionError,
    UnicodeDecodeError,
    TypeError,
)


class PublishReleaseError(Exception):
    """A digest-sidecar, target-resolution, or CLI-level failure this module itself
    detected. Local engine and catalogue errors propagate without rewrapping."""


class DestinationConflictError(PublishReleaseError):
    """An existing destination file shares its canonical name with an incoming
    asset but carries different bytes — same name/version, different source or
    provenance. A distinct subclass (rather than a generic PublishReleaseError)
    so an operator can tell "the catalogue already holds a different build of
    this exact version" apart from a digest-sidecar or target-resolution
    failure by exception type alone."""


def _load_digests(assets_dir: Path) -> dict[str, str]:
    path = assets_dir / _DIGESTS_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublishReleaseError(f"cannot read {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PublishReleaseError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise PublishReleaseError(
            f"{path} must be a non-empty JSON object of {{filename: sha256-hex}}"
        )
    for name, value in parsed.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not _SHA256_RE.fullmatch(value)
        ):
            raise PublishReleaseError(
                f"{path}: entry {name!r} must map to 64 lowercase hex characters"
            )
    return parsed


def _discover_assets(
    assets_dir: Path, digests: Mapping[str, str]
) -> tuple[tuple[str, Path], ...]:
    found = sorted(p for p in assets_dir.glob("*.pkg") if p.is_file())
    found_names = {p.name for p in found}
    if not found_names:
        raise PublishReleaseError(f"no .pkg assets found under {assets_dir}")
    missing_digest = sorted(found_names - set(digests))
    if missing_digest:
        raise PublishReleaseError(
            f"asset(s) with no {_DIGESTS_FILENAME} entry: {missing_digest}"
        )
    missing_file = sorted(set(digests) - found_names)
    if missing_file:
        raise PublishReleaseError(
            f"{_DIGESTS_FILENAME} entries with no matching asset file: {missing_file}"
        )
    return tuple((p.name, p) for p in found)


def _verify_all_assets(
    intake: pc.Intake,
    assets_dir: Path,
    digests: Mapping[str, str],
    work_dir: Path,
) -> list[pc.VerifiedAsset]:
    return [
        pc.verify_asset(
            path,
            name,
            intake=intake,
            expected_sha256=digests[name],
            work_dir=work_dir / f"asset-{index}",
        )
        for index, (name, path) in enumerate(_discover_assets(assets_dir, digests))
    ]


@dataclass
class _Target:
    row: Mapping[str, object]
    canonical: pc.VerifiedAsset
    dependencies: list[pc.VerifiedAsset] = field(default_factory=list)


def _origin_key(value: object) -> tuple[str, str] | None:
    """(category, unflavored last) for a port origin.

    extra_pkgs lists port origins (textproc/py-charset-normalizer). Some
    manifests and test fixtures use the flavored package origin
    (textproc/py311-charset-normalizer). Those name the same extra.
    Category is part of the identity: www/py-foo is not textproc/py-foo
    (issue #2403).
    """
    if not isinstance(value, str) or "/" not in value:
        return None
    category, last = value.rsplit("/", 1)
    if not category or not last:
        return None
    stripped = _PY_FLAVOR_ORIGIN.sub("", last, count=1)
    return (category, stripped or last)


def _row_declares_origin(row: Mapping[str, object], origin: object) -> bool:
    """True iff this ROUTE/BUILD row lists ``origin`` in extra_pkgs."""
    extras = row.get("extra_pkgs")
    if not isinstance(extras, list):
        return False
    if origin in extras:
        return True
    key = _origin_key(origin)
    if key is None:
        return False
    return any(_origin_key(extra) == key for extra in extras)


def _row_declares_dep(row: Mapping[str, object], dep: pc.VerifiedAsset) -> bool:
    """True iff this ROUTE/BUILD row lists the dependency's origin in extra_pkgs."""
    return _row_declares_origin(row, dep.manifest.get("origin"))


def _build_targets(run_result: pc.RunResult) -> dict[str, _Target]:
    brp = catalogue_engine
    targets: dict[str, _Target] = {}
    for asset in run_result.canonical_assets:
        row = pc._canonical_record(asset)["matrix_row"]
        varver = brp.catalog_name_from_version(row["pfsense_version"], row["variant"])
        if varver in targets:
            raise PublishReleaseError(
                f"two canonical assets resolve to the same varver {varver!r}: "
                f"{targets[varver].canonical.declared_name!r} and {asset.declared_name!r}"
            )
        targets[varver] = _Target(row=row, canonical=asset)

    for dep in run_result.dependency_assets:
        # issue #2468: routes to the varver of the dep's OWN suffix row only — never
        # by ABI-match against every same-major declaring row. That row must be a
        # canonical target of this run, declare the dep's origin, and match ABI.
        reject = PublishReleaseError(
            f"dependency asset {dep.declared_name!r} matches no varver targeted "
            "by this run's own canonical assets (per-suffix routing, ABI, and "
            "extra_pkgs declaration)"
        )
        if dep.release_suffix is None:
            raise reject
        variant, pfsense_version = dep.release_suffix
        varver = brp.catalog_name_from_version(pfsense_version, variant)
        target = targets.get(varver)
        if target is None:
            raise reject
        if not brp._pkg_matches_abi(
            dep.manifest, f"FreeBSD:{target.row['freebsd_major']}:*"
        ):
            raise reject
        if not _row_declares_dep(target.row, dep):
            raise reject
        target.dependencies.append(dep)

    return targets


def _asset_map(target: _Target) -> dict[str, pc.VerifiedAsset]:
    mapping: dict[str, pc.VerifiedAsset] = {
        target.canonical.canonical_name: target.canonical
    }
    for dep in target.dependencies:
        existing = mapping.get(dep.canonical_name)
        if existing is not None:
            # issue #2468: two assets resolving to one canonical name always conflict
            # — no byte-compare-then-tolerate branch; dependency identity is the
            # filename alone, and per-suffix routing should make this unreachable.
            raise DestinationConflictError(
                f"{dep.canonical_name}: two verified assets share this canonical name — "
                f"{existing.declared_name!r} (sha256={existing.sha256}) and "
                f"{dep.declared_name!r} (sha256={dep.sha256})"
            )
        mapping[dep.canonical_name] = dep
    return mapping


def _files_identical(a: Path, b: Path) -> bool:
    if a.stat().st_size != b.stat().st_size:
        return False
    return a.read_bytes() == b.read_bytes()


def _evict_undeclared_deps(dest_dir: Path, *, row: Mapping[str, object]) -> bool:
    """Unlink non-catalog, non-canonical .pkg files this dest row does not declare.

    issue #2402: prune_retained never touches dependency .pkg files, so a stray
    extra already on an undeclaring dest survives attach and is re-indexed.
    Scoped to this dest directory only. A declared leftover is kept even when
    this run did not re-attach it.
    """
    if not dest_dir.is_dir():
        return False
    brp = catalogue_engine
    evicted = False
    for path in sorted(dest_dir.glob("*.pkg")):
        if not path.is_file() or path.name in brp._CATALOG_PKG_FILES:
            continue
        manifest = pfb_pkg.read_compact_manifest(path)
        if manifest.get("name") == pfb_pkg.CANONICAL_EMITTED_IDENTITY:
            continue
        if _row_declares_origin(row, manifest.get("origin")):
            continue
        path.unlink()
        evicted = True
    return evicted


def _drop_assets(dest_dir: Path, asset_map: Mapping[str, pc.VerifiedAsset]) -> bool:
    dest_dir.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, asset in asset_map.items():
        dest = dest_dir / name
        src = asset.work_path
        if dest.is_file():
            if asset.asset_class == "dependency":
                # issue #2468: a dependency .pkg's identity IS its filename — place
                # it only when missing, never byte-compare or overwrite an existing one.
                continue
            if _files_identical(dest, src):
                continue
            raise DestinationConflictError(
                f"{dest}: already publishes a different build of {name} — "
                f"existing sha256={hashlib.sha256(dest.read_bytes()).hexdigest()}, "
                f"incoming sha256={hashlib.sha256(src.read_bytes()).hexdigest()}"
            )
        shutil.copy2(src, dest)
        changed = True
    return changed


def _strict_json(raw: bytes) -> object:
    def reject_constant(value: str) -> object:
        raise ValueError(f"invalid JSON constant {value}")

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=pfb_pkg._reject_duplicate_json_keys,
        parse_constant=reject_constant,
    )


def _catalogue_archive_payload(path: Path, member_name: str) -> tuple[bytes, bool]:
    with path.open("rb") as descriptor:
        if descriptor.read(4) != pfb_pkg.ZSTD_MAGIC:
            raise ValueError(f"{path.name}: catalogue archive is not zstd-framed")
    members = cso._read_members(path)
    unsigned = {member_name}
    signed = {member_name, f"{member_name}.sig", f"{member_name}.pub"}
    names = set(members)
    if names not in (unsigned, signed):
        raise ValueError(f"{path.name}: invalid catalogue archive member set")
    if any(kind != tarfile.REGTYPE for kind, _content in members.values()):
        raise ValueError(f"{path.name}: catalogue members must be regular files")
    payload = members[member_name][1]
    is_signed = names == signed
    if is_signed and not catalogue_engine.catalogue_signature_valid(
        payload,
        members[f"{member_name}.sig"][1],
        members[f"{member_name}.pub"][1],
    ):
        raise ValueError(f"{path.name}: invalid catalogue signature")
    return payload, is_signed


def _catalogue_identities(rows: object) -> set[tuple[str, str, str]]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("catalogue packages must be a non-empty array")
    identities: set[tuple[str, str, str]] = set()
    name_versions: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("catalogue package row must be an object")
        name, version, path, repopath = (
            row.get("name"),
            row.get("version"),
            row.get("path"),
            row.get("repopath"),
        )
        if any(
            not isinstance(value, str) or not value
            for value in (name, version, path, repopath)
        ):
            raise ValueError(
                "catalogue name/version/path/repopath must be non-empty strings"
            )
        name = cast(str, name)
        version = cast(str, version)
        path = cast(str, path)
        repopath = cast(str, repopath)
        if path != repopath or Path(path).name != path:
            raise ValueError(
                "catalogue path/repopath must be the same package basename"
            )
        name_version = (name, version)
        if name_version in name_versions:
            raise ValueError("duplicate catalogue name/version")
        name_versions.add(name_version)
        identities.add((name, version, repopath))
    return identities


def _catalogue_destination_safe(dest_dir: Path, *, root: Path) -> bool:
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        return False
    try:
        relative = dest_dir.relative_to(root)
    except ValueError:
        return False
    current = root
    for segment in relative.parts:
        current /= segment
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            return False
    return dest_dir.resolve().is_relative_to(root.resolve())


def _require_safe_catalogue_destination(dest_dir: Path, *, root: Path) -> None:
    if not _catalogue_destination_safe(dest_dir, root=root):
        raise PublishReleaseError(
            f"catalogue destination escapes or traverses a symlink: {dest_dir}"
        )


def _catalogue_descriptor_complete(dest_dir: Path, *, root: Path) -> bool:
    if not _catalogue_destination_safe(dest_dir, root=root) or not dest_dir.is_dir():
        return False
    descriptor_names = ("meta", "meta.conf", "data.pkg", "packagesite.pkg")
    descriptor_paths = tuple(dest_dir / name for name in descriptor_names)
    if any(path.is_symlink() or not path.is_file() for path in descriptor_paths):
        return False
    try:
        meta = catalogue_engine.META_CONF.encode()
        if any(
            (dest_dir / name).read_bytes() != meta for name in ("meta", "meta.conf")
        ):
            return False

        payload_identities: set[tuple[str, str, str]] = set()
        for path in sorted(dest_dir.glob("*.pkg")):
            if path.name in catalogue_engine._CATALOG_PKG_FILES:
                continue
            if path.is_symlink() or not path.is_file():
                return False
            manifest = pfb_pkg.read_compact_manifest(path)
            name, version = manifest.get("name"), manifest.get("version")
            if not isinstance(name, str) or not name:
                return False
            if not isinstance(version, str) or not version:
                return False
            if path.name != f"{name}-{version}.pkg":
                return False
            identity = (name, version, path.name)
            if identity in payload_identities:
                return False
            payload_identities.add(identity)
        if not payload_identities:
            return False

        packagesite_raw, packagesite_signed = _catalogue_archive_payload(
            dest_dir / "packagesite.pkg", "packagesite.yaml"
        )
        packagesite_rows = [
            _strict_json(line) for line in packagesite_raw.splitlines() if line
        ]
        if _catalogue_identities(packagesite_rows) != payload_identities:
            return False

        data_raw, data_signed = _catalogue_archive_payload(
            dest_dir / "data.pkg", "data"
        )
        if packagesite_signed != data_signed:
            return False
        data = _strict_json(data_raw)
        if not isinstance(data, dict):
            return False
        if set(data) != {"groups", "expired_packages", "packages"}:
            return False
        if data["groups"] != [] or data["expired_packages"] != []:
            return False
        return _catalogue_identities(data["packages"]) == payload_identities
    except _DESCRIPTOR_ERRORS:
        return False


def _expected_public_member(sign_key: Path | None) -> bytes | None:
    """The `.pub` member a catalogue signed with ``sign_key`` carries, or None when
    there is no key.

    Derived ONCE per publish, before any destination is touched: ``signing_public_der``
    runs two openssl subprocesses and raises on a key pkg could not verify, so deriving
    it per destination would both pay that per destination and turn a bad key into a
    failure whose timing depends on iteration order — after earlier destinations had
    already been healed and rewritten.
    """
    if sign_key is None:
        return None
    brp = catalogue_engine
    return brp.PKGSIGN_ECDSA_HEAD + brp.signing_public_der(sign_key)


def _catalogue_carries_key(dest_dir: Path, expected_public: bytes) -> bool:
    """True when every catalogue archive under ``dest_dir`` already embeds ``expected_public``.

    Nothing else in the `changed` decision can see a signature, and a destination
    whose package set has not moved is skipped — so a catalogue published before
    signing existed, or one still carrying a retired key, would never be
    re-signed, and a box landing on that varver would meet an unsigned catalogue
    with a signature-requiring conf (issue #2675). An unreadable archive answers
    False: republishing it is the recoverable direction.
    """
    brp = catalogue_engine
    return all(
        cso.public_key_members(dest_dir / name) == [expected_public]
        for name in brp._CATALOG_PKG_FILES
    )


@dataclass(frozen=True)
class PublishReport:
    touched: tuple[tuple[str, str], ...]

    @property
    def noop(self) -> bool:
        return not self.touched

    def describe(self) -> list[str]:
        if not self.touched:
            return [
                "NOOP: every destination already matches this run's verified assets"
            ]
        return [f"updated {channel}/{varver}" for channel, varver in self.touched]


def publish(
    run_result: pc.RunResult,
    pkg_repo: str | Path,
    *,
    sign_key: Path | None = None,
) -> PublishReport:
    intake = run_result.intake
    site_root = Path(pkg_repo) / _SITE_SUBDIR
    targets = _build_targets(run_result)

    expected_public = _expected_public_member(sign_key)
    touched: list[tuple[str, str]] = []
    source_index: dict[Path, list[tuple[str, str]]] = {}
    for varver in sorted(targets):
        target = targets[varver]
        asset_map = _asset_map(target)
        for channel in intake.destinations:
            dest_dir = site_root / channel / varver
            _require_safe_catalogue_destination(dest_dir, root=site_root)
            # Eviction runs FIRST: a dependency is placed only when its name is
            # missing, so an undeclared leftover under that same name has to go
            # before the drop, or the run would skip the incoming dependency and
            # then unlink the leftover — publishing a catalogue without the extra.
            changed = _evict_undeclared_deps(dest_dir, row=target.row)
            if _drop_assets(dest_dir, asset_map):
                changed = True
            if not changed and not _catalogue_descriptor_complete(
                dest_dir, root=site_root
            ):
                changed = True
            if (
                not changed
                and expected_public is not None
                and not _catalogue_carries_key(dest_dir, expected_public)
            ):
                changed = True
            # Heal historical holes before prune: copy every canonical version
            # still on a slower tagged channel (never nightly) onto this dest.
            copied = ca.backfill_from_slower_channels(site_root, channel, varver)
            if copied:
                changed = True
                for src, destinations in copied.items():
                    bucket = source_index.setdefault(src, [])
                    for dest in destinations:
                        if dest not in bucket:
                            bucket.append(dest)
            # issue #2468: only the canonical asset feeds the fan-out identity index —
            # a place-if-missing dependency skipped at one destination but placed
            # fresh at another would otherwise falsely trip verify_multi_destination_identity.
            source_index.setdefault(target.canonical.work_path.resolve(), []).append(
                (channel, varver)
            )
            if changed:
                ca.prune_retained(site_root, channel, varver)
                ca.regenerate_catalogue(site_root, channel, varver, sign_key=sign_key)
                touched.append((channel, varver))

    if source_index:
        ca.verify_multi_destination_identity(site_root, source_index)

    return PublishReport(touched=tuple(touched))


def _load_compatibility_route_matrix(
    path: str | Path | None,
) -> list[dict[str, object]]:
    if path is None:
        raise trh.HandoffError(
            "Release has no handoff and no pkg compatibility route matrix was provided"
        )
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise trh.HandoffError(
            f"cannot read pkg compatibility route matrix {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise trh.HandoffError(
            f"pkg compatibility route matrix is not valid JSON: {exc}"
        ) from exc
    return trh._route_matrix(raw)


def run(
    *,
    source_repository: str,
    release_id: str,
    release_tag: str,
    source_sha: str,
    destinations: str,
    source_run_id: str,
    assets_dir: str | Path,
    pkg_repo: str | Path,
    handoff_file: str | Path | None,
    compatibility_route_matrix_file: str | Path | None = None,
    sign_key: Path | None = None,
) -> PublishReport:
    intake = pc.parse_intake(
        source_repository, release_id, release_tag, destinations, source_run_id
    )
    if intake.kind != "tagged":
        raise PublishReleaseError(
            f"publish_release.py only handles tagged intake; got kind={intake.kind!r} — "
            "Nightly publishing is not implemented here"
        )

    handoff: Mapping[str, object] | None = None
    if handoff_file is not None:
        handoff = trh.load_handoff(
            handoff_file,
            expected_release_tag=release_tag,
            expected_source_sha=source_sha,
        )
        route_matrix_rows = cast(list[Mapping[str, object]], handoff["route_matrix"])
    else:
        route_matrix_rows = cast(
            list[Mapping[str, object]],
            _load_compatibility_route_matrix(compatibility_route_matrix_file),
        )

    assets_dir = Path(assets_dir)
    digests = _load_digests(assets_dir)

    with tempfile.TemporaryDirectory(prefix="publish-release-verify-") as work_dir:
        verified_assets = _verify_all_assets(
            intake, assets_dir, digests, Path(work_dir)
        )
        run_result = pc.verify_run(intake, verified_assets, route_matrix_rows)
        records = [
            cast(Mapping[str, object], asset.record)
            for asset in run_result.canonical_assets
        ]
        try:
            if handoff is not None:
                trh.validate_build_records(handoff, records)
            else:
                expected_source_sha = trh._git_sha(source_sha, "expected source_sha")
                for index, record in enumerate(records):
                    if record.get("source_sha") != expected_source_sha:
                        raise trh.BuildRecordIdentityError(index, "source_sha")
                ports_shas = {record.get("freebsd_ports_sha") for record in records}
                if len(ports_shas) != 1:
                    raise PublishReleaseError(
                        "compatibility Release canonical records disagree on freebsd_ports_sha"
                    )
        except trh.BuildRecordIdentityError as exc:
            if exc.field == "source_sha":
                site_root = Path(pkg_repo) / _SITE_SUBDIR
                for varver, target in _build_targets(run_result).items():
                    for channel in intake.destinations:
                        existing = (
                            site_root
                            / channel
                            / varver
                            / target.canonical.canonical_name
                        )
                        if existing.is_file():
                            raise DestinationConflictError(
                                f"{existing}: {exc}"
                            ) from exc
                raise DestinationConflictError(str(exc)) from exc
            raise
        return publish(run_result, pkg_repo, sign_key=sign_key)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a tagged release's .pkg assets and publish them into the "
            "pfBlockerNG/pkg catalogue tree (never runs git)."
        )
    )
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--destinations", required=True, help="compact JSON array")
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument(
        "--assets-dir",
        required=True,
        help="directory of downloaded .pkg assets + digests.json sidecar",
    )
    parser.add_argument(
        "--pkg-repo",
        required=True,
        help="the checked-out pfBlockerNG/pkg working tree (site is <pkg-repo>/docs)",
    )
    parser.add_argument(
        "--handoff", default="", help="build-time tagged release handoff JSON"
    )
    parser.add_argument(
        "--compatibility-route-matrix",
        default="",
        help="pkg-owned ROUTE matrix for immutable Releases published before handoffs existed",
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
            source_repository=args.source_repository,
            release_id=args.release_id,
            release_tag=args.release_tag,
            source_sha=args.source_sha,
            destinations=args.destinations,
            source_run_id=args.source_run_id,
            assets_dir=args.assets_dir,
            pkg_repo=args.pkg_repo,
            handoff_file=Path(args.handoff) if args.handoff else None,
            compatibility_route_matrix_file=(
                Path(args.compatibility_route_matrix)
                if args.compatibility_route_matrix
                else None
            ),
            sign_key=Path(args.sign_key) if args.sign_key else None,
        )
    except (
        PublishReleaseError,
        trh.HandoffError,
        pc.PublishError,
        ca.CatalogueAssemblyError,
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
