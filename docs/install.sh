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

# Spelled out per combination so every path stays quoted: a single accumulated string
# would have to be word-split to become separate env(1) operands, which breaks the moment
# a location contains a space.
# TRUE when the hash directory holds at least one entry. FreeBSD ships /etc/ssl/certs
# EMPTY until `certctl rehash` populates it, and libfetch abandons
# SSL_CTX_set_default_verify_paths() as soon as EITHER variable is set -- so exporting an
# empty hash dir with no bundle beside it leaves an EMPTY store, strictly worse than
# exporting nothing (issue #2524). Mirrors pfb_pkgconf_dir_populated() (pfblockerng.inc)
# and the boot hook's glob loop: any non-dot entry counts, including a dangling symlink,
# which is what a hash dir is made of.
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
# regenerator (ADR-39) AND consented pkg.conf CA re-applier (issue #2518).
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
# JOB 2 — consented pkg.conf CA re-apply (issue #2518): on Plus,
# pfSense-repo-setup deletes and regenerates /usr/local/etc/pkg.conf on
# upgrades and branch switches, appending a PKG_ENV block that pins
# SSL_CA_CERT_FILE to a Netgate-only bundle. libpkg applies that block with
# setenv(key, value, 1), so only an added SSL_CA_CERT_PATH line — which
# PKG_ENV never sets — survives it and restores the public roots third-party
# repos need. Because the wipe can happen at any OS upgrade or branch switch,
# and boot follows both, this hook re-applies that one line every boot when
# the admin has consented (config field, checked live — never cached). See
# _pkgconf_ca_reapply() below; this job is UNLIKE job 1 in every other way —
# patch-in-place, not regenerate, and consent-gated rather than unconditional.
#
# WHY AT BOOT: a pfSense OS upgrade can change the box's edition/version (which
# requires a reboot), which moves the catalog subtree, and can also wipe
# pkg.conf via pfSense-repo-setup. Regenerating/re-applying every boot keeps
# both aligned with no upgrade hook to register.
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

# JOB 2 paths (issue #2518) — see _pkgconf_ca_reapply().
: "${PFB_PKG_CONF:=/usr/local/etc/pkg.conf}"
: "${PFB_CONFIG_XML:=/cf/conf/config.xml}"
: "${PFB_SSL_CA_CERT_PATH:=/etc/ssl/certs}"
: "${PFB_PKG_DIRTY:=/var/run/pkg.dirty}"
: "${PFB_LOCKF:=/usr/bin/lockf}"
: "${PFB_UPGRADE_LOCK:=/tmp/pfSense-upgrade.lock}"

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

