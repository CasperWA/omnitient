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
| Tests | pytest: unit tests on commit, the installed-wheel test in the manual stage, and the example's test alone when a commit changes only the contract or the example's package (not its generated handoffs, see below) |

The generated handoffs under `examples/*/handoffs/` are excluded from every hook, because they embed files verbatim.
So a commit (as opposed to `pre-commit run --all-files`) that changes only those files runs neither local pytest hook, and a hand-edited committed handoff is not caught locally.
CI catches it: its test jobs run the whole suite, and `tests/test_example.py` regenerates the handoff and compares it byte for byte.

Black, Ruff, mypy and Bandit are intentionally not in the `dev` extra.
Their versions are pinned once, in `.pre-commit-config.yaml`.
The `dev` extra holds the test tools (`pytest`, `pytest-cov`, and `markdown-it-py`, which parses handoffs in tests), `pre-commit` and the build backend; none of it is a runtime dependency.

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

CI runs the full suite and the installed-wheel test on Python 3.12, 3.13, 3.14 and 3.15 as `actions/setup-python` provides them; each run's log records the exact versions (run 39587221044: 3.12.15, 3.13.16, 3.14.8 and 3.15.0).
Before CI, the same checks passed locally on 3.12.15 (the development environment), 3.13.16, 3.14.4 from the Linux distribution and 3.15.0.
The lint, format and type hooks stay on 3.12.
Only one interpreter is needed to use or develop Omnitient.

To try another interpreter, create a disposable environment outside the repository and install the same test dependencies.
Create it with `uv venv --seed` as below.
Where an interpreter lacks `ensurepip` (for example a distribution Python without its `-venv` package, which was the case for 3.13 and 3.15 here), `uv venv` still works for an installed interpreter; `virtualenv -p /usr/bin/python3.15 PATH` also works, as does `mkvirtualenv`, but upgrade `pip` afterwards because the bundled seed can be older.
To run the pytest hooks under that interpreter, put its `bin` first on a `PATH` without unusual entries, install `pre-commit` there as well, and run `pre-commit run pytest --all-files` and `pre-commit run pytest-packaging --all-files --hook-stage manual`; check the printed interpreter in the coverage banner.

Install the `flit_core` range that `pyproject.toml` declares: with `--no-build-isolation`, pip builds with whatever `flit_core` is installed and does not check it against the build requirement.

```bash
uv venv --python /usr/bin/python3.14 --no-python-downloads --seed /tmp/omnitient-py314
/tmp/omnitient-py314/bin/python -m pip install "pytest~=9.1" "pytest-cov~=7.1" "flit_core>=4.1,<5" "markdown-it-py~=4.2"
/tmp/omnitient-py314/bin/python -m pip install --no-build-isolation -e .
/tmp/omnitient-py314/bin/python -m pytest
```

## Notices

Two files hold the license text, and both are needed.
`LICENSE` is the file that people, GitHub and the wheel's metadata look for, and it names the copyright holders.
`LICENSES/MIT.txt` is the unmodified SPDX text that the REUSE specification requires for every license identifier used in `SPDX-License-Identifier` lines; without it `reuse lint`, and so the `reuse` hook, reports `Missing licenses: MIT`.
The wheel ships both (`license-files` in `pyproject.toml`), and the packaging test checks that.
Do not edit either by hand.

`REUSE.toml` carries the notice for files whose format cannot hold a comment, or whose bytes are generated and compared exactly: `LICENSE`, the pull request template and the example work package.
The packaged JSON Schema has a companion `.license` file instead, because it ships in the wheel, where `REUSE.toml` does not.

The committed example handoff is generated from the example work package and embeds only three first-party files (the O1 contract, `context.py` and `readiness.py`), so it carries the same notice through `REUSE.toml`.
That is specific to this example: a handoff generated from other packages embeds whatever context those packages list, carries no notice and makes no claim about who owns that context.

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
  See "Notices" below.
- Comments explain a non-obvious reason; they do not repeat what the code or a configuration key already says.
  Rationale for tooling and policy belongs in these documents, not in the configuration file.
- Markdown prose follows one sentence per line, so `.markdownlint.yaml` allows long lines (headings stay short), repeats a heading text under different parents and does not require aligned table pipes.
- Do not commit `*.local.json`, `*.meta.json` or anything holding host paths or credentials.
