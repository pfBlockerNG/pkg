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
# Published at https://${PFB_REPO_HOST}/install.sh; run ON the box:
#   fetch -qo - https://${PFB_REPO_HOST}/install.sh | sh -s -- --channel stable
#
# Env (all overridable; forks/staging/tests set these):
#   PKG_BIN           pkg(8) binary path (default: /usr/local/sbin/pkg)
#   FETCH_BIN         fetch(1) binary for the catalogue probe (default: /usr/bin/fetch)
#   TIMEOUT_BIN       timeout(1) binary for the probe wall-clock cap (default: timeout)
#   PFBLOCKERNG_ROOT  filesystem root prefix (default: /)
#   PFB_BASE_URL      catalog base (default: http://<PFB_REPO_HOST>)
#   PFB_SSL_CA_CERT_PATH  CA hash dir exported to pkg (default: <root>/etc/ssl/certs)
#   PFB_SSL_CA_CERT_FILE  CA bundle exported to pkg (default: <root>/etc/ssl/cert.pem)
#                         Each half is guarded independently and exported on its own: the
#                         path when it is a directory, the bundle when it is a non-empty
#                         regular file. A box failing one guard still gets the other. Set
#                         either to "" to opt that half out.
#
# Exit codes: see usage() below (kept in sync — the header is the interface doc).

set -eu

# The checkout form keeps the hook beside this script; the rendered form embeds
# the same bytes. Resolve the local fallback once under a CDPATH-neutral lookup.
SCRIPT_DIR="$(CDPATH='' cd "$(dirname "$0")" && pwd)"
HOOK_SRC="${SCRIPT_DIR}/pfblockerng_repo_generate.sh"

PKG_BIN="${PKG_BIN:-/usr/local/sbin/pkg}"
FETCH_BIN="${FETCH_BIN:-/usr/bin/fetch}"
TIMEOUT_BIN="${TIMEOUT_BIN:-timeout}"
PFBLOCKERNG_ROOT="${PFBLOCKERNG_ROOT:-/}"
ROOT="${PFBLOCKERNG_ROOT%/}"
# The pkg repository domain, once. The scheme is chosen per use: the CATALOGUE is fetched
# over plain HTTP, because pkg on pfSense Plus runs against a Netgate-pinned CA bundle
# nothing we ship can widen — authenticity rides the catalogue signature instead (issue
# #2675). Fetching THIS script has no signature to fall back on, so that stays HTTPS.
PFB_DEFAULT_REPO_HOST='pkg.pfblockerng.com'
PFB_REPO_HOST="${PFB_REPO_HOST:-${PFB_DEFAULT_REPO_HOST}}"
PFB_DEFAULT_BASE_URL='http://pkg.pfblockerng.com'
PFB_BASE_URL="${PFB_BASE_URL:-${PFB_DEFAULT_BASE_URL}}"
# Normalised once, here at the input boundary, so nothing downstream rewrites a scheme:
# an operator (or an older doc) handing us https for OUR host would otherwise produce a
# conf pairing TLS with signature_type: fingerprints — the one combination pkg cannot
# fetch on Plus. Any other host is left exactly as given.
case "${PFB_BASE_URL}" in
    "https://${PFB_REPO_HOST}" | "https://${PFB_REPO_HOST}/"*)
        PFB_BASE_URL="http://${PFB_BASE_URL#https://}"
        ;;
esac

CANONICAL_PKG="pfSense-pkg-pfBlockerNG"
REPOS_DIR="${ROOT}/usr/local/etc/pkg/repos"
# Staged like every other on-box path: a ROOT-staged run (the test harnesses, a
# chroot install) must never write the host's real fingerprint store.
FINGERPRINT_DIR="${ROOT}/usr/local/etc/pkg/fingerprints/pfblockerng"
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

# Hard cap on the reader's polling below: 6 hours at one tick a second. An install that
# has not finished by then is not one this script can still narrate.
_PFB_DRAIN_MAX_TICKS=21600

# _pkg_mutate_cleanup — reap the background reader and drop both capture files. Runs from
# the EXIT trap and from every signal trap _pkg_mutate installs; safe to run twice, and
# safe before either name is set.
_pkg_mutate_cleanup() {
    if [ -n "${_mut_reader:-}" ]; then
        kill "${_mut_reader}" 2>/dev/null || true
        wait "${_mut_reader}" 2>/dev/null || true
    fi
    rm -f "${_mut_log:-}" "${_mut_done:-}"
}

