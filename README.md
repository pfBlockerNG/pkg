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
from the source repo on a pfSense box, then:

```sh
pkg install pfSense-pkg-pfBlockerNG-devel   # or: pfSense-pkg-pfBlockerNG (stable)
```

The client repo conf points `pkg` at `https://pfblockerng.github.io/pkg/${ABI}`
(NONE-signed, TLS-anchored). See the
[pfBlockerNG README](https://github.com/pfBlockerNG/pfBlockerNG#readme) for details.
