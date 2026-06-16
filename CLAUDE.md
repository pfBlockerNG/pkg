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
