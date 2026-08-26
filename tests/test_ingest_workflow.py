from __future__ import annotations

import json
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
            {("CE", "2.8"), ("CE", "2.9"), ("Plus", "26.03"), ("Plus", "26.07")},
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
        self.assertNotIn('oras manifest delete "$ARTIFACT_REF"', text)
        self.assertNotRegex(text, r'oras pull "?[^\n]*:(?:latest|nightly)')
        self.assertLess(
            text.index("source_run_id does not match OCI handoff"),
            text.index("python3 scripts/verify_nightly_publication.py"),
        )
        self.assertRegex(
            text, r"(?m)^          python3 scripts/verify_nightly_publication\.py \\$"
        )
        cleanup_step = text[
            text.index("- name: Delete consumed Nightly OCI manifest") : text.index(
                "- name: Record ingestion result"
            )
        ]
        for contract in (
            "SOURCE_RUN_ID: ${{ inputs.source_run_id }}",
            "NIGHTLY_VERSION: ${{ inputs.nightly_version }}",
            'git fetch --no-tags origin "+refs/heads/main:refs/remotes/origin/main"',
            '--source-run-id "$SOURCE_RUN_ID"',
            '--nightly-version "$NIGHTLY_VERSION"',
            '--artifact-ref "$ARTIFACT_REF"',
        ):
            self.assertIn(contract, cleanup_step)
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


if __name__ == "__main__":
    unittest.main()
