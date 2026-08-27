from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import verify_nightly_publication as verify


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    ).stdout.strip()


def _published_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = tmp_path / "pkg"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "seed").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed")
    _git(repo, "commit", "-q", "-m", "seed")
    run_id = "123:2"
    version = "20260826010101.abcdef1"
    artifact_ref = "ghcr.io/pfblockerng/pfblockerng-nightly@sha256:" + "a" * 64
    message = (
        f'publish: nightly {version} -> ["nightly"]\n\n'
        f"pfBlockerNG-Nightly-Version: {version}\n"
        f"pfBlockerNG-Source-Run-Id: {run_id}\n"
        f"pfBlockerNG-Nightly-Artifact-Ref: {artifact_ref}\n"
    )
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo, run_id, version, artifact_ref


_LIST_ENDPOINT = (
    "orgs/pfBlockerNG/packages/container/pfblockerng-nightly/versions"
    "?per_page=100&state=active"
)
_DELETE_ENDPOINT = (
    "orgs/pfBlockerNG/packages/container/pfblockerng-nightly/versions"
)


def _version_row(
    version: str,
    artifact_ref: str,
    *,
    version_id: object = 1176645017,
    package_type: object = "container",
    tags: object | None = None,
) -> dict[str, object]:
    digest = artifact_ref.removeprefix(
        "ghcr.io/pfblockerng/pfblockerng-nightly@"
    )
    return {
        "id": version_id,
        "name": digest,
        "url": f"https://api.github.com/{_DELETE_ENDPOINT}/{version_id}",
        "package_html_url": (
            "https://github.com/orgs/pfBlockerNG/packages/container/package/"
            "pfblockerng-nightly"
        ),
        "created_at": "2026-08-27T03:04:03Z",
        "updated_at": "2026-08-27T03:04:03Z",
        "html_url": (
            "https://github.com/orgs/pfBlockerNG/packages/container/"
            f"pfblockerng-nightly/{version_id}"
        ),
        "metadata": {
            "package_type": package_type,
            "container": {"tags": [version] if tags is None else tags},
        },
    }


