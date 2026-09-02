from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INGEST = ROOT / ".github" / "workflows" / "ingest.yml"
RENDER = ROOT / ".github" / "workflows" / "render-site.yml"


class IngestionWorkflowContractTests(unittest.TestCase):
    def test_ingestion_is_pkg_local_and_retryable_by_exact_input(self) -> None:
        text = INGEST.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn(
            "run-name: Ingest ${{ inputs.operation }} ${{ inputs.source_run_id }}", text
        )
        for name in (
            "operation",
            "source_repository",
            "release_id",
            "release_tag",
            "source_sha",
            "destinations",
            "source_run_id",
            "artifact_ref",
            "nightly_version",
            "staging_prefix",
        ):
            self.assertRegex(text, rf"(?m)^      {name}:$")
        self.assertNotIn("repository: pfBlockerNG/pfBlockerNG", text)
        self.assertNotRegex(text, r"(?m)^\s+ref:\s+(?:devel|main)$")
        self.assertEqual(text.count("uses: actions/checkout@"), 1)
        self.assertIn("fetch-depth: 0", text)

    def test_permissions_and_commit_ownership_are_local(self) -> None:
        text = INGEST.read_text(encoding="utf-8")
        self.assertRegex(
            text, r"(?s)permissions:\n\s+contents: write\n\s+packages: write"
        )
        self.assertIn('export PFB_SRC="$GITHUB_WORKSPACE"', text)
        self.assertIn('export PKG_REPO="$GITHUB_WORKSPACE"', text)
        self.assertIn("sh scripts/publish-pkg-repo.sh", text)
        self.assertNotIn("PKG_GITHUB_APP", text)
        self.assertNotIn("persist-credentials: false", text)

    def test_tagged_release_intake_is_exact_and_immutable(self) -> None:
        text = INGEST.read_text(encoding="utf-8")
        for needle in (
            "repos/${SOURCE_REPOSITORY}/releases/${RELEASE_ID}",
            '[ "$ACTUAL_TAG" = "$RELEASE_TAG" ]',
            '[ "$DRAFT" = false ]',
            '[ "$IMMUTABLE" = true ]',
            "one or more .pkg release assets have no sha256 digest",
            "digests.json",
            "compatibility-route-matrix.json",
            "--compatibility-route-matrix",
        ):
            self.assertIn(needle, text)
        self.assertIn("pfblockerng-release-handoff.json", text)
        self.assertIn("HANDOFF_FILE", text)
        release_step = text[
            text.index("- name: Download exact immutable Release input") : text.index(
                "- name: Set up ORAS"
            )
        ]
        for binding in (
            "SOURCE_REPOSITORY: ${{ inputs.source_repository }}",
            "RELEASE_ID: ${{ inputs.release_id }}",
            "RELEASE_TAG: ${{ inputs.release_tag }}",
            "OPERATION: ${{ inputs.operation }}",
            "GH_TOKEN: ${{ github.token }}",
        ):
            self.assertIn(binding, release_step)

    def test_tagged_promote_uses_staged_route_input_without_redownloading_release(
        self,
    ) -> None:
        text = INGEST.read_text(encoding="utf-8")
        release_step = text[
            text.index("- name: Download exact immutable Release input") : text.index(
                "- name: Set up ORAS"
            )
        ]
        self.assertIn("if: inputs.operation == 'tagged-stage'", release_step)
        self.assertNotIn("tagged-promote", release_step)
        self.assertIn("docs/${STAGING_PREFIX}/.route-matrix.json", text)

    def test_compatibility_matrix_covers_every_production_route(self) -> None:
        rows = json.loads(
            (ROOT / "publication" / "compatibility-route-matrix.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            {(row["variant"], row["pfsense_version"]) for row in rows},
            {("CE", "2.8"), ("CE", "2.9"), ("Plus", "26.03"), ("Plus", "26.07"), ("Plus", "25.11")},
        )

    def test_nightly_pull_and_cleanup_use_only_the_validated_digest_reference(
        self,
    ) -> None:
        text = INGEST.read_text(encoding="utf-8")
        self.assertIn("ghcr.io/pfblockerng/pfblockerng-nightly@sha256:", text.lower())
        pull_step = text[
            text.index("- name: Pull exact Nightly digest") : text.index(
                "- name: Materialize catalogue signing key"
            )
        ]
        self.assertIn(
            "if: inputs.operation == 'nightly' || inputs.operation == 'nightly-cleanup'",
            pull_step,
        )
        self.assertIn('oras pull "$ARTIFACT_REF"', text)
        self.assertNotIn("oras manifest delete", text)
        self.assertNotRegex(text, r'oras pull "?[^\n]*:(?:latest|nightly)')
        self.assertLess(
            text.index("source_run_id does not match OCI handoff"),
            text.index("python3 scripts/verify_nightly_publication.py"),
        )
        self.assertRegex(
            text, r"(?m)^          python3 scripts/verify_nightly_publication\.py \\$"
        )
        cleanup_step = text[
            text.index(
                "- name: Retain consumed Nightly version and delete its predecessors"
            ) : text.index("- name: Record ingestion result")
        ]
        for contract in (
            "SOURCE_RUN_ID: ${{ inputs.source_run_id }}",
            "GH_TOKEN: ${{ github.token }}",
            "NIGHTLY_VERSION: ${{ inputs.nightly_version }}",
            'git fetch --no-tags origin "+refs/heads/main:refs/remotes/origin/main"',
            '--source-run-id "$SOURCE_RUN_ID"',
            '--nightly-version "$NIGHTLY_VERSION"',
            '--artifact-ref "$ARTIFACT_REF"',
        ):
            self.assertIn(contract, cleanup_step)
        self.assertNotIn("DELETE", cleanup_step)
        self.assertIn("application/vnd.pfblockerng.nightly.handoff.v1+json", text)

    def test_tagged_stage_promote_discard_and_nightly_are_explicit(self) -> None:
        text = INGEST.read_text(encoding="utf-8")
        for operation in (
            "tagged-stage",
            "tagged-promote",
            "tagged-discard",
            "nightly",
        ):
            self.assertIn(operation, text)
        self.assertIn("PUBLISH_STAGE=stage", text)
        self.assertIn("PUBLISH_STAGE=promote", text)
        self.assertIn("PUBLISH_STAGE=discard", text)
        self.assertIn("PUBLISH_KIND=nightly", text)
        self.assertIn("PUBLISH_RENDER_SITE=1", text)
        self.assertIn("publication-result", text)
        self.assertIn("if: always()", text)

    def test_site_source_push_renders_only_with_pkg_code(self) -> None:
        text = RENDER.read_text(encoding="utf-8")
        self.assertIn("pkg-site/**", text)
        self.assertIn("scripts/gen_landing.py", text)
        self.assertIn("scripts/install.sh", text)
        self.assertIn("scripts/pfblockerng_repo_generate.sh", text)
        self.assertIn("sh scripts/render-pkg-site.sh", text)
        self.assertEqual(text.count("uses: actions/checkout@"), 1)
        self.assertNotIn("pfBlockerNG/pfBlockerNG", text)
        self.assertRegex(text, r"(?s)permissions:\n\s+contents: write")

    def test_writer_jobs_configure_pfblockerng_bot_ssh_signing(self) -> None:
        signing = "Configure pfblockerng-bot signing"
        for path in (INGEST, RENDER):
            text = path.read_text(encoding="utf-8")
            self.assertIn(signing, text, f"{path.name} lacks {signing!r}")
            setup = text[text.index(f"- name: {signing}") :]
            self.assertIn("secrets.PFB_BOT_SIGNING_KEY", setup)
            self.assertIn('[ -n "${PFB_BOT_SIGNING_KEY:-}" ]', setup)
            self.assertIn('install -m 600 /dev/null "$RUNNER_TEMP/pfb-bot-signing-key"', setup)
            self.assertIn(
                'printf \'%s\\n\' "$PFB_BOT_SIGNING_KEY" > "$RUNNER_TEMP/pfb-bot-signing-key"',
                setup,
            )
            self.assertLess(
                setup.index('install -m 600 /dev/null "$RUNNER_TEMP/pfb-bot-signing-key"'),
                setup.index('printf \'%s\\n\' "$PFB_BOT_SIGNING_KEY"'),
            )
            self.assertIn("PFB_BOT_SIGNING_KEY_FILE", setup)
        ingest = INGEST.read_text(encoding="utf-8")
        self.assertLess(
            ingest.index("- name: Configure pfblockerng-bot signing"),
            ingest.index("- name: Publish with guarded local commit"),
        )
        render = RENDER.read_text(encoding="utf-8")
        self.assertLess(
            render.index("- name: Configure pfblockerng-bot signing"),
            render.index("- name: Render and commit the pkg website"),
        )

    def test_workflows_never_configure_the_generic_actions_commit_identity(self) -> None:
        generic = re.compile(
            r"git config user\.(?:name|email).*github-actions(?:\[bot\])?",
            re.IGNORECASE,
        )
        for path in (INGEST, RENDER):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(generic.search(text), f"generic Actions identity remains in {path.name}")
            self.assertNotIn('user.name="github-actions[bot]"', text)
            self.assertNotIn("github-actions[bot]@users.noreply.github.com", text)


    def test_publication_tests_disable_uv_cache_without_a_lockfile(self) -> None:
        text = (ROOT / ".github" / "workflows" / "test.yml").read_text(
            encoding="utf-8"
        )
        setup = text[text.index("astral-sh/setup-uv@") :]
        self.assertIn("enable-cache: false", setup)


def _tagged_intake_script() -> str:
    text = INGEST.read_text(encoding="utf-8")
    block = text.split("      - name: Download exact immutable Release input", 1)[1]
    return textwrap.dedent(
        block.split("        run: |\n", 1)[1].split("\n      - name: Set up ORAS", 1)[0]
    )


def _run_tagged_intake(
    tmp_path: Path,
    *,
    handoff_count: int,
    correct_digest: bool = True,
    metadata_tag: str = "v4.0.0",
    draft: bool = False,
    immutable: bool | None = True,
    source_repository: str = "pfBlockerNG/pfBlockerNG",
    release_id: str = "123",
    pkg_digest: str | None = "sha256:" + "a" * 64,
    include_pkg: bool = True,
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_log = tmp_path / "gh.log"
    handoff = tmp_path / "handoff.json"
    handoff.write_text('{"route_matrix":[]}\n', encoding="utf-8")
    digest = hashlib.sha256(handoff.read_bytes()).hexdigest()
    if not correct_digest:
        digest = "0" * 64
    handoffs = [
        {
            "id": 77 + index,
            "name": "pfblockerng-release-handoff.json",
            "digest": f"sha256:{digest}",
        }
        for index in range(handoff_count)
    ]
    metadata = tmp_path / "metadata.json"
    pkg_assets: list[dict[str, str]] = []
    if include_pkg:
        pkg_asset = {"name": "pfSense-pkg-pfBlockerNG-4.0.0.pkg"}
        if pkg_digest is not None:
            pkg_asset["digest"] = pkg_digest
        pkg_assets.append(pkg_asset)
    metadata_object = {
        "tag_name": metadata_tag,
        "draft": draft,
        "assets": [*handoffs, *pkg_assets],
    }
    if immutable is not None:
        metadata_object["immutable"] = immutable
    metadata.write_text(json.dumps(metadata_object), encoding="utf-8")
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_GH_LOG"
case "$1" in
  api)
    case "$*" in
      *"/releases/assets/"*) cat "$FAKE_HANDOFF" ;;
      *) cat "$FAKE_METADATA" ;;
    esac
    ;;
  release)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = --dir ]; then shift; out=$1; fi
      shift
    done
    : > "$out/pfSense-pkg-pfBlockerNG-4.0.0.pkg"
    ;;