# JOB 2 (issue #2518): re-apply the consented SSL_CA_CERT_PATH line to pkg.conf
# after pfSense-repo-setup wipes it. Every guard below fails CLOSED and quiet —
# this runs at boot for the overwhelmingly common CE case, where none of it
# applies, so a miss must never print or wedge boot. The boot path never
# reverts; the explicit ca-revoke command below handles an on-to-off transition.
_pkgconf_ca_reapply() {
    PFB_CA_REAPPLY_CONSENT=off
    [ "${PFB_UPGRADE_LOCK_HELD:-1}" = 1 ] || return 0
    grep -q 'Plus' "${PFB_PRODUCT_LABEL}" 2>/dev/null || return 0
    # Consent gate, fail-closed. pfb_pkg_ca_consent is a registered config field
    # read on the PHP side at installedpackages/pfblockerng/config/0 --
    # PfbConfig::read('gen/pfb_pkg_ca_consent') -- meaning the element must be a
    # DIRECT CHILD of the FIRST <config> block under the single <pfblockerng>
    # section (config/0): never nested under a <row> or any other wrapper, and
    # never a later <config> row. <config> is NOT unique tree-wide (every
    # installed package gets one under <installedpackages>), so a whole-file
    # grep for the element can key on the WRONG <config> block and disagree
    # with the PHP side about whether the admin consented.
    #
    # Scoped instead: take the FIRST <pfblockerng>...</pfblockerng> range, then
    # within it the FIRST <config>...</config> block -- that is exactly
    # config/0 -- then require the element AT DEPTH 1 of that block (a running
    # open/close-tag count, not just "present somewhere inside"), on a line BY
    # ITSELF (full-line match; the value compares case-insensitively --
    # PfbToggle::fromLegacy() also accepts On/ON). Every opening line ALSO
    # checks for its OWN closing tag before advancing the scope -- a self-closed
    # `<pfblockerng></pfblockerng>` / `<config></config>`, or a whole element on
    # one line, closes on the SAME line it opens. Earlier revisions of this awk
    # used `next` before that same-line check and LATCHED the scope open to
    # EOF, which is what let a same-named sibling field, a second <config> row,
    # or a nested <row> wrapper read as consent when PfbConfig disagreed
    # (issue #2518 B2).
    #
    # What this guarantees: config/0's own direct-child element, and only that
    # element, ever supplies "on" -- a sibling package's field, a later
    # <config> row, and a <row>-nested (or any deeper-nested) copy are all
    # refused. What it does NOT guarantee: a literal "<pfblockerng>" (or
    # "<config>") substring belonging to a DIFFERENT, EARLIER package in the
    # document exhausts the "first occurrence" search this hook does
    # (seen_pb/seen_cfg never re-arm), so a decoy ahead of the real block can
    # cause a FALSE NEGATIVE -- never a false positive -- on every hook call;
    # pfSense's own config writer never emits such a decoy or reorders sections,
    # so this is a defensive bound, not an
    # expected case. Nor is it sound against an XML attribute on the
    # <pfblockerng> open tag, a CDATA-wrapped value, or the whole element
    # collapsed onto one line (PfbConfig reads all three as consent, this hook
    # matches none of them) -- tracking any of those in POSIX sh is
    # disproportionate to the risk, and pfSense's own config writer emits none
    # of them (verified against a live config.xml); each is a bounded,
    # documented miss across hook calls,
    # pinned by its own spec row. Also NOT sound against a MULTI-LINE XML
    # comment that happens to wrap the element inside config/0 -- tracking
    # multi-line comment state in POSIX sh is disproportionate to that risk
    # too, and pfSense's own config writer emits no XML comments at all.
    _pcr_consent="$(awk '
            !seen_pb && /<pfblockerng>/ {
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
    [ "${_pcr_consent}" = 'on' ] || { unset _pcr_consent; return 0; }
    unset _pcr_consent
    PFB_CA_REAPPLY_CONSENT=on
    [ -e "${PFB_PKG_DIRTY}" ] && return 0

    # -h before -f: a symlink also passes -f, and the tmp+mv patch below would
    # replace the LINK's identity rather than editing through it to its target.
    [ -h "${PFB_PKG_CONF}" ] && return 0
    [ -f "${PFB_PKG_CONF}" ] || return 0

    # FreeBSD ships /etc/ssl/certs empty until `certctl rehash` populates it;
    # exporting an empty hash dir to pkg LOOKS fixed but verifies nothing, so
    # refuse rather than patch a file that only appears to work. Glob-based
    # (no `ls | wc -l`): with no match dash leaves the pattern word literal, so
    # -e/-L on it is false and the loop body never sets the flag. A bare `*`
    # glob under POSIX never matches a dotfile, so a directory holding only
    # e.g. ".DS_Store" is not considered populated.
    [ -d "${PFB_SSL_CA_CERT_PATH}" ] || return 0
    _pcr_has_entry=0
    for _pcr_entry in "${PFB_SSL_CA_CERT_PATH}"/*; do
        if [ -e "${_pcr_entry}" ] || [ -L "${_pcr_entry}" ]; then
            _pcr_has_entry=1
            break
        fi
    done
    unset _pcr_entry
    [ "${_pcr_has_entry}" -eq 1 ] || { unset _pcr_has_entry; return 0; }
    unset _pcr_has_entry

    # CA-path character whitelist: `^/[A-Za-z0-9._/+-]+$`, including that a bare
    # "/" is refused because it
    # has nothing after the leading slash). A '#' landing inside the PKG_ENV
    # block would make libucl treat the rest of the line as a comment and
    # silently truncate the CA path; a space or quote corrupts the block --
    # refuse rather than write either. `/?*` requires the leading slash plus
    # at least one more character (rejects the bare-slash case); the second
    # case matches any string containing a character outside the whitelist
    # (the idiom this file already uses at the varver check above).
    case "${PFB_SSL_CA_CERT_PATH}" in
        /?*) ;;
        *) return 0 ;;
    esac
    case "${PFB_SSL_CA_CERT_PATH}" in
        *[!A-Za-z0-9._/+-]*) return 0 ;;
    esac

    # Shape guard: refuse anything but exactly what pfSense-repo-setup writes.
    # Never touch a file already patched (SSL_CA_CERT_PATH present anywhere) or
    # hand-edited into an unrecognised shape — each check below is one clause
    # of that shape, checked independently so a near-miss is still refused.
    grep -q 'SSL_CA_CERT_PATH' "${PFB_PKG_CONF}" 2>/dev/null && return 0
    # grep -c on an unreadable file prints nothing to stdout and exits nonzero;
    # an unguarded `[ "$_pcr_open_count" -eq 1 ]` then errors with a literal
    # "Illegal number:" on stderr, contradicting this file's own "never print"
    # intent (issue #2518 nitpick N-illegal-number) -- `|| _pcr_open_count=0`
    # defaults it on ANY grep failure (unreadable file or a genuine zero
    # matches; either way the -eq 1 check below correctly refuses).
    _pcr_open_count="$(grep -c '^PKG_ENV {$' "${PFB_PKG_CONF}" 2>/dev/null)" || _pcr_open_count=0
    [ "${_pcr_open_count:-0}" -eq 1 ] || { unset _pcr_open_count; return 0; }
    unset _pcr_open_count
    # The block: from the (unique) opener to the first column-0 `}` after it.
    # If no such `}` exists this range runs to EOF and its last line is not
    # `}` — the "later line equal to `}`" check that catches an unclosed block.
    _pcr_block="$(sed -n '/^PKG_ENV {$/,/^}$/p' "${PFB_PKG_CONF}" 2>/dev/null)"
    [ "$(printf '%s\n' "${_pcr_block}" | tail -n 1)" = '}' ] || { unset _pcr_block; return 0; }
    _pcr_ca_file="$(printf '%s\n' "${_pcr_block}" | sed -n 's/^	SSL_CA_CERT_FILE=//p')"
    [ "$(printf '%s\n' "${_pcr_ca_file}" | wc -l | tr -d ' ')" -eq 1 ] \
        || { unset _pcr_block _pcr_ca_file; return 0; }
    case "${_pcr_ca_file}" in
        /?*) ;;
        *) unset _pcr_block _pcr_ca_file; return 0 ;;
    esac
    case "${_pcr_ca_file}" in
        *[!A-Za-z0-9._/+-]*) unset _pcr_block _pcr_ca_file; return 0 ;;
    esac
    [ -f "${_pcr_ca_file}" ] && [ -r "${_pcr_ca_file}" ] && [ -s "${_pcr_ca_file}" ] \
        || { unset _pcr_block _pcr_ca_file; return 0; }
    unset _pcr_ca_file
    # Refuse a block whose "close" is really a NESTED sub-object's own `}`
    # (issue #2518 nitpick N-nested-brace): the sed range above stops at the
    # FIRST column-0 `}` after the opener, same as the insertion awk below --
    # so a `SOMETHING {` sub-block occurring before the true close makes that
    # `}` look like PKG_ENV's own, and the line below would be inserted INSIDE
    # the sub-object instead (looks patched, verifies nothing: libpkg would set
    # the key on the sub-object, not PKG_ENV). Rule: strip the block's own
    # opening and closing lines; the remaining lines must be brace-BALANCED (as
    # many "...{"-opening lines as bare "}" lines) -- a nested open with no
    # matching nested close inside that middle means the "close" found above is
    # not PKG_ENV's own.
    _pcr_mid="$(printf '%s\n' "${_pcr_block}" | sed '1d;$d')"
    _pcr_mid_opens="$(printf '%s\n' "${_pcr_mid}" | grep -c '{$' 2>/dev/null)" || _pcr_mid_opens=0
    _pcr_mid_closes="$(printf '%s\n' "${_pcr_mid}" | grep -cx '}' 2>/dev/null)" || _pcr_mid_closes=0
    if [ "${_pcr_mid_opens:-0}" -ne "${_pcr_mid_closes:-0}" ]; then
        unset _pcr_block _pcr_mid _pcr_mid_opens _pcr_mid_closes
        return 0
    fi
    unset _pcr_block _pcr_mid _pcr_mid_opens _pcr_mid_closes

    # Patch: insert the one line immediately before the block's closing `}`,
    # nothing else touched. tmp+mv mirrors _regen_one()'s idiom, with two extra
    # steps for mode and trailing-newline preservation:
    #   - `cp -p` seeds the temp with the ORIGINAL file's permission bits
    #     before the `>` redirect below truncates it -- truncation keeps the
    #     inode (and its mode); a fresh `>` on a name that did not exist would
    #     not, which is why the patched file used to land at the process
    #     umask instead of pkg.conf's own mode.
    #   - awk's `print` always terminates its last output line, which would
    #     otherwise turn a pkg.conf whose last byte is `}` (no trailing
    #     newline) into one that has one; the tail-c1 check + reprint below
    #     restores that exact newline-less state when the original had it.
    _pcr_original_sum="$(cksum < "${PFB_PKG_CONF}" 2>/dev/null)" \
        || { unset _pcr_original_sum; return 0; }
    _pcr_tmp="${PFB_PKG_CONF}.tmp"
    _pcr_had_no_trailing_nl=0
    [ -n "$(tail -c1 "${PFB_PKG_CONF}" 2>/dev/null)" ] && _pcr_had_no_trailing_nl=1
    if cp -p "${PFB_PKG_CONF}" "${_pcr_tmp}" 2>/dev/null \
        && awk -v ins="	SSL_CA_CERT_PATH=${PFB_SSL_CA_CERT_PATH}" '
            $0 == "PKG_ENV {" { seen_open = 1 }
            seen_open && !done && $0 == "}" { print ins; done = 1 }
            { print }
        ' "${PFB_PKG_CONF}" > "${_pcr_tmp}" 2>/dev/null; then
        if [ "${_pcr_had_no_trailing_nl}" -eq 1 ]; then
            printf '%s' "$(cat "${_pcr_tmp}" 2>/dev/null)" > "${_pcr_tmp}" 2>/dev/null
        fi
        if [ -e "${PFB_PKG_DIRTY}" ]; then
            _pcr_live_sum=''
        else
            _pcr_live_sum="$(cksum < "${PFB_PKG_CONF}" 2>/dev/null)" || _pcr_live_sum=''
        fi
        if [ -z "${_pcr_live_sum}" ] || [ "${_pcr_live_sum}" != "${_pcr_original_sum}" ]; then
            rm -f "${_pcr_tmp}" 2>/dev/null
            unset _pcr_tmp _pcr_had_no_trailing_nl _pcr_original_sum _pcr_live_sum
            return 0
        fi
        if mv "${_pcr_tmp}" "${PFB_PKG_CONF}" 2>/dev/null; then
            printf '[%s] INFO: patched %s with the consented SSL_CA_CERT_PATH\n' "${name}" "${PFB_PKG_CONF}" >&2
        else
            rm -f "${_pcr_tmp}" 2>/dev/null
            printf '[%s] WARNING: could not patch %s\n' "${name}" "${PFB_PKG_CONF}" >&2
        fi
    else
        rm -f "${_pcr_tmp}" 2>/dev/null
        printf '[%s] WARNING: could not patch %s\n' "${name}" "${PFB_PKG_CONF}" >&2
    fi
    unset _pcr_tmp _pcr_had_no_trailing_nl _pcr_original_sum _pcr_live_sum
    return 0
}

_pkgconf_ca_sync_command() {
    _pkgconf_ca_reapply
    _pcr_owned_line="$(printf '\tSSL_CA_CERT_PATH=%s' "${PFB_SSL_CA_CERT_PATH}")"
    if [ "${PFB_CA_REAPPLY_CONSENT:-off}" = on ] \
        && ! grep -F -qx "${_pcr_owned_line}" "${PFB_PKG_CONF}" 2>/dev/null; then
        return 1
    fi
    return 0
}

_pkgconf_ca_revoke() {
    [ "${PFB_UPGRADE_LOCK_HELD:-}" = 1 ] || return 1
    [ -e "${PFB_PKG_CONF}" ] && [ ! -h "${PFB_PKG_CONF}" ] \
        && [ -f "${PFB_PKG_CONF}" ] && [ -r "${PFB_PKG_CONF}" ] || return 1
    [ ! -e "${PFB_PKG_DIRTY}" ] || return 1
    _pcr_original_sum="$(cksum < "${PFB_PKG_CONF}" 2>/dev/null)" || return 1
    _pcr_open_count="$(grep -c '^PKG_ENV {$' "${PFB_PKG_CONF}" 2>/dev/null)" || _pcr_open_count=0
    [ "${_pcr_open_count:-0}" -eq 1 ] || return 1
    _pcr_block="$(sed -n '/^PKG_ENV {$/,/^}$/p' "${PFB_PKG_CONF}" 2>/dev/null)" || return 1
    [ "$(printf '%s\n' "${_pcr_block}" | tail -n 1)" = '}' ] || return 1
    _pcr_ca_file_count="$(grep -F -c 'SSL_CA_CERT_FILE' "${PFB_PKG_CONF}" 2>/dev/null)" || _pcr_ca_file_count=0
    [ "${_pcr_ca_file_count:-0}" -eq 1 ] || return 1
    _pcr_ca_file="$(printf '%s\n' "${_pcr_block}" | sed -n 's/^\tSSL_CA_CERT_FILE=//p')"
    [ "$(printf '%s\n' "${_pcr_ca_file}" | grep -c .)" -eq 1 ] || return 1
    case "${_pcr_ca_file}" in
        /?*) ;;
        *) return 1 ;;
    esac
    case "${_pcr_ca_file}" in
        *[!A-Za-z0-9._/+-]*) return 1 ;;
    esac
    _pcr_target="$(printf '%s\n' "${_pcr_block}" | grep -F -x -c "	SSL_CA_CERT_PATH=${PFB_SSL_CA_CERT_PATH}" 2>/dev/null)" || _pcr_target=0
    _pcr_any_path="$(grep -F -c 'SSL_CA_CERT_PATH' "${PFB_PKG_CONF}" 2>/dev/null)" || _pcr_any_path=0
    _pcr_mid="$(printf '%s\n' "${_pcr_block}" | sed '1d;$d')"
    _pcr_mid_opens="$(printf '%s\n' "${_pcr_mid}" | grep -c '{$' 2>/dev/null)" || _pcr_mid_opens=0
    _pcr_mid_closes="$(printf '%s\n' "${_pcr_mid}" | grep -cx '}' 2>/dev/null)" || _pcr_mid_closes=0
    [ "${_pcr_mid_opens:-0}" -eq "${_pcr_mid_closes:-0}" ] || return 1
    if [ "${_pcr_any_path}" -eq 0 ]; then
        return 0
    fi
    [ "${_pcr_target}" -eq 1 ] && [ "${_pcr_any_path}" -eq 1 ] || return 1
    _pcr_tmp="${PFB_PKG_CONF}.tmp"
    _pcr_had_no_trailing_nl=0
    [ -n "$(tail -c1 "${PFB_PKG_CONF}" 2>/dev/null)" ] && _pcr_had_no_trailing_nl=1
    if ! cp -p "${PFB_PKG_CONF}" "${_pcr_tmp}" 2>/dev/null \
        || ! awk -v target="	SSL_CA_CERT_PATH=${PFB_SSL_CA_CERT_PATH}" '
            !removed && $0 == target { removed = 1; next }
            { print }
        ' "${PFB_PKG_CONF}" > "${_pcr_tmp}" 2>/dev/null; then
        rm -f "${_pcr_tmp}" 2>/dev/null
        return 1
    fi
    if [ "${_pcr_had_no_trailing_nl}" -eq 1 ]; then
        printf '%s' "$(cat "${_pcr_tmp}" 2>/dev/null)" > "${_pcr_tmp}" 2>/dev/null || {
            rm -f "${_pcr_tmp}" 2>/dev/null
            return 1
        }
    fi
    _pcr_live_sum="$(cksum < "${PFB_PKG_CONF}" 2>/dev/null)" || _pcr_live_sum=''
    if [ -z "${_pcr_live_sum}" ] || [ "${_pcr_live_sum}" != "${_pcr_original_sum}" ] \
        || ! mv "${_pcr_tmp}" "${PFB_PKG_CONF}" 2>/dev/null; then
        rm -f "${_pcr_tmp}" 2>/dev/null
        return 1
    fi
    return 0
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
    _pkgconf_ca_reapply
    return 0
}

# pfSense-upgrade holds this same lock while pfSense-repo-setup rewrites pkg.conf.
# Re-exec keeps verification and replacement inside one supported-writer critical section.
if [ "${PFB_UPGRADE_LOCK_HELD:-}" != 1 ] && [ -x "${PFB_LOCKF}" ]; then
    PFB_UPGRADE_LOCK_HELD=1
    export PFB_UPGRADE_LOCK_HELD
    if "${PFB_LOCKF}" -s -t 0 "${PFB_UPGRADE_LOCK}" /bin/sh "$0" "$@"; then
        exit 0
    fi
    PFB_UPGRADE_LOCK_HELD=0
    export PFB_UPGRADE_LOCK_HELD
fi

case "${1:-}" in
    ca-sync|ca-revoke)
        [ "${PFB_UPGRADE_LOCK_HELD:-}" = 1 ] || exit 1
        ;;
esac

case "${1:-}" in
    ca-sync) _pkgconf_ca_sync_command; exit $? ;;
    ca-revoke) _pkgconf_ca_revoke; exit $? ;;
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
        sh "${ON_BOX_HOOK}" onestart </dev/null || true

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
