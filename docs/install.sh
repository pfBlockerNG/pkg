#!/bin/sh
# shellcheck shell=sh
# install.sh — put this pfSense box on a pfBlockerNG channel (issue #2416 follow-up:
# one script, --channel parameterizes it, replacing the earlier per-channel
# install-<ch>.sh + install-common.sh split).
#
# WHY ONE SCRIPT: repo bootstrap (conf + boot hook) and an installed package's
# channel move fold into ONE idempotent state machine, parameterized by --channel —
# a fresh box and an already-installed box converge through the same steps instead
# of separate scripts run in sequence, and re-running on a converged box mutates
# nothing (check-then-act throughout). Four near-identical per-channel wrapper
# scripts added nothing but drift risk over a single --channel argument.
#
# WHY EVERY pkg(8) CALL REDIRECTS STDIN FROM /dev/null: the published form is piped into
# `sh` (`fetch -qo - .../install.sh | sh -s -- --channel <ch>`), so the script's own
# stdin IS the script text — any child process that reads stdin without redirection
# would consume trailing script bytes and corrupt the run.
#
# Usage:
#   install.sh --channel <stable|testing|edge|nightly>   subscribe + install/converge
#   install.sh -h|--help                                  this text
#
# Published at ${PFB_BASE_URL}/install.sh; run ON the box:
#   fetch -qo - https://pkg.pfblockerng.com/install.sh | sh -s -- --channel stable
#
# Env (all overridable; forks/staging/tests set these):
#   PKG_BIN           pkg(8) binary path (default: /usr/local/sbin/pkg)
#   PFBLOCKERNG_ROOT  filesystem root prefix (default: /)
#   PFB_BASE_URL      catalog base (default: https://pkg.pfblockerng.com)
#   PFB_SSL_CA_CERT_PATH  CA hash dir exported to pkg (default: <root>/etc/ssl/certs)
#   PFB_SSL_CA_CERT_FILE  CA bundle exported to pkg (default: <root>/etc/ssl/cert.pem)
#                         Each half is guarded independently and exported on its own: the
#                         path when it is a directory, the bundle when it is a non-empty
#                         regular file. A box failing one guard still gets the other. Set
#                         either to "" to opt that half out.
#   PFB_WEBGUI_RESTART    webConfigurator restart script (default: /etc/rc.restart_webgui),
#                         run once when this install run just changed login.conf (issue
#                         #2623); a missing/non-executable path is a silent skip (no-op
#                         off-box), and setting it to the empty string opts out (`-`, not
#                         `:-`, like the CA knobs). Never runs on an idempotent re-run or
#                         an upgrade — those never touch login.conf again.
#
# Exit codes: see usage() below (kept in sync — the header is the interface doc).

set -eu

# The hook source lives next to this file's sibling scripts/rc.d/ in a checkout.
# Resolved once at source time — CDPATH='' guard used throughout scripts/, see
# tests/shell/cdpath_spec.sh.
SCRIPT_DIR="$(CDPATH='' cd "$(dirname "$0")" && pwd)"
HOOK_SRC="${SCRIPT_DIR}/rc.d/pfblockerng_repo_generate.sh"

PKG_BIN="${PKG_BIN:-/usr/local/sbin/pkg}"
PFBLOCKERNG_ROOT="${PFBLOCKERNG_ROOT:-/}"
ROOT="${PFBLOCKERNG_ROOT%/}"
PFB_DEFAULT_BASE_URL='https://pkg.pfblockerng.com'
PFB_BASE_URL="${PFB_BASE_URL:-${PFB_DEFAULT_BASE_URL}}"

CANONICAL_PKG="pfSense-pkg-pfBlockerNG"
REPOS_DIR="${ROOT}/usr/local/etc/pkg/repos"
ON_BOX_HOOK="${ROOT}/usr/local/etc/rc.d/pfblockerng_repo_generate.sh"
CONFIG_XML="${ROOT}/cf/conf/config.xml"
CONF_MARKER="Generated at boot by pfblockerng_repo_generate"

# Every project conf this script (or a pre-#2148 legacy bootstrap) may have written.
# Exactly one may be enabled at a time — single-repository subscription, issue #2148.
PROJECT_CONFS="pfblockerng.conf
pfblockerng-stable.conf
pfblockerng-testing.conf
pfblockerng-edge.conf
pfblockerng-nightly.conf"

# die CODE MSG... — every non-zero exit goes through this.
die() {
    _die_code="$1"
    shift
    printf 'install.sh: %s\n' "$*" >&2
    exit "${_die_code}"
}

# _pkg ARGS... — every pkg(8) invocation: /dev/null stdin (piped-script safety, see
# header) + ASSUME_ALWAYS_YES so a stray prompt cannot wedge a non-interactive run.
# Callers add -y themselves on verbs that take it (delete/install); read-only verbs
# (query/rquery/version/info) ignore ASSUME_ALWAYS_YES harmlessly.
#
# CA locations (issue #2514): on pfSense Plus, pfSense-repo-setup writes a PKG_ENV block
# into pkg.conf pinning SSL_CA_CERT_FILE to Netgate's private CA bundle, which carries
# only Netgate CAs. libpkg applies that block with setenv(..., overwrite=1), so a
# libfetch-based pkg (1.x) verifies against Netgate's CAs and nothing else, and every
# fetch from a third-party catalog fails with "certificate verify failed". PKG_ENV never
# sets SSL_CA_CERT_PATH, and libfetch loads file and path into one store
# (SSL_CTX_load_verify_locations(ctx, ca_cert_file, ca_cert_path)), so exporting the path
# survives the pin. That mirrors what a libcurl-based pkg (2.x) already does by default:
# peer verification stays fully enabled, the trust store is not modified, and no vendor
# configuration is touched.
#
# The bundle goes with it, because libfetch takes load_verify_locations() as soon as
# EITHER variable is set and then never calls SSL_CTX_set_default_verify_paths(). On a box
# with no pin (CE), exporting the path alone would therefore drop the default bundle from
# the store, and FreeBSD ships /etc/ssl/certs EMPTY until certctl rehash populates it — so
# path-only would turn a working stock box into this very failure. On Plus, PKG_ENV
# overwrites this value with Netgate's bundle, which is what should happen there.
#
# The bundle is checked with -f AND -s. load_verify_locations() reads the file eagerly and
# abandons the path when that read fails, so an empty or truncated bundle would take the
# whole store down with it, a state set_default_verify_paths() would have tolerated. -s
# alone is not enough because it is TRUE for a directory, whose size is nonzero.
#
# Setting either variable to the empty string opts that half out (hence `-`, not `:-`).
# Opting the BUNDLE out on a box with no PKG_ENV pin leaves the path-only store this code
# exists to avoid, so that half is for boxes whose bundle is known bad.
PFB_SSL_CA_CERT_PATH="${PFB_SSL_CA_CERT_PATH-${ROOT}/etc/ssl/certs}"
PFB_SSL_CA_CERT_FILE="${PFB_SSL_CA_CERT_FILE-${ROOT}/etc/ssl/cert.pem}"

# webConfigurator restart knob (issue #2623) -- semantics in the header above; the
# change-gate lives at the hook invocation below.
PFB_WEBGUI_RESTART="${PFB_WEBGUI_RESTART-${ROOT}/etc/rc.restart_webgui}"

