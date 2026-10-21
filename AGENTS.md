<!--
SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
SPDX-FileCopyrightText: 2026 SINTEF Ocean AS

SPDX-License-Identifier: MIT
-->

# Agent guide

Omnitient is a small, local, deterministic CLI and library with no runtime dependencies.
The specification is [docs/o1-contract.md](docs/o1-contract.md); read it before changing behaviour.
Environment, checks and test commands are in [docs/development.md](docs/development.md).

Rules that are easy to get wrong:

- Use the project's virtual environment for every Python command, including helper scripts.
- Tests must stay offline and write only to temporary directories.
- Never record an approval (`omnitient approve`) on a person's behalf, and never mark engine work complete from evidence.
- Only context files a package lists explicitly may be read.
  Do not add scanning, URL fetching, credential files or template execution.
- Keep validation, context resolution and rendering free of CLI I/O.
- Publication (pushes, pull requests, the GitHub repository) needs the owner's go-ahead; see [docs/github-handoff.md](docs/github-handoff.md).
- New first-party files need the SPDX notice described in [docs/development.md](docs/development.md); do not edit `LICENSE` or `LICENSES/`.
- Workflows follow [docs/ci.md](docs/ci.md): no secrets, no write permissions, no `pull_request_target`, and no change to the runner selection without the owner's go-ahead.
- Run `pre-commit run --all-files` before committing.
