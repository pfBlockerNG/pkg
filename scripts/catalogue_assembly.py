"""Regenerate one channel/varver catalogue directly from the .pkg files already
sitting on disk: the tree itself IS the state.

Scope: no ledger, no git, no network, no scratch tree, no backup/rollback. The
retired design staged a whole multi-catalogue ``Plan`` into a scratch tree and
moved it into place with a recoverable per-target swap; that machinery existed
to protect a durable ledger this repo no longer has. Under this architecture the
release job itself IS the transaction: it drops verified ``.pkg`` assets straight into
``site_root/<channel>/<varver>/``, calls ``regenerate_catalogue`` to rebuild
that one directory from whatever is now present, and commits ``site_root`` to
``main`` — a failed run simply never reaches that commit, so there is nothing
here to roll back.

The caller supplies the pkg-local engine. This module adds retention,
slower-channel backfill, and multi-destination identity checks around direct
catalogue emission.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

import catalogue_engine
import pfb_pkg


class CatalogueAssemblyError(Exception):
    """A validation or post-condition failure detected by catalogue assembly."""


# The closed set of channels this repo's catalogue tree ever serves. Not derived from
# publish_catalogues.py's destination tuples (those are per-run subsets a tagged/nightly
# intake may target); this is the full universe a (channel, varver) pair must belong to.
_KNOWN_CHANNELS: frozenset[str] = frozenset({"stable", "testing", "edge", "nightly"})

# NAME_MAX on every mainstream filesystem (ext4, APFS, HFS+, most FUSE ports) is 255
# bytes/codepoints. The engine's own _validate_catalog_name enforces character shape
# only (its regex has no length cap) — an accepted-but-oversized varver would still be
# an unusable directory component on the filesystem that ultimately serves it. This
# guard is this module's own; the engine has no reason to know about NAME_MAX.
_MAX_VARVER_LENGTH = 255

# pkg picks the FIRST candidate a repository lists, not the newest (#3390): "3.3.10" <
# "3.3.9" as strings, so a tagged catalogue lists exactly one version. Nightly is out
# of scope for #3390 and keeps its five newest builds.
NIGHTLY_RETENTION_KEEP = 5
TAGGED_RETENTION_KEEP = 1

# Containment order (slower -> faster): a faster channel may list any slower channel's
# build (issue #2147's four-channel model). Nightly is untagged and independent of the
# tagged channels — no containment either direction. Keys MUST
# equal _KNOWN_CHANNELS; the assertion right below is the import-time pin, and
# SlowerChannelsConsistencyTests in tests/test_catalogue_assembly.py is the test-time one.
_SLOWER_CHANNELS: dict[str, tuple[str, ...]] = {
    "stable": (),
    "testing": ("stable",),
    "edge": ("stable", "testing"),
    "nightly": (),
}
assert set(_SLOWER_CHANNELS) == _KNOWN_CHANNELS, (
    "_SLOWER_CHANNELS must cover every _KNOWN_CHANNELS entry"
)


def _validate_channel(channel: str) -> None:
    if channel not in _KNOWN_CHANNELS:
        raise CatalogueAssemblyError(
            f"unknown channel {channel!r}: must be one of {sorted(_KNOWN_CHANNELS)!r}"
        )


def _validate_varver(varver: str) -> None:
    if len(varver) > _MAX_VARVER_LENGTH:
        raise CatalogueAssemblyError(
            f"varver exceeds {_MAX_VARVER_LENGTH} characters ({len(varver)}): {varver!r}"
        )
    brp = catalogue_engine
    try:
        brp._validate_catalog_name(varver, single_segment=True)
    except brp.BuildRepoError as exc:
        raise CatalogueAssemblyError(f"invalid varver {varver!r}: {exc}") from exc


def _catalogue_dir(site_root: Path, channel: str, varver: str) -> Path:
    """Validate ``channel``/``varver`` and return the existing catalogue directory.

    Everything checked here runs before catalogue emission; a rejected
    (channel, varver) touches nothing beyond this existence check. A missing
    directory (including the case where ``site_root`` itself is not a directory —
    ``Path.is_dir()`` returns ``False`` rather than raising for any invalid
    ancestor) is a hard error: this module never creates the catalogue directory
    itself, the caller (the release job) owns dropping assets into place first.
    """
    _validate_channel(channel)
    _validate_varver(varver)
    catalogue_dir = Path(site_root) / channel / varver
    if not catalogue_dir.is_dir():
        raise CatalogueAssemblyError(
            f"{channel}/{varver}: catalogue directory does not exist: {catalogue_dir}"
        )
    return catalogue_dir


def regenerate_catalogue(
    site_root: str | Path,
    channel: str,
    varver: str,
    *,
    sign_key: Path | None = None,
) -> None:
    """Rebuild one catalogue from its current installable package pool."""
    site_root = Path(site_root)
    catalogue_dir = _catalogue_dir(site_root, channel, varver)
    brp = catalogue_engine
    pool = sorted(
        p
        for p in catalogue_dir.glob("*.pkg")
        if p.is_file() and p.name not in brp._CATALOG_PKG_FILES
    )
    if not pool:
        raise CatalogueAssemblyError(f"{channel}/{varver}: empty pool")

    brp.emit_catalog(catalogue_dir, pool, root=site_root, sign_key=sign_key)


def _canonical_version(path: Path, manifest: Mapping[str, object]) -> str:
    version = manifest.get("version")
    if not isinstance(version, str):
        raise CatalogueAssemblyError(
            f"{path}: canonical package manifest version must be a string"
        )
    return version


def _files_byte_identical(a: Path, b: Path) -> bool:
    """True iff ``a`` and ``b`` hold the same bytes. Size first — a new build
    almost always differs there, so most calls never read either file."""
    if a.stat().st_size != b.stat().st_size:
        return False
    return a.read_bytes() == b.read_bytes()


def _canonical_filename(version: str) -> str:
    return f"{pfb_pkg.CANONICAL_EMITTED_IDENTITY}-{version}.pkg"


def _iter_canonical_packages(catalogue_dir: Path) -> list[tuple[Path, str]]:
    """Canonical ``.pkg`` files in ``catalogue_dir`` as ``(path, version)``.

    Catalog descriptor files and non-canonical (dependency) packages are skipped,
    so a dependency is never counted, pruned, or copied as a containment source.
    """
    brp = catalogue_engine
    found: list[tuple[Path, str]] = []
    for path in sorted(catalogue_dir.glob("*.pkg")):
        if not path.is_file() or path.name in brp._CATALOG_PKG_FILES:
            continue
        manifest = pfb_pkg.read_compact_manifest(path)
        if manifest.get("name") != pfb_pkg.CANONICAL_EMITTED_IDENTITY:
            continue
        found.append((path, _canonical_version(path, manifest)))
    return found


def _copy_declared_dependencies(src: Path, dest_dir: Path) -> None:
    """Copy the dependency ``.pkg`` files the canonical build ``src`` declares (manifest
    ``deps``; file ``<name>-<version>.pkg``) from ``src``'s own catalogue into
    ``dest_dir``, so a lifted build never lands without the packages it needs.

    A dependency's identity is its filename, so one already at the destination is left
    exactly as it is. A declared package ``src``'s catalogue does not hold (pfSense's own
    repositories serve it) is skipped, as is any name that would leave the catalogue.
    """
    deps = pfb_pkg.read_compact_manifest(src).get("deps")
    if not isinstance(deps, dict):
        return
    for name, dep in deps.items():
        version = dep.get("version") if isinstance(dep, dict) else None
        if not isinstance(version, str):
            continue
        filename = f"{name}-{version}.pkg"
        if Path(filename).name != filename:
            continue
        source, target = src.parent / filename, dest_dir / filename
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)


def newest_eligible_version(
    site_root: str | Path, channel: str, varver: str
) -> str | None:
    """Newest canonical version (``pfb_pkg.pkg_version_sort_key``) the ``channel``
    catalogue for ``varver`` may list: one already on ``channel`` or on one of its
    slower channels (``_SLOWER_CHANNELS``) for the same ``varver``. ``None`` when
    nothing is published; a missing channel directory contributes nothing.
    """
    _validate_channel(channel)
    _validate_varver(varver)
    site_root = Path(site_root)
    versions = [
        version
        for source in (channel, *_SLOWER_CHANNELS[channel])
        if (source_dir := site_root / source / varver).is_dir()
        for _path, version in _iter_canonical_packages(source_dir)
    ]
    return max(versions, key=pfb_pkg.pkg_version_sort_key, default=None)


def backfill_from_slower_channels(
    site_root: str | Path,
    channel: str,
    varver: str,
) -> dict[Path, list[tuple[str, str]]]:
    """Copy the newest canonical package a slower tagged channel carries for this
    ``varver`` onto ``channel`` (byte-identical), unless ``channel`` already has
    that build or a newer one. Older slower builds are never copied: a tagged
    catalogue lists exactly one version (#3390), so ``prune_retained`` would drop
    them again. The dependency ``.pkg`` files the copied build declares come with it
    when the destination lacks them (``_copy_declared_dependencies``).

    Nightly is untagged and independent: this function never copies from it or
    into it (``_SLOWER_CHANNELS["nightly"]`` is empty, and a nightly destination
    returns immediately). A same-name, different-byte collision — dest vs source,
    or two slower sources disagreeing with each other — is a hard error.

    Returns a ``source_index`` fragment: the copied source path maps to the
    ``(channel, varver)`` destinations that now hold those bytes (every slower
    origin that carried the package, plus ``channel``). An already-identical
    destination is left untouched and yields ``{}``.
    """
    site_root = Path(site_root)
    if channel == "nightly":
        _validate_channel(channel)
        _validate_varver(varver)
        return {}

    dest_dir = _catalogue_dir(site_root, channel, varver)
    # version -> first source, then every slower channel that carries it
    first_source: dict[str, Path] = {}
    origins: dict[str, list[str]] = {}
    for slower_channel in _SLOWER_CHANNELS[channel]:
        if slower_channel == "nightly":
            continue
        slower_dir = site_root / slower_channel / varver
        if not slower_dir.is_dir():
            continue
        for path, version in _iter_canonical_packages(slower_dir):
            existing = first_source.get(version)
            if existing is not None and not _files_byte_identical(existing, path):
                raise CatalogueAssemblyError(
                    f"{_canonical_filename(version)}: slower channels disagree on bytes for {varver} — "
                    f"{existing} sha256={hashlib.sha256(existing.read_bytes()).hexdigest()}, "
                    f"{path} sha256={hashlib.sha256(path.read_bytes()).hexdigest()}"
                )
            first_source.setdefault(version, path)
            origins.setdefault(version, []).append(slower_channel)
    if not first_source:
        return {}

    newest = max(first_source, key=pfb_pkg.pkg_version_sort_key)
    own_newest = max(
        (version for _path, version in _iter_canonical_packages(dest_dir)),
        key=pfb_pkg.pkg_version_sort_key,
        default=None,
    )
    if own_newest is not None and pfb_pkg.pkg_version_sort_key(
        newest
    ) < pfb_pkg.pkg_version_sort_key(own_newest):
        return {}

    src = first_source[newest]
    dest = dest_dir / _canonical_filename(newest)
    if dest.is_file():
        if _files_byte_identical(dest, src):
            return {}
        raise CatalogueAssemblyError(
            f"{dest}: already publishes a different build of {dest.name} — "
            f"existing sha256={hashlib.sha256(dest.read_bytes()).hexdigest()}, "
            f"incoming sha256={hashlib.sha256(src.read_bytes()).hexdigest()}"
        )
    shutil.copy2(src, dest)
    _copy_declared_dependencies(src, dest_dir)
    destinations = [(slower, varver) for slower in origins[newest]]
    destinations.append((channel, varver))
    return {src.resolve(): destinations}


def prune_retained(
    site_root: str | Path,
    channel: str,
    varver: str,
) -> tuple[Path, ...]:
    """Delete every CANONICAL ``.pkg`` in ``site_root/channel/varver`` beyond the
    newest generations kept, newest-first by ``pfb_pkg.pkg_version_sort_key``:
    ``TAGGED_RETENTION_KEEP`` on stable/testing/edge, ``NIGHTLY_RETENTION_KEEP`` on
    nightly. Returns the deleted paths.

    Only this catalogue's own files count. A slower channel serving an older build
    does NOT keep it here: that would leave a stale second version, which pkg may
    pick over the newest (#3390). A newer slower build becomes this catalogue's
    newest through ``backfill_from_slower_channels``, which runs first.

    Scoped to the canonical package only (manifest ``name`` ==
    ``pfb_pkg.CANONICAL_EMITTED_IDENTITY``): a dependency ``.pkg`` sitting in the
    same directory (e.g. ``py311-charset-normalizer-3.4.0.pkg``) has no
    independent retention count of its own and is never touched here, and
    neither is the catalog's own ``data.pkg``/``packagesite.pkg``. Call this
    BEFORE ``regenerate_catalogue`` so an evicted generation never reaches the
    rebuilt catalog; never mutates ``.pkg`` bytes, only removes whole files.
    """
    site_root = Path(site_root)
    catalogue_dir = _catalogue_dir(site_root, channel, varver)
    keep = NIGHTLY_RETENTION_KEEP if channel == "nightly" else TAGGED_RETENTION_KEEP
    canonical = sorted(
        _iter_canonical_packages(catalogue_dir),
        key=lambda item: pfb_pkg.pkg_version_sort_key(item[1]),
        reverse=True,
    )
    evicted = tuple(path for path, _version in canonical[keep:])
    for path in evicted:
        path.unlink()
    return evicted


def verify_multi_destination_identity(
    site_root: str | Path,
    source_index: Mapping[Path, Sequence[tuple[str, str]]],
) -> None:
    """Hard-fail if a fanned-out source's emitted bytes/checksum/record ever diverge.

    ``source_index`` maps a resolved source ``.pkg`` path to every
    ``(channel, varver)`` catalogue it was dropped into (a caller's own
    bookkeeping — this module has no ledger to derive it from). A source
    appearing at only one destination is not a fan-out and is skipped. Every
    listed destination is expected to already be a REGENERATED catalogue under
    ``site_root`` — this reads the real, published directories, not a scratch
    tree; there is no scratch tree in this design.

    Every destination must carry the source package's exact bytes and provenance;
    any divergence means the fan-out or catalogue write selected the wrong input.
    """
    site_root = Path(site_root)
    brp = catalogue_engine
    for source_path, destinations in source_index.items():
        if len(destinations) < 2:
            continue
        manifest = pfb_pkg.read_compact_manifest(source_path)
        canonical_name = (
            f"{manifest['name']}-{_canonical_version(source_path, manifest)}.pkg"
        )

        baseline = (
            hashlib.sha256(source_path.read_bytes()).hexdigest(),
            brp._canonical_build_record(source_path, manifest),
        )
        for channel, varver in destinations:
            dest_path = site_root / channel / varver / canonical_name
            if not dest_path.is_file():
                raise CatalogueAssemblyError(
                    f"{canonical_name}: missing at destination {channel}/{varver}, "
                    f"expected from fan-out of {source_path}"
                )
            # sha256 alone already detects any byte divergence — keeping the raw
            # bytes of every destination copy alive in `baseline` is wasted memory
            # for a fan-out that can span every varver on the tree.
            sha256 = hashlib.sha256(dest_path.read_bytes()).hexdigest()
            dest_manifest = pfb_pkg.read_compact_manifest(dest_path)
            _canonical_version(dest_path, dest_manifest)
            record = brp._canonical_build_record(dest_path, dest_manifest)
            current = (sha256, record)
            if current != baseline:
                raise CatalogueAssemblyError(
                    f"{canonical_name}: multi-destination identity violation across "
                    f"{destinations!r} — bytes/sha256/provenance record diverged"
                )
