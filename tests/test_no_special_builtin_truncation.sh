#!/bin/sh

set -eu

cd "$(dirname "$0")/.."

pattern='(^|[;&|()])[[:space:]]*:'
pattern="${pattern} >[[:space:]]"

printf '  %s > file\n' ':' | grep -Eq "$pattern" || {
	echo 'test matcher did not detect the legacy truncation command' >&2
	exit 1
}

if hits=$(git grep -nE "$pattern" -- '*.sh' '*.yml' '*.yaml'); then
	printf 'special-builtin truncation commands remain:\n%s\n' "$hits" >&2
	exit 1
fi
