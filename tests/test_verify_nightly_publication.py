from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import verify_nightly_publication as verify

_REF_PREFIX = "ghcr.io/pfblockerng/pfblockerng-nightly@"
_DELETE_ENDPOINT = "orgs/pfBlockerNG/packages/container/pfblockerng-nightly/versions"
_LIST_ENDPOINT = f"{_DELETE_ENDPOINT}?per_page=100&state=active"

_PRIOR_VERSION = "20260826010101.abcdef1"
_PRIOR_RUN_ID = "123:2"
_PRIOR_REF = _REF_PREFIX + "sha256:" + "a" * 64
_CURRENT_VERSION = "20260827030157.f3c7e31"
_CURRENT_RUN_ID = "124:1"
_CURRENT_REF = _REF_PREFIX + "sha256:" + "c" * 64


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    if date is not None:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _receipt_block(version: str, run_id: str, artifact_ref: str) -> str:
    return (
        f"pfBlockerNG-Nightly-Version: {version}\n"
        f"pfBlockerNG-Source-Run-Id: {run_id}\n"
        f"pfBlockerNG-Nightly-Artifact-Ref: {artifact_ref}\n"
    )


def _publish(
    repo: Path,
    version: str,
    run_id: str,
    artifact_ref: str,
    *,
    date: str | None = None,
) -> None:
    message = f'publish: nightly {version} -> ["nightly"]\n\n' + _receipt_block(
        version, run_id, artifact_ref
    )
    _git(repo, "commit", "-q", "--allow-empty", "-m", message, date=date)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def _seed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "pkg"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "seed").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _published_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = _seed_repo(tmp_path)
    _publish(repo, _PRIOR_VERSION, _PRIOR_RUN_ID, _PRIOR_REF)
    return repo, _PRIOR_RUN_ID, _PRIOR_VERSION, _PRIOR_REF


def _version_row(
    version: str,
    artifact_ref: str,
    *,
    version_id: object = 1176645017,
    package_type: object = "container",
    tags: object | None = None,
) -> dict[str, object]:
    digest = artifact_ref.removeprefix(_REF_PREFIX)
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


def _delete_calls(calls: list[str]) -> list[str]:
    """Return the endpoint of every DELETE the cleanup transport actually issued.

    Recognises each spelling ``gh`` accepts for the method flag, so a rewritten
    call site cannot hide a deletion from these assertions, and treats any
    ``oras manifest delete`` as an outright contract breach.
    """
    deletes: list[str] = []
    for call in calls:
        words = shlex.split(call)
        assert words[:3] != ["oras", "manifest", "delete"], calls
        if words[:2] != ["gh", "api"]:
            continue
        args = words[2:]
        method = "GET"
        target = ""
        skip_next = False
        for index, arg in enumerate(args):
            if skip_next:
                skip_next = False
            elif arg in ("-X", "--method"):
                if index + 1 < len(args):
                    method = args[index + 1]
                    skip_next = True
            elif arg.startswith("--method="):
                method = arg.removeprefix("--method=")
            elif arg.startswith("-X") and len(arg) > 2:
                method = arg[2:].removeprefix("=")
            elif not arg.startswith("-"):
                target = arg
        if method.upper() == "DELETE":
            deletes.append(target)
    return deletes


def _assert_no_delete(calls: list[str]) -> None:
    assert _delete_calls(calls) == [], calls


def test_only_successful_version_is_retained_with_no_delete(tmp_path: Path) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=run_id,
        nightly_version=version,
        artifact_ref=artifact_ref,
        response=json.dumps([[_version_row(version, artifact_ref)]]),
    )
    assert proc.returncode == 0, proc.stderr
    assert calls == [f"gh api --paginate --slurp {_LIST_ENDPOINT}"]


def test_prior_successful_version_deleted_and_consumed_version_retained(
    tmp_path: Path,
) -> None:
    repo, _, _, _ = _published_repo(tmp_path)
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    prior = _version_row(_PRIOR_VERSION, _PRIOR_REF, version_id=1170000000)
    consumed = _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017)
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([[prior], [consumed]]),
    )
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        f"gh api --paginate --slurp {_LIST_ENDPOINT}",
        f"gh api --method DELETE {_DELETE_ENDPOINT}/1170000000",
    ]
    assert f"{_DELETE_ENDPOINT}/1176645017" not in _delete_calls(calls)


