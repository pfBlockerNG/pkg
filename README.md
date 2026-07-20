# pfBlockerNG self-hosted `pkg` repository

This repository **hosts the GitHub Pages site** for the pfBlockerNG self-hosted
FreeBSD `pkg` repository (ADR-17). It contains **no source code** — only the
publishing workflow. The package source lives at
[pfBlockerNG/pfBlockerNG](https://github.com/pfBlockerNG/pfBlockerNG).

The catalog is a **derived index**: [`.github/workflows/publish.yml`](.github/workflows/publish.yml)
enumerates every GitHub Release of `pfBlockerNG/pfBlockerNG`, downloads its `.pkg`
assets, runs the catalog generator, and deploys the per-`${ABI}` tree to this
repo's GitHub Pages. It runs on a daily schedule and is triggered on each
pfBlockerNG release.

**Served at:** `https://pfblockerng.github.io/pkg`

## Using it on pfSense

Run [`scripts/add-repo.sh`](https://github.com/pfBlockerNG/pfBlockerNG/blob/devel/scripts/add-repo.sh)
from the source repo on a pfSense box (no argument), then:

```sh
pkg install pfSense-pkg-pfBlockerNG-devel   # or: pfSense-pkg-pfBlockerNG (stable)
```

The **stable** and **devel** packages are served from one shared repo
(`pfblockerng`, written to `pfblockerng.conf`) — exactly as Netgate ships both
from its single `pfSense` repo — so one `add-repo.sh` run exposes both; pick
which to `pkg install`. The **nightly** repo is opt-in and separate: run
`add-repo.sh --nightly` for the bleeding-edge `pfSense-pkg-pfBlockerNG-nightly`
build (CI-passing only — not for daily use).

The client repo conf points `pkg` at `https://pfblockerng.github.io/pkg/${ABI}`
(NONE-signed, TLS-anchored). See the
[pfBlockerNG README](https://github.com/pfBlockerNG/pfBlockerNG#readme) for details.
