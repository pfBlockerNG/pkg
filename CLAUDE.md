# CLAUDE.md — pfBlockerNG/pkg

`pfBlockerNG/pkg` hosts and deploys the self-hosted FreeBSD `pkg` repository to
GitHub Pages (ADR-17). Its `.github/workflows/publish.yml` builds the current
`devel` `.pkg` (via `pfBlockerNG/pfBlockerNG`'s `build-pkg-portable.py`), folds
in every Release `.pkg`, regenerates the per-ABI catalog, and deploys to
`pfblockerng.github.io/pkg`.

As a `pfBlockerNG`-org repo, this **inherits `pfBlockerNG/pfBlockerNG`'s
`CLAUDE.md` + project/user `.claude/settings.json` as the org default** (see its
"Scope" section): communication (caveman + exceptions), the working principles
(don't-guess / investigate / confirm ambiguity), worktrees + the rebase-only
landing flow, branch naming, the test-coverage mandate, linting discipline,
GitHub-issue handling + labels, and commit style. Read that file — it is the
source of truth. The pfBlockerNG-package mechanics (the DNSBL/ABP pipeline, the
smoke/UI suites) and language/runtime code-standard specifics do **not** apply
here. **Any rule below overrides the inherited default for this repo.**

## Git hooks

Activate once after cloning: `sh scripts/setup-hooks.sh` (sets `core.hooksPath` to `.githooks`).
git can't auto-apply a committed hooks path, so this opt-in is required. **Claude: ensure hooks
are active before working here** — if `git config core.hooksPath` is not `.githooks`, run it once
at session start (idempotent).

`.githooks/prepare-commit-msg` (byte-synced with `pfBlockerNG/pfBlockerNG`) appends a
`Co-authored-by:` trailer for the human owner so GitHub credits them when an agent is the
committer — resolving the owner generically (`coauthor.*` git config, else
`$CLAUDE_CODE_USER_EMAIL`, else the author), and a no-op when the human is already the committer
or credited. See the public repo's *Commit style → Author, committer, and signing*.

## Commit attribution and signing

Inherited — see `pfBlockerNG/pfBlockerNG`'s `## Commit style` → **Author, committer, and signing**
(the source of truth). Not restated here, to avoid drift.
