# catalogue_engine.py — repository-local catalogue assembly and signing.
#
# It reads already-built .pkg files, validates their manifests, and emits the
# arch-less FreeBSD catalogue descriptors used by the pkg publisher. It has no
# package-builder, matrix-producer, retention-policy, or CLI surface.

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TypeGuard

from pfb_pkg import (
    CANONICAL_EMITTED_IDENTITY,
    PFB_BUILD_RECORD_KEY,
    PkgError,
    load_build_record,
    read_compact_manifest,
    validate_project_pkg,
    zstd_compress,
)

# --------------------------------------------------------------------------- #
# Catalog descriptor (meta.conf / meta) — byte-identical to real `pkg repo`.
# --------------------------------------------------------------------------- #

META_CONF = (
    "version = 2;\n"
    'packing_format = "tzst";\n'
    'manifests = "packagesite.yaml";\n'
    'data = "data";\n'
    'filesite = "files";\n'
    'manifests_archive = "packagesite";\n'
    'filesite_archive = "files";\n'
)

# A NO_ARCH package's manifest ABI wildcards ONLY the final CPU segment — e.g.
# "FreeBSD:15:*" (probed live against a real Netgate noarch package; issue
# #1806). Tight: '*' is valid ONLY as the whole final segment, never elsewhere
# and never partial — "FreeBSD:*:amd64" / "*" / "FreeBSD:15:*extra" are not ABIs.
_ABI_WILDCARD_RE = re.compile(r"^[A-Za-z0-9._+-]+:[A-Za-z0-9._+-]+:\*$")


def _is_wildcard_abi(abi: object) -> TypeGuard[str]:
    """True if ``abi`` is a NO_ARCH package's tight, CPU-wildcarded ABI string."""
    return isinstance(abi, str) and bool(_ABI_WILDCARD_RE.fullmatch(abi))


# A .pkg's manifest name/version become the published filename ``<name>-<version>.pkg``.
# The manifest is READ from the package (attacker-controlled input, never derived), so
# both fields get a segment guard too. Wider class than a varver — real pkg names and
# versions carry uppercase, '_' (port revision, "1.0_1") and ',' (PORTEPOCH, "1.0,1").
_PKG_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._,+-]*$")


class BuildRepoError(Exception):
    """A fatal, user-facing error (bad input / collision / missing tool)."""


def _require_wildcard_abi(path: Path, abi: object) -> str:
    """Validate one NO_ARCH package ABI."""
    if not _is_wildcard_abi(abi):
        raise BuildRepoError(
            f"{path}: package ABI {abi!r} is not CPU-wildcarded; "
            "ship a wildcard-ABI (NO_ARCH) build instead"
        )
    return abi


def _safe_segment(value: object, *, what: str, pattern: re.Pattern[str]) -> str:
    """Return ``value`` if it is safe to use as ONE path segment, else raise.

    Everything joined onto the output root here is either read from an untrusted
    manifest or passed in from the CLI/matrix, and the destination directory is
    ``rmtree``'d before it is rebuilt — so an unvalidated segment is an arbitrary
    directory wipe (``".."``) or an escape from the output root entirely (an
    absolute path makes ``Path.__truediv__`` discard its left operand). Mirrors the
    guard ``scripts/build-repo.sh`` applies to ``--varver``.
    """
    if not isinstance(value, str) or not pattern.fullmatch(value) or ".." in value:
        raise BuildRepoError(
            f"unsafe or invalid {what}: {value!r} — must be a single path segment "
            f"matching {pattern.pattern} with no '..' (it becomes an rm -rf'd directory)"
        )
    return value


# --------------------------------------------------------------------------- #
# libpkg checksum type 2:  "2$" + z-base-32(blake2b(file bytes))
#
# blake2b default digest = 64 bytes; z-base-32 (RFC-less human base32, alphabet
# "ybndrfg8ejkmcpqxot1uwisza345h769") packs the bit stream LSB-FIRST within each
# byte — matching libpkg's pkg_checksum_encode_base32(). Cracked against a real
# `pkg repo` oracle. 64 bytes -> 103 base32 chars (ceil(64*8/5)).
# --------------------------------------------------------------------------- #

_ZBASE32 = "ybndrfg8ejkmcpqxot1uwisza345h769"