def test_every_prior_successful_version_deleted_by_exact_id(tmp_path: Path) -> None:
    repo, _, _, _ = _published_repo(tmp_path)
    middle_version = "20260826230000.bbbbbb2"
    middle_ref = _REF_PREFIX + "sha256:" + "b" * 64
    _publish(repo, middle_version, "123:9", middle_ref)
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    rows = [
        _version_row(_PRIOR_VERSION, _PRIOR_REF, version_id=1170000000),
        _version_row(middle_version, middle_ref, version_id=1173000000),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    assert _delete_calls(calls) == [
        f"{_DELETE_ENDPOINT}/1173000000",
        f"{_DELETE_ENDPOINT}/1170000000",
    ]


@pytest.mark.parametrize("second_run_id", [_CURRENT_RUN_ID, "900:7"])
def test_repeated_consumed_receipt_never_deletes_the_retained_anchor(
    tmp_path: Path, second_run_id: str
) -> None:
    repo, _, _, _ = _published_repo(tmp_path)
    _publish(repo, _CURRENT_VERSION, second_run_id, _CURRENT_REF)
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    rows = [
        _version_row(_PRIOR_VERSION, _PRIOR_REF, version_id=1170000000),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    assert _delete_calls(calls) == [f"{_DELETE_ENDPOINT}/1170000000"]


def test_receipt_repeated_after_a_later_publication_keeps_the_newer_version(
    tmp_path: Path,
) -> None:
    repo, prior_run_id, prior_version, prior_ref = _published_repo(tmp_path)
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    _publish(repo, prior_version, prior_run_id, prior_ref)
    rows = [
        _version_row(prior_version, prior_ref, version_id=1170000000),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=prior_run_id,
        nightly_version=prior_version,
        artifact_ref=prior_ref,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_delete(calls)


def test_repeated_prior_receipt_deletes_that_version_exactly_once(
    tmp_path: Path,
) -> None:
    repo, _, _, _ = _published_repo(tmp_path)
    _publish(repo, _PRIOR_VERSION, _PRIOR_RUN_ID, _PRIOR_REF)
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    rows = [
        _version_row(_PRIOR_VERSION, _PRIOR_REF, version_id=1170000000),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    assert _delete_calls(calls) == [f"{_DELETE_ENDPOINT}/1170000000"]


def test_publication_outside_the_consumed_ancestry_is_not_a_prior(
    tmp_path: Path,
) -> None:
    repo = _seed_repo(tmp_path)
    side_version = "20260828030200.eeeeeee"
    side_ref = _REF_PREFIX + "sha256:" + "e" * 64
    _git(repo, "checkout", "-q", "-b", "side")
    _publish(repo, side_version, "999:1", side_ref, date="2026-08-28T08:00:00")
    _git(repo, "checkout", "-q", "main")
    _publish(
        repo,
        _CURRENT_VERSION,
        _CURRENT_RUN_ID,
        _CURRENT_REF,
        date="2026-08-28T10:00:00",
    )
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "side",
        "-m",
        "merge side",
        date="2026-08-28T11:00:00",
    )
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    rows = [
        _version_row(side_version, side_ref, version_id=1188888888),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_delete(calls)


def test_repeated_receipt_anchors_on_ancestry_not_on_commit_date(
    tmp_path: Path,
) -> None:
    repo = _seed_repo(tmp_path)
    middle_version = "20260826220000.bbbbbbb"
    middle_ref = _REF_PREFIX + "sha256:" + "b" * 64
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF, date="@5000 +0000")
    _git(repo, "checkout", "-q", "-b", "side")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "chore: side", date="@9000 +0000")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _publish(repo, middle_version, "125:9", middle_ref, date="@2000 +0000")
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF, date="@1000 +0000")
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "chore: merge side",
        side,
        date="@9500 +0000",
    )
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    rows = [
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
        _version_row(middle_version, middle_ref, version_id=1188888888),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_delete(calls)


def test_receipt_quoted_outside_the_trailer_block_is_not_a_prior(
    tmp_path: Path,
) -> None:
    repo = _seed_repo(tmp_path)
    quoted_version = "20260824170736.468a267"
    quoted_ref = _REF_PREFIX + "sha256:" + "7" * 64
    _git(
        repo,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "docs: describe the Nightly receipt format\n\n"
        + _receipt_block(quoted_version, "32754672803:1", quoted_ref)
        + "\nThe three lines above are the receipt trailer block.\n",
    )
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    rows = [
        _version_row(quoted_version, quoted_ref, version_id=1170000000),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_delete(calls)


def test_cleanup_rejects_an_identity_quoted_outside_the_trailer_block(
    tmp_path: Path,
) -> None:
    repo = _seed_repo(tmp_path)
    _git(
        repo,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "docs: quote the cleanup identity\n\n"
        + _receipt_block(_CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
        + "\nQuoted for illustration only.\n",
    )
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([[_version_row(_CURRENT_VERSION, _CURRENT_REF)]]),
    )
    assert proc.returncode == 1
    assert "no committed Nightly publication" in proc.stderr
    assert calls == []


@pytest.mark.parametrize(
    "smuggled", ["\r", "\x1e", "\x1c", "\x1d", "\x85", "\u2028", "\u2029", "\v", "\f"]
)
def test_control_characters_in_a_trailer_cannot_forge_a_receipt(
    tmp_path: Path, smuggled: str
) -> None:
    repo = _seed_repo(tmp_path)
    forged_version = "20260824170736.468a267"
    forged_ref = _REF_PREFIX + "sha256:" + "7" * 64
    block = _receipt_block(forged_version, "32754672803:1", forged_ref)
    _git(
        repo,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "chore: sign off\n\nSigned-off-by: n <n@example.com>"
        + smuggled
        + smuggled.join(block.rstrip("\n").split("\n"))
        + "\n",
    )
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    rows = [
        _version_row(forged_version, forged_ref, version_id=1170000000),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_delete(calls)


def test_repository_local_trailer_config_cannot_forge_a_receipt(
    tmp_path: Path,
) -> None:
    repo = _seed_repo(tmp_path)
    _git(repo, "config", "trailer.separators", ":=")
    forged_version = "20260824170736.468a267"
    forged_ref = _REF_PREFIX + "sha256:" + "7" * 64
    _git(
        repo,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "chore: unrelated\n\n"
        f"pfBlockerNG-Nightly-Version={forged_version}\n"
        "pfBlockerNG-Source-Run-Id=32754672803:1\n"
        f"pfBlockerNG-Nightly-Artifact-Ref={forged_ref}\n",
    )
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    rows = [
        _version_row(forged_version, forged_ref, version_id=1170000000),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_delete(calls)


def test_repository_local_comment_char_cannot_forge_a_receipt(
    tmp_path: Path,
) -> None:
    repo = _seed_repo(tmp_path)
    _git(repo, "config", "core.commentChar", ";")
    forged_version = "20260824170736.468a267"
    forged_ref = _REF_PREFIX + "sha256:" + "7" * 64
    padding = "\n".join(f"; padding {index}" for index in range(20))
    _git(
        repo,
        "commit",
        "-q",
        "--allow-empty",
        "--cleanup=verbatim",
        "-m",
        "chore: unrelated\n\n"
        + padding
        + "\n"
        + _receipt_block(forged_version, "32754672803:1", forged_ref),
    )
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    rows = [
        _version_row(forged_version, forged_ref, version_id=1170000000),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_delete(calls)


@pytest.mark.parametrize("collision", ["publication ref", "consumed commit"])
def test_worktree_path_colliding_with_a_revision_still_cleans_up(
    tmp_path: Path, collision: str
) -> None:
    repo, _, _, _ = _published_repo(tmp_path)
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    if collision == "publication ref":
        (repo / "refs" / "remotes" / "origin" / "main").mkdir(parents=True)
    else:
        (repo / _git(repo, "rev-parse", "HEAD")).mkdir()
    rows = [
        _version_row(_PRIOR_VERSION, _PRIOR_REF, version_id=1170000000),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    assert _delete_calls(calls) == [f"{_DELETE_ENDPOINT}/1170000000"]


@pytest.mark.parametrize("present", [1, 2])
def test_partial_receipt_trailers_are_not_a_publication(
    tmp_path: Path, present: int
) -> None:
    repo = _seed_repo(tmp_path)
    legacy_version = "20260825133122.fd978e0"
    legacy_ref = _REF_PREFIX + "sha256:" + "7" * 64
    block = _receipt_block(legacy_version, "32853794776:1", legacy_ref)
    _git(
        repo,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        f'publish: nightly {legacy_version} -> ["nightly"]\n\n'
        + "".join(block.splitlines(keepends=True)[:present]),
    )
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    rows = [
        _version_row(legacy_version, legacy_ref, version_id=1170000000),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_delete(calls)


def test_newer_successful_version_is_never_deleted_by_a_late_cleanup(
    tmp_path: Path,
) -> None:
    repo, prior_run_id, prior_version, prior_ref = _published_repo(tmp_path)
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    rows = [
        _version_row(prior_version, prior_ref, version_id=1170000000),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=prior_run_id,
        nightly_version=prior_version,
        artifact_ref=prior_ref,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_delete(calls)


def test_versions_without_a_publication_receipt_are_retained(tmp_path: Path) -> None:
    repo, _, _, _ = _published_repo(tmp_path)
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    unconsumed = _version_row(
        "20260827020000.dddddd3",
        _REF_PREFIX + "sha256:" + "d" * 64,
        version_id=1174000000,
    )
    foreign = _version_row(
        "not-a-nightly-tag",
        _REF_PREFIX + "sha256:" + "e" * 64,
        version_id=1175000000,
    )
    rows = [
        unconsumed,
        foreign,
        _version_row(_PRIOR_VERSION, _PRIOR_REF, version_id=1170000000),
        _version_row(_CURRENT_VERSION, _CURRENT_REF, version_id=1176645017),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 0, proc.stderr
    assert _delete_calls(calls) == [f"{_DELETE_ENDPOINT}/1170000000"]


def test_already_deleted_prior_version_is_not_an_error(tmp_path: Path) -> None:
    repo, _, _, _ = _published_repo(tmp_path)
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([[_version_row(_CURRENT_VERSION, _CURRENT_REF)]]),
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_delete(calls)


def test_cleanup_finds_receipt_behind_later_commit(tmp_path: Path) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    (repo / "later").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later")
    _git(repo, "commit", "-q", "-m", "render: later site change")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=run_id,
        nightly_version=version,
        artifact_ref=artifact_ref,
        response=json.dumps([[_version_row(version, artifact_ref)]]),
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_delete(calls)


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
        "artifact_ref": _REF_PREFIX + "sha256:" + "b" * 64,
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
    "name",
    ["missing", "wrong digest", "wrong tag", "duplicate"],
)
def test_cleanup_rejects_non_exact_or_ambiguous_versions_without_delete(
    tmp_path: Path, name: str
) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    rows: list[list[dict[str, object]]]
    if name == "missing":
        rows = [[]]
    elif name == "wrong digest":
        rows = [[_version_row(version, _REF_PREFIX + "sha256:" + "b" * 64)]]
    elif name == "wrong tag":
        rows = [[_version_row(version, artifact_ref, tags=["wrong-tag"])]]
    else:
        rows = [
            [
                _version_row(version, artifact_ref),
                _version_row(version, artifact_ref, version_id=1176645018),
            ]
        ]
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


@pytest.mark.parametrize("contradiction", ["digest without tag", "tag without digest"])
def test_cleanup_rejects_contradictory_prior_version_rows_without_delete(
    tmp_path: Path, contradiction: str
) -> None:
    repo, _, _, _ = _published_repo(tmp_path)
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    if contradiction == "digest without tag":
        prior = _version_row(_PRIOR_VERSION, _PRIOR_REF, version_id=1170000000)
        prior["metadata"]["container"]["tags"] = ["unrelated"]  # type: ignore[index]
    else:
        prior = _version_row(
            _PRIOR_VERSION,
            _REF_PREFIX + "sha256:" + "f" * 64,
            version_id=1170000000,
        )
    rows = [prior, _version_row(_CURRENT_VERSION, _CURRENT_REF)]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 1
    assert "contradictory" in proc.stderr
    _assert_no_delete(calls)


def test_cleanup_rejects_duplicate_prior_version_rows_without_delete(
    tmp_path: Path,
) -> None:
    repo, _, _, _ = _published_repo(tmp_path)
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    rows = [
        _version_row(_PRIOR_VERSION, _PRIOR_REF, version_id=1170000000),
        _version_row(_PRIOR_VERSION, _PRIOR_REF, version_id=1170000001),
        _version_row(_CURRENT_VERSION, _CURRENT_REF),
    ]
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([rows]),
    )
    assert proc.returncode == 1
    assert "contradictory" in proc.stderr
    _assert_no_delete(calls)


@pytest.mark.parametrize("broken", ["version", "run_id", "artifact_ref"])
def test_cleanup_rejects_malformed_prior_receipt_without_delete(
    tmp_path: Path, broken: str
) -> None:
    repo = _seed_repo(tmp_path)
    if broken == "version":
        _publish(repo, "not-a-version", _PRIOR_RUN_ID, _PRIOR_REF)
    elif broken == "run_id":
        _publish(repo, _PRIOR_VERSION, "not-a-run-id", _PRIOR_REF)
    else:
        _publish(repo, _PRIOR_VERSION, _PRIOR_RUN_ID, "ghcr.io/other/thing:latest")
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _CURRENT_REF)
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_CURRENT_REF,
        response=json.dumps([[_version_row(_CURRENT_VERSION, _CURRENT_REF)]]),
    )
    assert proc.returncode == 1
    assert "malformed prior Nightly publication receipt" in proc.stderr
    _assert_no_delete(calls)


def test_cleanup_rejects_receipts_sharing_one_digest_without_delete(
    tmp_path: Path,
) -> None:
    repo, _, _, _ = _published_repo(tmp_path)
    _publish(repo, _CURRENT_VERSION, _CURRENT_RUN_ID, _PRIOR_REF)
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=_CURRENT_RUN_ID,
        nightly_version=_CURRENT_VERSION,
        artifact_ref=_PRIOR_REF,
        response=json.dumps([[_version_row(_CURRENT_VERSION, _PRIOR_REF)]]),
    )
    assert proc.returncode == 1
    assert "contradictory" in proc.stderr
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


@pytest.mark.parametrize(
    "call",
    [
        "gh api -X DELETE endpoint",
        "gh api -XDELETE endpoint",
        "gh api -X=DELETE endpoint",
        "gh api --method=DELETE endpoint",
        "gh api --method DELETE endpoint",
    ],
)
def test_delete_detection_reads_every_gh_method_spelling(call: str) -> None:
    assert _delete_calls([call]) == ["endpoint"]
    with pytest.raises(AssertionError):
        _assert_no_delete([call])


def test_delete_detection_rejects_oras_manifest_delete() -> None:
    with pytest.raises(AssertionError):
        _assert_no_delete(["oras manifest delete --force exact-ref"])


def test_delete_detection_ignores_reads() -> None:
    assert _delete_calls([f"gh api --paginate --slurp {_LIST_ENDPOINT}"]) == []


@pytest.mark.parametrize(
    "duplicate_key",
    [
        "pfBlockerNG-Nightly-Version",
        "pfBlockerNG-Source-Run-Id",
        "pfBlockerNG-Nightly-Artifact-Ref",
    ],
)
def test_cleanup_rejects_duplicate_receipt_identity_before_version_lookup(
    tmp_path: Path, duplicate_key: str
) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    expected = {
        "pfBlockerNG-Nightly-Version": version,
        "pfBlockerNG-Source-Run-Id": run_id,
        "pfBlockerNG-Nightly-Artifact-Ref": artifact_ref,
    }
    wrong = {
        "pfBlockerNG-Nightly-Version": "20260826010102.abcdef1",
        "pfBlockerNG-Source-Run-Id": "124:2",
        "pfBlockerNG-Nightly-Artifact-Ref": _REF_PREFIX + "sha256:" + "b" * 64,
    }
    trailers = []
    for key, value in expected.items():
        if key == duplicate_key:
            trailers.append(f"{key}: {wrong[key]}")
        trailers.append(f"{key}: {value}")
    message = (
        f'publish: nightly {version} -> ["nightly"]\n\n' + "\n".join(trailers) + "\n"
    )
    commit = _git(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD^", "-m", message)
    _git(repo, "update-ref", "refs/remotes/origin/main", commit)
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=run_id,
        nightly_version=version,
        artifact_ref=artifact_ref,
        response=json.dumps([[_version_row(version, artifact_ref)]]),
    )
    assert proc.returncode == 1
    assert "duplicate" in proc.stderr
    assert calls == []


@pytest.mark.parametrize(
    "malformation",
    ["duplicate JSON key", "invalid digest name", "duplicate version id"],
)
def test_cleanup_rejects_malformed_inventory_even_with_one_exact_row(
    tmp_path: Path, malformation: str
) -> None:
    repo, run_id, version, artifact_ref = _published_repo(tmp_path)
    exact = _version_row(version, artifact_ref)
    if malformation == "duplicate JSON key":
        exact_json = json.dumps(exact).replace(
            '"id": 1176645017', '"id": 9, "id": 1176645017', 1
        )
        response = f"[[{exact_json}]]"
    else:
        unrelated = _version_row(
            "20260825010101.abcdef0",
            _REF_PREFIX + "sha256:" + "b" * 64,
            version_id=(
                1176645017 if malformation == "duplicate version id" else 1170000000
            ),
        )
        if malformation == "invalid digest name":
            unrelated["name"] = "not-a-digest"
        response = json.dumps([[unrelated, exact]])
    proc, calls = _run_cleanup(
        tmp_path,
        repo,
        source_run_id=run_id,
        nightly_version=version,
        artifact_ref=artifact_ref,
        response=response,
    )
    assert proc.returncode == 1
    _assert_no_delete(calls)