def _run_cleanup(
    tmp_path: Path,
    repo: Path,
    *,
    source_run_id: str,
    nightly_version: str,
    artifact_ref: str,
    response: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "transport.log"
    response_file = tmp_path / "versions.json"
    response_file.write_text(response, encoding="utf-8")

    gh = bin_dir / "gh"
    gh.write_text(
        """#!/bin/sh
set -eu
printf 'gh %s\\n' "$*" >> "$TRANSPORT_LOG"
case " $* " in
  *" --method DELETE "*) exit 0 ;;
esac
cat "$GH_RESPONSE"
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)

    oras = bin_dir / "oras"
    oras.write_text(
        """#!/bin/sh
printf 'oras %s\\n' "$*" >> "$TRANSPORT_LOG"
echo 'Error response from registry: unsupported: The operation is unsupported.' >&2
exit 1
""",
        encoding="utf-8",
    )
    oras.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TRANSPORT_LOG": str(log),
        "GH_RESPONSE": str(response_file),
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(verify.__file__).resolve()),
            "--repo",
            str(repo),
            "--source-run-id",
            source_run_id,
            "--nightly-version",
            nightly_version,
            "--artifact-ref",
            artifact_ref,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc, log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _assert_no_delete(calls: list[str]) -> None:
    assert not [call for call in calls if " --method DELETE " in f" {call} "]


def test_exact_publication_receipt_deletes_one_exact_rest_version(
    tmp_path: Path,
) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    unrelated = _version_row(
        "20260825010101.abcdef0",
        "ghcr.io/pfblockerng/pfblockerng-nightly@sha256:" + "b" * 64,
        version_id=1170000000,
    )
    exact = _version_row(version, artifact_ref, tags=["older-tag", version])
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=run_id,
        nightly_version=version,
        artifact_ref=artifact_ref,
        response=json.dumps([[unrelated], [exact]]),
    )
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        f"gh api --paginate --slurp {_LIST_ENDPOINT}",
        f"gh api --method DELETE {_DELETE_ENDPOINT}/1176645017",
    ]


def test_cleanup_finds_receipt_behind_later_commit(tmp_path: Path) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    (repo / "later").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later")
    _git(repo, "commit", "-q", "-m", "render: later site change")
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=run_id,
        nightly_version=version,
        artifact_ref=artifact_ref,
        response=json.dumps([[_version_row(version, artifact_ref)]]),
    )
    assert proc.returncode == 0, proc.stderr
    assert calls[-1] == f"gh api --method DELETE {_DELETE_ENDPOINT}/1176645017"


def test_cleanup_rejects_receipt_forged_only_on_dispatch_branch(
    tmp_path: Path,
) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD^")
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=run_id,
        nightly_version=version,
        artifact_ref=artifact_ref,
        response=json.dumps([[_version_row(version, artifact_ref)]]),
    )
    assert proc.returncode == 1
    assert "no committed Nightly publication" in proc.stderr
    assert calls == []


@pytest.mark.parametrize("field", ["source_run_id", "nightly_version", "artifact_ref"])
def test_cleanup_rejects_receipt_identity_mismatch_before_version_lookup(
    tmp_path: Path, field: str
) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    values = {
        "source_run_id": run_id,
        "nightly_version": version,
        "artifact_ref": artifact_ref,
    }
    values[field] = {
        "source_run_id": "124:2",
        "nightly_version": "20260826010102.abcdef1",
        "artifact_ref": "ghcr.io/pfblockerng/pfblockerng-nightly@sha256:" + "b" * 64,
    }[field]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        **values,
        response=json.dumps([[_version_row(version, artifact_ref)]]),
    )
    assert proc.returncode == 1
    assert "no committed Nightly publication" in proc.stderr
    assert calls == []


@pytest.mark.parametrize(
    ("name", "rows"),
    [
        ("missing", [[]]),
        ("wrong digest", None),
        ("wrong tag", None),
        ("duplicate", None),
    ],
)
def test_cleanup_rejects_non_exact_or_ambiguous_versions_without_delete(
    tmp_path: Path, name: str, rows: object
) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    if name == "wrong digest":
        rows = [[
            _version_row(
                version,
                "ghcr.io/pfblockerng/pfblockerng-nightly@sha256:" + "b" * 64,
            )
        ]]
    elif name == "wrong tag":
        rows = [[_version_row(version, artifact_ref, tags=["wrong-tag"])]]
    elif name == "duplicate":
        rows = [[
            _version_row(version, artifact_ref),
            _version_row(version, artifact_ref, version_id=1176645018),
        ]]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=run_id,
        nightly_version=version,
        artifact_ref=artifact_ref,
        response=json.dumps(rows),
    )
    assert proc.returncode == 1
    _assert_no_delete(calls)


@pytest.mark.parametrize("malformed_field", ["id", "package_type", "tags"])
def test_cleanup_rejects_contradictory_version_metadata_without_delete(
    tmp_path: Path, malformed_field: str
) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    row = _version_row(version, artifact_ref)
    if malformed_field == "id":
        row["id"] = "1176645017"
    elif malformed_field == "package_type":
        row["metadata"]["package_type"] = "docker"  # type: ignore[index]
    else:
        row["metadata"]["container"]["tags"] = version  # type: ignore[index]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=run_id,
        nightly_version=version,
        artifact_ref=artifact_ref,
        response=json.dumps([[row]]),
    )
    assert proc.returncode == 1
    _assert_no_delete(calls)


def test_cleanup_rejects_malformed_version_json_without_delete(tmp_path: Path) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=run_id,
        nightly_version=version,
        artifact_ref=artifact_ref,
        response="{not-json",
    )
    assert proc.returncode == 1
    _assert_no_delete(calls)
