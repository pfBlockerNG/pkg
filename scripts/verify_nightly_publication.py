#!/usr/bin/env python3
"""Verify the exact Nightly publication before deleting its package version."""

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
_PUBLICATION_REF = "refs/remotes/origin/main"
_PACKAGE_VERSIONS_ENDPOINT = (
    "orgs/pfBlockerNG/packages/container/pfblockerng-nightly/versions"
)
_ACTIVE_PACKAGE_VERSIONS_ENDPOINT = (
    f"{_PACKAGE_VERSIONS_ENDPOINT}?per_page=100&state=active"
)


class PublicationReceiptError(ValueError):
    """No committed publication matches the cleanup request."""


def verify_publication(
    repo: str | Path, *, source_run_id: str, nightly_version: str, artifact_ref: str
) -> None:
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
    expected = {
        "pfBlockerNG-Nightly-Version": nightly_version,
        "pfBlockerNG-Source-Run-Id": source_run_id,
        "pfBlockerNG-Nightly-Artifact-Ref": artifact_ref,
    }
    for message in proc.stdout.split("\0"):
        trailers = {}
        for line in message.splitlines():
            key, separator, value = line.partition(": ")
            if separator and key in expected:
                trailers[key] = value
        if trailers == expected:
            return
    raise PublicationReceiptError(
        "no committed Nightly publication matches the cleanup identity"
    )


def _resolve_package_version_id(
    *, nightly_version: str, artifact_ref: str
) -> int:
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
        pages = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PublicationReceiptError(
            f"malformed GitHub Packages version response: {exc.msg}"
        ) from exc
    if not isinstance(pages, list) or any(
        not isinstance(page, list) for page in pages
    ):
        raise PublicationReceiptError(
            "malformed GitHub Packages version response: expected pages of versions"
        )

    digest = artifact_ref.rsplit("@", 1)[1]
    matches: list[int] = []
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
            or not isinstance(metadata, dict)
            or metadata.get("package_type") != "container"
        ):
            raise PublicationReceiptError(
                "malformed GitHub Packages version metadata"
            )
        container = metadata.get("container")
        tags = container.get("tags") if isinstance(container, dict) else None
        if not isinstance(tags, list) or any(
            not isinstance(tag, str) for tag in tags
        ):
            raise PublicationReceiptError(
                "malformed GitHub Packages container tags"
            )
        digest_matches = name == digest
        tag_matches = nightly_version in tags
        if digest_matches != tag_matches:
            raise PublicationReceiptError(
                "contradictory Nightly package version metadata"
            )
        if digest_matches:
            matches.append(version_id)

    if len(matches) != 1:
        raise PublicationReceiptError(
            "expected exactly one active Nightly package version matching "
            f"digest and tag, found {len(matches)}"
        )
    return matches[0]


def cleanup(
    repo: str | Path, *, source_run_id: str, nightly_version: str, artifact_ref: str
) -> None:
    verify_publication(
        repo,
        source_run_id=source_run_id,
        nightly_version=nightly_version,
        artifact_ref=artifact_ref,
    )
    version_id = _resolve_package_version_id(
        nightly_version=nightly_version, artifact_ref=artifact_ref
    )
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
            f"cannot delete exact Nightly package version: {proc.stderr.strip()}"
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
