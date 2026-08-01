#!/bin/sh

set -eu

cd "$(dirname "$0")/.."

# Pin issue #1830's two named sites; the tree-wide shell-context sweep is review evidence.
check_site() {
	assignment=$1
	expected="true > \"\$ARGS_FILE\""
	next_line=$(awk -v assignment="$assignment" '
		function trim(value) {
			sub(/^[[:space:]]*/, "", value)
			sub(/[[:space:]]*$/, "", value)
			return value
		}
		trim($0) == assignment {
			count++
			if (getline > 0) {
				next_line = trim($0)
			}
		}
		END {
			if (count != 1) {
				exit 2
			}
			print next_line
		}
	' .github/workflows/publish.yml) || {
		printf 'expected one workflow assignment: %s\n' "$assignment" >&2
		return 1
	}

	if [ "$next_line" != "$expected" ]; then
		printf '%s must be followed by %s; got: %s\n' "$assignment" "$expected" "$next_line" >&2
		return 1
	fi
}

failures=0
check_site "ARGS_FILE=\"\${RUNNER_TEMP}/release_pkgs_args\"" || failures=$((failures + 1))
check_site "ARGS_FILE=\"\${RUNNER_TEMP}/route_only_args\"" || failures=$((failures + 1))
[ "$failures" -eq 0 ]
