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
Python 3.15 is an ordinary lane; it is not allowed to fail.
Jobs time out after 30 minutes (checks) and 20 minutes (tests).
Each lane asks `actions/setup-python` for its minor version, which resolves to the newest patch release in the runner's tool cache or, failing that, the latest stable one it can download, and then checks that the interpreter it got is that minor version, a standard (GIL) build, and the one running inside the job's own virtual environment.

## Permissions and credentials

- The workflow permission is `contents: read`, for the whole workflow.
- `actions/checkout` runs with `persist-credentials: false`, so the token is not left in the checkout's Git configuration.
- No step uses a secret, a model-provider credential, a deployment credential or a cache from another repository.
- `pull_request_target` is not used and never should be for code from a pull request.
- GitHub-maintained actions use their newest stable major tag (`actions/checkout@v7`, `actions/setup-python@v7`).
  A moving tag means the revision can change; each run's "Set up job" log records the revision it resolved to.
  Dependabot proposes a new major version when one appears.

## Runner selection

The jobs choose their runner from the repository's visibility.

| Repository | `runs-on` | Pull requests |
| --- | --- | --- |
| Private | `self-hosted`, `Linux`, `X64`, `sintef-desktop-cwa` | Only from this repository; a fork's pull request skips the jobs |
| Public | `ubuntu-24.04` (GitHub-hosted) | All |

The private selector is four labels, so it is neither bare `self-hosted` nor the custom `ubuntu-latest` label that the repository's two runners also carry.
That label does not identify GitHub's Ubuntu image.
The public path names a fixed image on purpose: `runs-on: ubuntu-latest` would match GitHub-hosted runners and any self-hosted runner that carries a custom `ubuntu-latest` label.
Both paths provision Python the same way, so the only difference is where the job runs.

A skipped job is not a pass.
While the repository is private, a pull request from a fork gets no checks, and that is not test evidence.
Skipped jobs also count as passing for required status checks, so do not rely on a skipped lane.

The GitHub-hosted path has not been run.
It becomes active only when the repository is made public, and its first run will be its first test.

## Isolation

A workflow condition is not an isolation boundary.
Anyone who can change a workflow in this repository can point it at an attached runner, and can run arbitrary code there.
The same-repository condition only keeps pull requests from forks out of the private pool.
Treat everyone with write access as able to run code on the runner, and keep nothing on it that they should not reach.

The runner process itself is persistent and not sandboxed.

## Provisioning, downloads and cleanup

Every job follows the same steps:

1. Check out the repository.
2. Create the job's scratch directory (`.github/scripts/ci-scratch.sh create`).
3. Print the runner's platform, user id, tool-cache writability and cached Python versions (`.github/scripts/describe-runner.sh`).
   It prints no environment variables and reads no runner files.
4. Provision the interpreter with `actions/setup-python`.
5. Create a virtual environment inside the scratch directory and check the interpreter.
6. `pip install -e ".[dev]"`.
7. Run the pre-commit entry points.
8. Remove the scratch directory (`ci-scratch.sh cleanup`), always, and fail the step if that did not work.

What is downloaded, and where it goes:

| What | From | To |
| --- | --- | --- |
| The two actions | GitHub | the runner's action cache |
| CPython, when the runner's tool cache lacks that version | `github.com/actions/python-versions` releases | the runner's tool cache (`RUNNER_TOOL_CACHE`), **persistent** and shared by later jobs on that runner |
| Python packages for `.[dev]` and the build requirement | the default package index | the job's virtual environment and pip cache, in scratch |
| In the `checks` job: hook repositories and their tools (Black, Ruff, mypy, Bandit, pyupgrade, blacken-docs, REUSE lint, markdownlint-cli2 and its Node runtime, actionlint and a Go toolchain if the machine has none) | GitHub, the package indexes, `go.dev`, `nodejs.org` | the job's `PRE_COMMIT_HOME`, in scratch |
| In the test jobs: the manifests of the hook repositories (no tools) | GitHub | the job's `PRE_COMMIT_HOME`, in scratch |

The only persistent write outside the job's own directories is the interpreter in the tool cache.
Nothing deletes it.

The scratch directory is `$RUNNER_TEMP/omnitient-ci.XXXXXX`, created with a random suffix and an owner marker naming the run, attempt, job and matrix entry.
Cleanup removes it only if it is a plain directory directly inside `RUNNER_TEMP`, has that name, is owned by the running user and carries this job's marker.
It never removes `RUNNER_TEMP`, the tool cache or another job's files.
If the host dies or the job is killed, the directory can be left behind; only the runner's own temporary-directory handling covers that.

There is no CI-specific requirements file: `setup-python` provides `pip`, and `pyproject.toml` owns every other dependency (`.[dev]` for the test tools, the hooks for the linters).

## Updating

Dependabot is configured in [.github/dependabot.yml](../.github/dependabot.yml) for GitHub Actions and for the `dev` extra and build requirement in `pyproject.toml`.
It opens at most two grouped pull requests a week per ecosystem and merges nothing.
It starts only once that file is on the default branch.
Hook revisions in `.pre-commit-config.yaml` are not covered by it; see [development.md](development.md).

## Owner checklist: making the repository public

This is a checklist; nothing here is done by the workflow.

1. Confirm that the MIT license and the copyright notices have been merged into `main`.
2. Remove the two dedicated self-hosted runners from the repository, or restrict who can reach them, before changing visibility.
   A public repository must not keep a runner that outside pull requests could use.
3. Change the visibility yourself.
4. In the Actions settings, require approval for workflows from outside collaborators and keep the default token read-only.
5. Check the first GitHub-hosted run and each of its four Python lanes, including the interpreter each one reports, before treating the hosted path as tested.
6. Review which checks branch protection requires.

## Local equivalents

```bash
pre-commit run --all-files
pre-commit run --all-files --hook-stage manual
```

See [development.md](development.md) for the other interpreters.