# Spelled out per combination so every path stays quoted: a single accumulated string
# would have to be word-split to become separate env(1) operands, which breaks the moment
# a location contains a space.
# TRUE when the hash directory holds at least one entry. FreeBSD ships /etc/ssl/certs
# EMPTY until `certctl rehash` populates it, and libfetch abandons
# SSL_CTX_set_default_verify_paths() as soon as EITHER variable is set -- so exporting an
# empty hash dir with no bundle beside it leaves an EMPTY store, strictly worse than
# exporting nothing (issue #2524). Mirrors the boot hook's populated-directory glob loop
# and pfb_pkg_exec()'s glob() guard: any non-dot entry counts, including a dangling
# symlink, which is what a hash dir is made of.
_ca_path_populated() {
    [ -d "$1" ] || return 1
    _cap_has_entry=0
    for _cap_entry in "$1"/*; do
        if [ -e "${_cap_entry}" ] || [ -L "${_cap_entry}" ]; then
            _cap_has_entry=1
            break
        fi
    done
    unset _cap_entry
    [ "${_cap_has_entry}" -eq 1 ] || { unset _cap_has_entry; return 1; }
    unset _cap_has_entry
    return 0
}

_pkg() {
    if [ -d "${PFB_SSL_CA_CERT_PATH}" ] &&
        [ -f "${PFB_SSL_CA_CERT_FILE}" ] && [ -s "${PFB_SSL_CA_CERT_FILE}" ]; then
        env ASSUME_ALWAYS_YES=yes \
            SSL_CA_CERT_PATH="${PFB_SSL_CA_CERT_PATH}" \
            SSL_CA_CERT_FILE="${PFB_SSL_CA_CERT_FILE}" \
            "${PKG_BIN}" "$@" </dev/null
    elif _ca_path_populated "${PFB_SSL_CA_CERT_PATH}"; then
        env ASSUME_ALWAYS_YES=yes \
            SSL_CA_CERT_PATH="${PFB_SSL_CA_CERT_PATH}" \
            "${PKG_BIN}" "$@" </dev/null
    elif [ -f "${PFB_SSL_CA_CERT_FILE}" ] && [ -s "${PFB_SSL_CA_CERT_FILE}" ]; then
        env ASSUME_ALWAYS_YES=yes \
            SSL_CA_CERT_FILE="${PFB_SSL_CA_CERT_FILE}" \
            "${PKG_BIN}" "$@" </dev/null
    else
        env ASSUME_ALWAYS_YES=yes "${PKG_BIN}" "$@" </dev/null
    fi
}

# _pkg_mutate DIE_CODE DIE_MSG ARGS... — mutating pkg verbs (install/delete).
# Stream captured output, then die if pkg rc != 0 OR a line is
# `pkg: * script failed` (pkg(8) can exit 0 after POST-INSTALL/DEINSTALL
# still failed — the files are already in place; issue #2575).
_pkg_mutate() {
    _mut_code="$1"
    shift
    _mut_msg="$1"
    shift
    if [ -n "${ROOT}" ]; then
        mkdir -p "${ROOT}/tmp" || die "${_mut_code}" "could not create ${ROOT}/tmp"
        _mut_log=$(mktemp "${ROOT}/tmp/pfb-install-pkg.XXXXXX") ||
            die "${_mut_code}" "mktemp failed while capturing pkg output"
    else
        _mut_log=$(mktemp "${TMPDIR:-/tmp}/pfb-install-pkg.XXXXXX") ||
            die "${_mut_code}" "mktemp failed while capturing pkg output"
    fi
    # die() exits the script; EXIT trap removes the file on that path too.
    # /tmp on pfSense is a small RAM disk.
    trap 'rm -f "${_mut_log}"' EXIT
    _mut_rc=0
    _pkg "$@" >"${_mut_log}" 2>&1 || _mut_rc=$?
    cat "${_mut_log}"
    if [ "${_mut_rc}" -ne 0 ]; then
        die "${_mut_code}" "${_mut_msg}"
    fi
    while IFS= read -r _mut_line || [ -n "${_mut_line}" ]; do
        # Hook stdout often ends without a newline, so pkg's message is glued
        # mid-line: ``thrown</pre>pkg: POST-INSTALL script failed``.
        case "${_mut_line}" in
            *'pkg: '*' script failed'*)
                die "${_mut_code}" "pkg reported a package-script failure — ${_mut_msg}"
                ;;
        esac
    done < "${_mut_log}"
    trap - EXIT
    rm -f "${_mut_log}"
    unset _mut_code _mut_msg _mut_log _mut_rc _mut_line
}

usage() {
    cat <<USAGE
install.sh — put this pfSense box on a pfBlockerNG channel.

Usage:
  install.sh --channel <stable|testing|edge|nightly>   subscribe + install/converge (idempotent)
  install.sh -h|--help                                  this text

Published at ${PFB_BASE_URL}/install.sh; run ON the box:
  fetch -qo - ${PFB_BASE_URL}/install.sh | sh -s -- --channel <stable|testing|edge|nightly>

Installs the boot-time repo-conf generator hook (ADR-39), subscribes this box to the
named channel ALONE (retiring any other pfBlockerNG channel conf), then installs or
moves the running package onto pfSense-pkg-pfBlockerNG from that channel. Safe to
re-run: a converged box performs no package changes.

Exit codes:
  0  ok, including a reported no-op (already up to date)
  1  environment: pkg binary or hook source not found
  2  usage: unknown argument, unknown/missing --channel
  4  target unavailable: the hook could not resolve the conf, pkg update failed, the
     catalogue offers nothing, or pkg version -t gave no usable answer
  5  a pkg operation (delete/install) failed, including a package-script failure while pkg exited 0
  6  post-install verification failed
USAGE
}

# pfb_emit_embedded_hook — print the rc.d generator hook to stdout. In the repository
# copy this is a STUB that fails loud: the standalone scripts/rc.d/pfblockerng_repo_generate.sh
# is the source of truth, used directly from a checkout via HOOK_SRC. The website build
# (gen_landing.py) replaces the body between the PFB_EMBED markers with the hook in a
# single-quoted heredoc, producing the self-contained install.sh served at
# <base>/install.sh for `fetch | sh`.
pfb_emit_embedded_hook() {
    # PFB_EMBED_HOOK_BEGIN — do not edit; replaced by gen_landing.py at website-build time.
    cat <<'PFB_HOOK_HEREDOC'
#!/bin/sh
# /usr/local/etc/rc.d/pfblockerng_repo_generate.sh — boot-time repo-conf
# regenerator (ADR-39) AND consent-gated login.conf CA carrier (issue #2617).
# Installed by install.sh.
#
# JOB 1 — WHAT IT DOES (and nothing more): for each pfBlockerNG pkg-repo conf
# file that EXISTS, it detects this box's pfSense edition/version and
# overwrites the conf with the canonical body — a fully-resolved catalog URL
# for the box's variant (ADR-17 / ADR-20) plus a marker comment. No pkg call,
# no network fetch, no snapshot, no reconcile. It is a conf REGENERATOR:
# re-deriving the conf from scratch is strictly simpler — and never wrong —
# than diffing and patching one in place.
#
# The one thing it does NOT re-derive is the catalog BASE. With no environment
# (which is every boot) the base is read back out of the conf's own url, so only
# the <varver> moves; a fork site, a staging prefix and a `file://` guest
# catalogue survive a reboot instead of being redirected to the primary Pages
# site, and a url this hook could not have written is left alone (issue #2459).
#
# JOB 2 — consent-gated login.conf CA carry (issue #2617, DEFAULT-ON — owner
# ruling): carries SSL_CA_CERT_PATH into the `default` login class's setenv
# (_logincap_setenv_add()) unless the admin has explicitly opted out (config
# field pfb_pkg_ca_consent, read live every call — never cached; see
# _login_ca_consent() below), in which case it is removed
# (_logincap_setenv_remove()). This supersedes the pkg.conf PKG_ENV patcher
# from issue #2518: that approach was retired because pfSense-repo-setup
# rewrites pkg.conf at arbitrary times (OS upgrades, branch switches) this hook
# cannot serialise against, whereas nothing on the box rewrites login.conf.
#
# WHY AT BOOT: a pfSense OS upgrade can change the box's edition/version (which
# requires a reboot and moves the catalog subtree), and can also revert
# login.conf to its stock shape. Boot follows either, and /etc/pfSense-rc
# recompiles login.conf.db on every boot regardless, so reconciling here keeps
# the carried variable aligned with no extra upgrade hook to register.
#
# rc.d ordering: REQUIRE FILESYSTEMS (so /usr/local is mounted) and
# BEFORE NETWORKING (so the conf is correct before anything that could invoke
# pkg over the network runs). Safe to run this early precisely because it is
# local-file-only — it touches no network and no daemon.
#
# HARD RULE: every path ends in `exit 0`. This hook MUST NEVER wedge boot.
#
# Detection (KISS): edition = "/etc/product_label contains 'Plus'" -> plus, else
# ce; version = major.minor of /etc/version, with any dash suffix (e.g.
# "-BETA"/"-RC") stripped FIRST. This MIRRORS catalog_name_from_version() in
# scripts/build-repo-portable.py exactly, including that strip: a live box's
# /etc/version can carry a pre-release suffix the matrix's version never does
# (issue #1786), and the producers strip it identically so a pre-release box and
# the publisher agree on one catalog dir (issue #1965). Arch-less since issue #1806 (NO_ARCH) — the catalog
# no longer has a per-arch leaf, so this hook no longer calls `pkg` at all (it
# used to read `pkg config abi` only to derive that leaf).
#
# The emitted conf body is BYTE-IDENTICAL to `build-repo.sh --print-conf` and
# `build-repo-portable.py --print-conf` (pinned by tests/test_repo_conf_generators.py).
#
# POSIX sh only; quote all expansions.

# shellcheck shell=sh
#
# FreeBSD rc.d script: rc.subr's run_rc_command dispatches the *_start handler
# via indirect variables, so shellcheck cannot see those call sites — SC2317
# (unreachable command) and SC2034 ($rcvar assigned-but-unused) are false
# positives here.
# shellcheck disable=SC2317,SC2034

# PROVIDE: pfblockerng_repo_generate
# REQUIRE: FILESYSTEMS
# BEFORE: NETWORKING

name="pfblockerng_repo_generate"

# On-box paths (installed by install.sh). Override
# via env for tests.
#
# One path per channel repository (issue #2148). The four channel repos each own
# a <channel>/<varver>/ catalogue serving the ONE canonical package. A box
# subscribes to exactly ONE of these — the orphan guard in _regen_one() is what
# keeps regeneration from re-enabling a channel the user switched away from. The
# legacy shared release repo (pfblockerng.conf, pre-#2148) is retired by the
# installers and never regenerated — a leftover is left byte-unchanged
# (issue #2416).
: "${PFB_STABLE_CONF:=/usr/local/etc/pkg/repos/pfblockerng-stable.conf}"
: "${PFB_TESTING_CONF:=/usr/local/etc/pkg/repos/pfblockerng-testing.conf}"
: "${PFB_EDGE_CONF:=/usr/local/etc/pkg/repos/pfblockerng-edge.conf}"
: "${PFB_NIGHTLY_CONF:=/usr/local/etc/pkg/repos/pfblockerng-nightly.conf}"
: "${PFB_PRODUCT_LABEL:=/etc/product_label}"
: "${PFB_VERSION_FILE:=/etc/version}"

# JOB 2 paths (issue #2617) — see _login_ca_consent().
: "${PFB_CONFIG_XML:=/cf/conf/config.xml}"
: "${PFB_SSL_CA_CERT_PATH:=/etc/ssl/certs}"

# login.conf editor paths (issue #2617) — see _logincap_setenv_add() below.
: "${PFB_LOGIN_CONF:=/etc/login.conf}"
: "${PFB_CAP_MKDB:=/usr/bin/cap_mkdb}"

# The catalog base. NOT defaulted into PFB_BASE_URL: an explicitly exported
# PFB_BASE_URL (install.sh, the smoke guests, a fork bootstrap) must stay
# distinguishable from "nothing in the environment", because at boot the base
# comes from the conf itself — see _base_from_conf() and issue #2459.
#
# The fallback below is reached only by a conf that carries no url line at all —
# today only the off-box test harnesses, since install.sh always supplies a base
# of its own. Deliberately NOT named PFB_DEFAULT_BASE_URL: gen_landing.py injects
# a variable of that name into install.sh, and the published installer carries
# this hook embedded in it.
PFB_FALLBACK_BASE_URL='https://pkg.pfblockerng.com'

CONF_PRIORITY=100

# Detect this box's catalog subtree "<varver>" (e.g. "ce-2.8") — arch-less
# since issue #1806 (NO_ARCH). Returns 1 (no output) if the version can't be
# resolved — the caller then leaves the existing conf untouched rather than
# writing a malformed URL.
_detect_catalog() {
    # Edition: lowercase prefix matching build-repo-portable.py (ce | plus).
    if grep -q 'Plus' "${PFB_PRODUCT_LABEL}" 2>/dev/null; then
        _dc_edition='plus'
    else
        _dc_edition='ce'
    fi
    # Version: major.minor of /etc/version, pre-release suffix stripped first
    # (e.g. "2.8.1" -> "2.8"; "26.07-BETA" -> "26.07"; issue #1786: a dash
    # suffix sitting inside the minor field, e.g. "-BETA"/"-RC"/"-RELEASE",
    # must not leak into the catalog varver — cut on '-' before cut on '.').
    _dc_ver=''
    [ -r "${PFB_VERSION_FILE}" ] && IFS= read -r _dc_ver < "${PFB_VERSION_FILE}"
    _dc_mm="$(printf '%s' "${_dc_ver}" | cut -d- -f1 | cut -d. -f1,2)"
    [ -n "${_dc_mm}" ] || return 1
    printf '%s-%s' "${_dc_edition}" "${_dc_mm}"
}

# Recover the catalog base a conf was last generated from. $1 = conf path,
# $2 = channel word. The canonical url is "<base>/<channel>/<varver>", so the
# base is what is left after stripping the two trailing segments — and the
# channel segment MUST equal this conf's own channel, which is what makes the
# url recognisably one this hook wrote.
#
# WHY (issue #2459): at boot there is no environment, so composing the url from
# a hardcoded default rewrote every conf to the primary Pages site — a fork
# site, a staging prefix and a `file://` guest catalogue all silently became
# https://pkg.pfblockerng.com, redirecting where the box fetches packages
# from. Reading the base back out of the conf keeps the OS-upgrade job the hook
# exists for (move the <varver>) without moving anything else.
#
# Prints the base on success. Returns 1 ONLY when the conf carries no url line at
# all (an install.sh stub pending first generation — the caller falls back to the
# built-in default); 2 whenever a url IS present but is not one this hook could
# have written (the caller leaves the conf alone). The discriminator is the
# presence of the url line, never whether the pattern below matched it:
# unquoted and single-quoted strings are valid UCL an operator can hand-write,
# and an unterminated quote is what a botched hand edit leaves behind — treating
# any of those as a pending stub would rewrite them from the built-in default,
# which is the redirect this whole guard exists to prevent.
_base_from_conf() {
    _bc_conf="$1"
    _bc_channel="$2"
    _bc_url="$(sed -n 's/^[[:space:]]*url:[[:space:]]*"\([^"]*\)".*/\1/p' "${_bc_conf}" 2>/dev/null | head -n 1)"
    if [ -z "${_bc_url}" ]; then
        # -i: a conf spelling the key `URL:` matches neither the extractor above
        # nor a case-sensitive presence check, and taking it for a conf with no
        # url at all would clobber it from the fallback base.
        grep -qi '^[[:space:]]*url[[:space:]]*:' "${_bc_conf}" 2>/dev/null && return 2
        return 1
    fi
    # One trailing slash is still our shape — a conf frozen as foreign over it
    # would sit on a stale varver forever after an OS upgrade.
    _bc_url="${_bc_url%/}"
    _bc_head="${_bc_url%/*}"
    [ "${_bc_head}" != "${_bc_url}" ] || return 2
    # The trailing segment must be shaped like the varver _detect_catalog()
    # above emits: a `ce-` or `plus-` prefix followed by major.minor. Anything
    # looser accepts a directory the operator chose (and would then replace it),
    # or a url carrying a query string or fragment (and would drop credentials
    # they put there while rewriting the path). Deliberately a shape check and
    # not an equality one — the point is to recognise OUR url, including the
    # pre-upgrade varver we are about to move off.
    _bc_varver="${_bc_url##*/}"
    case "${_bc_varver}" in
        ce-[0-9]* | plus-[0-9]*) ;;
        *) return 2 ;;
    esac
    case "${_bc_varver#*-}" in
        *[!0-9.]*) return 2 ;;
    esac
    [ "${_bc_head##*/}" = "${_bc_channel}" ] || return 2
    _bc_base="${_bc_head%/*}"
    [ "${_bc_base}" != "${_bc_head}" ] || return 2
    # The base must carry a whole scheme separator. Without one it is either a
    # bare scheme — what is left when the channel segment was in fact the host,
    # e.g. "https://nightly/ce-2.7" — or a one-slash scheme, neither of which
    # this hook emits and both of which rebuild into a malformed url.
    case "${_bc_base}" in
        *://*) ;;
        *) return 2 ;;
    esac
    printf '%s' "${_bc_base}"
}

