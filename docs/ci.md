<!--
SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
SPDX-FileCopyrightText: 2026 SINTEF Ocean AS

SPDX-License-Identifier: MIT
-->

# Continuous integration

One workflow, [.github/workflows/ci.yml](../.github/workflows/ci.yml), runs on pull requests and on pushes to `main`.
It has no secrets, no deployment, no package publication and no write access to the repository.

| Job | Python | What runs |
| --- | --- | --- |
| `Format, lint, types and docs` | 3.12 | `pre-commit run --all-files`, without the pytest hook |
| `Tests` (matrix) | 3.12, 3.13, 3.14, 3.15 | `pre-commit run pytest --all-files`, then `pre-commit run pytest-packaging --all-files --hook-stage manual` |

The matrix has `fail-fast: false`, so every interpreter reports its own result, and `max-parallel: 2`.
Python 3.15 is a normal required lane; it is not allowed to fail.
Jobs time out after 30 minutes (checks) and 20 minutes (tests).

## Permissions and credentials

- The workflow permission is `contents: read`, for the whole workflow.
- `actions/checkout` runs with `persist-credentials: false`, so the token is not left in the checkout's Git configuration.
- No step uses a secret, a model-provider credential, a deployment credential or a cache from another repository.
- `pull_request_target` is not used and never should be for code from a pull request.
- Actions are pinned to a full commit SHA, with the release in a comment: `actions/checkout` v7.0.1 and `actions/setup-python` v7.0.0 (the latter only on GitHub-hosted runners).
  Both run on Node 24, which needs a self-hosted runner of version 2.327.1 or newer.

## Runner selection

The jobs choose their runner from the repository's visibility.

| Repository | `runs-on` | Pull requests |
| --- | --- | --- |
| Private | `self-hosted`, `Linux`, `X64`, `sintef-desktop-cwa` | Only from this repository; a fork's pull request skips the jobs |
| Public | `ubuntu-24.04` (GitHub-hosted) | All |

The private selector is four labels, so it is neither bare `self-hosted` nor the custom `ubuntu-latest` label that the repository's two runners also carry.
The public path names a fixed image on purpose: `runs-on: ubuntu-latest` would match GitHub-hosted runners and any self-hosted runner that carries a custom `ubuntu-latest` label, and could therefore send public pull requests to a private machine.
Because the choice follows `github.event.repository.private`, a repository made public stops using the private pool for this workflow without any edit.
That protects this workflow only; see the public transition below for the runners themselves.

The GitHub-hosted path has not been run.
It becomes active only when the repository is made public, and its first run will be its first test.

## Isolation

A workflow condition is not an isolation boundary.
Anyone who can push a branch to this repository can change the workflow file on that branch and run arbitrary code on a self-hosted runner.
The same-repository condition only keeps pull requests from forks out of the private pool.
Treat everyone with write access as able to run code on the machine, and keep nothing on it that they should not reach.

Each job uses its own scratch directory under the runner's temporary directory and removes it afterwards, but the runner process itself is persistent and not sandboxed.

## Setup and downloads

Each job creates a virtual environment inside its scratch directory and installs `.[dev]` into it.
The jobs download:

- the checkout, through `actions/checkout`;
- `pip` itself, which `uv venv --seed` fetches from the package index into a job-local cache on the self-hosted path;
- Python packages from the default package index (`pip install -e ".[dev]"`), with pip's cache, configuration file and index settings kept job-local;
- in the `checks` job, the pre-commit hook repositories from GitHub and their tools (Black, Ruff, mypy, Bandit, pyupgrade, blacken-docs, REUSE lint, markdownlint-cli2 and its Node runtime if the machine has none), kept under the scratch directory through `PRE_COMMIT_HOME`.

The test jobs run a single hook with `pre-commit run <hook>`.
That still clones every hook repository's manifest from GitHub, into a job-local `PRE_COMMIT_HOME`, but installs no tools, because the pytest hooks use the `python` on `PATH`.

Self-hosted prerequisites, which the workflow checks but does not install:

- `python3.12`, `python3.13`, `python3.14` and `python3.15` on the runner's `PATH`, standard CPython builds;
- `uv`, used with `--no-python-downloads` to create environments even where a distribution Python lacks `ensurepip`;
- `git`, and network access to GitHub and the package index.

A missing `uv` or interpreter fails the job with a message rather than installing anything.

## Making the repository public

Do these before or at the moment of the change.
The workflow switches to GitHub-hosted runners by itself, but the runners attached to the repository do not go away.

1. Remove the two self-hosted runners from the repository, or restrict their runner group so that public workflows cannot reach them.
   This is the step that matters: a public repository must not keep a self-hosted runner that outside pull requests could use.
2. In the repository's Actions settings, require approval for workflows from outside collaborators, and keep the default workflow token read-only.
3. Run the workflow once on a branch after the change and read the first hosted results before treating them as evidence.
   If a Python 3.15 lane fails, check first whether `setup-python` offers that interpreter.
4. Review whether the branch protection or ruleset still names the right checks.
5. Remove the self-hosted branch of the workflow, and the `private` conditions in it, in a later change if the repository is not going back to private.

Skipped jobs count as passing for required status checks.
Pull requests from forks of a private repository therefore show no checks rather than failing ones; this only matters while the repository is private.

## Local equivalents

```bash
pre-commit run --all-files
pre-commit run --all-files --hook-stage manual
```

See [development.md](development.md) for the other interpreters.
