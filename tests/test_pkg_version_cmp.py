"""pkg_version_cmp() — the port of freebsd/pkg ``libpkg/pkg_version.c``.

Issue #3386: pkg keeps the LAST entry when a catalogue lists one package name several
times and never compares versions itself, so the publisher must order versions exactly as
pkg's own ``pkg_version_cmp()`` does. Pinned here: every ``pkg version -t`` vector of
freebsd/pkg ``tests/frontend/version.sh``, the rules ``pkg_version.c`` documents, and the
version shapes pfBlockerNG publishes. Every row was cross-checked against ``pkg_version.c``
compiled unmodified.
"""

from __future__ import annotations

import pytest
from pfb_pkg import pkg_version_cmp, pkg_version_sort_key

_SIGN = {"<": -1, "=": 0, ">": 1}


def _assert_pkg_order(a: str, b: str, op: str) -> None:
    expected = _SIGN[op]
    assert pkg_version_cmp(a, b) == expected, f"pkg orders {a!r} {op} {b!r}"
    assert pkg_version_cmp(b, a) == -expected, f"antisymmetry: {b!r} vs {a!r}"


def _ids(rows: list[tuple[str, str, str]]) -> list[str]:
    return [f"{a}{op}{b}" for a, b, op in rows]


# freebsd/pkg tests/frontend/version.sh, version_body: `pkg version -t A B` prints the op.
PKG_VERSION_T_VECTORS = [
    ("1", "2", "<"),
    ("2", "1", ">"),
    ("2", "2", "="),
    ("2", "1,1", "<"),
    ("1.pl1", "1.alpha1", "<"),
    ("1.alpha1", "1.beta1", "<"),
    ("1.beta1", "1.pre1", "<"),
    ("1.pre1", "1.rc1", "<"),
    ("1.rc1", "1", "<"),
    ("1.pl1", "1.snap1", "<"),
    ("1.snap1", "1.alpha1", ">"),
]

# version.sh compare_body / compare_ge_le_body install 5.20_3 and query `name<op>VER`
# constraints with `pkg info`; each accepted/rejected bound fixes one sign of
# cmp(installed, VER): >0 ok, <5 fails, >5<6 ok, >5<5.20 fails, >5.20_3<6 fails,
# >=5.20_3 / <=5.20_3 ok, >=5 ok, >=6 fails, <=5 fails.
PKG_INFO_CONSTRAINT_VECTORS = [
    ("5.20_3", "0", ">"),
    ("5.20_3", "5", ">"),
    ("5.20_3", "6", "<"),
    ("5.20_3", "5.20", ">"),
    ("5.20_3", "5.20_3", "="),
]

# The rules pkg_version.c documents above get_component()/pkg_version_cmp().
PKG_VERSION_C_RULES = [
    # `*` is the smallest possible component (2.* < 2pl1 < 2alpha3 < 2.9f7 < 3.*)
    ("2.*", "2pl1", "<"),
    ("2pl1", "2alpha3", "<"),
    ("2alpha3", "2.9f7", "<"),
    ("2.9f7", "3.*", "<"),
    ("2.*", "2.0", "<"),
    ("2.*+3", "2.*+4", "<"),
    ("2.*", "2.pl", "<"),
    # missing version numbers in a component starting with a letter sort as -1
    ("a", "0", "<"),
    ("10.a", "10", "<"),
    # ...and a missing patch number after the letter sorts as -1 (10a < 10a0)
    ("10a", "10a0", "<"),
    # a separator is inserted before a special string that follows a number
    ("10alpha", "10.a", "="),
    ("0.1beta2", "0.1.b2", "="),
    ("0.1beta2", "0.1", "<"),
    ("1snap1", "1.snap1", "="),
    ("1pre1", "1.pre1", "="),
    ("1snap1", "1", "<"),
    # pl sorts before every other letter; alpha/beta/pre/rc sort as a/b/p/r
    ("pl11", "alpha3", "<"),
    ("alpha3", "0.1beta2", "<"),
    ("1.pre1", "1.p1", "="),
    ("1.snap1", "1.s1", "="),
    # numbers without letters sort first: 10 < 10a < 10b
    ("10", "10a", "<"),
    ("10a", "10b", "<"),
    ("1alpha1", "1a1", "<"),
    # other strings use only their first letter, case is ignored
    ("1.d2", "1.dev2", "="),
    ("1.dev2", "1.Development2", "="),
    ("1.A1", "1.a1", "="),
    ("1.PL1", "1.pl1", "="),
    ("1.PL1", "1.alpha1", "<"),
    # characters outside [a-zA-Z0-9.+*] are separators, consecutive ones collapse
    ("1.0:2003.09.16", "1.0.2003.09.16", "="),
    ("1.0.1:2003.09.16", "1.0:2003.09.16", "<"),
    ("10..1", "10.1", "="),
    ("10a1b2", "10a1.b2", "="),
    # missing components are assumed to be 0
    ("10", "10.0", "="),
    ("10.0", "10.0.0", "="),
    ("1.0.1", "1.0", ">"),
    # components separated by `+` are compared block by block
    ("1.0+2", "1.0+3", "<"),
    ("1.0+2", "1.0.5", "<"),
    ("1.0", "1.0+1", "<"),
    ("1.0+1", "1.0+0", ">"),
    ("1+2", "1.0", ">"),
    # epoch (`,N`) supersedes version supersedes revision (`_N`)
    ("1_1", "1", ">"),
    ("1_2", "1_1", ">"),
    ("2", "1_9", ">"),
    ("1_2,1", "9_9", ">"),
    # the epoch is only looked for after the `_` that starts the revision
    ("1,1_2", "2", "<"),
    ("1_2,1", "2", ">"),
    # a leading `name-` is dropped: the version is whatever follows the last `-`
    ("foo-1.0", "1.0", "="),
    ("foo-1.0", "foo-1.1", "<"),
    # digit runs saturate like strtoll, so pkg cannot rank these apart
    ("99999999999999999999", "99999999999999999998", "="),
    # leading zeros do not count towards saturation
    ("1_00000000000000000000000000000000004", "1_5", "<"),
    # strtoll saturation: version and patch numbers clamp at LLONG_MAX
    ("9223372036854775807", "9223372036854775808", "="),
    ("1.a9223372036854775807", "1.a9223372036854775808", "="),
    # strtoul saturation: revision and epoch clamp at ULONG_MAX, not LLONG_MAX
    ("1_9223372036854775808", "1_9223372036854775809", "<"),
    ("1,9223372036854775808", "1,9223372036854775809", "<"),
    ("1_10000000000000000000", "1_18446744073709551615", "<"),
    ("1_18446744073709551615", "1_18446744073709551616", "="),
    # a NUL ends the C string
    ("1\x00.2", "1", "="),
    ("1", "1\x002", "="),
    # strtoul skips leading whitespace and a `+` sign
    ("1_ 5", "1_5", "="),
    ("1_+5", "1_5", "="),
    # the name prefix ends at the LAST `-`; the revision `_` must follow it
    ("pfSense-pkg-x-1.0", "1.0", "="),
    ("a_b-1", "1", "="),
    # `*` skips everything up to the next `+`
    ("2.*9", "2.*8", "="),
    # a stage word must end at a non-letter: `plx` is the letter p
    ("1.plx1", "1.pl1", ">"),
    # digits and letters are ASCII only (isdigit/isalpha in the C locale)
    ("\u0663", "0", "<"),
    ("\u00e9", "a", "<"),
]


