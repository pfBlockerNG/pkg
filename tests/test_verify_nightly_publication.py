from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import verify_nightly_publication as verify

_REF_PREFIX = "ghcr.io/pfblockerng/pfblockerng-nightly@"
_LIST_ENDPOINT = (
    "orgs/pfBlockerNG/packages/container/pfblockerng-nightly/versions"
    "?per_page=100&state=active"
)
_DELETE_ENDPOINT = "orgs/pfBlockerNG/packages/container/pfblockerng-nightly/versions"
_VERSION_SCOPED = re.compile(rf"^{re.escape(_DELETE_ENDPOINT)}/[1-9][0-9]*$")

_PRIOR_VERSION = "20260826010101.abcdef1"
_PRIOR_RUN_ID = "123:2"
_PRIOR_REF = _REF_PREFIX + "sha256:" + "a" * 64
_CURRENT_VERSION = "20260827030157.f3c7e31"
_CURRENT_RUN_ID = "124:1"
_CURRENT_REF = _REF_PREFIX + "sha256:" + "c" * 64


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    ).stdout.strip()


def _publish(repo: Path, version: str, run_id: str, artifact_ref: str) -> None:
    message = (
        f'publish: nightly {version} -> ["nightly"]\n\n'
        f"pfBlockerNG-Nightly-Version: {version}\n"
        f"pfBlockerNG-Source-Run-Id: {run_id}\n"
        f"pfBlockerNG-Nightly-Artifact-Ref: {artifact_ref}\n"
    )
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


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


def _assert_version_scoped(deletes: list[str]) -> None:
    for target in deletes:
        assert _VERSION_SCOPED.fullmatch(target), deletes


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
    _assert_no_delete(calls)


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
    deletes = _delete_calls(calls)
    _assert_version_scoped(deletes)
    assert f"{_DELETE_ENDPOINT}/1176645017" not in deletes


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
    _assert_version_scoped(_delete_calls(calls))


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


@pytest.mark.parametrize("broken", ["version", "artifact_ref"])
def test_cleanup_rejects_malformed_prior_receipt_without_delete(
    tmp_path: Path, broken: str
) -> None:
    repo = tmp_path / "pkg"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "seed").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed")
    _git(repo, "commit", "-q", "-m", "seed")
    if broken == "version":
        _publish(repo, "not-a-version", _PRIOR_RUN_ID, _PRIOR_REF)
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
    ("call", "target"),
    [
        ("gh api -X DELETE endpoint", "endpoint"),
        ("gh api -XDELETE endpoint", "endpoint"),
        ("gh api -X=DELETE endpoint", "endpoint"),
        ("gh api --method=DELETE endpoint", "endpoint"),
        ("gh api --method DELETE endpoint", "endpoint"),
    ],
)
def test_delete_detection_reads_every_gh_method_spelling(
    call: str, target: str
) -> None:
    assert _delete_calls([call]) == [target]
    with pytest.raises(AssertionError):
        _assert_no_delete([call])


def test_delete_detection_rejects_oras_manifest_delete() -> None:
    with pytest.raises(AssertionError):
        _assert_no_delete(["oras manifest delete --force exact-ref"])


def test_delete_detection_ignores_reads_and_flags_whole_package_targets() -> None:
    assert _delete_calls([f"gh api --paginate --slurp {_LIST_ENDPOINT}"]) == []
    package = _DELETE_ENDPOINT.removesuffix("/versions")
    with pytest.raises(AssertionError):
        _assert_version_scoped(_delete_calls([f"gh api --method DELETE {package}"]))


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