# _pfb_parent_gone PID — TRUE once PID can no longer be the running parent of this
# reader: either it is gone, or it is a zombie. The zombie half matters because a killed
# parent stays in the table until whatever started it reaps it, and `kill -0` succeeds
# against a zombie the whole time — so `kill -0` alone would keep a reader polling long
# after the run it belongs to died.
_pfb_parent_gone() {
    kill -0 "$1" 2>/dev/null || return 0
    case "$(ps -o state= -p "$1" 2>/dev/null | tr -d ' ')" in
        Z*) return 0 ;;
    esac
    return 1
}

# _pkg_mutate_drain FILE — print what FILE has grown by since this shell last drained
# it, carrying the position in _drain_seen. Addressed by LINE, so a half-written line is
# never split across two prints; a final line with no newline is left for the
# read-to-end-of-file drain that runs once pkg has exited.
_pkg_mutate_drain() {
    _drain_lines=$(wc -l <"$1" 2>/dev/null || printf '0')
    _drain_lines=$((_drain_lines + 0))
    if [ "${_drain_lines}" -gt "${_drain_seen}" ]; then
        sed -n "$((_drain_seen + 1)),${_drain_lines}p" "$1"
        _drain_seen=${_drain_lines}
    fi
}

# _pkg_mutate DIE_CODE DIE_MSG ARGS... — mutating pkg verbs (install/delete).
# Stream output while pkg runs, then die if pkg rc != 0 OR a line is
# `pkg: * script failed` (pkg(8) can exit 0 after POST-INSTALL/DEINSTALL
# still failed — the files are already in place; issue #2575).
#
# pkg writes to a capture FILE and a reader prints what that file grows by, rather than
# pkg writing down a pipe. A pipe outlives pkg: every process a package script leaves
# running inherits the write end, and a reader sees no EOF until the last holder closes
# it — the hazard pfblockerng.inc states as "LOG FILE, never a pipe" for its own capture
# (issue #662), and solves the same way its own hook mirror does (issue #693). Our
# POST-INSTALL starts unbound, so that risk is not theoretical, and a hang after a
# completed install is worse than the burst it would replace. A regular file cannot stall
# the run, and the foreground pkg hands back its own status with no side channel to read
# it out of (issue #2644).
#
# What a reader can show is bounded by what pkg flushes and when; the package scripts
# (PHP CLI writes unbuffered) appear as they print.
_pkg_mutate() {
    _mut_code="$1"
    shift
    _mut_msg="$1"
    shift
    if [ -n "${ROOT}" ]; then
        mkdir -p "${ROOT}/tmp" || die "${_mut_code}" "could not create ${ROOT}/tmp"
        _mut_dir="${ROOT}/tmp"
    else
        _mut_dir="${TMPDIR:-/tmp}"
    fi
    # mktemp for BOTH files: a derived name is a name an attacker can predict from the
    # first one and pre-create as a symlink, and this runs as root.
    _mut_log=$(mktemp "${_mut_dir}/pfb-install-pkg.XXXXXX") ||
        die "${_mut_code}" "mktemp failed while capturing pkg output"
    # Armed before the SECOND mktemp, so its failure cannot leak the first file. Every
    # path is read as `:-` because `set -u` makes an unset name in a trap fatal AND
    # replaces the exit status the trap fired on; `rm -f ""` is a no-op. die() exits the
    # script, so the EXIT trap runs on that path too. /tmp on pfSense is a small RAM disk.
    #
    # The signal traps exist because the reader below is a background job: a bare EXIT
    # trap never runs on an untrapped INT/TERM/HUP, which would leave the reader
    # reparented to PID 1 polling forever and both files behind. Each re-raises the
    # conventional 128+signal status rather than swallowing the interrupt.
    trap '_pkg_mutate_cleanup' EXIT
    trap '_pkg_mutate_cleanup; exit 130' INT
    trap '_pkg_mutate_cleanup; exit 143' TERM
    trap '_pkg_mutate_cleanup; exit 129' HUP
    _mut_done=$(mktemp "${_mut_dir}/pfb-install-pkg.XXXXXX") ||
        die "${_mut_code}" "mktemp failed while capturing pkg output"
    # The reader runs in the background and stops when pkg is done: mktemp already
    # created the done-file, so the flag is CONTENT, not existence.
    #
    # It also dies with its task rather than only on being told to. `$$` stays the
    # parent's pid inside a subshell, so the liveness test ends the loop even when the
    # parent is killed outright and no trap can fire; the counter is the backstop for the
    # case where the pid outlives the run some other way, i.e. a hard cap and a deadline
    # on a wait nothing else is tracking (AGENTS.md "No orphaned waits").
    _mut_parent=$$
    (
        _drain_seen=0
        _drain_ticks=0
        while [ ! -s "${_mut_done}" ] &&
            ! _pfb_parent_gone "${_mut_parent}" &&
            [ "${_drain_ticks}" -lt "${_PFB_DRAIN_MAX_TICKS}" ]; do
            _pkg_mutate_drain "${_mut_log}"
            _drain_ticks=$((_drain_ticks + 1))
            sleep 1
        done
        sed -n "$((_drain_seen + 1)),\$p" "${_mut_log}"
    ) &
    _mut_reader=$!
    _mut_rc=0
    _pkg "$@" >"${_mut_log}" 2>&1 || _mut_rc=$?
    printf 'done\n' >"${_mut_done}"
    wait "${_mut_reader}" 2>/dev/null || true
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
    trap - EXIT INT TERM HUP
    rm -f "${_mut_log}" "${_mut_done}"
    unset _mut_code _mut_msg _mut_dir _mut_log _mut_done _mut_parent _mut_reader _mut_rc _mut_line
}

