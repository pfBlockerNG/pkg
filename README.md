# pfBlockerNG self-hosted `pkg` repository

This repository **hosts the GitHub Pages site** for the pfBlockerNG self-hosted
FreeBSD `pkg` repository (ADR-17). It contains **no source code or publishing
workflow**. The package source and page-building process live at
[pfBlockerNG/pfBlockerNG](https://github.com/pfBlockerNG/pfBlockerNG).

The catalog is a **derived index** built by the source repository and committed
to this repository's `main` branch. GitHub Pages serves that branch directly.

**Served at:** `https://pfblockerng.github.io/pkg`

## Using it on pfSense

Run [`scripts/add-repo.sh`](https://github.com/pfBlockerNG/pfBlockerNG/blob/devel/scripts/add-repo.sh)
from the source repo on a pfSense box (no argument), then:

```sh
pkg install pfSense-pkg-pfBlockerNG-devel   # or: pfSense-pkg-pfBlockerNG (stable)
```

The available channel paths and package versions are determined by the
catalogue committed here. See the
[pfBlockerNG README](https://github.com/pfBlockerNG/pfBlockerNG#readme) for
installation and channel-selection instructions.

The client repo conf points `pkg` at `https://pfblockerng.github.io/pkg/${ABI}`
(NONE-signed, TLS-anchored). See the
[pfBlockerNG README](https://github.com/pfBlockerNG/pfBlockerNG#readme) for details.