esac
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    github_env = tmp_path / "github.env"
    proc = subprocess.run(
        ["sh", "-c", _tagged_intake_script()],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SOURCE_REPOSITORY": source_repository,
            "RELEASE_ID": release_id,
            "RELEASE_TAG": "v4.0.0",
            "OPERATION": "tagged-stage",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_ENV": str(github_env),
            "FAKE_GH_LOG": str(gh_log),
            "FAKE_HANDOFF": str(handoff),
            "FAKE_METADATA": str(metadata),
        },
    )
    calls = gh_log.read_text(encoding="utf-8") if gh_log.exists() else ""
    return proc, calls, github_env


def _expected_release_download(tmp_path: Path) -> str:
    return (
        "release download v4.0.0 -R pfBlockerNG/pfBlockerNG "
        "--pattern *.pkg --dir " + str(tmp_path / "runner" / "release-assets")
    )


def test_tagged_intake_executes_exact_release_and_handoff_downloads(
    tmp_path: Path,
) -> None:
    proc, calls, github_env = _run_tagged_intake(tmp_path, handoff_count=1)
    assert proc.returncode == 0, proc.stderr
    lines = calls.splitlines()
    assert lines == [
        "api repos/pfBlockerNG/pfBlockerNG/releases/123",
        (
            "api -H Accept: application/octet-stream "
            "repos/pfBlockerNG/pfBlockerNG/releases/assets/77"
        ),
        _expected_release_download(tmp_path),
    ]
    github_values = dict(
        line.split("=", 1)
        for line in github_env.read_text(encoding="utf-8").splitlines()
    )
    assert github_values["HANDOFF_FILE"].endswith(
        "/runner/release-assets/pfblockerng-release-handoff.json"
    )