usage() {
    cat <<USAGE
install.sh — put this pfSense box on a pfBlockerNG channel.

Usage:
  install.sh --channel <stable|testing|edge|nightly>   subscribe + install/converge (idempotent)
  install.sh -h|--help                                  this text

Published at https://${PFB_REPO_HOST}/install.sh; run ON the box:
  fetch -qo - https://${PFB_REPO_HOST}/install.sh | sh -s -- --channel <stable|testing|edge|nightly>

Installs the boot-time repo-conf generator hook (ADR-39), subscribes this box to the
named channel ALONE (retiring any other pfBlockerNG channel conf), then installs or
moves the running package onto pfSense-pkg-pfBlockerNG from that channel. Safe to
re-run: a converged box performs no package changes.

Exit codes:
  0  ok, including a reported no-op (already up to date)
  1  environment: required binary/hook missing, or hook/conf staging/activation failed
  2  usage: unknown argument, unknown/missing --channel
  4  target unavailable: the hook could not resolve the conf, the catalogue probe
     (meta.conf) failed, pkg update failed, the catalogue offers nothing, or pkg
     version -t gave no usable answer
  5  a pkg operation (delete/install) failed, including a package-script failure while pkg exited 0
  6  post-install verification failed
USAGE
}

# pfb_emit_embedded_hook — print the rc.d generator hook to stdout. In the repository
# copy this is a STUB that fails loud: the standalone
# scripts/pfblockerng_repo_generate.sh is the source of truth, used directly
# from a checkout via HOOK_SRC. The website build
# (gen_landing.py) replaces the body between the PFB_EMBED markers with the hook in a
# single-quoted heredoc, producing the self-contained install.sh served at
# <base>/install.sh for `fetch | sh`.
pfb_emit_embedded_hook() {
    # PFB_EMBED_HOOK_BEGIN — do not edit; replaced by gen_landing.py at website-build time.
    cat <<'PFB_HOOK_HEREDOC'
#!/bin/sh
# /usr/local/etc/rc.d/pfblockerng_repo_generate.sh — boot-time repo-conf
# regenerator (ADR-39). Installed by install.sh.
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
# WHY AT BOOT: a pfSense OS upgrade can change the box's edition/version, which
# requires a reboot and moves the catalog subtree; regenerating here keeps the
# conf's url aligned with no extra upgrade hook to register.
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
# scripts/catalogue_engine.py exactly, including that strip: a live box's
# /etc/version can carry a pre-release suffix the matrix's version never does
# (issue #1786), and the producers strip it identically so a pre-release box and
# the publisher agree on one catalog dir (issue #1965). Arch-less since issue #1806 (NO_ARCH) — the catalog
# no longer has a per-arch leaf, so this hook no longer calls `pkg` at all (it
# used to read `pkg config abi` only to derive that leaf).
#
# The emitted conf body is BYTE-IDENTICAL to `build-repo.sh --print-conf` and
# `catalogue_engine.py --print-conf` (pinned by tests/test_repo_conf_generators.py).
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
: "${PFB_FINGERPRINT_DIR:=/usr/local/etc/pkg/fingerprints/pfblockerng}"
: "${PFB_PRODUCT_LABEL:=/etc/product_label}"
: "${PFB_VERSION_FILE:=/etc/version}"

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
# The pkg repository domain. Scheme per use site: the catalogue is plain HTTP (pkg's
# CA store is Netgate-pinned on pfSense Plus, so TLS is not a trust anchor we can rely
# on there -- the catalogue signature is, issue #2675).
REPO_HOST='pkg.pfblockerng.com'
PFB_FALLBACK_BASE_URL="http://${REPO_HOST}"

CONF_PRIORITY=100

# Detect this box's catalog subtree "<varver>" (e.g. "ce-2.8") — arch-less
# since issue #1806 (NO_ARCH). Returns 1 (no output) if the version can't be
# resolved — the caller then leaves the existing conf untouched rather than
# writing a malformed URL.
_detect_catalog() {
    # Edition: lowercase prefix matching catalogue_engine.py (ce | plus).
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
    # ONE-WAY MIGRATION, and the only scheme rewrite anywhere: a box bootstrapped
    # before issue #2675 carries an https base in its own conf, and re-emitting that
    # verbatim would pair TLS with `signature_type: fingerprints` -- precisely the
    # combination pkg cannot fetch on pfSense Plus, where its CA store is
    # Netgate-pinned. Only OUR host is moved, and only downward: a fork base, a
    # staging host, a file:// tree all keep exactly what they had. Generators emit
    # the base they are handed; nothing else rewrites a scheme.
    case "${_bc_base}" in
        "https://${REPO_HOST}" | "https://${REPO_HOST}/"*)
            printf 'http://%s' "${_bc_base#https://}"
            return 0
            ;;
    esac
    printf '%s' "${_bc_base}"
}

