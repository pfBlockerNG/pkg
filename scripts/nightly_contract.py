"""Validation primitives for immutable Nightly OCI handoffs."""

from __future__ import annotations

import re
from collections.abc import Mapping

from publication_identity import combined_nightly_input_digest as _combined_digest
from publication_identity import validate_nightly_version as _validate_version

SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ABI = re.compile(r"^FreeBSD:[0-9]+:\*$")
_DEP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._,+-]*\.pkg$")
_FIELDS = {"abi", "name", "sha256"}


class ContractError(ValueError):
    """A Nightly handoff field is malformed or inconsistent."""


def combined_nightly_input_digest(
    source_sha: str, ports_sha: str, matrix_digest: str
) -> str:
    return _combined_digest(source_sha, ports_sha, matrix_digest)


def validate_nightly_version(version: object, *, source_sha: str | None = None) -> str:
    return _validate_version(version, source_sha=source_sha)


def validate_artifacts(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ContractError("artifacts must be a non-empty list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _FIELDS:
            raise ContractError("artifact exact fields are required")
        abi, name, digest = item["abi"], item["name"], item["sha256"]
        if not isinstance(abi, str) or not _ABI.fullmatch(abi) or abi in seen:
            raise ContractError("artifact ABI is malformed or duplicated")
        if (
            not isinstance(name, str)
            or not name.isascii()
            or not _DEP_NAME.fullmatch(name)
        ):
            raise ContractError("artifact name is unsafe or malformed")
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ContractError("artifact sha256 must be lowercase 64-character hex")
        seen.add(abi)
        result.append({"abi": abi, "name": name, "sha256": digest})
    return sorted(result, key=lambda item: item["abi"])


def validate_dep_artifacts(
    value: object, *, leg_abi: str, canonical_name: str
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ContractError("dep_artifacts must be a list")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _FIELDS:
            raise ContractError("dep_artifacts entry exact fields are required")
        abi, name, digest = item["abi"], item["name"], item["sha256"]
        if abi != leg_abi:
            raise ContractError(
                "dep_artifacts entry abi must match the leg wildcard ABI"
            )
        if (
            not isinstance(name, str)
            or ".." in name
            or not _DEP_NAME.fullmatch(name)
            or name == canonical_name
        ):
            raise ContractError("dep_artifacts entry name is unsafe or malformed")
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ContractError(
                "dep_artifacts entry sha256 must be lowercase 64-character hex"
            )
        key = (abi, name)
        if key in seen:
            raise ContractError("dep_artifacts entries must be unique per ABI and name")
        seen.add(key)
        result.append({"abi": abi, "name": name, "sha256": digest})
    return sorted(result, key=lambda item: item["name"])
