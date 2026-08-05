"""Assemble the four disjoint channel catalogues atomically from a resolved plan.

Scope (issue #2146 step S3): given a ``Plan`` whose retention has ALREADY been decided
by the caller (S4's ledger), stage each ``(channel, varver)`` catalogue's pool +
dependency ``.pkg`` files into a private directory and drive the pfBlockerNG source
repo's own ``build_repo`` engine once per catalogue. No intake parsing, no ledger, no
git, no network — S1 (``publish_catalogues.py``), S2, and S4 own those. This module
never imports ``catalogue_state``: it knows nothing about the ledger's shape.

stdlib-only, Python 3.11. The engine is loaded by the caller via
``publish_catalogues.load_engine`` and handed in as the already-built ``Engine`` —
this module never loads it a second time. Emission (manifest reading, ABI/collision
checks, the catalog descriptor) is entirely the engine's ``build_repo``; this module
adds only staging, whole-tree atomicity, and the multi-destination byte/checksum/
provenance identity post-condition build_repo itself has no reason to know about.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from publish_catalogues import Engine


class CatalogueAssemblyError(Exception):
    """A plan/staging/post-condition failure this module itself detected.

    Engine errors (``BuildRepoError`` from a mixed/concrete ABI or a name/version
    collision, ``PkgError`` from a corrupt archive) propagate UNWRAPPED — this module
    never re-derives a check ``build_repo``/``_emit_catalog_from_paths`` already makes.
    """


# The closed set of channels this repo's catalogue tree ever serves. Not derived from
# publish_catalogues.py's destination tuples (those are per-run subsets a tagged/nightly
# intake may target); this is the full universe a Plan entry's channel must belong to.
_KNOWN_CHANNELS: frozenset[str] = frozenset({"stable", "testing", "edge", "nightly"})

# NAME_MAX on every mainstream filesystem (ext4, APFS, HFS+, most FUSE ports) is 255
# bytes/codepoints. The engine's own _validate_catalog_name enforces character shape
# only (its regex has no length cap) — an accepted-but-oversized varver would still be
# an unusable directory component on the filesystem that ultimately serves it. This
# guard is this module's own; the engine has no reason to know about NAME_MAX.
_MAX_VARVER_LENGTH = 255


@dataclass(frozen=True)
class CatalogueTarget:
    """One resolved ``(channel, varver)`` catalogue: its pool + matching dependencies.

    ``pool``/``dependencies`` are ordered and ALREADY retention-applied by the caller —
    this module never prunes, sorts by version, or decides what belongs. Both are
    ``.pkg`` paths staged into the same catalogue directory; ``dependencies`` is kept
    as a separate field only because the caller resolves it separately (e.g. by ABI
    match against a ROUTE row), not because ``build_repo`` treats the two differently.
    """

    channel: str
    varver: str
    pool: tuple[Path, ...]
    dependencies: tuple[Path, ...] = ()

    @property
    def key(self) -> tuple[str, str]:
        return (self.channel, self.varver)

    @property
    def catalog_name(self) -> str:
        return f"{self.channel}/{self.varver}"


@dataclass(frozen=True)
class Plan:
    """An ordered, already-resolved set of catalogue targets.

    Rendered as an ordered sequence of ``CatalogueTarget`` records (each carrying its
    own ``(channel, varver)`` key) rather than a literal ``dict`` — this keeps the
    dataclass trivially frozen/hashable-free while still being, in effect, the ordered
    mapping the design calls for: ``targets[i].key`` is the key, ``targets[i]`` is the
    value.
    """

    targets: tuple[CatalogueTarget, ...]


def assemble(plan: Plan, out_dir: str | Path, engine: Engine) -> None:
    """Assemble every catalogue in ``plan`` and publish atomically to ``out_dir``.

    Assembles the WHOLE tree into a scratch root first (via the engine's own
    ``build_repo``, one call per catalogue — never move/rename/mutate a caller path).
    Only once every catalogue has emitted successfully, AND the multi-destination
    identity post-condition holds, does anything land in ``out_dir``. Any failure
    before that point leaves ``out_dir`` exactly as it was.
    """
    out_dir = Path(out_dir)
    if out_dir.exists() and not out_dir.is_dir():
        raise CatalogueAssemblyError(
            f"out_dir exists and is not a directory: {out_dir}"
        )

    _validate_plan(plan, engine)
    source_index = _build_source_index(plan)
    brp = engine.build_repo_portable

    with tempfile.TemporaryDirectory(prefix="catalogue-assembly-") as scratch_str:
        scratch = Path(scratch_str)
        tree_root = scratch / "tree"
        tree_root.mkdir()
        for i, target in enumerate(plan.targets):
            stage_dir = scratch / "stage" / f"{i:04d}"
            _stage((*target.pool, *target.dependencies), stage_dir)
            brp.build_repo(stage_dir, tree_root, catalog_name=target.catalog_name)

        _verify_multi_destination_identity(engine, tree_root, source_index)
        _publish(tree_root, out_dir, plan)


def _validate_plan(plan: Plan, engine: Engine) -> None:
    """Reject an unsafe/malformed plan before any filesystem work happens.

    Everything checked here is checked BEFORE the scratch tree exists — a rejected
    plan touches no filesystem at all, which is what makes the atomicity guarantee
    hold even for these hostile-input rows.
    """
    if not plan.targets:
        raise CatalogueAssemblyError("plan has no catalogue targets")

    brp = engine.build_repo_portable
    seen: set[tuple[str, str]] = set()
    for target in plan.targets:
        if target.channel not in _KNOWN_CHANNELS:
            raise CatalogueAssemblyError(
                f"unknown channel {target.channel!r}: must be one of {sorted(_KNOWN_CHANNELS)!r}"
            )
        if len(target.varver) > _MAX_VARVER_LENGTH:
            raise CatalogueAssemblyError(
                f"varver exceeds {_MAX_VARVER_LENGTH} characters ({len(target.varver)}): {target.varver!r}"
            )
        try:
            brp._validate_catalog_name(target.varver, single_segment=True)
        except brp.BuildRepoError as exc:
            raise CatalogueAssemblyError(
                f"invalid varver {target.varver!r}: {exc}"
            ) from exc

        if target.key in seen:
            raise CatalogueAssemblyError(
                f"duplicate catalogue target in plan: {target.key!r}"
            )
        seen.add(target.key)

        if not target.pool:
            raise CatalogueAssemblyError(f"{target.catalog_name}: empty pool")

        _validate_paths_exist((*target.pool, *target.dependencies), target.catalog_name)


def _validate_paths_exist(paths: Sequence[Path], catalog_name: str) -> None:
    for path in paths:
        # is_file() is False for both a missing path and a directory — one check
        # rejects both hostile rows without a separate exists()/is_dir() pair.
        if not path.is_file():
            raise CatalogueAssemblyError(
                f"{catalog_name}: pool/dependency path does not exist or is not a file: {path}"
            )


def _build_source_index(plan: Plan) -> dict[Path, list[tuple[str, str]]]:
    """Map each resolved source path to every ``(channel, varver)`` it was staged into.

    Built from the plan alone, before any staging — used only by
    ``_verify_multi_destination_identity`` after assembly to find which emitted
    packages must be byte-identical across destinations (fan-out / shared FreeBSD
    major). A path appearing once is not a fan-out and is skipped there.
    """
    index: dict[Path, list[tuple[str, str]]] = {}
    for target in plan.targets:
        for path in (*target.pool, *target.dependencies):
            index.setdefault(path.resolve(), []).append(target.key)
    return index


def _stage(paths: Sequence[Path], stage_dir: Path) -> None:
    """Link (or copy) ``paths`` into ``stage_dir`` under synthetic ``NNNNNN.pkg`` names.

    Never touches the caller's original files. A synthetic name (rather than the
    source's own basename) sidesteps two problems at once: two different source
    directories can legitimately contain same-named files, and ``build_repo`` globs
    ``in_dir`` for ``*.pkg`` — a caller path without that suffix would otherwise be
    silently skipped instead of staged. ``os.link`` preserves the source mtime for
    free (same inode); the ``shutil.copy2`` fallback preserves it explicitly for a
    cross-device source.
    """
    stage_dir.mkdir(parents=True, exist_ok=True)
    for idx, path in enumerate(paths):
        target = stage_dir / f"{idx:06d}.pkg"
        try:
            os.link(path, target)
        except OSError:
            shutil.copy2(path, target)


def _verify_multi_destination_identity(
    engine: Engine,
    tree_root: Path,
    source_index: Mapping[Path, list[tuple[str, str]]],
) -> None:
    """Hard-fail if a fanned-out source's emitted bytes/checksum/record ever diverge.

    Every multi-destination source is staged from the SAME file into every one of its
    destinations, and ``build_repo`` writes staged bytes verbatim — so a divergence
    here means something upstream of this check went wrong (wrong path staged, a
    canonical-name collision overwritten by the wrong package, ...). This is the
    ticket's byte/checksum/provenance identity requirement as an executable
    post-condition, not only a test assertion.
    """
    pfb_pkg = engine.pfb_pkg
    brp = engine.build_repo_portable
    for source_path, destinations in source_index.items():
        if len(destinations) < 2:
            continue
        manifest = pfb_pkg.read_compact_manifest(source_path)
        canonical_name = f"{manifest['name']}-{manifest['version']}.pkg"

        baseline: tuple[bytes, str, object] | None = None
        for channel, varver in destinations:
            dest_path = tree_root / channel / varver / canonical_name
            if not dest_path.is_file():
                raise CatalogueAssemblyError(
                    f"{canonical_name}: missing at destination {channel}/{varver}, "
                    f"expected from fan-out of {source_path}"
                )
            data = dest_path.read_bytes()
            sha256 = hashlib.sha256(data).hexdigest()
            dest_manifest = pfb_pkg.read_compact_manifest(dest_path)
            record = brp._canonical_build_record(dest_path, dest_manifest)
            current = (data, sha256, record)
            if baseline is None:
                baseline = current
                continue
            if current != baseline:
                raise CatalogueAssemblyError(
                    f"{canonical_name}: multi-destination identity violation across "
                    f"{destinations!r} — bytes/sha256/provenance record diverged"
                )


def _publish(tree_root: Path, out_dir: Path, plan: Plan) -> None:
    """Move every assembled catalogue from the scratch tree into ``out_dir``.

    Only ever called after every catalogue in ``plan`` built successfully and the
    multi-destination identity check passed — nothing here can run for a failed
    assembly. Each catalogue's prior directory (if any) is removed immediately before
    its replacement is moved in, so a catalogue not present in ``plan`` is never
    touched.
    """
    for target in plan.targets:
        src = tree_root / target.channel / target.varver
        dest = out_dir / target.channel / target.varver
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(src), str(dest))