def _zbase32_lsb(data: bytes) -> str:
    out: list[str] = []
    total_bits = len(data) * 8
    for i in range(0, total_bits, 5):
        val = 0
        for b in range(5):
            bit_index = i + b
            if bit_index < total_bits:
                bit = (data[bit_index // 8] >> (bit_index % 8)) & 1
                val |= bit << b
        out.append(_ZBASE32[val])
    return "".join(out)


def pkg_checksum(pkg_bytes: bytes) -> str:
    """The catalog `sum` for a .pkg file: libpkg checksum type 2 over the file bytes."""
    return "2$" + _zbase32_lsb(hashlib.blake2b(pkg_bytes).digest())


# --------------------------------------------------------------------------- #
# .pkg reading (zstd framing + +COMPACT_MANIFEST) lives in pfb_pkg, shared with
# gen_landing.py. zstd_decompress / read_compact_manifest are imported above.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Catalog object (one packagesite.yaml / data line per package)
#
# libpkg's packagesite object = the +COMPACT_MANIFEST with the repo fields
# sum/flatsize/path/repopath/pkgsize spliced in at libpkg's positions:
#   ...prefix, SUM, flatsize, PATH, REPOPATH, licenselogic, PKGSIZE, desc...
# We reproduce that exact key order (clients parse JSON order-independently, but
# matching the oracle keeps the output faithful + diffable).
# --------------------------------------------------------------------------- #


def catalog_object(manifest: dict, *, pkg_name: str, sum_: str, pkgsize: int) -> dict:
    """Build the packagesite object for one package from its compact manifest."""
    obj: dict = {}
    for key, value in manifest.items():
        if key in {"sum", "path", "repopath", "pkgsize"}:
            continue
        obj[key] = value
        if key == "prefix":
            # sum immediately follows prefix; flatsize (already in the manifest)
            # then path + repopath follow.
            obj["sum"] = sum_
        if key == "flatsize":
            # path/repopath land right after flatsize (which sits right after sum).
            obj["path"] = pkg_name
            obj["repopath"] = pkg_name
        if key == "licenselogic":
            obj["pkgsize"] = pkgsize
    # Defensive: if a manifest lacked `prefix`/`flatsize`/`licenselogic`, the repo
    # fields still MUST be present (libpkg always emits them). Append any missing.
    if "sum" not in obj:
        obj["sum"] = sum_
    if "path" not in obj:
        obj["path"] = pkg_name
    if "repopath" not in obj:
        obj["repopath"] = pkg_name
    if "pkgsize" not in obj:
        obj["pkgsize"] = pkgsize
    return obj


def _ndjson(objs: list[dict]) -> bytes:
    # newline-delimited JSON, compact (libpkg emits no spaces), trailing newline.
    return b"".join(
        json.dumps(o, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        for o in objs
    )


def _data_blob(objs: list[dict]) -> bytes:
    # The `data` member is a SINGLE JSON object with NO trailing newline (unlike
    # packagesite.yaml's NDJSON) — matches real `pkg repo` output exactly.
    payload = {"groups": [], "expired_packages": [], "packages": objs}
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


# --------------------------------------------------------------------------- #
# Catalogue signing (issue #2675) — `signature_type: fingerprints`, ECDSA.
#
# Authenticity used to rest on HTTPS to the host, which is exactly what breaks
# on pfSense Plus: `pfSense-repo-setup` pins pkg to a Netgate-only CA bundle,
# and no environment-based workaround can reach the GUI (php-fpm scrubs the
# worker environment, and pfSense's own pkg_call() replaces it outright). A
# signed catalogue moves the trust anchor onto our own key, so the fetch itself
# no longer has to be TLS.
#
# Every byte below follows freebsd/pkg (13f9f98); the details are unforgiving
# because a mismatch fails only at verification, on the box:
#   * the signed message is the 64-character ASCII SHA256 HEX of the
#     UNCOMPRESSED catalogue member, signed ECDSA-SHA256 (`ecc_new()` pins
#     sig_hash = SHA256 for ecdsa regardless of curve). It is the CERT path —
#     `ecc_verify_cert_cb()` — that FINGERPRINTS mode runs, and it hashes with
#     PKG_HASH_TYPE_SHA256_HEX passed at `strlen()`, so no NUL terminator.
#     The BLAKE2b-512 chain in `ecc_sign_file()`/`ecc_verify_file()` belongs to
#     PUBKEY mode (single `signature` member) — signing that instead yields a
#     catalogue a real pkg rejects with "ecc signature verification failure".
#   * a non-RSA signature is prefixed `$PKGSIGN:<type>$` (PKGSIGN_HEAD,
#     libpkg/private/pkgsign.h), which is how the client selects the signer.
#   * the public half is DER for ECDSA (exported as PKCS#8 "for interoperability
#     with OpenSSL"); the ECC loader calls libder_read() and knows no PEM.
#   * the trusted fingerprint is the SHA256 of exactly those DER bytes, so any
#     re-encoding invalidates every fingerprint file already deployed.
# --------------------------------------------------------------------------- #

# The signature member carries this before the raw DER signature.
PKGSIGN_ECDSA_HEAD = b"$PKGSIGN:ecdsa$"

# pkg accepts far fewer curves than OpenSSL offers: `ecc_read_pkgkey()` matches
# the curve OID against this set and nothing else. prime256v1/secp256r1 — the
# curve almost every tool defaults to — is NOT among them, which is why signing
# validates the curve up front rather than shipping a catalogue no box can
# verify.
PKG_ACCEPTED_CURVES = frozenset(
    {
        "secp256k1",
        "secp384r1",
        "secp521r1",
        "brainpoolP256r1",
        "brainpoolP256t1",
        "brainpoolP320r1",
        "brainpoolP320t1",
        "brainpoolP384r1",
        "brainpoolP384t1",
        "brainpoolP512r1",
        "brainpoolP512t1",
    }
)


def _openssl(args: list[str], *, stdin: bytes | None = None) -> bytes:
    """Run `openssl` and return stdout; every failure becomes a BuildRepoError."""
    try:
        proc = subprocess.run(
            ["openssl", *args],
            input=stdin,
            capture_output=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - environment-dependent
        # OSError, not FileNotFoundError: an `openssl` that exists on PATH but is not
        # executable raises PermissionError, which would otherwise escape this wrapper
        # as a bare traceback instead of a BuildRepoError.
        raise BuildRepoError(
            f"cannot run `openssl` for catalogue signing: {exc}"
        ) from exc
    if proc.returncode != 0:
        detail = (
            proc.stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"
        )
        raise BuildRepoError(f"openssl {args[0]} failed: {detail}")
    return proc.stdout


def signing_public_der(sign_key: Path) -> bytes:
    """The signing key's public half as RAW DER, after checking pkg accepts its curve.

    Raw, with no `$PKGSIGN:` header, because these exact bytes are what the client hashes
    for the trusted fingerprint (`pkg_repo_check_fingerprint` hashes the cert AFTER the
    extractor strips any header). The header belongs to the archive MEMBER, and
    write_zstd_tar() adds it there — keeping it out of here means a fingerprint file can
    never be generated over the wrong bytes.
    """
    text = _openssl(["ec", "-in", str(sign_key), "-noout", "-text"]).decode(
        errors="replace"
    )
    # `openssl ec -text` names the curve on an "ASN1 OID:" line. Keying on that
    # rather than the "NIST CURVE:" line keeps brainpool curves (which have no
    # NIST name) readable by the same parse.
    match = re.search(r"^\s*ASN1 OID:\s*(\S+)\s*$", text, re.MULTILINE)
    if match is None:
        raise BuildRepoError(
            f"{sign_key}: cannot determine the EC curve — `openssl ec -text` printed no "
            "`ASN1 OID:` line. Either this is not an EC private key, or it carries explicit "
            "curve parameters (`-param_enc explicit`) rather than naming a curve; pkg matches "
            "curves by OID, so the key must name one."
        )
    curve = match.group(1)
    if curve not in PKG_ACCEPTED_CURVES:
        raise BuildRepoError(
            f"{sign_key}: pkg cannot verify signatures on curve {curve!r} — "
            f"choose one of {', '.join(sorted(PKG_ACCEPTED_CURVES))} (secp384r1 recommended)"
        )
    return _openssl(["ec", "-in", str(sign_key), "-pubout", "-outform", "DER"])


def catalog_signature(data: bytes, sign_key: Path) -> bytes:
    """The `.sig` member body for ``data``: header + ECDSA-SHA256 over its SHA256 HEX text.

    The signed message is the 64-character ASCII hex of the catalogue's SHA256 — not a raw
    digest, and not BLAKE2b. `ecc_verify_cert_cb()`, which is what FINGERPRINTS mode runs,
    does `pkg_checksum_fd(fd, PKG_HASH_TYPE_SHA256_HEX)` and passes the result with
    `strlen()`, so there is no NUL terminator either. libecc then hashes that text with
    SHA256 internally, which is what `openssl dgst -sha256 -sign` reproduces.

    Do not reach for the BLAKE2b-512 chain in `ecc_sign_file()`/`ecc_verify_file()`: those
    serve PUBKEY mode, whose catalogue carries a single `signature` member instead of our
    `.sig`/`.pub` pair. Signing the wrong one still produces a well-formed catalogue that a
    real `pkg update` rejects with "ecc signature verification failure".
    """
    message = hashlib.sha256(data).hexdigest().encode()
    return PKGSIGN_ECDSA_HEAD + _openssl(
        ["dgst", "-sha256", "-sign", str(sign_key)], stdin=message
    )


def catalogue_signature_valid(
    data: bytes, signature_member: bytes, public_member: bytes
) -> bool:
    """Whether pkg's embedded ECDSA signature verifies ``data``."""
    head = PKGSIGN_ECDSA_HEAD
    if not signature_member.startswith(head) or not public_member.startswith(head):
        return False
    signature = signature_member[len(head) :]
    public_der = public_member[len(head) :]
    if not signature or not public_der:
        return False
    with tempfile.TemporaryDirectory(prefix="pfb-catalogue-verify-") as tmp:
        root = Path(tmp)
        signature_path = root / "signature"
        public_path = root / "public.der"
        signature_path.write_bytes(signature)
        public_path.write_bytes(public_der)
        try:
            public_text = _openssl(
                [
                    "pkey",
                    "-pubin",
                    "-inform",
                    "DER",
                    "-in",
                    str(public_path),
                    "-text",
                    "-noout",
                ]
            ).decode(errors="replace")
            curve = re.search(r"^\s*ASN1 OID:\s*(\S+)\s*$", public_text, re.MULTILINE)
            if curve is None or curve.group(1) not in PKG_ACCEPTED_CURVES:
                return False
            message = hashlib.sha256(data).hexdigest().encode()
            _openssl(
                [
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(public_path),
                    "-keyform",
                    "DER",
                    "-signature",
                    str(signature_path),
                ],
                stdin=message,
            )
        except BuildRepoError:
            return False
    return True


# Archive emission (zstd tar) — same framing contract as build-pkg-portable.py:
# USTAR, leading-slash-free member name, root:wheel, mode 0644, deterministic
# mtime 0 (the install/index clock is irrelevant to clients; 0 keeps re-runs
# byte-identical). USTAR is the proven-accepted framing (build-pkg-portable.py's
# .pkg uses it and a real pfSense box installs it).
#
# Byte-identical re-runs hold for UNSIGNED output only: ECDSA signatures are
# randomised (OpenSSL does not implement deterministic RFC 6979), so a signed
# catalogue differs between builds even when the package set does not — see
# issue #2675 for what that costs the publisher's NOOP detection.
# --------------------------------------------------------------------------- #


def _tar_one(members: list[tuple[str, bytes]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for member_name, data in members:
            ti = tarfile.TarInfo(name=member_name)
            ti.size = len(data)
            ti.mode = 0o644
            ti.uid = ti.gid = 0
            ti.uname, ti.gname = "root", "wheel"
            ti.mtime = 0
            ti.type = tarfile.REGTYPE
            tf.addfile(ti, io.BytesIO(data))
    return raw.getvalue()


def write_zstd_tar(
    member_name: str, data: bytes, out_path: Path, *, sign_key: Path | None = None
) -> None:
    """Write ``data`` as a one-member zstd tar, signed when ``sign_key`` is given.

    With a key, the archive gains ``<member_name>.sig`` and ``<member_name>.pub``
    AHEAD of the catalogue member — the order real `pkg repo` writes (pack_sign()
    runs before packing_append_file_attr()), and the names it derives from the
    MEMBER rather than the archive. Without a key the output is unchanged, so
    local and offline catalogues stay exactly as they were.
    """
    members: list[tuple[str, bytes]] = []
    if sign_key is not None:
        # BOTH members carry the `$PKGSIGN:ecdsa$` header. Not symmetry for its own sake:
        # pkg_repo_parse_sigkeys() sets the signer type from EVERY member it parses, keyed
        # by basename, so a bare `.pub` (parsed after `.sig`) resets the type to its "rsa"
        # default. Verification then runs the RSA signer, whose _load_public_key_buf() is
        # PEM_read_bio_PUBKEY, and a real `pkg update` dies with "error reading public key
        # ... DECODER routines::unsupported". Real `pkg repo` prefixes both too, because
        # pack_command_sign() never resets its iovec offset between the two appends.
        members.append((f"{member_name}.sig", catalog_signature(data, sign_key)))
        members.append(
            (f"{member_name}.pub", PKGSIGN_ECDSA_HEAD + signing_public_der(sign_key))
        )
    members.append((member_name, data))
    out_path.write_bytes(
        zstd_compress(
            _tar_one(members),
            BuildRepoError,
            "zstd compression needs the `zstd` binary or the python `zstandard` module "
            "(brew install zstd / apt install zstd)",
        )
    )


# --------------------------------------------------------------------------- #
# Flavor-collision guard (same semantics as build-repo.sh)
# --------------------------------------------------------------------------- #


def _flavor_signature(manifest: dict) -> str:
    """The php*/python*/py*- dependency NAMES of a pkg, sorted + comma-joined.

    Two builds of the same name+version+ABI that differ here are different flavors
    and cannot share a catalog. Empty for a flavor-free pkg. Mirrors build-repo.sh's
    `pkg query %dn | grep -E '^(php[0-9]+|python[0-9]+|py[0-9]+-)'`.
    """
    deps = manifest.get("deps")
    if not isinstance(deps, dict):
        return ""
    flavored: list[str] = []
    for name in deps:
        is_python = name.startswith("python") and name[len("python") :][:1].isdigit()
        is_php = name.startswith("php") and name[len("php") :][:1].isdigit()
        is_flavored_py = (
            name.startswith("py")
            and "-" in name
            and name[len("py") :].split("-", 1)[0].isdigit()
        )
        if is_python or is_php or is_flavored_py:
            flavored.append(name)
    return ",".join(sorted(flavored))


def _pkg_matches_abi(manifest: dict, row_abi: str) -> bool:
    """True if a pre-supplied package's manifest ABI belongs to ``row_abi``'s
    FreeBSD major (OS+major only — the CPU/arch segment is never compared).

    Every package the matrix-driven catalog handles is NO_ARCH (issue #1806):
    its manifest abi is CPU-wildcarded (e.g. ``"FreeBSD:15:*"``, see
    ``_is_wildcard_abi``), so an exact string compare against a matrix row's
    concrete ``"FreeBSD:15:amd64"`` abi would never match. Matching by OS+major
    is what decides which VARVER catalog(s) a pre-built/frozen/dep package
    lands in (``route_only_pkgs``/``release_pkgs``/``release_extra_pkgs``/
    ``dep_pkgs``) — the catalog tree is arch-less, so there is no arch bucket
    to route into, only a varver.
    """
    abi = manifest.get("abi", "")
    return isinstance(abi, str) and abi.split(":")[:2] == row_abi.split(":")[:2]


def _check_collisions(entries: list[tuple[Path, dict]]) -> None:
    """Fail loud if same-identity packages differ in flavor or archive bytes."""
    seen: dict[str, tuple[str, Path, bytes]] = {}  # key -> flavor, source, archive
    for path, manifest in entries:
        name = manifest.get("name")
        version = manifest.get("version")
        abi = manifest.get("abi")
        if not (name and version and abi):
            raise BuildRepoError(
                f"{path.name}: manifest missing name/version/abi (name={name!r} version={version!r} abi={abi!r})"
            )
        key = f"{name}|{version}|{abi}"
        sig = _flavor_signature(manifest)
        prev = seen.get(key)
        if prev is None:
            seen[key] = (sig, path, path.read_bytes())
        elif prev[0] != sig:
            raise BuildRepoError(
                f"FLAVOR COLLISION — two packages share name+version+ABI '{key}'\n"
                f"  but differ in php/py flavor:\n"
                f"    flavor A: {prev[0] or '<none>'}\n"
                f"    flavor B: {sig or '<none>'}\n"
                f"  They cannot coexist in one catalog (the second would shadow the first).\n"
                f"  Resolve by splitting into a flavored layout: <out>/<ABI>-<php><py>/\n"
                f"  (not implemented — no colliding combo exists today; teach the tool when one does)."
            )
        elif prev[2] != path.read_bytes():
            raise BuildRepoError(
                f"PACKAGE COLLISION — two packages share name+version+ABI+flavor '{key}'\n"
                f"  but archive bytes differ: {prev[1].name} vs {path.name}"
            )


# --------------------------------------------------------------------------- #
# ADR-20: catalog name derivation + routing manifest
# --------------------------------------------------------------------------- #


def catalog_name_from_version(
    pfsense_version: str, variant: str, *, channel: str = ""
) -> str:
    """Derive catalog dir name: major.minor only, prefixed by variant.

    Both CE and Plus strip any trailing patch component:
      "2.8.1"  + "CE"             -> "ce-2.8"
      "2.8.x"  + "CE"             -> "ce-2.8"
      "26.03"  + "Plus"           -> "plus-26.03"
      "26.03.1"+ "Plus"           -> "plus-26.03"

    A pre-release suffix is stripped BEFORE the split, so a pre-release publishes
    under its release line's catalog — the same strip the rc.d repo-generate hook
    applies on the box (issue #1965; the suffix sits inside the minor field, so a
    bare split would keep it and publish a directory no box ever resolves):
      "26.07-BETA" + "Plus"       -> "plus-26.07"
      "2.8.1-RELEASE" + "CE"      -> "ce-2.8"

    When channel is supplied (e.g. "nightly"), it is prepended as a path prefix:
      "2.8.1"  + "CE"   + "nightly" -> "nightly/ce-2.8"
      "26.03.1"+ "Plus" + "nightly" -> "nightly/plus-26.03"

    The derived name becomes an rmtree'd path segment, so it goes through the same
    ``_validate_catalog_name`` guard a caller-supplied ``--catalog-name`` does — the
    matrix fields it is built from are inputs, not constants.
    """
    major_minor = ".".join(pfsense_version.split("-")[0].split(".")[:2])
    name = f"{variant.lower()}-{major_minor}"
    _validate_catalog_name(name, single_segment=True)
    if not channel:
        return name
    # The channel is an argument like any other — validate the COMPOSED value, or a
    # caller-supplied channel would ride into the path unchecked (issue #1965).
    composed = f"{channel}/{name}"
    _validate_catalog_name(composed)
    return composed


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

# A catalog_name segment is a rm-rf'd + rebuilt path component (_write_catalog_dir
# shutil.rmtree()s it) — same safety rule as build-repo.sh's --varver guard, tightened
# to lowercase-only. '/' is allowed only BETWEEN segments (a legitimate catalog_name
# carries a channel prefix, e.g. "nightly/plus-26.03"). BOTH ends are alphanumeric so a
# segment can never open or close with '-' or '.': every real varver does (ce-2.8,
# plus-26.03, nightly, release), while an absent variant would otherwise derive the
# silently-wrong "-2.8" and an absent version the equally wrong "ce-" (issue #1965).
_CATALOG_NAME_SEGMENT_RE = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")

# pkg(8) catalog plumbing: a catalog_name equal to one of these collides with a file
# _write_catalog_dir writes at the catalog root, so `out_dir / catalog_name` would name
# an existing FILE and rmtree/mkdir would escape this module's BuildRepoError contract
# with a raw NotADirectoryError. A manifest can never reach these (the published name is
# always "<name>-<version>.pkg"), so this guards the catalog_name path only.
_RESERVED_CATALOG_NAMES = frozenset(
    {"meta", "meta.conf", "data.pkg", "packagesite.pkg"}
)


def _validate_catalog_name(name: str, *, single_segment: bool = False) -> None:
    """Reject an unsafe ``catalog_name`` before it becomes ``out_dir / catalog_name``
    (issue #1786): a ``".."`` segment escapes sideways/upward, and an ABSOLUTE value
    makes ``Path.__truediv__`` discard ``out_dir`` entirely — either lets a caller
    point ``_write_catalog_dir``'s ``shutil.rmtree()`` at an arbitrary directory.

    ``single_segment`` additionally forbids the channel prefix. A value DERIVED from
    matrix fields (``catalog_name_from_version``) is one segment by construction, so a
    '/' there means a field carried a separator — which this guard would otherwise read
    as a legitimate prefix and wave through (issue #1965).
    """
    segments = name.split("/")
    if single_segment and len(segments) != 1:
        raise BuildRepoError(
            f"unsafe catalog_name {name!r}: a derived varver is ONE segment — a '/' means "
            f"an input field carried a path separator"
        )
    if any(seg in _RESERVED_CATALOG_NAMES for seg in segments):
        raise BuildRepoError(
            f"unsafe catalog_name {name!r}: a segment collides with pkg(8) catalog plumbing "
            f"({', '.join(sorted(_RESERVED_CATALOG_NAMES))}) — it would name an existing file, not a directory"
        )
    if any(
        not seg or seg in (".", "..") or not _CATALOG_NAME_SEGMENT_RE.fullmatch(seg)
        for seg in segments
    ):
        raise BuildRepoError(
            f"unsafe catalog_name {name!r}: each '/'-separated segment must be non-empty, "
            f"not '.'/'..', and match [a-z0-9][a-z0-9.-]* (e.g. 'ce-2.8', 'release/ce-2.8')"
        )


def _write_catalog_dir(
    dest: Path,
    items: dict[tuple[str, str], tuple[Path, dict]],
    *,
    root: Path,
    sign_key: Path | None = None,
) -> int:
    """Write one pkg catalog at ``dest`` from a ``{(name, version): (path, manifest)}`` map.

    Wipes + rebuilds ``dest`` for determinism (a removed .pkg never lingers), copies each
    package CANONICALLY (``<name>-<version>.pkg``, source mtime preserved), and emits the
    catalog descriptor (``meta.conf`` + its identical ``meta``) plus ``packagesite.pkg``
    (NDJSON) and ``data.pkg`` (one JSON object). Returns the package count.

    All source bytes are read BEFORE the wipe so a source .pkg living inside ``dest``
    (e.g. nightly-retention inputs already in the bucket) survives the rebuild.
    """
    # Read every source up front (sources may live inside dest — see nightly retention).
    staged: list[tuple[str, bytes, float, dict]] = []
    for (name, version), (path, manifest) in sorted(items.items()):
        # name/version come from the .pkg's own manifest and become the published
        # filename — guard both before either is joined onto dest (issue #1965).
        _safe_segment(name, what=f"{path.name}: manifest name", pattern=_PKG_SEGMENT_RE)
        _safe_segment(
            version, what=f"{path.name}: manifest version", pattern=_PKG_SEGMENT_RE
        )
        canonical = f"{name}-{version}.pkg"
        staged.append((canonical, path.read_bytes(), path.stat().st_mtime, manifest))

    # Re-checked HERE, immediately before the wipe, not only at the caller: staging above
    # reads every input .pkg, and a layout that changed under us in that window would
    # otherwise be acted on. This narrows the race to no intervening I/O; it does not
    # eliminate it (only an O_NOFOLLOW/dir_fd walk would). Issue #1972.
    _require_contained(root, dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    catalog_objs: list[dict] = []
    for canonical, pkg_bytes, src_mtime, manifest in staged:
        target = dest / canonical
        target.write_bytes(pkg_bytes)
        # Preserve the source .pkg's mtime so the published artifact reflects its real
        # build time — a cache-restored nightly keeps its original datetime instead of
        # jumping to this catalog-regeneration run.
        os.utime(target, (src_mtime, src_mtime))
        catalog_objs.append(
            catalog_object(
                manifest,
                pkg_name=canonical,
                sum_=pkg_checksum(pkg_bytes),
                pkgsize=len(pkg_bytes),
            )
        )

    # meta.conf + its identical `meta` copy (real `pkg repo` writes both).
    (dest / "meta.conf").write_text(META_CONF)
    (dest / "meta").write_text(META_CONF)
    # packagesite.pkg (packagesite.yaml = NDJSON) + data.pkg (data = one JSON object).
    write_zstd_tar(
        "packagesite.yaml",
        _ndjson(catalog_objs),
        dest / "packagesite.pkg",
        sign_key=sign_key,
    )
    write_zstd_tar(
        "data", _data_blob(catalog_objs), dest / "data.pkg", sign_key=sign_key
    )
    return len(catalog_objs)


# Descriptor archives are not installable packages when an existing catalogue is rescanned.
_CATALOG_PKG_FILES = {"packagesite.pkg", "data.pkg"}


def _require_contained(root: Path, dest: Path) -> Path:
    """Return ``dest`` if it really lands inside ``root``, else raise (issue #1972).

    ``_validate_catalog_name`` checks the catalog name as a STRING; the FILESYSTEM
    decides where that string lands. A symlink at any component — including an
    intermediate one, which ``shutil.rmtree`` follows happily — redirects the
    wipe-and-rebuild outside the output root while every segment still looks like a
    legitimate varver. Resolving both sides and requiring containment is the check the
    name guard structurally cannot make.

    A symlinked LEAF is refused outright even when it points back inside the root: the
    catalog directory is wiped and recreated, so it has to be a real directory this
    tool owns (``rmtree`` declines a symlink, which would surface as a raw ``OSError``).
    """
    resolved_root = root.resolve()
    resolved_dest = dest.resolve()
    if not resolved_dest.is_relative_to(resolved_root):
        raise BuildRepoError(
            f"catalog destination {str(dest)!r} escapes the output root {str(root)!r} "
            f"(resolves to {str(resolved_dest)!r}) — a symlinked path component redirects "
            f"the rmtree+rebuild outside --out"
        )
    if dest.is_symlink():
        raise BuildRepoError(
            f"catalog destination {str(dest)!r} is a symlink — the catalog directory is wiped "
            f"and rebuilt, so it must be a real directory"
        )
    # Every component, not just the leaf: `dest.exists()` is False when an ANCESTOR is a
    # file (it does not raise), and resolve() passes straight through such a component, so
    # a leaf-only check lets `mkdir(parents=True)` raise a raw NotADirectoryError from
    # inside the writer. Walk from the root outwards so the message names the real culprit.
    # `is_relative_to` alone is the stop condition: the root IS a component that must be a
    # real directory (without a catalog name the destination simply IS the root), and the
    # first ancestor ABOVE it is not relative to it, so the walk never inspects anything
    # outside the output root.
    for component in (dest, *dest.parents):
        if not component.is_relative_to(root):
            break
        if component.exists() and not component.is_dir():
            raise BuildRepoError(
                f"catalog destination {str(dest)!r} is unusable: {str(component)!r} exists and is "
                f"not a directory — the catalog directory is wiped and rebuilt, so every component "
                f"of its path must be a real directory"
            )
    return dest


def emit_catalog(
    dest: Path,
    pkg_paths: list[Path],
    *,
    root: Path,
    sign_key: Path | None = None,
) -> int:
    """Read each .pkg's manifest, dedup by (name, version), collision-check, emit at dest.

    Every package here MUST carry a NO_ARCH (wildcard) ABI (``_is_wildcard_abi``,
    issue #1806) — the matrix-driven catalog tree is arch-less: one directory
    (``release/<varver>/`` / ``nightly/<varver>/``) serves every arch of a
    FreeBSD major, so a concrete-ABI package would silently install on only
    one. A concrete ABI reaching catalog emission is a hard error — the
    tripwire that forces a conscious layout decision if a compiled, per-arch
    dependency is ever added.

    ``root`` is the output root ``dest`` must stay inside. Checked here to fail before
    reading every input package, and again in ``_write_catalog_dir`` immediately before
    the wipe — that second one is authoritative, so the guard holds even for a future
    caller that reaches the writer by another route (issue #1972).
    """
    _require_contained(root, dest)
    entries: list[tuple[Path, dict]] = [
        (p, read_compact_manifest(p)) for p in sorted(set(pkg_paths))
    ]
    if not entries:
        raise BuildRepoError("catalogue package pool is empty")
    for path, manifest in entries:
        _validate_annotated_project_pkg(path, manifest)
    _check_collisions(entries)
    abis = {
        _require_wildcard_abi(path, manifest.get("abi")) for path, manifest in entries
    }
    if len(abis) != 1:
        raise BuildRepoError(f"mixed ABIs in one catalogue: {sorted(abis)}")
    items: dict[tuple[str, str], tuple[Path, dict]] = {}
    for path, manifest in entries:
        nv = (manifest["name"], manifest["version"])
        if nv in items:
            sys.stderr.write(
                f"==> dedup: {path.name} duplicates {items[nv][0].name} ({nv[0]}-{nv[1]})\n"
            )
            continue
        items[nv] = (path, manifest)
    return _write_catalog_dir(dest, items, root=root, sign_key=sign_key)


def _canonical_build_record(
    path: Path, manifest: Mapping[str, object]
) -> dict[str, object] | None:
    """Load a canonical package's optional provenance annotation, if present."""
    name = manifest.get("name")
    if name != CANONICAL_EMITTED_IDENTITY:
        return None
    annotations = manifest.get("annotations")
    if annotations is not None and not isinstance(annotations, Mapping):
        raise BuildRepoError(f"{path.name}: annotations must be an object")
    annotation = (
        annotations.get(PFB_BUILD_RECORD_KEY)
        if isinstance(annotations, Mapping)
        else None
    )
    if annotation is None:
        # Native stable artifacts predate the provenance annotation.
        return None
    if not isinstance(annotation, str):
        raise BuildRepoError(
            f"{path.name}: {PFB_BUILD_RECORD_KEY} annotation must be JSON text"
        )
    if not annotation.lstrip().startswith("{"):
        raise BuildRepoError(
            f"{path.name}: {PFB_BUILD_RECORD_KEY} annotation must be a JSON object"
        )
    try:
        return load_build_record(annotation)
    except (PkgError, TypeError, ValueError) as exc:
        raise BuildRepoError(
            f"{path.name}: invalid {PFB_BUILD_RECORD_KEY} annotation: {exc}"
        ) from None


def _validate_annotated_project_pkg(path: Path, manifest: Mapping[str, object]) -> None:
    """Require full archive validation for canonical packages carrying provenance."""
    record = _canonical_build_record(path, manifest)
    if record is None:
        return
    try:
        validate_project_pkg(path, record)
    except (PkgError, OSError, TypeError, ValueError) as exc:
        raise BuildRepoError(
            f"{path.name}: project package validation failed: {exc}"
        ) from None
