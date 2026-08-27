#!/usr/bin/env python3
"""Retain the consumed Nightly package version and delete its proven predecessors.

GitHub Packages refuses to delete a package's last tagged version, so the
consumed Nightly version is kept as the package's anchor and rollback receipt
(pfBlockerNG/pfBlockerNG#2752, owner ruling: bounded one-success retention).
Cleanup deletes only the successful versions that pkg `main` proves came
before it, leaving exactly one successful version behind.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_RUN_ID = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")
_VERSION = re.compile(r"^[0-9]{14}\.[0-9a-f]{7}$")
_ARTIFACT_REF = re.compile(
    r"^ghcr\.io/pfblockerng/pfblockerng-nightly@sha256:[0-9a-f]{64}$"
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PUBLICATION_REF = "refs/remotes/origin/main"
_VERSION_TRAILER = "pfBlockerNG-Nightly-Version"
_RUN_ID_TRAILER = "pfBlockerNG-Source-Run-Id"
_ARTIFACT_REF_TRAILER = "pfBlockerNG-Nightly-Artifact-Ref"
_RECEIPT_TRAILERS = (_VERSION_TRAILER, _RUN_ID_TRAILER, _ARTIFACT_REF_TRAILER)
_PACKAGE_VERSIONS_ENDPOINT = (
    "orgs/pfBlockerNG/packages/container/pfblockerng-nightly/versions"
)
_ACTIVE_PACKAGE_VERSIONS_ENDPOINT = (
    f"{_PACKAGE_VERSIONS_ENDPOINT}?per_page=100&state=active"
)

# (digest, Nightly version) of one successful publication.
Identity = tuple[str, str]


class PublicationReceiptError(ValueError):
    """No committed publication matches the cleanup request."""


def _receipt_identity(receipt: dict[str, str]) -> Identity:
    version = receipt[_VERSION_TRAILER]
    artifact_ref = receipt[_ARTIFACT_REF_TRAILER]
    if not _VERSION.fullmatch(version) or not _ARTIFACT_REF.fullmatch(artifact_ref):
        raise PublicationReceiptError(
            "malformed prior Nightly publication receipt in pkg history"
        )
    return artifact_ref.rsplit("@", 1)[1], version


def verify_publication(
    repo: str | Path, *, source_run_id: str, nightly_version: str, artifact_ref: str
) -> list[Identity]:
    """Return the successful publications committed before the consumed one.

    Raises unless pkg `main` records the exact cleanup identity. The returned
    predecessors are ordered newest first and come only from committed
    receipts, never from registry timestamps, so a publication that never
    reached pkg `main` is never treated as successful.
    """
    if not _RUN_ID.fullmatch(source_run_id):
        raise PublicationReceiptError("source_run_id is malformed")
    if not _VERSION.fullmatch(nightly_version):
        raise PublicationReceiptError("nightly_version is malformed")
    if not _ARTIFACT_REF.fullmatch(artifact_ref):
        raise PublicationReceiptError(
            "artifact_ref is not an exact Nightly digest reference"
        )
    proc = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%B%x00", _PUBLICATION_REF],
        check=False,
        capture_output=True,
        text=True,
        env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    if proc.returncode != 0:
        raise PublicationReceiptError(
            f"cannot read pkg publication history at {_PUBLICATION_REF}: "
            f"{proc.stderr.strip()}"
        )
    consumed = {
        _VERSION_TRAILER: nightly_version,
        _RUN_ID_TRAILER: source_run_id,
        _ARTIFACT_REF_TRAILER: artifact_ref,
    }
    receipts: list[dict[str, str]] = []
    for message in proc.stdout.split("\0"):
        trailers: dict[str, str] = {}
        for line in message.splitlines():
            key, separator, value = line.partition(": ")
            if separator and key in _RECEIPT_TRAILERS:
                if key in trailers:
                    raise PublicationReceiptError(
                        f"duplicate Nightly publication trailer: {key}"
                    )
                trailers[key] = value
        if len(trailers) == len(_RECEIPT_TRAILERS):
            receipts.append(trailers)
    for index, receipt in enumerate(receipts):
        if receipt == consumed:
            return [_receipt_identity(prior) for prior in receipts[index + 1 :]]
    raise PublicationReceiptError(
        "no committed Nightly publication matches the cleanup identity"
    )


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _active_package_versions() -> list[tuple[int, str, list[str]]]:
    proc = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            _ACTIVE_PACKAGE_VERSIONS_ENDPOINT,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise PublicationReceiptError(
            f"cannot list Nightly package versions: {proc.stderr.strip()}"
        )
    try:
        pages = json.loads(proc.stdout, object_pairs_hook=_reject_duplicate_json_keys)
    except ValueError as exc:
        raise PublicationReceiptError(
            f"malformed GitHub Packages version response: {exc}"
        ) from exc
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise PublicationReceiptError(
            "malformed GitHub Packages version response: expected pages of versions"
        )

    versions: list[tuple[int, str, list[str]]] = []
    seen_ids: set[int] = set()
    for row in (row for page in pages for row in page):
        if not isinstance(row, dict):
            raise PublicationReceiptError(
                "malformed GitHub Packages version response: version is not an object"
            )
        version_id = row.get("id")
        name = row.get("name")
        metadata = row.get("metadata")
        if (
            not isinstance(version_id, int)
            or isinstance(version_id, bool)
            or version_id < 1
            or not isinstance(name, str)
            or not _DIGEST.fullmatch(name)
            or not isinstance(metadata, dict)
            or metadata.get("package_type") != "container"
        ):
            raise PublicationReceiptError("malformed GitHub Packages version metadata")
        if version_id in seen_ids:
            raise PublicationReceiptError(
                "contradictory Nightly package version metadata: duplicate id"
            )
        seen_ids.add(version_id)
        container = metadata.get("container")
        tags = container.get("tags") if isinstance(container, dict) else None
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise PublicationReceiptError("malformed GitHub Packages container tags")
        versions.append((version_id, name, tags))
    return versions


def _prior_version_ids(
    versions: list[tuple[int, str, list[str]]],
    *,
    consumed: Identity,
    priors: list[Identity],
) -> list[int]:
    """Resolve which active version ids the priors own, retaining the consumed one.

    Every version the receipts do not account for — a failed or unconsumed
    input, or any artifact that is not a successful Nightly — is left alone.
    """
    version_by_digest: dict[str, str] = {}
    digest_by_version: dict[str, str] = {}
    for digest, version in (consumed, *priors):
        if (
            version_by_digest.setdefault(digest, version) != version
            or digest_by_version.setdefault(version, digest) != digest
        ):
            raise PublicationReceiptError(
                "contradictory Nightly publication receipts share one identity"
            )

    ids_by_digest: dict[str, list[int]] = {}
    for version_id, name, tags in versions:
        expected_tag = version_by_digest.get(name)
        if expected_tag is not None and expected_tag not in tags:
            raise PublicationReceiptError(
                "contradictory Nightly package version metadata: "
                "published digest is missing its version tag"
            )
        if any(digest_by_version.get(tag, name) != name for tag in tags):
            raise PublicationReceiptError(
                "contradictory Nightly package version metadata: "
                "published version tag is on a different digest"
            )
        if expected_tag is not None:
            ids_by_digest.setdefault(name, []).append(version_id)

    consumed_ids = ids_by_digest.get(consumed[0], [])
    if len(consumed_ids) != 1:
        raise PublicationReceiptError(
            "expected exactly one active Nightly package version matching "
            f"digest and tag, found {len(consumed_ids)}"
        )
    targets: list[int] = []
    for digest, _version in priors:
        prior_ids = ids_by_digest.get(digest, [])
        if len(prior_ids) > 1:
            raise PublicationReceiptError(
                "contradictory Nightly package version metadata: "
                "duplicate rows for one prior successful version"
            )
        targets.extend(prior_ids)
    return targets


def cleanup(
    repo: str | Path, *, source_run_id: str, nightly_version: str, artifact_ref: str
) -> None:
    priors = verify_publication(
        repo,
        source_run_id=source_run_id,
        nightly_version=nightly_version,
        artifact_ref=artifact_ref,
    )
    consumed = (artifact_ref.rsplit("@", 1)[1], nightly_version)
    for version_id in _prior_version_ids(
        _active_package_versions(), consumed=consumed, priors=priors
    ):
        proc = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "DELETE",
                f"{_PACKAGE_VERSIONS_ENDPOINT}/{version_id}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise PublicationReceiptError(
                "cannot delete prior Nightly package version "
                f"{version_id}: {proc.stderr.strip()}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--nightly-version", required=True)
    parser.add_argument("--artifact-ref", required=True)
    args = parser.parse_args()
    try:
        cleanup(
            args.repo,
            source_run_id=args.source_run_id,
            nightly_version=args.nightly_version,
            artifact_ref=args.artifact_ref,
        )
    except PublicationReceiptError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