# The catalogue signing key's public half, as the fingerprint pkg checks: the SHA256 of
# the DER public key exactly as the catalogue embeds it (issue #2675). Shipped as the
# hex rather than the key itself because that is all `signature_type: fingerprints`
# needs on the box -- the key travels inside each signed catalogue.
# What the conf TELLS pkg, and where this hook WRITES, are two different paths and
# must stay so: PFB_FINGERPRINT_DIR is staged by every off-box caller (install.sh
# under ROOT, the specs under their own tmp box), while the emitted conf has to
# name the path that will exist on the running box -- a chroot install whose conf
# pointed at the staging directory would find no trusted key once it booted as /.
CONF_FINGERPRINT_DIR='/usr/local/etc/pkg/fingerprints/pfblockerng'
CONF_FINGERPRINT_NAME="${REPO_HOST}"
CONF_FINGERPRINT_SHA256='081df5476f84d8d20417c400f576c355069a4a9979d170bcaae1c9da32778915'

# Install the trusted fingerprint. Runs BEFORE any conf is rewritten: a box that reached
# a signature-requiring conf without the key that validates it could no longer reach the
# repository that would fix it. Every failure is non-fatal -- this hook must never wedge
# boot -- and a conf rewrite that follows a failed write is still safe, because pkg
# treats an unreadable fingerprint dir as "no trusted key" and refuses the catalogue
# rather than trusting it.
_write_fingerprint() {
    _wf_trusted="${PFB_FINGERPRINT_DIR}/trusted"
    _wf_file="${_wf_trusted}/${CONF_FINGERPRINT_NAME}"
    # $$-suffixed and OUTSIDE trusted/: two hooks racing at boot would otherwise
    # collide on one temp name, and pkg reads every file in trusted/ as a key.
    _wf_tmp="${PFB_FINGERPRINT_DIR}/.${CONF_FINGERPRINT_NAME}.$$"
    mkdir -p "${_wf_trusted}" "${PFB_FINGERPRINT_DIR}/revoked" 2>/dev/null || {
        printf '[%s] WARNING: could not create %s\n' "${name}" "${PFB_FINGERPRINT_DIR}" >&2
        return 1
    }
    if printf 'function: "sha256"\nfingerprint: "%s"\n' "${CONF_FINGERPRINT_SHA256}" >"${_wf_tmp}" 2>/dev/null; then
        if mv "${_wf_tmp}" "${_wf_file}" 2>/dev/null; then
            return 0
        fi
    fi
    rm -f "${_wf_tmp}" 2>/dev/null
    printf '[%s] WARNING: could not write %s\n' "${name}" "${_wf_file}" >&2
    return 1
}

