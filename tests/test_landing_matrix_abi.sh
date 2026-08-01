#!/bin/sh

set -eu

cd "$(dirname "$0")/.."

WORKFLOW=.github/workflows/publish.yml

# The landing page's matrix (landing_matrix.json) is keyed by an ABI string this
# workflow builds from the ci-metadata matrix entry. `arch` was retired from that
# matrix by pfBlockerNG/pfBlockerNG#1806 (every published .pkg is NO_ARCH), so an
# interpolation of `.arch` yields the literal "null" — e.g. "FreeBSD:15:null".
# The catalog is arch-less, so the honest ABI is the CPU wildcard the packages
# themselves carry. These pin both halves: the retired field is gone, and the
# expression as written in the workflow really does emit the wildcard.

failures=0

# 1. The retired matrix field never reaches an ABI string.
if grep -Fn '\(.arch)' "$WORKFLOW" >&2; then
	printf 'the retired matrix field arch is still interpolated in %s (lines above)\n' "$WORKFLOW" >&2
	failures=$((failures + 1))
fi

# 2. The abi expression AS WRITTEN in the workflow emits the NO_ARCH wildcard.
#    Extracted from the file rather than restated, so the test cannot drift from it.
abi_expr=$(grep -o 'abi: "FreeBSD:[^"]*"' "$WORKFLOW" | head -1)
if [ -z "$abi_expr" ]; then
	printf 'no abi FreeBSD expression found in %s\n' "$WORKFLOW" >&2
	failures=$((failures + 1))
else
	got=$(printf '%s' '{"freebsd_major":"15"}' | jq -c "{ $abi_expr }")
	expected='{"abi":"FreeBSD:15:*"}'
	if [ "$got" != "$expected" ]; then
		printf 'landing_matrix abi expression\n  expression: %s\n  expected:   %s\n  actual:     %s\n' \
			"$abi_expr" "$expected" "$got" >&2
		failures=$((failures + 1))
	fi
fi

[ "$failures" -eq 0 ]
