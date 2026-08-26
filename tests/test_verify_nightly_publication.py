from __future__ import annotations

import subprocess
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
    run_id = "123:2"
    version = "20260826010101.abcdef1"
    artifact_ref = "ghcr.io/pfblockerng/pfblockerng-nightly@sha256:" + "a" * 64
    message = (
        f'publish: nightly {version} -> ["nightly"]\n\n'
        f"pfBlockerNG-Nightly-Version: {version}\n"
        f"pfBlockerNG-Source-Run-Id: {run_id}\n"
        f"pfBlockerNG-Nightly-Artifact-Ref: {artifact_ref}\n"
    )
    _git(repo, "commit", "-q", "-m", message)
    return repo, run_id, version, artifact_ref


def _fake_oras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "oras.log"
    executable = bin_dir / "oras"
    executable.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" > "$ORAS_LOG"\n', encoding="utf-8"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("ORAS_LOG", str(log))
    return log


def test_exact_publication_receipt_allows_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    log = _fake_oras(tmp_path, monkeypatch)
    verify.cleanup(
        repo, source_run_id=run_id, nightly_version=version, artifact_ref=artifact_ref
    )
    assert log.read_text().strip() == f"manifest delete --force {artifact_ref}"


def test_cleanup_finds_receipt_behind_later_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    (repo / "later").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later")
    _git(repo, "commit", "-q", "-m", "render: later site change")
    log = _fake_oras(tmp_path, monkeypatch)
    verify.cleanup(
        repo,
        source_run_id=run_id,
        nightly_version=version,
        artifact_ref=artifact_ref,
    )
    assert log.read_text().strip() == f"manifest delete --force {artifact_ref}"


@pytest.mark.parametrize("field", ["source_run_id", "nightly_version", "artifact_ref"])
def test_cleanup_rejects_receipt_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    log = _fake_oras(tmp_path, monkeypatch)
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
    with pytest.raises(
        verify.PublicationReceiptError, match="no committed Nightly publication"
    ):
        verify.cleanup(repo, **values)
    assert not log.exists()