# The URL a conf points at, for a resolved catalogue base. HTTPS is downgraded to plain
# HTTP deliberately: pkg on pfSense Plus runs against a Netgate-pinned CA bundle that
# nothing we ship can widen, so TLS to our host is not a trust anchor we can rely on --
# authenticity comes from the catalogue signature instead, and package payloads are
# checksummed by that signed catalogue. Any other scheme is left alone; a file://
# catalogue has no network in its path at all.
# The one host whose catalogues our signing key signs. The signed shape keys on the
# HOST, never the scheme alone: a fork base serves a catalogue our key never touched,
# so pinning our fingerprint to it would leave that fork unusable -- and downgrading
# someone else's host to plaintext is not ours to do.
_conf_signed_host() {
    case "$1" in
        "https://${REPO_HOST}" | "https://${REPO_HOST}/"* | \
            "http://${REPO_HOST}" | "http://${REPO_HOST}/"*) return 0 ;;
    esac
    return 1
}

# Trust comment + signature fields, keyed on the URL: a file:// catalogue is built
# locally and carries no signature, so requiring one would fail a catalogue that is fine.
_conf_trust_comment() {
    if _conf_signed_host "$1"; then
        printf '%s\n%s\n%s\n' \
            '# Signed catalogue (issue #2675): the trust anchor is our own ECDSA key, whose' \
            "# fingerprint the boot rc.d hook installs; the fetch is plain HTTP because pkg's" \
            '# CA store is Netgate-pinned on pfSense Plus and unreachable from the GUI.'
    else
        printf '%s\n' '# Unsigned catalogue: this base is not the signed project host.'
    fi
}

