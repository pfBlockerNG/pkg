#!/bin/sh
# shellcheck shell=sh

PFB_ROOT="${SHELLSPEC_PROJECT_ROOT}"

scrub_git_env() {
	for name in $(env | sed -n 's/^\(GIT_[A-Za-z0-9_]*\)=.*/\1/p'); do
		unset "$name"
	done
}

# The specs stand in for a LOCAL run of the writer scripts. shellspec's own CI
# job exports GITHUB_ACTIONS=true, which those scripts read as "a workflow must
# have provisioned a signing key", so both variables are cleared per example and
# set explicitly by the examples that exercise signing.
scrub_writer_env() {
	unset GITHUB_ACTIONS PFB_BOT_SIGNING_KEY_FILE
}

git_fixture() {
	GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null git "$@"
}

export PFB_ROOT
