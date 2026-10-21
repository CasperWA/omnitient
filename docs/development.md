<!--
SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
SPDX-FileCopyrightText: 2026 SINTEF Ocean AS

SPDX-License-Identifier: MIT
-->

# Development guide

## Environment

Use a Python 3.12 virtual environment.
Python 3.12 is the development baseline; the code must stay valid for `requires-python = ">=3.12"`.

```bash
pip install -e ".[dev]"
pre-commit install
```

Run the checks and tests from the activated environment.
The local pytest hooks use the `python` on `PATH`, so an inactive environment tests the wrong interpreter.

## Checks

`pre-commit` is the shared entry point, and CI runs it (see [ci.md](ci.md)).

```bash
pre-commit run --all-files                       # what runs on every commit
pre-commit run --all-files --hook-stage manual   # also the installed-wheel test
```

| Check | Tool |
| --- | --- |
| Formatting | Black (also applied to Python in Markdown through blacken-docs, pinned to the same version) |
| Lint and safe fixes | Ruff (`--no-unsafe-fixes`), configured in `pyproject.toml` |
| Types | mypy, strict, on `src/` |
| Security lint | Bandit on `src/` |
| Docs | markdownlint, and an offline check that relative Markdown links resolve |
| Notices | REUSE lint (`fsfe/reuse-tool`): every file has a license and copyright notice, in the file or in `REUSE.toml` |
| Workflows | actionlint (GitHub Actions workflow files) |
| Files | pre-commit-hooks (JSON, TOML, YAML, whitespace, large files, private keys, test naming) |
| Tests | pytest: unit tests on commit, the installed-wheel test in the manual stage |

Black, Ruff, mypy and Bandit are intentionally not in the `dev` extra.
Their versions are pinned once, in `.pre-commit-config.yaml`.

To update them, run `pre-commit autoupdate` in a branch, restore anything that is a prerelease or drops a supported Python version, and run the checks.
Dependabot does not touch hook revisions; it covers GitHub Actions and the `dev` extra and build requirement in `pyproject.toml`.
`pre-commit autoupdate` does not update the Black pin inside the blacken-docs hook.
When you bump Black, change it in both the Black hook and the blacken-docs `additional_dependencies`.

## Tests

```bash
python -m pytest              # offline unit tests and the installed-wheel test, with coverage
python -m pytest -m "not packaging"
python -m pytest -m packaging
```

Every test is offline and works in temporary directories.
An autouse fixture makes any attempt to open a network connection fail the test.
The packaging test builds a wheel with `--no-build-isolation --no-index`, installs it into a throwaway virtual environment and runs the console script from another directory.
It needs `flit_core`, which the `dev` extra installs.
The render tests parse every handoff with `markdown-it-py` (a CommonMark parser, installed by the `dev` extra) to check that author text cannot change the document's structure; it is a test dependency only.
Tests that could block, such as opening a FIFO, run the command in a child process with a timeout, so a regression fails the test instead of hanging the run.

Tested versions: 3.12.15 (the development environment), 3.13.16, 3.14.4 and 3.15.0.
All four ran the full suite and the installed-wheel test; the lint, format and type hooks stay on 3.12.
Only one interpreter is needed to use or develop Omnitient.

To try another interpreter, create a disposable environment outside the repository and install the same test dependencies.
Create it with `uv venv --seed` as below.
Where an interpreter lacks `ensurepip` (for example a distribution Python without its `-venv` package, which was the case for 3.13 and 3.15 here), `uv venv` still works for an installed interpreter; `virtualenv -p /usr/bin/python3.15 PATH` also works, as does `mkvirtualenv`, but upgrade `pip` afterwards because the bundled seed can be older.
To run the pytest hooks under that interpreter, put its `bin` first on a `PATH` without unusual entries, install `pre-commit` there as well, and run `pre-commit run pytest --all-files` and `pre-commit run pytest-packaging --all-files --hook-stage manual`; check the printed interpreter in the coverage banner.

```bash
uv venv --python /usr/bin/python3.14 --no-python-downloads --seed /tmp/omnitient-py314
/tmp/omnitient-py314/bin/python -m pip install "pytest~=9.1" "pytest-cov~=7.1" "flit_core>=3.4,<4" "markdown-it-py~=4.2"
/tmp/omnitient-py314/bin/python -m pip install --no-build-isolation -e .
/tmp/omnitient-py314/bin/python -m pytest
```

## Layout

| Path | Contents |
| --- | --- |
| `src/omnitient/schema.py` | Strict parsing and validation. |
| `src/omnitient/serialize.py` | Canonical form, stored format, content digest. |
| `src/omnitient/context.py` | Host mapping and safe context resolution. |
| `src/omnitient/readiness.py` | Per-stage readiness rules. |
| `src/omnitient/ops.py` | Pure transformations: approve, add evidence, edit context. |
| `src/omnitient/store.py` | Exclusive creation, checked updates, handoff bundle publication and verification. |
| `src/omnitient/safeio.py` | Bounded regular-file reads and strict JSON parsing. |
| `src/omnitient/render.py` | Markdown and JSON handoffs, with no I/O. |
| `src/omnitient/cli.py` | All terminal and file I/O. |
| `src/omnitient/data/` | Packaged JSON Schema. |
| `tests/` | One test module per area. |
| `examples/` | A worked work package and its handoff. |

Keep validation, context resolution and rendering free of CLI I/O so they stay testable on their own.

## Conventions

- Runtime dependencies stay at zero unless a library simplifies the contract materially.
- Change `docs/o1-contract.md` together with behaviour; the acceptance table there names the tests.
- A diagnostic code is part of the interface. Add tests when adding or renaming one.
- The example work package pins digests of repository files.
  After changing a pinned file, run `omnitient context pin` for it and render the handoff again, as described in [examples/README.md](../examples/README.md); a test fails until you do.
- Every new first-party file needs an SPDX notice for the license and the copyright holders, in a comment where the format allows one and in `REUSE.toml` where it does not.
  Copy the header from a neighbouring file.
- Do not commit `*.local.json`, `*.meta.json` or anything holding host paths or credentials.
