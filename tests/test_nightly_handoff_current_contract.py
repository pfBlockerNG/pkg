"""Regression coverage for the retained dependency-bound Nightly handoff."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pfb_pkg
import publish_nightly as pn

_RUN_ID = "33035100506:1"
_FIXTURE = Path(__file__).parent / "fixtures/nightly-handoff-current-contract.json"
_FIXTURE_SHA256 = "fb9d00a0a2806c9f324c5508dc4ead65d06f54bab5fe481dddf93905dc9f8adf"
_INPUT_DIGEST = "13184e7f7587d17f82a11ff622e965fa05b480b28c1b80c96548111bd7e129a5"
_TOP_FIELDS = {
    "schema",
    "kind",
    "run_id",
    "source_ref",
    "ports_repo",
    "ports_ref",
    "pkg_version",
    "input_digest",
    "source_sha",
    "ports_sha",
    "tools_sha",
    "matrix_sha",
    "matrix_digest",
    "source_date_epoch",
    "dependency_builder",
    "build_matrix",
    "route_matrix",
    "builds",
}
_RECORD_FIELDS = {
    "schema",
    "channel",
    "release_line",
    "classification",
    "source_tag",
    "source_sha",
    "canonical_package_version",
    "native_recipe_identity",
    "emitted_identity",
    "matrix_row",
    "freebsd_ports_sha",
    "route",
    "source_date_epoch",
    "dependency_builder",
    "build_input_digest",
}
_BUILDER = {
    "python": "3.11.15",
    "pip": "26.2.1",
    "setuptools": "75.6.0",
    "wheel": "0.45.1",
    "zstandard": "0.25.0",
    "uv": "0.12.6",
    "uv_lock_sha256": "2d9aa34742bd0a43e69c8cc1216e23130145369b7ac32a5603e5eb42094d00d9",
}


def _handoff() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _current_input_digest(handoff: dict[str, Any]) -> str:
    dependency_payload = json.dumps(
        {
            "matrix_digest": handoff["matrix_digest"],
            "source_date_epoch": handoff["source_date_epoch"],
            "dependency_builder": handoff["dependency_builder"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    dependency_digest = hashlib.sha256(dependency_payload).hexdigest()
    combined_payload = "\0".join(
        (handoff["source_sha"], handoff["ports_sha"], dependency_digest)
    ).encode("ascii")
    return hashlib.sha256(combined_payload).hexdigest()


def _refresh_record_digest(record: dict[str, Any]) -> None:
    record["build_input_digest"] = pfb_pkg.build_input_digest(record)


def _assert_rejected(handoff: dict[str, Any], *messages: str) -> None:
    with pytest.raises(pn.PublishNightlyError) as caught:
        pn._validate_handoff(handoff, source_run_id=_RUN_ID)
    rendered = str(caught.value)
    for message in messages:
        assert message in rendered, rendered


def test_retained_current_handoff_is_accepted_with_exact_provenance() -> None:
    raw = _FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _FIXTURE_SHA256
    handoff = json.loads(raw)

    assert set(handoff) == _TOP_FIELDS
    assert handoff["pkg_version"] == "20260827030157.f3c7e31"
    assert handoff["run_id"] == _RUN_ID
    assert handoff["source_ref"] == "f3c7e3178eff885d1c681b638c8c0044f17cfda2"
    assert handoff["source_sha"] == "f3c7e3178eff885d1c681b638c8c0044f17cfda2"
    assert handoff["ports_sha"] == "dda1c42793ba8f6b78cf5472ca6db25f7a0a13c2"
    assert handoff["matrix_digest"] == "a7b2d53068643b7cb91a1ecc3d6119426d0c9d6ed311bee4395e99be23b09f19"
    assert handoff["source_date_epoch"] == 1787797418
    assert type(handoff["source_date_epoch"]) is int
    assert handoff["dependency_builder"] == _BUILDER
    assert handoff["input_digest"] == _INPUT_DIGEST
    assert _current_input_digest(handoff) == _INPUT_DIGEST
    assert {
        (row["variant"], row["pfsense_version"], row["freebsd_major"])
        for row in handoff["route_matrix"]
    } == {
        ("CE", "2.8", "15"),
        ("CE", "2.9", "16"),
        ("Plus", "26.03", "16"),
        ("Plus", "26.07", "16"),
    }
    assert [
        (row["variant"], row["pfsense_version"], row["freebsd_major"])
        for row in handoff["build_matrix"]
    ] == [("CE", "2.8", "15"), ("CE", "2.9", "16")]
    for build in handoff["builds"]:
        assert set(build) == {"matrix_row", "record", "artifact", "dep_artifacts"}
        assert set(build["record"]) == _RECORD_FIELDS
        assert build["record"]["source_date_epoch"] == handoff["source_date_epoch"]
        assert build["record"]["dependency_builder"] == handoff["dependency_builder"]

    validated = pn._validate_handoff(handoff, source_run_id=_RUN_ID)
    assert validated.pkg_version == handoff["pkg_version"]
    assert validated.source_sha == handoff["source_sha"]
    assert validated.ports_sha == handoff["ports_sha"]
    assert len(validated.builds) == 2


@pytest.mark.parametrize("field", ["source_date_epoch", "dependency_builder"])
def test_new_top_level_fields_are_required(field: str) -> None:
    handoff = _handoff()
    del handoff[field]
    _assert_rejected(handoff, "exact fields", field)


def test_unknown_top_level_field_is_rejected() -> None:
    handoff = _handoff()
    handoff["unexpected"] = None
    _assert_rejected(handoff, "exact fields", "unknown=['unexpected']")


@pytest.mark.parametrize(
    "bad_epoch",
    [True, 1787797418.0, "1787797418", -1],
    ids=["bool", "float", "string", "negative"],
)
def test_source_date_epoch_requires_an_exact_non_negative_integer(
    bad_epoch: object,
) -> None:
    handoff = _handoff()
    handoff["source_date_epoch"] = bad_epoch
    _assert_rejected(handoff, "source_date_epoch", "non-negative integer")


@pytest.mark.parametrize("bad_builder", [None, []], ids=["null", "list"])
def test_dependency_builder_must_be_an_object(bad_builder: object) -> None:
    handoff = _handoff()
    handoff["dependency_builder"] = bad_builder
    _assert_rejected(handoff, "dependency_builder", "exact fields")


@pytest.mark.parametrize("shape", ["missing", "extra"])
def test_dependency_builder_requires_exact_fields(shape: str) -> None:
    handoff = _handoff()
    builder = handoff["dependency_builder"]
    if shape == "missing":
        del builder["pip"]
    else:
        builder["unexpected"] = "1.0"
    _assert_rejected(handoff, "dependency_builder", "exact fields")


@pytest.mark.parametrize(
    ("field", "bad_version"),
    [("wheel", 0.451), ("python", "3")],
    ids=["numeric-equal-looking", "malformed"],
)
def test_dependency_builder_versions_are_strict_strings(
    field: str, bad_version: object
) -> None:
    handoff = _handoff()
    handoff["dependency_builder"][field] = bad_version
    _assert_rejected(handoff, f"dependency_builder.{field}", "malformed")


@pytest.mark.parametrize(
    "bad_lock", ["A" * 64, "a" * 63], ids=["uppercase", "wrong-size"]
)
def test_dependency_builder_lock_is_lowercase_sha256(bad_lock: str) -> None:
    handoff = _handoff()
    handoff["dependency_builder"]["uv_lock_sha256"] = bad_lock
    _assert_rejected(handoff, "dependency_builder.uv_lock_sha256", "lowercase SHA-256")


@pytest.mark.parametrize("build_index", [0, 1], ids=["freebsd-15", "freebsd-16"])
def test_every_build_record_requires_dependency_builder(build_index: int) -> None:
    handoff = _handoff()
    del handoff["builds"][build_index]["record"]["dependency_builder"]
    _assert_rejected(handoff, "build record", "dependency_builder")


@pytest.mark.parametrize("build_indexes", [(0,), (0, 1)], ids=["one-leg", "every-leg"])
def test_build_record_epoch_must_match_handoff(build_indexes: tuple[int, ...]) -> None:
    handoff = _handoff()
    for index in build_indexes:
        record = handoff["builds"][index]["record"]
        record["source_date_epoch"] = handoff["source_date_epoch"] + 1
        _refresh_record_digest(record)
    _assert_rejected(handoff, "build record", "source_date_epoch", "provenance")


@pytest.mark.parametrize("build_indexes", [(0,), (0, 1)], ids=["one-leg", "every-leg"])
def test_build_record_builder_must_match_handoff(build_indexes: tuple[int, ...]) -> None:
    handoff = _handoff()
    for index in build_indexes:
        record = handoff["builds"][index]["record"]
        record["dependency_builder"]["uv"] = "0.12.7"
        _refresh_record_digest(record)
    _assert_rejected(handoff, "build record", "dependency_builder", "provenance")


@pytest.mark.parametrize("digest_kind", ["old-three-part", "drifted"])
def test_input_digest_must_bind_current_dependency_contract(digest_kind: str) -> None:
    handoff = _handoff()
    if digest_kind == "old-three-part":
        old_payload = "\0".join(
            (handoff["source_sha"], handoff["ports_sha"], handoff["matrix_digest"])
        ).encode("ascii")
        handoff["input_digest"] = hashlib.sha256(old_payload).hexdigest()
    else:
        handoff["input_digest"] = "0" * 64
    assert handoff["input_digest"] != _INPUT_DIGEST
    _assert_rejected(handoff, "input_digest", "dependency")


@pytest.mark.parametrize("field", ["source_date_epoch", "dependency_builder"])
def test_recomputed_digest_cannot_hide_top_level_build_provenance_drift(
    field: str,
) -> None:
    handoff = _handoff()
    if field == "source_date_epoch":
        handoff[field] += 1
    else:
        handoff[field]["uv"] = "0.12.7"
    handoff["input_digest"] = _current_input_digest(handoff)
    assert handoff["input_digest"] != _INPUT_DIGEST
    _assert_rejected(handoff, "build record", field, "provenance")
