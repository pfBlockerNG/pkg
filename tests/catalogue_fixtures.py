from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import catalogue_engine as brp
import pfb_pkg

_PKGSIGN_ECDSA_HEAD = brp.PKGSIGN_ECDSA_HEAD


def make_pkg(
    path: Path,
    *,
    name: str = "demo",
    version: str = "1.0_1",
    abi: str = "FreeBSD:15:*",
    deps: dict[str, dict[str, str]] | None = None,
    extra: dict[str, Any] | None = None,
    payload: bytes = b"hey",
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "name": name,
        "origin": f"net/{name}",
        "version": version,
        "comment": "demo package",
        "maintainer": "dev@example.com",
        "www": "https://example.com",
        "abi": abi,
        "arch": "freebsd:15:x86:64",
        "prefix": "/usr/local",
        "flatsize": len(payload),
        "licenselogic": "single",
        "desc": "demo",
    }
    if deps:
        manifest["deps"] = deps
    manifest["categories"] = ["net"]
    if extra:
        manifest.update(extra)
    compact = json.dumps(manifest, separators=(",", ":")).encode() + b"\n"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo(name="+COMPACT_MANIFEST")
        info.size = len(compact)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(compact))
        payload_info = tarfile.TarInfo(name="/usr/local/bin/demo")
        payload_info.size = len(payload)
        payload_info.mode = 0o555
        archive.addfile(payload_info, io.BytesIO(payload))
    path.write_bytes(
        pfb_pkg.zstd_compress(raw.getvalue(), brp.BuildRepoError, "zstd unavailable")
    )
    return manifest


def _read_member(zstd_tar: Path, member: str) -> bytes:
    data = pfb_pkg.zstd_decompress(zstd_tar.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        extracted = archive.extractfile(member)
        assert extracted is not None
        return extracted.read()


def _gen_key(path: Path, curve: str = "secp384r1") -> Path:
    subprocess.run(
        ["openssl", "ecparam", "-name", curve, "-genkey", "-noout", "-out", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def _pkg_signed_message(catalog: bytes) -> bytes:
    return hashlib.sha256(catalog).hexdigest().encode()


def _openssl_verify(digest: bytes, sig: bytes, pub_der: bytes, tmp_path: Path) -> bool:
    message = tmp_path / "verify.msg"
    signature = tmp_path / "verify.sig"
    public = tmp_path / "verify.pub.der"
    message.write_bytes(digest)
    signature.write_bytes(sig)
    public.write_bytes(pub_der)
    proc = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(public),
            "-keyform",
            "DER",
            "-signature",
            str(signature),
            str(message),
        ],
        check=False,
        capture_output=True,
    )
    return proc.returncode == 0


def _sig_members(archive: Path) -> dict[str, bytes]:
    data = pfb_pkg.zstd_decompress(archive.read_bytes())
    result: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for info in tar.getmembers():
            if info.name.endswith((".sig", ".pub")):
                extracted = tar.extractfile(info)
                assert extracted is not None
                result[info.name] = extracted.read()
    return result
