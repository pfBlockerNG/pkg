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
adds only staging, a recoverable publish step, and the multi-destination byte/
checksum/provenance identity post-condition build_repo itself has no reason to know
about. Publishing is RECOVERABLE, not atomic (see ``assemble``'s docstring for the
precise guarantee and its one honest gap) — the durable transaction boundary for the
published site is the caller's (S4's) git commit of ``out_dir``, not anything this
module does on disk.
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
    """Assemble every catalogue in ``plan`` and publish it into ``out_dir``.

    Phase 1 — build. The WHOLE tree is assembled into a scratch root first (via the
    engine's own ``build_repo``, one call per catalogue — never move/rename/mutate a
    caller path), staged on the SAME filesystem as ``out_dir`` (a sibling of
    ``out_dir``'s parent) so every move in phase 2 below is a single ``rename``
    syscall rather than a cross-device copy. Nothing reaches ``out_dir`` until every
    catalogue has built successfully AND the multi-destination identity
    post-condition holds.

    Phase 2 — publish. This is RECOVERABLE, not atomic: each target is published one
    rename at a time (existing content, if any, moved aside to a sibling backup, then
    the new content renamed into place). If every target lands, the backups are
    discarded. If any single step fails partway through the plan, every target this
    call already published is rolled back — restored from its backup, or removed if
    it was newly created (no prior backup) — before the exception is re-raised. So a
    failure ANYWHERE (phase 1, phase 2, or the identity check between them) leaves
    ``out_dir`` byte-identical to its state when this call started, including the
    case where the failing step is a target's own replace-move, after that same
    target's aside-to-backup move already ran.

    The one honest gap: this guarantee assumes the rollback's OWN moves succeed. A
    fault DURING rollback itself (e.g. the disk fills exactly there) is an
    unrecoverable double-fault no amount of application-level bookkeeping can paper
    over on a non-transactional filesystem; it raises rather than silently claiming
    success. This module also commits nothing durable itself — the durable
    transaction boundary for the published site is the caller's (S4's) git commit of
    ``out_dir``. A run that raises here never reaches that commit, so the
    *repository* is unaffected regardless; the rollback above exists to keep a
    live/served ``out_dir`` correct too, for the window before that commit happens.
    """
    out_dir = Path(out_dir)
    if out_dir.exists() and not out_dir.is_dir():
        raise CatalogueAssemblyError(
            f"out_dir exists and is not a directory: {out_dir}"
        )

    _validate_plan(plan, engine)
    source_index = _build_source_index(plan)
    brp = engine.build_repo_portable

    # dir=out_dir.parent (not the platform temp root): the publish step below moves
    # everything from this scratch tree into out_dir, and staying on the SAME
    # filesystem turns every one of those moves into a single atomic os.rename
    # instead of a cross-device copy+delete that can fail midway and leave a partial
    # directory on either side.
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="catalogue-assembly-", dir=out_dir.parent
    ) as scratch_str:
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


_BACKUP_SUFFIX = ".catalogue-assembly-backup"


def _publish(tree_root: Path, out_dir: Path, plan: Plan) -> None:
    """Move every assembled catalogue from the scratch tree into ``out_dir``.

    Only ever called after every catalogue in ``plan`` built successfully and the
    multi-destination identity check passed. Recoverable, not atomic (see
    ``assemble``'s docstring for the full guarantee): each target with existing
    content is moved aside to a sibling backup before its replacement is moved in;
    a target with no prior content is tracked as freshly created instead. On any
    failure, every target already published in THIS call is rolled back — in
    reverse order — before the exception is re-raised, so a catalogue not present in
    ``plan`` is never touched, and one present in ``plan`` ends up exactly as it was
    if this call does not fully succeed.
    """
    backups: list[tuple[Path, Path]] = []
    created: list[Path] = []
    try:
        for target in plan.targets:
            src = tree_root / target.channel / target.varver
            dest = out_dir / target.channel / target.varver
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                backup = dest.parent / f".{dest.name}{_BACKUP_SUFFIX}"
                if backup.exists():
                    shutil.rmtree(backup)
                shutil.move(str(dest), str(backup))
                backups.append((dest, backup))
            else:
                created.append(dest)
            shutil.move(str(src), str(dest))
    except Exception:
        for dest in reversed(created):
            if dest.exists():
                shutil.rmtree(dest)
        for dest, backup in reversed(backups):
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(backup), str(dest))
        raise
    else:
        for _dest, backup in backups:
            shutil.rmtree(backup)