def test_tagged_intake_rejects_duplicate_or_wrong_digest_handoff(
    tmp_path: Path,
) -> None:
    duplicate, duplicate_calls, _ = _run_tagged_intake(
        tmp_path / "duplicate", handoff_count=2
    )
    assert duplicate.returncode == 1
    assert "duplicate handoffs" in duplicate.stdout + duplicate.stderr
    assert "/releases/assets/" not in duplicate_calls
    assert "release download " not in duplicate_calls

    wrong, wrong_calls, _ = _run_tagged_intake(
        tmp_path / "wrong", handoff_count=1, correct_digest=False
    )
    assert wrong.returncode == 1
    assert "handoff digest mismatch" in wrong.stdout + wrong.stderr
    assert "release download " not in wrong_calls


def test_tagged_intake_without_handoff_uses_immutable_compatibility_route(
    tmp_path: Path,
) -> None:
    proc, calls, github_env = _run_tagged_intake(tmp_path, handoff_count=0)
    assert proc.returncode == 0, proc.stderr
    assert _expected_release_download(tmp_path) in calls.splitlines()
    assert "/releases/assets/" not in calls
    values = dict(
        line.split("=", 1) for line in github_env.read_text(encoding="utf-8").splitlines()
    )
    assert "HANDOFF_FILE" not in values
    expected_route = json.dumps(
        json.loads(
            (ROOT / "publication" / "compatibility-route-matrix.json").read_text(
                encoding="utf-8"
            )
        ),
        separators=(",", ":"),
    )
    assert values["ROUTE_MATRIX"] == expected_route


