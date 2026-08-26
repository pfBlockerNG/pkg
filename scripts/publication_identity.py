"""Validate package provenance identities carried by immutable inputs."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

PACKAGE = "pfSense-pkg-pfBlockerNG"

Stage = Literal["final", "alpha", "beta", "rc"]
Channel = Literal["stable", "testing", "edge"]
GithubRelease = Literal["final", "prerelease"]

_CORE = r"(0|[1-9][0-9]*)"
_FINAL_RE = re.compile(
    rf"^v(?P<major>{_CORE})\.(?P<minor>{_CORE})\.(?P<patch>{_CORE})$"
)
_PREVIEW_RE = re.compile(
    rf"^v(?P<major>{_CORE})\.(?P<minor>{_CORE})\.(?P<patch>{_CORE})\."
    r"(?P<stage>[abr])(?P<sequence>[1-9][0-9]*)$"
)
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_NIGHTLY_VERSION_RE = re.compile(
    r"^(?P<timestamp>[0-9]{14})\.(?P<source_sha>[0-9a-f]{7})$"
)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_RELEASE_TEXT = 128


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str | None
    version: str
    stage: Stage
    sequence: str | None
    target_final: str
    release_line: str
    channel: Channel
    prerelease: bool
    final: bool
    notes_required: bool
    github_release: GithubRelease
    pkg_version: str
    package: str


def _invalid(tag: str) -> ValueError:
    return ValueError(f"invalid release tag: {tag!r}")


def parse_release_tag(tag: str, channel: Channel | None = None) -> ReleaseInfo:
    """Parse one strict release tag and validate its channel context."""
    if not isinstance(tag, str) or not tag or len(tag) > 128 or not tag.isascii():
        raise _invalid(tag)

    if not isinstance(channel, str) or channel not in ("stable", "testing", "edge"):
        raise ValueError(f"invalid release channel: {channel!r}")

    match = _FINAL_RE.fullmatch(tag)
    if match:
        if channel != "stable":
            raise ValueError("final tag requires stable channel")
        major, minor, patch = (
            match.group(name) for name in ("major", "minor", "patch")
        )
        version = f"{major}.{minor}.{patch}"
        return ReleaseInfo(
            tag=tag,
            version=version,
            stage="final",
            sequence=None,
            target_final=version,
            release_line=f"release/{major}.{minor}",
            channel="stable",
            prerelease=False,
            final=True,
            notes_required=True,
            github_release="final",
            pkg_version=version,
            package=PACKAGE,
        )

    match = _PREVIEW_RE.fullmatch(tag)
    if match:
        if channel == "stable":
            raise ValueError("preview tag requires testing or edge channel")
        major, minor, patch = (
            match.group(name) for name in ("major", "minor", "patch")
        )
        expected_channel: Channel = "edge" if patch == "0" else "testing"
        if channel != expected_channel:
            raise ValueError(
                f"preview tag patch {patch} requires {expected_channel} channel"
            )
        stage_code = match.group("stage")
        stage = {"a": "alpha", "b": "beta", "r": "rc"}[stage_code]
        sequence = match.group("sequence")
        version = f"{major}.{minor}.{patch}"
        return ReleaseInfo(
            tag=tag,
            version=f"{version}.{stage_code}{sequence}",
            stage=stage,  # type: ignore[arg-type]
            sequence=sequence,
            target_final=version,
            release_line=f"release/{major}.{minor}",
            channel=channel,
            prerelease=True,
            final=False,
            notes_required=True,
            github_release="prerelease",
            pkg_version=f"{version}.{stage_code}{sequence}",
            package=PACKAGE,
        )

    raise _invalid(tag)


def _validate_source_sha(source_sha: str, *, name: str = "source_sha") -> None:
    if not isinstance(source_sha, str) or not _SHA_RE.fullmatch(source_sha):
        raise ValueError(f"{name} must be lowercase 40- or 64-character hex")


def _validate_digest(value: object, *, name: str = "input_digest") -> None:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{name} must be lowercase 64-character hex")


def validate_nightly_version(value: object, *, source_sha: str | None = None) -> str:
    """Validate one stateless UTC-timestamp and source-commit identity."""
    if not isinstance(value, str) or len(value) > _MAX_RELEASE_TEXT:
        raise ValueError(
            "Nightly version must be YYYYMMDDHHMMSS.<7-character source SHA>"
        )
    match = _NIGHTLY_VERSION_RE.fullmatch(value)
    if match is None:
        raise ValueError(
            "Nightly version must be YYYYMMDDHHMMSS.<7-character source SHA>"
        )
    try:
        datetime.strptime(match.group("timestamp"), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ValueError(
            f"invalid Nightly UTC timestamp: {match.group('timestamp')}"
        ) from exc
    version_sha = match.group("source_sha")
    if source_sha is not None:
        _validate_source_sha(source_sha)
        if version_sha != source_sha[:7]:
            raise ValueError(
                "Nightly version source SHA does not match pinned source SHA"
            )
    return value


def combined_nightly_input_digest(
    source_sha: str, ports_sha: str, input_digest: str
) -> str:
    """Return deterministic provenance digest for downstream build annotations."""
    _validate_source_sha(source_sha)
    _validate_source_sha(ports_sha, name="ports_sha")
    _validate_digest(input_digest)
    payload = f"{source_sha}\0{ports_sha}\0{input_digest}".encode("ascii")
    return hashlib.sha256(payload).hexdigest()
