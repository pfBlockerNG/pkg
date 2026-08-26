"""Render the pkg client repository configuration shown by the website."""

from __future__ import annotations

REPO_HOST = "pkg.pfblockerng.com"
CONF_PRIORITY = 100
CONF_FINGERPRINT_DIR = "/usr/local/etc/pkg/fingerprints/pfblockerng"
_REPO_NAMES = {
    "nightly": "pfblockerng-nightly",
    "stable": "pfblockerng-stable",
    "testing": "pfblockerng-testing",
    "edge": "pfblockerng-edge",
}


def _signed_host(url: str) -> bool:
    return any(
        url == f"{scheme}{REPO_HOST}" or url.startswith(f"{scheme}{REPO_HOST}/")
        for scheme in ("http://", "https://")
    )


def render(resolved_url: str, channel: str) -> str:
    if channel not in _REPO_NAMES:
        raise ValueError(f"unsupported pkg channel: {channel!r}")
    url = resolved_url.rstrip("/")
    if _signed_host(url):
        trust = (
            "# Signed catalogue (issue #2675): the trust anchor is our own ECDSA key, whose\n"
            "# fingerprint the boot rc.d hook installs; the fetch is plain HTTP because pkg's\n"
            "# CA store is Netgate-pinned on pfSense Plus and unreachable from the GUI.\n"
        )
        signature = (
            "  signature_type: fingerprints,\n"
            f'  fingerprints: "{CONF_FINGERPRINT_DIR}",\n'
        )
    else:
        trust = "# Unsigned catalogue: this base is not the signed project host.\n"
        signature = "  signature_type: none,\n"
    return (
        f"# Generated at boot by pfblockerng_repo_generate (ADR-39) — do not edit; re-run install.sh --channel {channel} to change.\n"
        f"# pfBlockerNG ({channel} channel) — self-hosted pkg repository (ADR-17).\n"
        f"{trust}"
        "# The URL is fully resolved for this box's edition/version (ADR-39; arch-less/NO_ARCH,\n"
        "# issue #1806); the boot rc.d hook updates it on a pfSense OS upgrade.\n"
        f"# priority {CONF_PRIORITY} sits above the base Netgate `pfSense` repo so cross-repo\n"
        "# resolution (pkg install/upgrade, GUI Install) selects the pfBlockerNG build.\n"
        f"{_REPO_NAMES[channel]}: {{\n"
        f'  url: "{url}",\n'
        "  mirror_type: none,\n"
        f"{signature}"
        f"  priority: {CONF_PRIORITY},\n"
        "  enabled: yes\n"
        "}\n"
    )