# Emit the canonical conf body. $1 = channel word, $2 = repo name, $3 = url.
# Kept byte-identical to the *_print_conf generators (drift-pinned by tests).
_emit_conf() {
    _ec_channel="$1"
    _ec_repo="$2"
    _ec_url="$3"
    cat <<EOF
# Generated at boot by pfblockerng_repo_generate (ADR-39) — do not edit; re-run install.sh --channel ${_ec_channel} to change.
# pfBlockerNG (${_ec_channel} channel) — self-hosted pkg repository (ADR-17).
# NONE-signed: trust anchor is HTTPS to the host (no signing key). The URL is
# fully resolved for this box's edition/version (ADR-39; arch-less/NO_ARCH,
# issue #1806); the boot rc.d hook updates it on a pfSense OS upgrade.
# priority ${CONF_PRIORITY} sits above the base Netgate \`pfSense\` repo so cross-repo
# resolution (pkg install/upgrade, GUI Install) selects the pfBlockerNG build.
${_ec_repo}: {
  url: "${_ec_url}",
  mirror_type: none,
  signature_type: none,
  priority: ${CONF_PRIORITY},
  enabled: yes
}
EOF
}

# Regenerate one conf IF it exists (orphan guard: an absent conf stays absent —
# we never create a channel the user didn't bootstrap).
# $1 = conf path, $2 = channel word (stable|testing|edge|nightly), $3 = repo name.
_regen_one() {
    _ro_conf="$1"
    _ro_channel="$2"
    _ro_repo="$3"
    [ -f "${_ro_conf}" ] || return 0
    # Base: an explicit PFB_BASE_URL wins (install.sh drives the hook with one
    # precisely to MOVE a box onto another base); otherwise it is read back out
    # of the conf, so a boot with no environment preserves it (issue #2459).
    if [ -n "${PFB_BASE_URL:-}" ]; then
        _ro_base="${PFB_BASE_URL%/}"
    else
        _ro_base="$(_base_from_conf "${_ro_conf}" "${_ro_channel}")"
        _ro_rc=$?
        if [ "${_ro_rc}" -eq 1 ]; then
            _ro_base="${PFB_FALLBACK_BASE_URL}"
        elif [ "${_ro_rc}" -ne 0 ]; then
            printf '[%s] WARNING: %s carries a url this hook did not write (expected <base>/%s/<varver>) — leaving it unchanged; re-run install.sh --channel %s to re-point it\n' \
                "${name}" "${_ro_conf}" "${_ro_channel}" "${_ro_channel}" >&2
            return 0
        fi
    fi
    _ro_catalog="$(_detect_catalog)" || {
        printf '[%s] WARNING: variant detection failed — leaving %s unchanged\n' \
            "${name}" "${_ro_conf}" >&2
        return 0
    }
    # No %/ here: a base derived from the conf is already bare, except for the
    # degenerate "file://" (a catalogue rooted at /), whose slash is load-bearing.
    _ro_url="${_ro_base}/${_ro_channel}/${_ro_catalog}"
    if _emit_conf "${_ro_channel}" "${_ro_repo}" "${_ro_url}" > "${_ro_conf}.tmp" 2>/dev/null \
        && mv "${_ro_conf}.tmp" "${_ro_conf}" 2>/dev/null; then
        printf '[%s] INFO: regenerated %s -> %s\n' "${name}" "${_ro_conf}" "${_ro_url}" >&2
    else
        rm -f "${_ro_conf}.tmp" 2>/dev/null
        printf '[%s] WARNING: could not rewrite %s\n' "${name}" "${_ro_conf}" >&2
    fi
}