def test_tagged_intake_rejects_untrusted_release_metadata_before_download(
    tmp_path: Path,
) -> None:
    cases = (
        ("wrong-source", {"source_repository": "attacker/repo"}),
        ("wrong-tag", {"metadata_tag": "v4.0.1"}),
        ("draft", {"draft": True}),
        ("mutable", {"immutable": False}),
        ("missing-immutable", {"immutable": None}),
        ("malformed-id", {"release_id": "tags/v4.0.0"}),
    )
    for name, kwargs in cases:
        proc, calls, _ = _run_tagged_intake(
            tmp_path / name,
            handoff_count=1,
            **kwargs,
        )
        assert proc.returncode == 1, name
        assert "/releases/assets/" not in calls
        assert "release download " not in calls
        if name in {"wrong-source", "malformed-id"}:
            assert calls == ""


def test_tagged_intake_rejects_missing_or_unverified_packages_before_download(
    tmp_path: Path,
) -> None:
    cases = (
        ("no-package", {"include_pkg": False}),
        ("missing-digest", {"pkg_digest": None}),
        ("wrong-digest-kind", {"pkg_digest": "sha512:" + "a" * 128}),
    )
    for name, kwargs in cases:
        proc, calls, _ = _run_tagged_intake(
            tmp_path / name,
            handoff_count=1,
            **kwargs,
        )
        assert proc.returncode == 1, name
        assert "release download " not in calls


if __name__ == "__main__":
    unittest.main()
