<!--
SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
SPDX-FileCopyrightText: 2026 SINTEF Ocean AS

SPDX-License-Identifier: MIT
-->

# Work packages

This directory is the project's own explicit collection for `omnitient overview`.
Each immediate subdirectory holds one `work-package.json`:

```bash
omnitient overview work --root omnitient=$PWD
```

This file and any other entry that is not a subdirectory with a `work-package.json` are ignored by the overview, and counted.
A symbolic link is the exception: it is not ignored but an excluded candidate that is never followed, and it makes the overview exit `1`, so keep none here.
Do not put synthetic or edge-case packages here; they belong in a test's temporary directory, so that this collection stays the project's real record.
Do not render handoffs into it either: render them somewhere outside the collection, and do not commit the `*.meta.json` sidecars.

## o2-overview

`omnitient/foundation/o2-overview` plans and tracks the O2 slice, the read-only overview ([docs/o2-overview-contract.md](../docs/o2-overview-contract.md)).
The identifier is a choice, not a record of earlier work: project `omnitient`, initiative `foundation` (the O1 baseline's initiative, as the example package refers to it) and work package `o2-overview`.
The package names the O1 baseline in its objective only; it declares no dependency because no earlier package file exists.

It is `draft` and carries no approval.
Pinned context digests are the bytes at the time it was prepared; a later change to a pinned file makes the package report `context.digest_mismatch` until a person reviews the change and runs `omnitient context pin`.
Nothing here was approved on anyone's behalf.
