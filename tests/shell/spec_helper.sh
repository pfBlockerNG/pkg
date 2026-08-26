#!/bin/sh
# shellcheck shell=sh

PFB_ROOT="${SHELLSPEC_PROJECT_ROOT}"

scrub_git_env() {
	for name in $(env | sed -n 's/^\(GIT_[A-Za-z0-9_]*\)=.*/\1/p'); do
		unset "$name"
	done
}

git_fixture() {
	GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null git "$@"
}

export PFB_ROOT
