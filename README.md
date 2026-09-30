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

## One version per catalogue

Each stable, testing and edge catalogue lists exactly one `pfSense-pkg-pfBlockerNG`
version: the newest one eligible for that channel. A stable release is eligible for
stable, testing and edge; a testing prerelease for testing and edge; an edge
prerelease for edge only. After `3.3.10` is released all three channels list
`3.3.10`; after a later `3.3.11.a1` testing prerelease, testing and edge list
`3.3.11.a1` and stable stays on `3.3.10`. Nightly catalogues are the exception: they
keep their five newest builds. A catalogue that still lists several versions from
before this rule converges on the next publish that reaches it.

Why: `pkg` (FreeBSD) installs the first candidate a repository lists, not the
newest. It loads candidates in string order, where `3.3.9` sorts above `3.3.10.a1`, so
a catalogue holding several tagged versions can install or keep an older one
([pfBlockerNG/pfBlockerNG#3390](https://github.com/pfBlockerNG/pfBlockerNG/issues/3390)).

Limitation: a tagged catalogue has no in-catalogue rollback. To go back to an older
tagged build, install its `.pkg` from the matching
[GitHub Release](https://github.com/pfBlockerNG/pfBlockerNG/releases), for example with
`pkg add`. Older tagged builds are removed from this repository's catalogues; their
Releases keep them.
