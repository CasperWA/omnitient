<!--
SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
SPDX-FileCopyrightText: 2026 SINTEF Ocean AS

SPDX-License-Identifier: MIT
-->

# Omnitient

Omnitient prepares portable work packages and self-contained agent handoffs.
It is a small, local, deterministic Python command-line tool and library.
It makes no model calls, runs no agents and contacts no network service.

A *work package* is one JSON file that records approved scope, decisions and the context an implementer needs.
`omnitient render` turns it into a Markdown and a JSON handoff that can be pasted into a fresh session without replaying a long conversation.

Omnitient owns its own work-package metadata only.
It does not read or write the state of any autonomous-development engine.

## Status

Pre-release, slice O1.
The behaviour is specified in [docs/o1-contract.md](docs/o1-contract.md).

Supported Python: 3.12 or newer (`requires-python = ">=3.12"`).
Tested: CPython 3.12.15 (development baseline), 3.13.16, 3.14.4 and 3.15.0, standard GIL builds from the Linux distribution, on Linux only.
Free-threaded and JIT builds, other platforms (including Windows) and other implementations are not verified.
You need only one supported interpreter to use Omnitient; the other versions matter only if you want to reproduce the compatibility checks (see [docs/development.md](docs/development.md)).

## License

Omnitient is released under the [MIT License](LICENSE), which permits commercial use, modification and resale.
Copyright (c) 2026 Casper Welzel Andersen and SINTEF Ocean AS.

Source files carry SPDX notices.
Files that cannot carry one, such as strict JSON, are covered by `REUSE.toml`, and the packaged JSON Schema by a companion `.license` file so the notice travels with the wheel.
Handoffs that `omnitient render` generates carry no notice: they embed whatever context a package lists, and Omnitient makes no claim about who owns or licenses that content.

## Set up for development

Create a Python 3.12 virtual environment, activate it, and install the project in editable mode:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

The runtime has no third-party dependencies.
`.[dev]` adds pytest, pytest-cov, pre-commit and the build backend.
Black, Ruff, mypy and Bandit are not installed into the environment; pre-commit runs them in its own isolated environments.
See [docs/development.md](docs/development.md) for the checks and conventions.

## Quick tour

Scaffold a package.
It starts as a `draft` with no approvals and a placeholder objective:

```bash
omnitient new work/my-package/work-package.json \
    --project demo --initiative first --work-package wp1 --title "My package"
```

Edit the JSON to fill in the objective, scope, exclusions, acceptance criteria and any decisions.
`omnitient schema` prints a JSON Schema for editor support.

Add context files by logical root.
The package stores `repo:docs/spec.md` and a digest, never your checkout path.
The `--root` option supplies the host directory for the root name on this machine only:

```bash
omnitient context add work/my-package repo:docs/spec.md \
    --root repo=$HOME/src/my-repo --purpose "The specification"
```

Inspect and validate.
Both only read:

```bash
omnitient show work/my-package --root repo=$HOME/src/my-repo
omnitient validate work/my-package --root repo=$HOME/src/my-repo --stage implementation
```

Record approval, then generate the handoff:

```bash
omnitient approve work/my-package --stage implementation --by "Your Name" \
    --root repo=$HOME/src/my-repo
omnitient render work/my-package --stage implementation --out work/handoffs \
    --root repo=$HOME/src/my-repo
```

Later, attach evidence.
Evidence never changes the approved scope or the approval:

```bash
omnitient evidence add work/my-package --kind pr \
    --ref https://example.org/owner/repo/pull/12 --by "Your Name"
```

A worked example with unresolved decisions lives in [examples/](examples/).

### Commands

| Command | Purpose |
| --- | --- |
| `new` | Scaffold a draft, unapproved package. |
| `validate` | Schema validity, then readiness for each handoff stage. |
| `show` | Compact read-only inspection. |
| `render` | Generate `handoff-<stage>.md`, `.json` and a metadata sidecar that doubles as the bundle's completion manifest. |
| `approve` | Record approval of the present revision for a stage. |
| `evidence add` | Append a report, PR or commit reference. |
| `context add` / `context pin` | Add a context file, or accept its changed digest. |
| `schema` | Print the packaged JSON Schema. |

Exit codes: `0` success; `1` refused or invalid input; `2` usage error; `3` valid but not ready for the requested stage.

## Things worth knowing

- **Valid is not ready.** A package can be schema-valid and still blocked: for an implementation handoff it needs every blocking decision resolved, every required context file present with the expected digest, and a current approval.
- **A blocked handoff says so.** A planning handoff for an unready package is still generated and prominently marked BLOCKED with the open decisions.
  An implementation or review handoff is refused unless `--allow-blocked` asks for a document that is itself marked BLOCKED.
- **Approvals follow content.** Changing the objective, scope, decisions or context makes earlier approvals stale.
  Evidence, recommendations and the lifecycle stage are outside that revision.
- **Approvals are provenance, not authentication.** They record who said what, when; nothing stops another process running as you from editing the file.
- **Only listed files are read.** Paths must be relative and stay inside their root, including through symlinks.
  Files such as `.env`, key files and anything under `.git` or `.ssh` are always refused.
- **Context is evidence, not instructions.** Embedded files and all package text are quoted literally, in fenced blocks and code spans, so under CommonMark they cannot add or hide sections; the handoff states that they grant no permission to run commands.
  This is structural separation, not protection against a persuasive text.
- **Recommendations are declarations.** A recommended model, effort or account is not verified availability and not authorization.
- **Reproducible output.** The handoff contains no timestamps or host paths; host details go only into the `.meta.json` sidecar, which is not meant to be committed.
- **Bounded.** Package files, embedded context and rendered output have fixed size limits; exceeding one is an error, never a silent cut.

## Documentation

- [docs/o1-contract.md](docs/o1-contract.md): the O1 contract, schema, readiness rules and acceptance plan.
- [docs/development.md](docs/development.md): environment, checks, tests and conventions.
- [docs/ci.md](docs/ci.md): the CI workflow, runner selection and the steps for making the repository public.
- [docs/github-handoff.md](docs/github-handoff.md): the state of the GitHub repository, its templates and labels, and the independent review handoff.