@pytest.mark.parametrize(
    ("a", "b", "op"),
    PKG_VERSION_T_VECTORS,
    ids=_ids(PKG_VERSION_T_VECTORS),
)
def test_freebsd_pkg_version_sh_pkg_version_t_vector(a: str, b: str, op: str) -> None:
    _assert_pkg_order(a, b, op)


@pytest.mark.parametrize(
    ("a", "b", "op"),
    PKG_INFO_CONSTRAINT_VECTORS,
    ids=_ids(PKG_INFO_CONSTRAINT_VECTORS),
)
def test_freebsd_pkg_version_sh_constraint_vector(a: str, b: str, op: str) -> None:
    _assert_pkg_order(a, b, op)


@pytest.mark.parametrize(
    ("a", "b", "op"),
    PKG_VERSION_C_RULES,
    ids=_ids(PKG_VERSION_C_RULES),
)
def test_pkg_version_c_documented_rule(a: str, b: str, op: str) -> None:
    _assert_pkg_order(a, b, op)


# Each chain is strictly ascending in pkg order.
PFB_ASCENDING_CHAINS = [
    ["3.3.9", "3.3.10"],
    ["3.3.9", "3.3.10.a1", "3.3.10.a2", "3.3.10.b1", "3.3.10.r1", "3.3.10"],
    ["3.3.10.r486", "3.3.10"],
    ["4.0.0.a9", "4.0.0.a10", "4.0.0.a21", "4.0.0"],
    ["3.3.3.a1", "3.3.3"],
    ["2.8", "2.8.1"],
    # a Nightly (timestamp.sha) outranks every release; timestamps order by time, not SHA
    ["4.0.0", "20260929153153.d05c2e5"],
    ["20260929153153.f05c2e5", "20260930010101.a123456"],
    ["3.3.10", "3.3.10_1"],
    ["9", "1,1"],
    ["3.3.9", "3.3.3,1"],
]


@pytest.mark.parametrize("chain", PFB_ASCENDING_CHAINS, ids=" < ".join)
def test_pfblockerng_versions_ascend_in_pkg_order(chain: list[str]) -> None:
    for i, lower in enumerate(chain):
        for higher in chain[i + 1 :]:
            _assert_pkg_order(lower, higher, "<")
    descending = list(reversed(chain))
    assert sorted(descending, key=pkg_version_sort_key) == chain
    assert max(descending, key=pkg_version_sort_key) == chain[-1]


def test_pkg_distinguishes_dotted_rN_from_glued_rN() -> None:
    """`3.3.10.r486` is a stage below the release; glued `3.3.10r486` is letter r,
    patch 486 (pkg: 10 < 10a), so it sorts ABOVE `3.3.10`."""
    _assert_pkg_order("3.3.10.r486", "3.3.10", "<")
    _assert_pkg_order("3.3.10", "3.3.10r486", "<")
    _assert_pkg_order("3.3.10.r486", "3.3.10r486", "<")