# JOB 2 (issue #2617): read the admin's consent for carrying SSL_CA_CERT_PATH
# into login.conf. Prints `on`, `off`, or `skip`; DEFAULT-ON (owner ruling) --
# an absent ELEMENT means the registered default, which is now On. PHP writes
# an empty token for an explicit Off and the literal token "on" for an
# explicit On, so "present but empty" is an explicit opt-out, never "absent".
# A missing/unreadable config.xml is `skip`, not On: consent is unknowable
# there, and a pfSense box cannot boot without /cf/conf/config.xml, so the
# only runs that hit this are off-box (a ROOT-staged install.sh, a dev host)
# -- exactly the runs that must never edit the host's real login.conf.
#
# pfb_pkg_ca_consent is a registered config field read on the PHP side at
# installedpackages/pfblockerng/config/0 -- PfbConfig::read('gen/pfb_pkg_ca_consent')
# -- meaning the element must be a DIRECT CHILD of the FIRST <config> block
# under the single <pfblockerng> section (config/0): never nested under a
# <row> or any other wrapper, and never a later <config> row. <config> is NOT
# unique tree-wide (every installed package gets one under
# <installedpackages>), so a whole-file grep for the element can key on the
# WRONG <config> block and disagree with the PHP side. Scoped instead: the
# FIRST <pfblockerng>...</pfblockerng> range, then within it the FIRST
# <config>...</config> block (config/0), then the element AT DEPTH 0 of that
# block on a line BY ITSELF (case-insensitive value; PfbToggle::fromLegacy()
# also accepts On/ON); every opening line also checks for its own closing tag
# before advancing scope, so a self-closed or one-line element closes on the
# line it opens rather than latching the scope open to EOF.
#
# Hardening (issue #2617, decoy-vs-default-on): under the OLD fail-closed
# default a scoping miss was a bounded FALSE NEGATIVE; under default-on the
# same miss reads as "absent" = On -- a FALSE POSITIVE against an explicit
# opt-out. The opening match is therefore line-anchored (only a "<pfblockerng>"
# or "<pfblockerng ...attrs...>" starting its own line opens the scope, so a
# decoy embedded in another element's text never does), and an attribute on
# the open tag is accepted. Remaining accepted bounded misses, all shapes
# pfSense's own config writer never emits: an attribute on the consent element
# itself, and a CDATA value containing a literal "</config>" ahead of the
# element -- each would read as "absent" = On against an explicit opt-out.
_login_ca_consent() {
    [ -r "${PFB_CONFIG_XML}" ] || { printf 'skip'; return 0; }
    _lcc_consent="$(awk '
            !seen_pb && /^[[:space:]]*<pfblockerng([[:space:]][^>]*)?>/ {
                in_pb = 1; seen_pb = 1
                if ($0 ~ /<\/pfblockerng>/) { in_pb = 0 }
                next
            }
            in_pb && /<\/pfblockerng>/ { in_pb = 0; next }
            in_pb && !seen_cfg && /<config>/ {
                in_cfg = 1; seen_cfg = 1; cfg_depth = 0
                if ($0 ~ /<\/config>/) { in_cfg = 0 }
                next
            }
            in_cfg && /<\/config>/ { in_cfg = 0; next }
            in_cfg {
                if (cfg_depth == 0 && /^[[:space:]]*<pfb_pkg_ca_consent>[Oo][Nn]<\/pfb_pkg_ca_consent>[[:space:]]*$/) {
                    print "on"; exit
                }
                if (cfg_depth == 0 && /^[[:space:]]*<pfb_pkg_ca_consent>[^<]*<\/pfb_pkg_ca_consent>[[:space:]]*$/) {
                    print "off"; exit
                }
                if (cfg_depth == 0 && /^[[:space:]]*<pfb_pkg_ca_consent\/>[[:space:]]*$/) {
                    print "off"; exit
                }
                _line = $0
                _self_closing = gsub(/<[A-Za-z_][A-Za-z0-9_.:-]*[[:space:]][^<>]*\/>/, "&", _line)
                _line = $0
                _opens = gsub(/<[A-Za-z_][A-Za-z0-9_.:-]*([[:space:]][^<>]*)?>/, "&", _line)
                _line = $0
                _closes = gsub(/<\/[A-Za-z_][A-Za-z0-9_.:-]*[[:space:]]*>/, "&", _line)
                cfg_depth += (_opens - _closes - _self_closing)
                if (cfg_depth < 0) { cfg_depth = 0 }
            }
        ' "${PFB_CONFIG_XML}" 2>/dev/null)"
    case "${_lcc_consent}" in
        off) printf 'off' ;;
        *) printf 'on' ;;
    esac
    unset _lcc_consent
}

# Reconcile login.conf with the live consent read: on -> carry the CA path
# (_logincap_setenv_add()); explicitly off -> strip it
# (_logincap_setenv_remove()); skip (no readable config.xml) -> touch nothing.
# Propagates the editor's rc.
_login_ca_reconcile() {
    case "$(_login_ca_consent)" in
        on) _logincap_setenv_add ;;
        off) _logincap_setenv_remove ;;
        *) return 0 ;;
    esac
}

# login.conf `default`-class setenv editor (issue #2617): the actual write side
# of JOB 2 above -- _login_ca_reconcile() calls _logincap_setenv_add() or
# _logincap_setenv_remove() depending on the live consent read. Ground truth
# from a live box:
#   1. getcap keeps only the FIRST `setenv` per class record; duplicates
#      compile but are dead.
#   2. a non-default class with its OWN setenv shadows `default` for its
#      users; reported, never edited.
#   3. login.conf.db (compiled by cap_mkdb), not login.conf, is what libc
#      reads.
#   4. cap_mkdb validates nothing — the byte-exact write result is the oracle.
# Wired into onestart via consent (_login_ca_reconcile()); the login-ca-sync
# and login-ca-revoke verbs below stay direct and consent-independent -- the
# PHP caller flushes config before invoking either, so it trusts its own
# read, and a boot reconcile self-heals any mismatch.