_conf_signature_lines() {
    if _conf_signed_host "$1"; then
        printf '  signature_type: fingerprints,\n  fingerprints: "%s",' "${CONF_FINGERPRINT_DIR}"
    else
        printf '  signature_type: none,'
    fi
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
$(_conf_trust_comment "${_ec_url}")
# The URL is fully resolved for this box's edition/version (ADR-39; arch-less/NO_ARCH,
# issue #1806); the boot rc.d hook updates it on a pfSense OS upgrade.
# priority ${CONF_PRIORITY} sits above the base Netgate \`pfSense\` repo so cross-repo
# resolution (pkg install/upgrade, GUI Install) selects the pfBlockerNG build.
${_ec_repo}: {
  url: "${_ec_url}",
  mirror_type: none,
$(_conf_signature_lines "${_ec_url}")
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

# Regenerate each channel's conf independently (channel keyed by conf path). Only
# the channel(s) the box actually subscribed to are touched — _regen_one()'s
# orphan guard skips every absent conf, so a box on one channel stays on that one
# channel across a reboot (single-repository subscription, issue #2148).
pfblockerng_repo_generate_start() {
    # FIRST, and a hard gate: a conf that requires a signature is useless without the
    # key that validates it, and a box that got one could no longer reach the repository
    # that would fix it. If the store cannot be written, every conf is left exactly as
    # it is -- whatever the box had kept working until now.
    if ! _write_fingerprint; then
        printf '[%s] WARNING: no trusted fingerprint installed — leaving every conf unchanged\n' \
            "${name}" >&2
        return 0
    fi
    _regen_one "${PFB_STABLE_CONF}"  'stable'  'pfblockerng-stable'
    _regen_one "${PFB_TESTING_CONF}" 'testing' 'pfblockerng-testing'
    _regen_one "${PFB_EDGE_CONF}"    'edge'    'pfblockerng-edge'
    _regen_one "${PFB_NIGHTLY_CONF}" 'nightly' 'pfblockerng-nightly'
    return 0
}

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
    command -v "${FETCH_BIN}" >/dev/null 2>&1 ||
        die 1 "'${FETCH_BIN}' not found — set FETCH_BIN to a fetch(1) binary"
    command -v "${TIMEOUT_BIN}" >/dev/null 2>&1 ||
        die 1 "'${TIMEOUT_BIN}' not found — set TIMEOUT_BIN to a timeout(1) binary"

    # 2. Boot-time generator hook: install/refresh only if missing or different.
    #    Try the EMBEDDED hook first; HOOK_SRC (the checkout copy under scripts/) is
    #    consulted only when the embedded hook is the repository stub
    #    (pfb_emit_embedded_hook fails). Die if neither source is available.
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

    # 3. Conf: the hook's output is staged into an INACTIVE candidate file — never
    #    into CONF_PATH. Activating a repository conf before the catalogue it names
    #    is proven reachable is the failure mode this staging exists to prevent
    #    (issue #2926): a fresh box would be stranded with a dead subscription and a
    #    working one overwritten. The candidate basename deliberately does not end
    #    in .conf, so pkg(8)'s repos glob never sees a half-generated repository
    #    definition; it becomes the real conf only in 3c, after the probe.
    mkdir -p "${REPOS_DIR}"
    CONF_CREATED=0
    _candidate="$(mktemp "${REPOS_DIR}/pfb-candidate.XXXXXX")" ||
        die 1 "mktemp failed while staging the candidate conf"
    # Every exit path — die(), a signal, normal completion — must leave no candidate
    # behind. Same discipline as _pkg_mutate's capture files; _pkg_mutate re-arms
    # these traps later, which is safe because the candidate is gone by then
    # (_candidate="" after 3c). rm -f "" is a no-op, so the unset case is safe too.
    _candidate_cleanup() {
        rm -f "${_candidate:-}"
    }
    trap '_candidate_cleanup' EXIT
    trap '_candidate_cleanup; exit 130' INT
    trap '_candidate_cleanup; exit 143' TERM
    trap '_candidate_cleanup; exit 129' HUP

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
        "${_own_conf_var}=${_candidate}" \
        PFB_BASE_URL="${PFB_BASE_URL}" \
        PFB_FINGERPRINT_DIR="${FINGERPRINT_DIR}" \
        PFB_PRODUCT_LABEL="${ROOT}/etc/product_label" \
        PFB_VERSION_FILE="${ROOT}/etc/version" \
        sh "${ON_BOX_HOOK}" onestart </dev/null || true

    if ! grep -q "${CONF_MARKER}" "${_candidate}" 2>/dev/null; then
        die 4 "the generator hook did not resolve a ${PFB_CHANNEL} conf (no marker line in the candidate) — variant detection may have failed; inspect: sh ${ON_BOX_HOOK} onestart"
    fi
    # The marker alone is not enough — and neither is the expected prefix appearing
    # ANYWHERE in the candidate: extraction and validation are ONE operation. The
    # candidate must carry EXACTLY ONE url key, and that line must be canonically
    # formed — `url: "<value>",` and nothing else — or the run stops before the
    # probe. Otherwise a quote or newline smuggled through PFB_BASE_URL makes the
    # probe fetch a truncated/foreign URL, and a commented or extra url line could
    # satisfy a grep-anywhere check while the real value points elsewhere.
    _url_keys="$(grep -c '^[[:space:]]*url[[:space:]]*:' "${_candidate}" 2>/dev/null || true)"
    case "${_url_keys}" in '' | *[!0-9]*) _url_keys=0 ;; esac
    _url_wellformed="$(sed -n 's/^[[:space:]]*url:[[:space:]]*"\([^"]*\)",[[:space:]]*$/\1/p' "${_candidate}" 2>/dev/null | grep -c . || true)"
    case "${_url_wellformed}" in '' | *[!0-9]*) _url_wellformed=0 ;; esac
    _catalog_url="$(sed -n 's/^[[:space:]]*url:[[:space:]]*"\([^"]*\)",[[:space:]]*$/\1/p' "${_candidate}" 2>/dev/null | head -n 1)"
    [ "${_url_keys}" -eq 1 ] && [ "${_url_wellformed}" -eq 1 ] || {
        die 4 "$(printf 'the candidate conf does not carry exactly one canonical quoted url key (found %s url key line(s), %s well-formed) — refusing to probe or activate; inspect: sh %s onestart' \
            "${_url_keys}" "${_url_wellformed}" "${ON_BOX_HOOK}")"
    }
    # The extracted VALUE itself must be the URL this run drove the hook to write —
    # a value pointing anywhere else is a stale or foreign conf.
    case "${_catalog_url}" in
        "${PFB_BASE_URL%/}/${PFB_CHANNEL}/"*) ;;
        *)
            die 4 "the candidate conf does not resolve to ${PFB_BASE_URL%/}/${PFB_CHANNEL}/ — a stale or foreign conf; inspect: sh ${ON_BOX_HOOK} onestart"
            ;;
    esac
    # The value must be plain catalogue coordinates: no control characters (a
    # double quote is already impossible in the extracted value) and exactly one
    # <varver> path segment after the channel.
    if printf '%s' "${_catalog_url}" | grep -q '[[:cntrl:]]'; then
        die 4 "the candidate conf's url value carries control characters — refusing to probe or activate"
    fi
    _catalog_varver="${_catalog_url#"${PFB_BASE_URL%/}/${PFB_CHANNEL}/"}"
    case "${_catalog_varver}" in
        '' | */*)
            die 4 "the candidate conf's url value is not <base>/<channel>/<varver> ('${_catalog_url}') — refusing to probe or activate"
            ;;
    esac
    # The URL must be byte-identical to what pkg(8) reads back out of the activated
    # conf: UCL — pkg's conf grammar — expands `$IDENT`/`${...}` references and a
    # backslash escapes the next character, so a url value carrying either would
    # have this probe fetch one string while pkg resolves another. Rejected BEFORE
    # the fetch (issue #2926).
    if printf '%s' "${_catalog_url}" | grep -Eq '\\|\$[[:alnum:]_{]'; then
        die 4 "the candidate conf's url value carries UCL-transforming syntax (a backslash or a \$IDENT/\${...} reference) — the probe would not fetch what pkg resolves; refusing to probe or activate"
    fi
    # 3b. Probe the catalogue BEFORE activating anything (issue #2926): the URL the
    #    candidate names must actually serve a catalogue, so <url>/meta.conf has to
    #    answer. timeout(1) owns the hard wall-clock cap; fetch's `-T` remains the
    #    per-operation stall cap. The body is discarded, and `-A` prevents the
    #    redirected-not-found behavior that option exists to reject.
    #    On failure NOTHING is activated: no pkg call is made, a fresh box keeps no
    #    conf, and an existing conf — never touched by this run — survives
    #    byte-identical.
    printf '==> Probing %s/meta.conf\n' "${_catalog_url%/}"
    if ! "${TIMEOUT_BIN}" -k 5 30 "${FETCH_BIN}" -q -A -o /dev/null -T 30 "${_catalog_url%/}/meta.conf" </dev/null; then
        die 4 "$(printf '%s/meta.conf is unreachable — not activating the %s repository conf. Inspect the catalogue: %s' \
            "${_catalog_url%/}" "${REPO_NAME}" "${_catalog_url%/}/meta.conf")"
    fi

    # 3c. Activation, only after the probe proved the catalogue. A pre-existing conf
    #    is replaced by the freshly generated bytes (exactly what the hook's old
    #    in-place rewrite did, now an atomic rename within REPOS_DIR). CONF_CREATED
    #    keeps its meaning for the failure paths below: a conf this run created is
    #    removed if a later step fails; an inherited one never is.
    #
    #    Fail closed unless the destination is ABSENT or a REGULAR file: `mv` into a
    #    directory (or a symlink to one) named pfblockerng-<ch>.conf succeeds by
    #    moving the candidate INSIDE it — activation never established, yet cleanup
    #    would be disarmed and pkg would run on.
    if [ -e "${CONF_PATH}" ] && [ ! -f "${CONF_PATH}" ]; then
        die 1 "${CONF_PATH} exists and is not a regular file — refusing to activate over it"
    fi
    [ -f "${CONF_PATH}" ] || CONF_CREATED=1
    chmod 644 "${_candidate}" ||
        die 1 "could not set the mode on the validated candidate conf"
    mv "${_candidate}" "${CONF_PATH}" ||
        die 1 "could not activate ${CONF_PATH} from the validated candidate"
    # _candidate stays armed for the EXIT trap until the destination is VERIFIED as
    # the activated regular file.
    [ -f "${CONF_PATH}" ] ||
        die 1 "${CONF_PATH} is not a regular file after activation — refusing to continue"
    _candidate=""
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
