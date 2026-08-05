"""Tests for scripts/catalogue_state.py — issue #2146 step S2 (the durable catalogue
ledger: decide/apply for one verified publish run, tagged fan-out and Nightly).

No tree assembly, no git commands, no network. Fixtures build genuine
pfb_pkg-validated build records (build_input_digest always recomputed via the
engine, never hand-typed) and construct publish_catalogues.VerifiedAsset /
RunResult objects directly — mirroring tests/test_publish_catalogues.py's own
_fabricated_asset idiom — since this module trusts RunResult as an already-
verified, opaque input (S1's job, not S2's).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalogue_state as cs
import publish_catalogues as pc
from _srcrepo import SourceRepoError, resolve_src_root

try:
    _SRC_ROOT = resolve_src_root()
    _ENGINE = cs.load_engine(_SRC_ROOT)
    _NP = _ENGINE.nightly_provenance
    _ENGINE_SKIP_REASON = ""
except (SourceRepoError, cs.EngineError) as exc:  # pragma: no cover - environment gap
    _SRC_ROOT = None
    _ENGINE = None
    _NP = None
    _ENGINE_SKIP_REASON = str(exc)

_requires_engine = unittest.skipIf(_ENGINE is None, _ENGINE_SKIP_REASON)
_REPO = pc.EXPECTED_SOURCE_REPOSITORY

# --------------------------------------------------------------------------- #
# Fixture builders.
# --------------------------------------------------------------------------- #

_TAG_FOR_CHANNEL = {"stable": "v4.0.0", "testing": "v4.0.1.b1", "edge": "v4.0.0.b1"}


def _matrix_row(**overrides: object) -> dict:
    row = {
        "pfsense_version": "2.8",
        "channel": "CE",
        "freebsd_version": "15.0-RELEASE",
        "freebsd_major": "15",
        "php_version": "8.3",
        "py_flavor": "py311",
        "variant": "CE",
        "status": "active",
        "extra_pkgs": [],
    }
    row.update(overrides)
    return row


def _record(
    *,
    channel: str = "testing",
    row: dict | None = None,
    source_sha: str = "a" * 40,
    canonical_package_version: str | None = None,
    source_tag: str | None = None,
    freebsd_ports_sha: str = "b" * 64,
) -> dict:
    """A genuine, digest-bound build record (build_input_digest via the engine)."""
    pfb_pkg = _ENGINE.pfb_pkg
    row = row or _matrix_row()
    major_minor = ".".join(row["pfsense_version"].split(".")[:2])
    tag = source_tag or _TAG_FOR_CHANNEL[channel]
    info = pfb_pkg.parse_release_tag(tag, channel)
    native = (
        pfb_pkg.CANONICAL_EMITTED_IDENTITY
        if channel == "stable"
        else f"{pfb_pkg.CANONICAL_EMITTED_IDENTITY}-{channel}"
    )
    record = {
        "schema": 1,
        "channel": channel,
        "release_line": info.release_line,
        "classification": info.stage,
        "source_tag": tag,
        "source_sha": source_sha,
        "canonical_package_version": canonical_package_version or info.pkg_version,
        "native_recipe_identity": native,
        "emitted_identity": pfb_pkg.CANONICAL_EMITTED_IDENTITY,
        "matrix_row": row,
        "freebsd_ports_sha": freebsd_ports_sha,
        "route": f"{channel}/{row['variant'].lower()}-{major_minor}",
        "source_date_epoch": 0,
        "build_input_digest": "",
    }
    record["build_input_digest"] = pfb_pkg.build_input_digest(record)
    return record


def _asset(record: dict, *, sha256: str = "1" * 64) -> pc.VerifiedAsset:
    pfb_pkg = _ENGINE.pfb_pkg
    name = f"{pfb_pkg.CANONICAL_EMITTED_IDENTITY}-{record['canonical_package_version']}.pkg"
    return pc.VerifiedAsset(
        asset_class="canonical",
        declared_name=name,
        canonical_name=name,
        work_path=Path(name),
        sha256=sha256,
        manifest={},
        record=record,
    )


def _run_result(intake: pc.Intake, asset: pc.VerifiedAsset) -> pc.RunResult:
    return pc.RunResult(
        intake=intake,
        canonical_assets=(asset,),
        dependency_assets=(),
        build_route_rows=(),
        route_only_rows=(),
    )


def _tagged(
    destinations: tuple[str, ...], *, sha256: str = "1" * 64, **record_overrides: object
):
    """Build (intake, run_result, record, varver) for one closed-set tagged tuple."""
    primary = destinations[0]
    tag = record_overrides.pop("source_tag", None) or _TAG_FOR_CHANNEL[primary]
    release_id = "1"
    intake = pc.parse_intake(
        _REPO, release_id, tag, json.dumps(list(destinations)), "10:1"
    )
    record = _record(channel=primary, source_tag=tag, **record_overrides)
    asset = _asset(record, sha256=sha256)
    run_result = _run_result(intake, asset)
    varver = record["route"].split("/", 1)[1]
    return intake, run_result, record, varver


def _seed_ledger() -> dict:
    return cs.empty_catalogue_state(engine=_ENGINE)


def _advance(state: dict, run_result: pc.RunResult, **apply_kwargs: object) -> dict:
    decision = cs.decide(state, run_result, engine=_ENGINE)
    return cs.apply(state, decision, engine=_ENGINE, **apply_kwargs)


def _nightly_allocation(
    *,
    source_sha: str = "a" * 40,
    ports_sha: str = "b" * 40,
    matrix_digest: str = "c" * 64,
    build_date=None,
):
    import datetime

    build_date = build_date or datetime.date(2026, 8, 5)
    input_digest = _NP.combined_nightly_input_digest(
        source_sha, ports_sha, matrix_digest
    )
    return _NP.allocate_nightly(
        build_date, source_sha, ports_sha, input_digest, existing=()
    )


def _nightly_handoff(
    allocation,
    *,
    source_sha: str,
    ports_sha: str,
    matrix_digest: str = "c" * 64,
    run_id: str = "555:1",
    artifact_sha256: str = "9" * 64,
    builds: list | None = None,
) -> dict:
    if builds is None:
        builds = [
            {
                "matrix_row": {},
                "record": {},
                "artifact": {
                    "abi": "FreeBSD:15:*",
                    "name": f"pfSense-pkg-pfBlockerNG-{allocation.pkg_version}.pkg",
                    "sha256": artifact_sha256,
                },
            }
        ]
    return {
        "schema": 1,
        "kind": "nightly-handoff",
        "run_id": run_id,
        "source_ref": "",
        "ports_repo": "",
        "ports_ref": "",
        "allocation": dataclasses.asdict(allocation),
        "source_sha": source_sha,
        "ports_sha": ports_sha,
        "tools_sha": "e" * 40,
        "matrix_sha": "f" * 40,
        "matrix_digest": matrix_digest,
        "build_matrix": [],
        "route_matrix": [],
        "builds": builds,
    }


# --------------------------------------------------------------------------- #
# Engine loading
# --------------------------------------------------------------------------- #


class EngineLoadingTests(unittest.TestCase):
    def test_missing_src_root_raises(self) -> None:
        with self.assertRaises(cs.EngineError):
            cs.load_engine("/nonexistent/path/does-not-exist")

    @_requires_engine
    def test_loads_real_engine(self) -> None:
        engine = cs.load_engine(_SRC_ROOT)
        self.assertTrue(hasattr(engine.nightly_provenance, "complete"))
        self.assertTrue(hasattr(engine.pfb_pkg, "pkg_version_sort_key"))


# --------------------------------------------------------------------------- #
# empty_catalogue_state / load_state / save_state / validate_state — shape
# --------------------------------------------------------------------------- #


@_requires_engine
class LedgerShapeTests(unittest.TestCase):
    def test_empty_state_shape(self) -> None:
        state = cs.empty_catalogue_state(engine=_ENGINE)
        self.assertEqual(state["schema"], 1)
        self.assertEqual(state["generation"], 0)
        self.assertEqual(
            set(state["channels"]), {"stable", "testing", "edge", "nightly"}
        )
        self.assertEqual(state["channels"]["stable"], {})
        self.assertEqual(state["channels"]["nightly"], _NP.empty_state())

    def test_empty_state_validates(self) -> None:
        state = cs.empty_catalogue_state(engine=_ENGINE)
        self.assertEqual(cs.validate_state(state, engine=_ENGINE), state)

    def test_save_and_load_round_trip(self) -> None:
        _, run_result, _, _ = _tagged(("testing",))
        state = _advance(_seed_ledger(), run_result)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "catalogue-state.json"
            cs.save_state(path, state, engine=_ENGINE)
            loaded = cs.load_state(path, engine=_ENGINE)
        self.assertEqual(loaded, state)


# --------------------------------------------------------------------------- #
# Hostile ledger-document rows — validate_state fails closed.
# --------------------------------------------------------------------------- #


@_requires_engine
class HostileLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        _, run_result, self.record, self.varver = _tagged(("testing",))
        self.state = _advance(_seed_ledger(), run_result)
        self.entry = self.state["channels"]["testing"][self.varver][0]

    def test_not_an_object_rejected(self) -> None:
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state("not-an-object", engine=_ENGINE)

    def test_missing_key_rejected(self) -> None:
        bad = dict(self.state)
        del bad["channels"]
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(bad, engine=_ENGINE)

    def test_unknown_key_rejected(self) -> None:
        bad = dict(self.state)
        bad["extra"] = 1
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(bad, engine=_ENGINE)

    def test_schema_zero_rejected(self) -> None:
        bad = dict(self.state)
        bad["schema"] = 0
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(bad, engine=_ENGINE)

    def test_schema_two_rejected(self) -> None:
        bad = dict(self.state)
        bad["schema"] = 2
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(bad, engine=_ENGINE)

    def test_schema_string_rejected(self) -> None:
        bad = dict(self.state)
        bad["schema"] = "1"
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(bad, engine=_ENGINE)

    def test_generation_negative_rejected(self) -> None:
        bad = dict(self.state)
        bad["generation"] = -1
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(bad, engine=_ENGINE)

    def test_generation_float_rejected(self) -> None:
        bad = dict(self.state)
        bad["generation"] = 1.0
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(bad, engine=_ENGINE)

    def test_generation_string_rejected(self) -> None:
        bad = dict(self.state)
        bad["generation"] = "1"
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(bad, engine=_ENGINE)

    def test_generation_disagrees_with_record_count_rejected(self) -> None:
        bad = dict(self.state)
        bad["generation"] = self.state["generation"] + 5
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(bad, engine=_ENGINE)

    def test_duplicate_entry_rejected(self) -> None:
        bad = copy.deepcopy(self.state)
        bad["channels"]["testing"][self.varver].append(dict(self.entry))
        bad["generation"] += 1
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(bad, engine=_ENGINE)

    def _with_bad_path(self, path: str) -> dict:
        bad = copy.deepcopy(self.state)
        bad["channels"]["testing"][self.varver][0]["path"] = path
        return bad

    def test_path_dotdot_rejected(self) -> None:
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(
                self._with_bad_path(f"testing/{self.varver}/../evil.pkg"),
                engine=_ENGINE,
            )

    def test_path_absolute_rejected(self) -> None:
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(
                self._with_bad_path(f"/testing/{self.varver}/x.pkg"), engine=_ENGINE
            )

    def test_path_backslash_rejected(self) -> None:
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(
                self._with_bad_path(f"testing\\{self.varver}\\x.pkg"), engine=_ENGINE
            )

    def test_path_nul_rejected(self) -> None:
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(
                self._with_bad_path(f"testing/{self.varver}/x\x00.pkg"), engine=_ENGINE
            )

    def test_path_newline_rejected(self) -> None:
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(
                self._with_bad_path(f"testing/{self.varver}/x\n.pkg"), engine=_ENGINE
            )

    def test_path_not_under_channel_varver_rejected(self) -> None:
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(
                self._with_bad_path(f"edge/{self.varver}/x.pkg"), engine=_ENGINE
            )

    def test_path_wrong_extension_rejected(self) -> None:
        """Correct channel/varver prefix, but the basename is not a safe .pkg
        filename — exercises the filename-shape check independent of the prefix
        check above."""
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(
                self._with_bad_path(f"testing/{self.varver}/notapackage.txt"),
                engine=_ENGINE,
            )

    def _with_bad_sha256(self, sha256: str) -> dict:
        bad = copy.deepcopy(self.state)
        bad["channels"]["testing"][self.varver][0]["sha256"] = sha256
        return bad

    def test_sha256_wrong_length_rejected(self) -> None:
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(self._with_bad_sha256("1" * 63), engine=_ENGINE)

    def test_sha256_uppercase_rejected(self) -> None:
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(self._with_bad_sha256("A" * 64), engine=_ENGINE)

    def test_sha256_non_hex_rejected(self) -> None:
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(self._with_bad_sha256("z" * 64), engine=_ENGINE)

    def test_unknown_channel_key_rejected(self) -> None:
        bad = copy.deepcopy(self.state)
        bad["channels"]["bogus"] = {}
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(bad, engine=_ENGINE)

    def test_varver_key_with_slash_rejected(self) -> None:
        bad = copy.deepcopy(self.state)
        bad["channels"]["testing"]["ce-2.8/evil"] = []
        with self.assertRaises(cs.CatalogueStateError):
            cs.validate_state(bad, engine=_ENGINE)

    def test_json_duplicate_keys_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "dup.json"
            path.write_text(
                '{"schema": 1, "schema": 1, "generation": 0, "updated_by": {}, "channels": {}}'
            )
            with self.assertRaises(cs.CatalogueStateError):
                cs.load_state(path, engine=_ENGINE)


# --------------------------------------------------------------------------- #
# decide() — tagged fan-out, the closed destination-tuple set.
# --------------------------------------------------------------------------- #


@_requires_engine
class DecideTaggedTests(unittest.TestCase):
    def test_destinations_edge(self) -> None:
        self._advance_and_check(("edge",))

    def test_destinations_testing(self) -> None:
        self._advance_and_check(("testing",))

    def test_destinations_testing_edge(self) -> None:
        self._advance_and_check(("testing", "edge"))

    def test_destinations_stable_testing(self) -> None:
        self._advance_and_check(("stable", "testing"))

    def test_destinations_stable_testing_edge(self) -> None:
        self._advance_and_check(("stable", "testing", "edge"))

    def _advance_and_check(self, destinations: tuple[str, ...]) -> None:
        _, run_result, record, varver = _tagged(destinations)
        decision = cs.decide(_seed_ledger(), run_result, engine=_ENGINE)
        self.assertEqual(decision.kind, "advance")
        self.assertEqual(
            {c for c, _v, _e in decision.channel_entries}, set(destinations)
        )
        state = cs.apply(_seed_ledger(), decision, engine=_ENGINE)
        for channel in destinations:
            entries = state["channels"][channel][varver]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["sha256"], "1" * 64)
            self.assertEqual(entries[0]["record"], record)

    def test_nightly_run_result_rejected(self) -> None:
        intake = pc.parse_intake(_REPO, "", "", '["nightly"]', "10:1")
        pfb_pkg = _ENGINE.pfb_pkg
        row = _matrix_row()
        record = {
            "schema": 1,
            "channel": "nightly",
            "release_line": "nightly",
            "classification": "nightly",
            "source_tag": None,
            "source_sha": "a" * 40,
            "canonical_package_version": "20260805",
            "native_recipe_identity": "pfSense-pkg-pfBlockerNG-nightly",
            "emitted_identity": pfb_pkg.CANONICAL_EMITTED_IDENTITY,
            "matrix_row": row,
            "freebsd_ports_sha": "b" * 64,
            "route": "nightly/ce-2.8",
            "source_date_epoch": 0,
            "build_input_digest": "",
        }
        record["build_input_digest"] = pfb_pkg.build_input_digest(record)
        run_result = _run_result(intake, _asset(record))
        with self.assertRaises(cs.CatalogueStateError):
            cs.decide(_seed_ledger(), run_result, engine=_ENGINE)


@_requires_engine
class DecideOutcomeTests(unittest.TestCase):
    def test_noop_on_identical_replay(self) -> None:
        _, run_result, _, _ = _tagged(("testing",))
        state = _advance(_seed_ledger(), run_result)
        decision = cs.decide(state, run_result, engine=_ENGINE)
        self.assertEqual(decision.kind, "noop")
        self.assertEqual(decision.generation_read, state["generation"])

    def test_advance_on_first_publish(self) -> None:
        _, run_result, _, _ = _tagged(("testing",))
        decision = cs.decide(_seed_ledger(), run_result, engine=_ENGINE)
        self.assertEqual(decision.kind, "advance")

    def test_partial_match_fills_the_gap(self) -> None:
        """testing already published; edge is the gap this run fills."""
        _, testing_only, record, varver = _tagged(("testing",))
        state = _advance(_seed_ledger(), testing_only)
        _, both, _record2, _varver2 = _tagged(
            ("testing", "edge"), source_tag=record["source_tag"]
        )
        decision = cs.decide(state, both, engine=_ENGINE)
        self.assertEqual(decision.kind, "advance")
        self.assertEqual({c for c, _v, _e in decision.channel_entries}, {"edge"})
        new_state = cs.apply(state, decision, engine=_ENGINE)
        self.assertEqual(
            new_state["channels"]["edge"][varver][0]["sha256"],
            new_state["channels"]["testing"][varver][0]["sha256"],
        )

    def test_reject_divergent_bytes(self) -> None:
        _, run_result, record, _ = _tagged(("testing",), sha256="1" * 64)
        state = _advance(_seed_ledger(), run_result)
        _, run_result_2, _r2, _v2 = _tagged(
            ("testing",), sha256="2" * 64, source_tag=record["source_tag"]
        )
        with self.assertRaisesRegex(cs.CatalogueStateError, "different bytes"):
            cs.decide(state, run_result_2, engine=_ENGINE)

    def test_reject_divergent_provenance(self) -> None:
        _, run_result, record, _ = _tagged(("testing",), sha256="1" * 64)
        state = _advance(_seed_ledger(), run_result)
        _, run_result_2, _r2, _v2 = _tagged(
            ("testing",),
            sha256="1" * 64,
            source_tag=record["source_tag"],
            freebsd_ports_sha="c" * 64,
        )
        with self.assertRaisesRegex(cs.CatalogueStateError, "different provenance"):
            cs.decide(state, run_result_2, engine=_ENGINE)

    def test_reject_partial_edge_mismatch(self) -> None:
        """testing matches this run; edge already diverges — rejected even though
        testing alone would have been a no-op-per-channel match."""
        _, testing_only, record, varver = _tagged(("testing",), sha256="1" * 64)
        state = _advance(_seed_ledger(), testing_only)
        # Corrupt edge's ledger with an "independently built" entry at the same
        # version but different bytes (simulating an earlier bad edge publish).
        bad_edge_entry = dict(state["channels"]["testing"][varver][0])
        bad_edge_entry["sha256"] = "3" * 64
        bad_edge_entry["path"] = (
            f"edge/{varver}/{bad_edge_entry['path'].rsplit('/', 1)[1]}"
        )
        bad_edge_entry["record"] = dict(bad_edge_entry["record"])
        state = copy.deepcopy(state)
        state["channels"]["edge"][varver] = [bad_edge_entry]
        state["generation"] += 1
        _, both, _r2, _v2 = _tagged(
            ("testing", "edge"), sha256="1" * 64, source_tag=record["source_tag"]
        )
        with self.assertRaisesRegex(cs.CatalogueStateError, r"^edge/"):
            cs.decide(state, both, engine=_ENGINE)


# --------------------------------------------------------------------------- #
# apply() specifics
# --------------------------------------------------------------------------- #


@_requires_engine
class ApplyTests(unittest.TestCase):
    def test_apply_noop_rejected(self) -> None:
        _, run_result, _r, _v = _tagged(("testing",))
        state = _advance(_seed_ledger(), run_result)
        decision = cs.decide(state, run_result, engine=_ENGINE)
        self.assertEqual(decision.kind, "noop")
        with self.assertRaises(cs.CatalogueStateError):
            cs.apply(state, decision, engine=_ENGINE)

    def test_apply_stale_generation_rejected(self) -> None:
        _, run_result, _r, _v = _tagged(("testing",))
        decision = cs.decide(_seed_ledger(), run_result, engine=_ENGINE)
        stale_decision = dataclasses.replace(
            decision, generation_read=decision.generation_read + 1
        )
        with self.assertRaises(cs.CatalogueStateError):
            cs.apply(_seed_ledger(), stale_decision, engine=_ENGINE)

    def test_apply_decision_with_both_payloads_rejected(self) -> None:
        _, run_result, _r, _v = _tagged(("testing",))
        decision = cs.decide(_seed_ledger(), run_result, engine=_ENGINE)
        bad_decision = dataclasses.replace(decision, nightly_state=_NP.empty_state())
        with self.assertRaises(cs.CatalogueStateError):
            cs.apply(_seed_ledger(), bad_decision, engine=_ENGINE)


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


@_requires_engine
class RetentionTests(unittest.TestCase):
    def _keep(self, n: int):
        return lambda channel, varver: n

    def _publish(
        self, state: dict, version_suffix: str, *, sha256: str, row: dict | None = None
    ) -> tuple[dict, str]:
        tag = f"v4.0.1.b{version_suffix}"
        record = _record(channel="testing", source_tag=tag, row=row or _matrix_row())
        asset = _asset(record, sha256=sha256)
        intake = pc.parse_intake(_REPO, "1", tag, '["testing"]', "10:1")
        run_result = _run_result(intake, asset)
        new_state = _advance(state, run_result, keep_count_for=self._keep(2))
        varver = record["route"].split("/", 1)[1]
        return new_state, varver

    def test_below_keep_count(self) -> None:
        state, varver = self._publish(_seed_ledger(), "1", sha256="1" * 64)
        self.assertEqual(len(state["channels"]["testing"][varver]), 1)

    def test_exactly_at_keep_count(self) -> None:
        state, varver = self._publish(_seed_ledger(), "1", sha256="1" * 64)
        state, varver = self._publish(state, "2", sha256="2" * 64)
        self.assertEqual(len(state["channels"]["testing"][varver]), 2)
        versions = {e["version"] for e in state["channels"]["testing"][varver]}
        self.assertEqual(versions, {"4.0.1.b1", "4.0.1.b2"})

    def test_above_keep_count_evicts_oldest(self) -> None:
        state, varver = self._publish(_seed_ledger(), "1", sha256="1" * 64)
        state, varver = self._publish(state, "2", sha256="2" * 64)
        state, varver = self._publish(state, "3", sha256="3" * 64)
        entries = state["channels"]["testing"][varver]
        self.assertEqual(len(entries), 2)
        versions = {e["version"] for e in entries}
        self.assertEqual(versions, {"4.0.1.b2", "4.0.1.b3"})

    def test_keep_count_of_one(self) -> None:
        row = _matrix_row()
        record1 = _record(channel="testing", source_tag="v4.0.1.b1", row=row)
        asset1 = _asset(record1, sha256="1" * 64)
        intake1 = pc.parse_intake(_REPO, "1", "v4.0.1.b1", '["testing"]', "10:1")
        state = _advance(
            _seed_ledger(), _run_result(intake1, asset1), keep_count_for=self._keep(1)
        )
        record2 = _record(channel="testing", source_tag="v4.0.1.b2", row=row)
        asset2 = _asset(record2, sha256="2" * 64)
        intake2 = pc.parse_intake(_REPO, "1", "v4.0.1.b2", '["testing"]', "10:1")
        state = _advance(
            state, _run_result(intake2, asset2), keep_count_for=self._keep(1)
        )
        varver = record1["route"].split("/", 1)[1]
        entries = state["channels"]["testing"][varver]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["version"], "4.0.1.b2")

    def test_two_varvers_evict_independently(self) -> None:
        state = _seed_ledger()
        row_a = _matrix_row(pfsense_version="2.8")
        row_b = _matrix_row(pfsense_version="2.9")
        for suffix, sha in (("1", "1"), ("2", "2"), ("3", "3")):
            state, varver_a = self._publish(state, suffix, sha256=sha * 64, row=row_a)
        for suffix, sha in (("1", "1"), ("2", "2"), ("3", "3")):
            state, varver_b = self._publish(state, suffix, sha256=sha * 64, row=row_b)
        self.assertEqual(
            {e["version"] for e in state["channels"]["testing"][varver_a]},
            {"4.0.1.b2", "4.0.1.b3"},
        )
        self.assertEqual(
            {e["version"] for e in state["channels"]["testing"][varver_b]},
            {"4.0.1.b2", "4.0.1.b3"},
        )


# --------------------------------------------------------------------------- #
# decide_nightly() — reuse of nightly_provenance.complete.
# --------------------------------------------------------------------------- #


@_requires_engine
class DecideNightlyTests(unittest.TestCase):
    def test_accepted_advance(self) -> None:
        allocation = _nightly_allocation()
        handoff = _nightly_handoff(allocation, source_sha="a" * 40, ports_sha="b" * 40)
        decision = cs.decide_nightly(
            _seed_ledger(),
            handoff,
            run_id="777:1",
            source_repository=_REPO,
            engine=_ENGINE,
        )
        self.assertEqual(decision.kind, "advance")
        self.assertEqual(decision.nightly_state["generation"], 1)

    def test_noop_replay(self) -> None:
        allocation = _nightly_allocation()
        handoff = _nightly_handoff(allocation, source_sha="a" * 40, ports_sha="b" * 40)
        state = _seed_ledger()
        decision = cs.decide_nightly(
            state, handoff, run_id="777:1", source_repository=_REPO, engine=_ENGINE
        )
        state = cs.apply(state, decision, engine=_ENGINE)
        decision2 = cs.decide_nightly(
            state, handoff, run_id="777:1", source_repository=_REPO, engine=_ENGINE
        )
        self.assertEqual(decision2.kind, "noop")

    def test_artifact_bytes_collision_rejected(self) -> None:
        allocation = _nightly_allocation()
        handoff1 = _nightly_handoff(
            allocation,
            source_sha="a" * 40,
            ports_sha="b" * 40,
            artifact_sha256="9" * 64,
        )
        state = _seed_ledger()
        decision = cs.decide_nightly(
            state, handoff1, run_id="777:1", source_repository=_REPO, engine=_ENGINE
        )
        state = cs.apply(state, decision, engine=_ENGINE)
        handoff2 = _nightly_handoff(
            allocation,
            source_sha="a" * 40,
            ports_sha="b" * 40,
            artifact_sha256="8" * 64,
        )
        with self.assertRaisesRegex(cs.CatalogueStateError, "artifact bytes collide"):
            cs.decide_nightly(
                state, handoff2, run_id="778:1", source_repository=_REPO, engine=_ENGINE
            )

    def test_version_collision_for_different_inputs_rejected(self) -> None:
        allocation = _nightly_allocation(source_sha="a" * 40, ports_sha="b" * 40)
        handoff1 = _nightly_handoff(allocation, source_sha="a" * 40, ports_sha="b" * 40)
        state = _seed_ledger()
        decision = cs.decide_nightly(
            state, handoff1, run_id="777:1", source_repository=_REPO, engine=_ENGINE
        )
        state = cs.apply(state, decision, engine=_ENGINE)

        other_source_sha = "c" * 40
        other_input_digest = _NP.combined_nightly_input_digest(
            other_source_sha, "b" * 40, "c" * 64
        )
        colliding_allocation = _NP.NightlyAllocation(
            outcome="build",
            portversion=allocation.portversion,
            portrevision=allocation.portrevision,
            pkg_version=allocation.pkg_version,
            source_sha=other_source_sha,
            ports_sha="b" * 40,
            input_digest=other_input_digest,
        )
        handoff2 = _nightly_handoff(
            colliding_allocation, source_sha=other_source_sha, ports_sha="b" * 40
        )
        with self.assertRaisesRegex(cs.CatalogueStateError, "version collision"):
            cs.decide_nightly(
                state, handoff2, run_id="778:1", source_repository=_REPO, engine=_ENGINE
            )

    def test_missing_artifacts_rejected(self) -> None:
        allocation = _nightly_allocation()
        handoff = _nightly_handoff(
            allocation, source_sha="a" * 40, ports_sha="b" * 40, builds=[]
        )
        with self.assertRaisesRegex(cs.CatalogueStateError, "requires artifacts"):
            cs.decide_nightly(
                _seed_ledger(),
                handoff,
                run_id="777:1",
                source_repository=_REPO,
                engine=_ENGINE,
            )

    # --- hostile Nightly rows ---

    def test_input_digest_mismatch_rejected(self) -> None:
        allocation = _nightly_allocation()
        handoff = _nightly_handoff(allocation, source_sha="a" * 40, ports_sha="b" * 40)
        handoff["allocation"]["input_digest"] = "f" * 64  # well-shaped, but wrong
        with self.assertRaisesRegex(
            cs.CatalogueStateError, "does not match verified handoff"
        ):
            cs.decide_nightly(
                _seed_ledger(),
                handoff,
                run_id="777:1",
                source_repository=_REPO,
                engine=_ENGINE,
            )

    def test_allocation_outcome_not_build_rejected(self) -> None:
        allocation = _nightly_allocation()
        unchanged = dataclasses.replace(allocation, outcome="unchanged")
        handoff = _nightly_handoff(unchanged, source_sha="a" * 40, ports_sha="b" * 40)
        with self.assertRaisesRegex(cs.CatalogueStateError, "outcome must be 'build'"):
            cs.decide_nightly(
                _seed_ledger(),
                handoff,
                run_id="777:1",
                source_repository=_REPO,
                engine=_ENGINE,
            )

    def test_run_id_empty_rejected(self) -> None:
        allocation = _nightly_allocation()
        handoff = _nightly_handoff(allocation, source_sha="a" * 40, ports_sha="b" * 40)
        with self.assertRaises(cs.CatalogueStateError):
            cs.decide_nightly(
                _seed_ledger(),
                handoff,
                run_id="",
                source_repository=_REPO,
                engine=_ENGINE,
            )

    def test_run_id_too_long_rejected(self) -> None:
        allocation = _nightly_allocation()
        handoff = _nightly_handoff(allocation, source_sha="a" * 40, ports_sha="b" * 40)
        with self.assertRaises(cs.CatalogueStateError):
            cs.decide_nightly(
                _seed_ledger(),
                handoff,
                run_id="7" * 129,
                source_repository=_REPO,
                engine=_ENGINE,
            )

    def test_run_id_non_ascii_rejected(self) -> None:
        allocation = _nightly_allocation()
        handoff = _nightly_handoff(allocation, source_sha="a" * 40, ports_sha="b" * 40)
        with self.assertRaises(cs.CatalogueStateError):
            cs.decide_nightly(
                _seed_ledger(),
                handoff,
                run_id="777:é",
                source_repository=_REPO,
                engine=_ENGINE,
            )


if __name__ == "__main__":
    unittest.main()
