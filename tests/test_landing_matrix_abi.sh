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
#    Exactly ONE such expression must exist: with more than one, checking the first
#    would leave a second (possibly broken, possibly only an example in a comment)
#    unexamined, and check 1 catches only the retired `arch` token by name.
#    Counted with grep -o | wc -l, not grep -c: the latter counts matching LINES, so a
#    second expression sharing a line with the first (a trailing-comment decoy) would
#    report 1. wc also yields 0 rather than an empty string if the file is unreadable,
#    which keeps this check live instead of erroring past itself.
abi_count=$(grep -o 'abi: "FreeBSD:[^"]*"' "$WORKFLOW" | wc -l | tr -d ' ')
if [ "$abi_count" -ne 1 ]; then
	printf 'expected exactly one abi FreeBSD expression in %s, found %s\n' "$WORKFLOW" "$abi_count" >&2
	failures=$((failures + 1))
fi

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