# One awk pass over PFB_LOGIN_CONF: a label starts at column 0, a record
# continues while lines end in `\`. Only the FIRST `default` record counts
# (rule 1). Prints KEY=value lines read back via _logincap_field().
_logincap_scan() {
    _lc_scan_raw="$(awk '
        {
            line = $0
            has_cont = (line ~ /\\$/)
            if (!prev_cont) {
                in_def = 0
                if (line ~ /^[^ \t#]/) {
                    cur = line
                    sub(/[:|].*/, "", cur)
                    if (cur == "default" && !done_def) {
                        in_def = 1
                        done_def = 1
                        label = NR
                        last = NR
                        wellformed = (line == "default:\\") ? 1 : 0
                    }
                } else {
                    cur = ""
                }
            } else if (in_def) {
                last = NR
                if (se_line == 0 && index(line, ":setenv=") > 0) {
                    se_line = NR
                    se_text = line
                    tmp = line
                    n = 0
                    while ((p = index(tmp, ":setenv=")) > 0) { n++; tmp = substr(tmp, p + 8) }
                    p = index(line, ":setenv=")
                    vs = p + 8
                    rest = substr(line, vs)
                    c = index(rest, ":")
                    if (n == 1 && c > 0) {
                        v = substr(rest, 1, c - 1)
                        if (index(v, "\\") == 0) {
                            se_ok = 1
                            vstart = vs
                            vend = vs + c - 1
                            value = v
                        }
                    }
                }
            } else if (cur != "" && cur != "default" && index(line, ":setenv=") > 0) {
                if (index(" " other " ", " " cur " ") == 0) {
                    other = (other == "" ? cur : other " " cur)
                }
            }
            prev_cont = has_cont
        }
        END {
            printf "WELLFORMED=%d\n", wellformed ? 1 : 0
            printf "LABEL=%d\n", label + 0
            printf "LAST=%d\n", last + 0
            printf "SETENV_LINE=%d\n", se_line + 0
            printf "SETENV_OK=%d\n", se_ok ? 1 : 0
            printf "VSTART=%d\n", vstart + 0
            printf "VEND=%d\n", vend + 0
            printf "VALUE=%s\n", value
            printf "LINE=%s\n", se_text
            printf "OTHER=%s\n", other
        }
    ' "${PFB_LOGIN_CONF}" 2>/dev/null)"
}

# Pull one KEY out of the last _logincap_scan() result.
_logincap_field() {
    printf '%s\n' "${_lc_scan_raw}" | sed -n "s/^$1=//p"
}

# Shared writer: $@ is an awk program (with any -v args) applied over
# PFB_LOGIN_CONF. A raw value handed in MUST travel via ENVIRON, never -v
# (which decodes backslash escapes and would corrupt it).
# One shared value-splice program: replace the chars [vs, ve) on line tgt with
# $PFB_LC_NEWVAL (via ENVIRON -- `awk -v` would decode escapes in the value).
# Line tgt must still read exactly as the scan saw it ($PFB_LC_EXPECT): the
# scan and this transform are separate reads of the file, so a concurrent
# editor invocation (boot reconcile vs a Software-page save) could land its
# mv in between -- splicing scan-time offsets into changed content would
# corrupt the class, while aborting here degrades the race to a clean
# refusal/lost update that the next boot reconcile repairs.
# shellcheck disable=SC2016  # awk's own $0/vs/ve, not shell expansion
_LC_SPLICE='NR==tgt { if ($0 != ENVIRON["PFB_LC_EXPECT"]) exit 9; $0 = substr($0,1,vs-1) ENVIRON["PFB_LC_NEWVAL"] substr($0,ve) } { print }'

_logincap_write() {
    _lc_tmp="${PFB_LOGIN_CONF}.tmp.$$"
    if cp -p "${PFB_LOGIN_CONF}" "${_lc_tmp}" 2>/dev/null \
        && awk "$@" "${PFB_LOGIN_CONF}" > "${_lc_tmp}" 2>/dev/null \
        && mv "${_lc_tmp}" "${PFB_LOGIN_CONF}" 2>/dev/null; then
        unset _lc_tmp
        return 0
    fi
    rm -f "${_lc_tmp}" 2>/dev/null
    unset _lc_tmp
    return 1
}

# Unconditional after every successful write (rule 3) -- /etc/pfSense-rc
# recompiles at boot anyway, so there's no stale-.db state worth tracking.
_logincap_compile() {
    if [ -x "${PFB_CAP_MKDB}" ] && ! "${PFB_CAP_MKDB}" "${PFB_LOGIN_CONF}" >/dev/null 2>&1; then
        printf '[%s] WARNING: could not recompile %s.db -- the change will not take effect until something else compiles it\n' \
            "${name}" "${PFB_LOGIN_CONF}" >&2
    fi
}

# Ensure SSL_CA_CERT_PATH is carried in the FIRST setenv of the `default`
# login class.
_logincap_setenv_add() {
    # -h before -f: a symlink also passes -f, and _logincap_write()'s tmp+mv
    # would replace the LINK's identity instead of editing through it.
    [ -h "${PFB_LOGIN_CONF}" ] && return 1
    [ -f "${PFB_LOGIN_CONF}" ] || return 1

    case "${PFB_SSL_CA_CERT_PATH}" in
        /?*) ;;
        *) return 1 ;;
    esac
    case "${PFB_SSL_CA_CERT_PATH}" in
        *[!A-Za-z0-9._/+-]*) return 1 ;;
    esac

    # Load-bearing (issue #2524): once set, libfetch skips its own default
    # verify paths, so an empty/missing hash dir would leave no trust store.
    [ -d "${PFB_SSL_CA_CERT_PATH}" ] || return 1
    _lc_has_entry=0
    for _lc_entry in "${PFB_SSL_CA_CERT_PATH}"/*; do
        if [ -e "${_lc_entry}" ] || [ -L "${_lc_entry}" ]; then
            _lc_has_entry=1
            break
        fi
    done
    unset _lc_entry
    if [ "${_lc_has_entry}" -ne 1 ]; then
        unset _lc_has_entry
        return 1
    fi
    unset _lc_has_entry

    _logincap_scan
    _lc_wellformed="$(_logincap_field WELLFORMED)"
    _lc_se_line="$(_logincap_field SETENV_LINE)"
    _lc_se_ok="$(_logincap_field SETENV_OK)"
    if [ "${_lc_wellformed}" != 1 ] || { [ "${_lc_se_line}" != 0 ] && [ "${_lc_se_ok}" != 1 ]; }; then
        printf '[%s] WARNING: login.conf default class has a shape this editor does not recognise -- not touching it\n' "${name}" >&2
        unset _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok
        return 1
    fi

    # Rule 2: report a shadowing sibling class by name; not a refusal.
    _lc_other="$(_logincap_field OTHER)"
    if [ -n "${_lc_other}" ]; then
        for _lc_cls in ${_lc_other}; do
            printf '[%s] WARNING: login.conf class "%s" defines its own setenv, shadowing default for its users -- SSL_CA_CERT_PATH will not reach them; not touching that class\n' \
                "${name}" "${_lc_cls}" >&2
        done
        unset _lc_cls
    fi
    unset _lc_other

    _lc_want="SSL_CA_CERT_PATH=${PFB_SSL_CA_CERT_PATH}"

    if [ "${_lc_se_line}" = 0 ]; then
        _lc_label="$(_logincap_field LABEL)"
        PFB_LC_NEWVAL="${_lc_want}"
        export PFB_LC_NEWVAL
        # shellcheck disable=SC2016  # awk's own $0/lbl, not shell expansion
        if _logincap_write -v lbl="${_lc_label}" \
            'NR==lbl && $0 != "default:\\" { exit 9 } { print } NR==lbl { print "\t:setenv=" ENVIRON["PFB_LC_NEWVAL"] ":\\" }'; then
            printf '[%s] INFO: added SSL_CA_CERT_PATH to the default class setenv in %s\n' "${name}" "${PFB_LOGIN_CONF}" >&2
            _logincap_compile
            unset PFB_LC_NEWVAL PFB_LC_EXPECT _lc_label _lc_want _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok
            return 0
        fi
        printf '[%s] WARNING: could not patch %s\n' "${name}" "${PFB_LOGIN_CONF}" >&2
        unset PFB_LC_NEWVAL PFB_LC_EXPECT _lc_label _lc_want _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok
        return 1
    fi

    _lc_value="$(_logincap_field VALUE)"
    _lc_found_ours=0
    _lc_found_foreign=0
    IFS=,
    set -f
    for _lc_entry_v in ${_lc_value}; do
        case "${_lc_entry_v}" in
            "${_lc_want}") _lc_found_ours=1 ;;
            SSL_CA_CERT_PATH=*) _lc_found_foreign=1 ;;
        esac
    done
    set +f
    unset IFS _lc_entry_v

    # Foreign first: getcap applies the list in order with overwrite
    # semantics, so when ours and a foreign entry coexist the LATER one wins
    # at login -- a mixed list must warn, never read as a clean no-op.
    if [ "${_lc_found_foreign}" -eq 1 ]; then
        printf '[%s] WARNING: login.conf already sets SSL_CA_CERT_PATH to a different value in the default class -- leaving it unchanged, something else owns that variable\n' "${name}" >&2
        unset _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok _lc_value _lc_want _lc_found_ours _lc_found_foreign
        return 0
    fi
    if [ "${_lc_found_ours}" -eq 1 ]; then
        unset _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok _lc_value _lc_want _lc_found_ours _lc_found_foreign
        return 0
    fi

    _lc_vstart="$(_logincap_field VSTART)"
    _lc_vend="$(_logincap_field VEND)"
    if [ -z "${_lc_value}" ]; then
        PFB_LC_NEWVAL="${_lc_want}"
    else
        PFB_LC_NEWVAL="${_lc_value},${_lc_want}"
    fi
    PFB_LC_EXPECT="$(_logincap_field LINE)"
    export PFB_LC_NEWVAL PFB_LC_EXPECT
    if _logincap_write -v tgt="${_lc_se_line}" -v vs="${_lc_vstart}" -v ve="${_lc_vend}" \
        "${_LC_SPLICE}"; then
        printf '[%s] INFO: added SSL_CA_CERT_PATH to the default class setenv in %s\n' "${name}" "${PFB_LOGIN_CONF}" >&2
        _logincap_compile
        unset PFB_LC_NEWVAL PFB_LC_EXPECT _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok _lc_value _lc_want _lc_found_ours _lc_found_foreign _lc_vstart _lc_vend
        return 0
    fi
    printf '[%s] WARNING: could not patch %s\n' "${name}" "${PFB_LOGIN_CONF}" >&2
    unset PFB_LC_NEWVAL PFB_LC_EXPECT _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok _lc_value _lc_want _lc_found_ours _lc_found_foreign _lc_vstart _lc_vend
    return 1
}

# Inverse of _logincap_setenv_add(). No CA whitelist/populated-dir check here:
# an opt-out must succeed even with the CA dir now empty or gone.
_logincap_setenv_remove() {
    [ -h "${PFB_LOGIN_CONF}" ] && return 1
    [ -f "${PFB_LOGIN_CONF}" ] || return 0

    # Fast no-op: never nag about a file that never carried our value.
    grep -F -q "SSL_CA_CERT_PATH" "${PFB_LOGIN_CONF}" 2>/dev/null || return 0

    _logincap_scan
    _lc_wellformed="$(_logincap_field WELLFORMED)"
    _lc_se_line="$(_logincap_field SETENV_LINE)"
    _lc_se_ok="$(_logincap_field SETENV_OK)"
    if [ "${_lc_wellformed}" != 1 ] || { [ "${_lc_se_line}" != 0 ] && [ "${_lc_se_ok}" != 1 ]; }; then
        printf '[%s] WARNING: login.conf default class has a shape this editor does not recognise -- not touching it\n' "${name}" >&2
        unset _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok
        return 1
    fi
    if [ "${_lc_se_line}" = 0 ]; then
        unset _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok
        return 0
    fi

    _lc_value="$(_logincap_field VALUE)"
    _lc_want="SSL_CA_CERT_PATH=${PFB_SSL_CA_CERT_PATH}"
    _lc_newval=""
    IFS=,
    set -f
    for _lc_entry_v in ${_lc_value}; do
        [ "${_lc_entry_v}" = "${_lc_want}" ] && continue
        if [ -z "${_lc_newval}" ]; then
            _lc_newval="${_lc_entry_v}"
        else
            _lc_newval="${_lc_newval},${_lc_entry_v}"
        fi
    done
    set +f
    unset IFS _lc_entry_v

    if [ "${_lc_newval}" = "${_lc_value}" ]; then
        # Not ours -- a foreign value is never stripped, but an opt-out that
        # leaves the variable exported must say so instead of reporting a
        # clean success. A list with no SSL_CA_CERT_PATH at all stays silent.
        case ",${_lc_value}," in
            *,SSL_CA_CERT_PATH=*)
                printf '[%s] WARNING: login.conf sets SSL_CA_CERT_PATH to a value this hook did not write -- leaving it in place, the opt-out did not remove it\n' "${name}" >&2
                ;;
        esac
        unset _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok _lc_value _lc_want _lc_newval
        return 0
    fi

    _lc_vstart="$(_logincap_field VSTART)"
    _lc_vend="$(_logincap_field VEND)"

    if [ -n "${_lc_newval}" ]; then
        PFB_LC_NEWVAL="${_lc_newval}"
        PFB_LC_EXPECT="$(_logincap_field LINE)"
        export PFB_LC_NEWVAL PFB_LC_EXPECT
        if _logincap_write -v tgt="${_lc_se_line}" -v vs="${_lc_vstart}" -v ve="${_lc_vend}" \
            "${_LC_SPLICE}"; then
            printf '[%s] INFO: removed SSL_CA_CERT_PATH from the default class setenv in %s\n' "${name}" "${PFB_LOGIN_CONF}" >&2
            _logincap_compile
            unset PFB_LC_NEWVAL PFB_LC_EXPECT _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok _lc_value _lc_want _lc_newval _lc_vstart _lc_vend
            return 0
        fi
        printf '[%s] WARNING: could not patch %s\n' "${name}" "${PFB_LOGIN_CONF}" >&2
        unset PFB_LC_NEWVAL PFB_LC_EXPECT _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok _lc_value _lc_want _lc_newval _lc_vstart _lc_vend
        return 1
    fi

    # newval empty: ours was the only entry, so the field (fs = start of its
    # ":setenv=" tag) or whole line goes. "whole" is precomputed here, not in
    # the writeback awk below, because that awk must strip a dangling `\`
    # from the PRECEDING line in the same pass -- before it has read this
    # line's own content to know whether the removal empties it.
    _lc_fs=$((_lc_vstart - 8))
    _lc_line="$(sed -n "${_lc_se_line}p" "${PFB_LOGIN_CONF}")"
    _lc_last="$(_logincap_field LAST)"
    _lc_whole="$(PFB_LC_LINE="${_lc_line}" awk -v fs="${_lc_fs}" -v ve="${_lc_vend}" '
        BEGIN {
            line = ENVIRON["PFB_LC_LINE"]
            pre = substr(line, 1, fs - 1)
            trail = substr(line, ve + 1)
            print (pre ~ /^[ \t]*$/ && (trail == "\\" || trail == "")) ? 1 : 0
        }
    ' 2>/dev/null)"

    # shellcheck disable=SC2016  # awk's own $0/tgt/whole/fs/ve, not shell expansion
    PFB_LC_EXPECT="$(_logincap_field LINE)"
    export PFB_LC_EXPECT
    # shellcheck disable=SC2016  # awk's own $0/tgt/whole/fs/ve, not shell expansion
    if _logincap_write -v tgt="${_lc_se_line}" -v last="${_lc_last}" -v whole="${_lc_whole}" -v fs="${_lc_fs}" -v ve="${_lc_vend}" \
        'NR == tgt && $0 != ENVIRON["PFB_LC_EXPECT"] { exit 9 }
         NR == tgt - 1 && whole == 1 && tgt == last { sub(/\\$/, "") }
         NR == tgt && whole == 1 { next }
         NR == tgt && whole == 0 { $0 = substr($0, 1, fs - 1) substr($0, ve) }
         { print }'; then
        printf '[%s] INFO: removed SSL_CA_CERT_PATH from the default class setenv in %s\n' "${name}" "${PFB_LOGIN_CONF}" >&2
        _logincap_compile
        unset PFB_LC_NEWVAL PFB_LC_EXPECT _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok _lc_value _lc_want _lc_newval _lc_vstart _lc_vend _lc_fs _lc_line _lc_last _lc_whole
        return 0
    fi
    printf '[%s] WARNING: could not patch %s\n' "${name}" "${PFB_LOGIN_CONF}" >&2
    unset PFB_LC_NEWVAL PFB_LC_EXPECT _lc_scan_raw _lc_wellformed _lc_se_line _lc_se_ok _lc_value _lc_want _lc_newval _lc_vstart _lc_vend _lc_fs _lc_line _lc_last _lc_whole
    return 1
}

# Regenerate each channel's conf independently (channel keyed by conf path). Only
# the channel(s) the box actually subscribed to are touched — _regen_one()'s
# orphan guard skips every absent conf, so a box on one channel stays on that one
# channel across a reboot (single-repository subscription, issue #2148).
pfblockerng_repo_generate_start() {
    _regen_one "${PFB_STABLE_CONF}"  'stable'  'pfblockerng-stable'
    _regen_one "${PFB_TESTING_CONF}" 'testing' 'pfblockerng-testing'
    _regen_one "${PFB_EDGE_CONF}"    'edge'    'pfblockerng-edge'
    _regen_one "${PFB_NIGHTLY_CONF}" 'nightly' 'pfblockerng-nightly'
    _login_ca_reconcile
    return 0
}

# login.conf editing verbs (issue #2617): no upgrade lock to take -- login.conf
# has no supported concurrent rewriter to serialise against, unlike pkg.conf
# under the retired JOB 2 approach (issue #2518).
case "${1:-}" in
    login-ca-sync) _logincap_setenv_add; exit $? ;;
    login-ca-revoke) _logincap_setenv_remove; exit $? ;;
esac

# Run as an rc.d service when rc.subr is present (the pfSense box); otherwise run
# the regeneration directly (off-box: install.sh's bootstrap + the shellspec
# suite, where /etc/rc.subr does not exist).
if [ -r /etc/rc.subr ]; then
    . /etc/rc.subr
    rcvar="${name}_enable"
    start_cmd="pfblockerng_repo_generate_start"
    stop_cmd=":"
    load_rc_config "${name}"
    : "${pfblockerng_repo_generate_enable:=YES}"
    run_rc_command "${1:-onestart}"
else
    pfblockerng_repo_generate_start
fi
# Always exit 0 regardless of the above — never wedge boot.
exit 0
PFB_HOOK_HEREDOC
    # PFB_EMBED_HOOK_END
}

# pfb_channel_install ARGS... — the state machine. Every step is check-then-act
# against live state, so a second run on a converged box performs zero package
# mutations and leaves the conf + hook bytes unchanged (idempotent).
pfb_channel_install() {
    PFB_CHANNEL=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --channel)
                [ $# -ge 2 ] || {
                    usage >&2
                    die 2 "--channel requires a value"
                }
                PFB_CHANNEL="$2"
                shift 2
                ;;
            --channel=*)
                PFB_CHANNEL="${1#--channel=}"
                shift
                ;;
            -h | --help)
                usage
                return 0
                ;;
            *)
                usage >&2
                die 2 "unknown argument: $1"
                ;;
        esac
    done

    case "${PFB_CHANNEL}" in
        stable | testing | edge | nightly) ;;
        "")
            usage >&2
            die 2 "--channel is required — there is no default"
            ;;
        release)
            usage >&2
            die 2 "'release' is not a channel — choose stable, testing, edge, or nightly"
            ;;
        *)
            usage >&2
            die 2 "unknown channel '${PFB_CHANNEL}' — choose stable, testing, edge, or nightly"
            ;;
    esac

    REPO_NAME="pfblockerng-${PFB_CHANNEL}"
    CONF_NAME="pfblockerng-${PFB_CHANNEL}.conf"
    CONF_PATH="${REPOS_DIR}/${CONF_NAME}"

    # 1. Environment.
    command -v "${PKG_BIN}" >/dev/null 2>&1 ||
        die 1 "'${PKG_BIN}' not found — run this ON a pfSense box, or set PKG_BIN"

    # 2. Boot-time generator hook: install/refresh only if missing or different.
    #    Try the EMBEDDED hook first: the published artifact's own filename IS
    #    install.sh, so a downloaded copy saved beside a stale on-box hook (in
    #    /usr/local/etc, where ROOT="") would make "-f SCRIPT_DIR/install.sh"
    #    true for a non-checkout copy too — trusting that check first would then
    #    `cmp` the on-box hook against HOOK_SRC, its own collided path, and never
    #    refresh a stale one. HOOK_SRC (the checkout sibling) is consulted only
    #    when the embedded hook is the repository stub (pfb_emit_embedded_hook
    #    fails); die if neither source is available.
    _hook_tmp="$(mktemp "${TMPDIR:-/tmp}/pfb-hook.XXXXXX")" || die 1 "mktemp failed while staging the boot hook"
    if pfb_emit_embedded_hook >"${_hook_tmp}" 2>/dev/null; then
        :
    elif [ -f "${HOOK_SRC}" ]; then
        cp "${HOOK_SRC}" "${_hook_tmp}"
    else
        rm -f "${_hook_tmp}"
        die 1 "no embedded hook in this copy and no checkout sibling at ${HOOK_SRC} — run the published install.sh, or run from a checkout"
    fi
    if [ -f "${ON_BOX_HOOK}" ] && cmp -s "${_hook_tmp}" "${ON_BOX_HOOK}"; then
        # "Up to date" means the BYTES match — never that the mode is already
        # correct (a restored config backup / tar extraction can drop the exec
        # bit on an otherwise byte-identical file), so chmod runs unconditionally.
        chmod 755 "${ON_BOX_HOOK}"
        printf '==> Hook up to date\n'
    else
        mkdir -p "$(dirname "${ON_BOX_HOOK}")"
        cp "${_hook_tmp}" "${ON_BOX_HOOK}"
        chmod 755 "${ON_BOX_HOOK}"
        printf '==> Installed boot-time generator hook to %s\n' "${ON_BOX_HOOK}"
    fi
    rm -f "${_hook_tmp}"

    # 3. Conf: stage a stub ONLY if absent — an existing conf is never truncated, the
    #    hook rewrites it in place (or leaves it untouched if detection fails), so no
    #    backup/restore dance is needed here (the predecessor two-script flow used to
    #    stage over a possibly-working conf before proving the new one).
    CONF_CREATED=0
    if [ ! -f "${CONF_PATH}" ]; then
        mkdir -p "${REPOS_DIR}"
        printf '# pfBlockerNG %s repo conf — pending boot-time generation (ADR-39).\n' "${PFB_CHANNEL}" >"${CONF_PATH}"
        CONF_CREATED=1
    fi

    # Drive the hook for THIS channel's conf only. Its orphan guard skips an absent
    # path, so every peer conf is aimed at a path that cannot exist: a run against
    # another base (fork, staged prefix) that fails before verify must not have
    # re-pointed a working peer subscription — peers are only ever retired, after.
    # JOB 2's paths are ROOT-prefixed too (issue #2617): without them a ROOT-staged
    # run would reconcile the HOST's /etc/login.conf against the HOST's config.xml.
    # login.conf change-gate (issue #2623): captured around the hook run, never around
    # the whole script, so an idempotent re-run or a channel move on an already-carried
    # box (JOB 2 is a no-op the second time) sees no change and never bounces the GUI --
    # restart on install only. `cksum` reads from stdin so an absent file (a fresh box,
    # nothing to reconcile yet) never trips `set -e`; it gets its own placeholder so
    # "absent before, absent after" reads as unchanged rather than a spurious diff.
    _login_conf="${ROOT}/etc/login.conf"
    _login_conf_before="$(cksum 2>/dev/null <"${_login_conf}" || printf 'absent\n')"

    printf '==> Running the generator hook to resolve the conf now\n'
    _no_conf="${REPOS_DIR}/.pfb-no-such-conf"
    _own_conf_var="PFB_$(printf '%s' "${PFB_CHANNEL}" | tr '[:lower:]' '[:upper:]')_CONF"
    env PFB_STABLE_CONF="${_no_conf}" \
        PFB_TESTING_CONF="${_no_conf}" \
        PFB_EDGE_CONF="${_no_conf}" \
        PFB_NIGHTLY_CONF="${_no_conf}" \
        "${_own_conf_var}=${CONF_PATH}" \
        PFB_BASE_URL="${PFB_BASE_URL}" \
        PFB_PRODUCT_LABEL="${ROOT}/etc/product_label" \
        PFB_VERSION_FILE="${ROOT}/etc/version" \
        PFB_CONFIG_XML="${ROOT}/cf/conf/config.xml" \
        PFB_LOGIN_CONF="${ROOT}/etc/login.conf" \
        PFB_SSL_CA_CERT_PATH="${PFB_SSL_CA_CERT_PATH}" \
        sh "${ON_BOX_HOOK}" onestart </dev/null || true

    _login_conf_after="$(cksum 2>/dev/null <"${_login_conf}" || printf 'absent\n')"
    if [ "${_login_conf_before}" != "${_login_conf_after}" ] && [ -x "${PFB_WEBGUI_RESTART}" ]; then
        printf '==> login.conf changed -- restarting the webConfigurator\n'
        # Export only what a future boot would deliver: the value must actually be in
        # the post-hook login.conf (a strip run restarts CLEAN -- never re-arm the
        # variable the admin just revoked) and the CA dir must be populated.
        # Delimiter-anchored: a capability entry is always followed by `,` or `:`,
        # so a foreign value merely sharing our path as a prefix never matches.
        if grep -F -q -e "SSL_CA_CERT_PATH=${PFB_SSL_CA_CERT_PATH}," -e "SSL_CA_CERT_PATH=${PFB_SSL_CA_CERT_PATH}:" "${_login_conf}" 2>/dev/null \
            && _ca_path_populated "${PFB_SSL_CA_CERT_PATH}"; then
            env SSL_CA_CERT_PATH="${PFB_SSL_CA_CERT_PATH}" "${PFB_WEBGUI_RESTART}" </dev/null || true
        else
            "${PFB_WEBGUI_RESTART}" </dev/null || true
        fi
    fi

    if ! grep -q "${CONF_MARKER}" "${CONF_PATH}" 2>/dev/null; then
        [ "${CONF_CREATED}" -eq 1 ] && rm -f "${CONF_PATH}"
        die 4 "the generator hook did not resolve ${CONF_PATH} (no marker line) — variant detection may have failed; inspect: sh ${ON_BOX_HOOK} onestart"
    fi
    # The marker alone is not enough: the hook leaves an EXISTING conf UNCHANGED
    # when detection fails, so a pre-existing conf carrying the marker but
    # resolving to ANOTHER base/channel (a stale conf from a fork, a staged
    # prefix, or a restored config backup) must be rejected too.
    _expect_url_prefix="url: \"${PFB_BASE_URL%/}/${PFB_CHANNEL}/"
    if ! grep -qF "${_expect_url_prefix}" "${CONF_PATH}" 2>/dev/null; then
        [ "${CONF_CREATED}" -eq 1 ] && rm -f "${CONF_PATH}"
        die 4 "${CONF_PATH} does not resolve to ${PFB_BASE_URL%/}/${PFB_CHANNEL}/ — a stale or foreign conf; inspect: sh ${ON_BOX_HOOK} onestart"
    fi
    printf '==> Conf resolved:\n'
    sed -n 's/^[[:space:]]*url:[[:space:]]*/    url: /p' "${CONF_PATH}"

    # 4. Refresh THIS repo's catalog only (a stale/unpublished peer must not veto the
    #    switch — issue #2384).
    printf '==> pkg update -f -r %s (refreshing the pfBlockerNG catalog)\n' "${REPO_NAME}"
    _pkg update -f -r "${REPO_NAME}" || {
        [ "${CONF_CREATED}" -eq 1 ] && rm -f "${CONF_PATH}"
        die 4 "$(printf '%s update -f -r %s failed — the catalog was not refreshed. Repo '\''%s'\'' is unreachable or serving an unreadable catalog. Inspect with: %s -d update -r %s' \
            "${PKG_BIN}" "${REPO_NAME}" "${REPO_NAME}" "${PKG_BIN}" "${REPO_NAME}")"
    }

    # 5. Pick the newest offered version with the SAME comparator pkg install itself
    #    resolves by (issue #2393) — rquery order is catalogue order, not version order.
    OFFERED=""
    for _offered in $(_pkg rquery -r "${REPO_NAME}" '%v' "${CANONICAL_PKG}" 2>/dev/null || true); do
        if [ -z "${OFFERED}" ]; then
            OFFERED="${_offered}"
            continue
        fi
        _order="$(_pkg version -t "${_offered}" "${OFFERED}" 2>/dev/null || true)"
        case "${_order}" in
            '>') OFFERED="${_offered}" ;;
            '<' | '=') ;;
            *)
                [ "${CONF_CREATED}" -eq 1 ] && rm -f "${CONF_PATH}"
                die 4 "\`${PKG_BIN} version -t\` gave no usable answer comparing '${_offered}' and '${OFFERED}' — cannot tell which build ${REPO_NAME} would install"
                ;;
        esac
    done
    [ -n "${OFFERED}" ] || {
        [ "${CONF_CREATED}" -eq 1 ] && rm -f "${CONF_PATH}"
        die 4 "repo '${REPO_NAME}' does not offer ${CANONICAL_PKG} — run \`pkg update -f\`, and check the ${PFB_CHANNEL} catalogue has a build for this pfSense edition/version"
    }
    printf '==> Target: %s-%s (repo %s)\n' "${CANONICAL_PKG}" "${OFFERED}" "${REPO_NAME}"

    # 6. Only now retire every OTHER project conf — the target is proven usable, so
    #    this cannot strand the box (issue #2148).
    for _other in ${PROJECT_CONFS}; do
        [ "${_other}" = "${CONF_NAME}" ] && continue
        [ -f "${REPOS_DIR}/${_other}" ] || continue
        printf '==> Retiring %s — a box subscribes to ONE pfBlockerNG channel\n' "${REPOS_DIR}/${_other}"
        rm -f "${REPOS_DIR}/${_other}"
    done

    # 7. Report installed state.
    _installed_names="$(_pkg query -g '%n' 'pfSense-pkg-pfBlockerNG*' 2>/dev/null || true)"
    _installed_names="$(printf '%s\n' "${_installed_names}" | grep -v '^[[:space:]]*$' || true)"
    if [ -n "${_installed_names}" ]; then
        for _iname in ${_installed_names}; do
            _iver="$(_pkg query '%v' "${_iname}" 2>/dev/null || true)"
            _irepo="$(_pkg query '%R' "${_iname}" 2>/dev/null || true)"
            printf '==> Installed: %s-%s (from repo %s)\n' "${_iname}" "${_iver:-unknown}" "${_irepo:-unknown}"
        done
    else
        printf '==> Installed: none\n'
    fi

    # 8. Snapshot the config section so a silent loss during 9 becomes a step-10 failure.
    CONFIG_SECTION_BEFORE=0
    if [ -f "${CONFIG_XML}" ] && grep -q '<pfblockerng>' "${CONFIG_XML}" 2>/dev/null; then
        CONFIG_SECTION_BEFORE=1
    fi

    # 9. Converge.
    # 9a. Every OTHER identity (legacy suffix, fork) is a different pkg NAME, so a
    #     plain install could never replace it — delete first.
    for _iname in ${_installed_names}; do
        [ "${_iname}" = "${CANONICAL_PKG}" ] && continue
        printf '==> Removing %s\n' "${_iname}"
        _pkg_mutate 5 "\`pkg delete ${_iname}\` failed — re-run after fixing the cause, or finish manually: ${PKG_BIN} delete -y ${_iname}" \
            delete -y "${_iname}"
    done

    _canon_ver="$(_pkg query '%v' "${CANONICAL_PKG}" 2>/dev/null || true)"
    _canon_repo="$(_pkg query '%R' "${CANONICAL_PKG}" 2>/dev/null || true)"

    if [ -n "${_canon_ver}" ] && [ "${_canon_repo}" = "${REPO_NAME}" ] && [ "${_canon_ver}" = "${OFFERED}" ]; then
        # 9b. Already converged — no pkg mutation.
        printf '==> Already up to date: %s-%s (repo %s) — nothing to do.\n' "${CANONICAL_PKG}" "${_canon_ver}" "${REPO_NAME}"
    elif [ -n "${_canon_ver}" ]; then
        # 9c. Canonical installed from another repo, or another version of this repo —
        #     a repository-qualified force reinstall (ordinary `pkg upgrade` refuses a
        #     same-or-older version; crossing repos or channels needs -f by design).
        _order="$(_pkg version -t "${OFFERED}" "${_canon_ver}" 2>/dev/null || true)"
        if [ "${_order}" = "<" ]; then
            _fam_offered="$(printf '%s' "${OFFERED}" | cut -d. -f1,2)"
            _fam_installed="$(printf '%s' "${_canon_ver}" | cut -d. -f1,2)"
            if [ "${_fam_offered}" != "${_fam_installed}" ]; then
                {
                    printf 'install.sh: WARNING — moving back from %s to %s crosses release families:\n' "${_canon_ver}" "${OFFERED}"
                    printf '  settings written by the newer build may be rolled back or dropped by the\n'
                    printf '  older build'\''s migrations, features present in the newer build disappear,\n'
                    printf '  and a re-save may be needed.\n'
                } >&2
            fi
        fi
        printf '==> Reinstalling %s from %s (repository-qualified)\n' "${CANONICAL_PKG}" "${REPO_NAME}"
        _pkg_mutate 5 "\`pkg install -f -r ${REPO_NAME} ${CANONICAL_PKG}-${OFFERED}\` failed — the previous build is still installed" \
            install -y -f -r "${REPO_NAME}" "${CANONICAL_PKG}-${OFFERED}"
    else
        # 9d. Nothing canonical installed (fresh box, or right after 9a's deletes).
        printf '==> Installing %s from %s\n' "${CANONICAL_PKG}" "${REPO_NAME}"
        _pkg_mutate 5 "\`pkg install -r ${REPO_NAME} ${CANONICAL_PKG}\` failed — no pfBlockerNG is currently installed; fix the cause, then finish with: ${PKG_BIN} install -y -r ${REPO_NAME} ${CANONICAL_PKG}" \
            install -y -r "${REPO_NAME}" "${CANONICAL_PKG}"
    fi

    # 10. Prove the result — every claim is re-read from pkg after the fact.
    _final_names="$(_pkg query -g '%n' "${CANONICAL_PKG}*" 2>/dev/null || true)"
    _final_names="$(printf '%s\n' "${_final_names}" | grep -v '^[[:space:]]*$' || true)"
    [ "${_final_names}" = "${CANONICAL_PKG}" ] ||
        die 6 "$(printf 'expected exactly %s installed, found:\n%s' "${CANONICAL_PKG}" "${_final_names:-(none)}")"

    _final_repo="$(_pkg query '%R' "${CANONICAL_PKG}" 2>/dev/null || true)"
    [ "${_final_repo}" = "${REPO_NAME}" ] ||
        die 6 "${CANONICAL_PKG} reports repo '${_final_repo:-unknown}', expected '${REPO_NAME}'"

    _final_ver="$(_pkg query '%v' "${CANONICAL_PKG}" 2>/dev/null || true)"
    [ "${_final_ver}" = "${OFFERED}" ] ||
        die 6 "${CANONICAL_PKG} reports version '${_final_ver:-unknown}', expected '${OFFERED}' from ${REPO_NAME}"

    _missing="$(
        _pkg info -l "${CANONICAL_PKG}" 2>/dev/null |
            sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' |
            grep '^/' |
            while IFS= read -r _path; do
                [ -e "${ROOT}${_path}" ] || printf '%s\n' "${_path}"
            done
    )"
    [ -z "${_missing}" ] ||
        die 6 "$(printf 'the installed payload is incomplete — these files listed by pkg info -l are missing:\n%s' "${_missing}")"

    if [ "${CONFIG_SECTION_BEFORE}" -eq 1 ]; then
        grep -q '<pfblockerng>' "${CONFIG_XML}" 2>/dev/null ||
            die 6 "the installedpackages/pfblockerng section is gone from ${CONFIG_XML} — restore your configuration backup"
    fi

    # 11. Done.
    printf '==> Done — %s-%s installed from %s (%s channel).\n' "${CANONICAL_PKG}" "${OFFERED}" "${REPO_NAME}" "${PFB_CHANNEL}"
}

pfb_channel_install "$@"
