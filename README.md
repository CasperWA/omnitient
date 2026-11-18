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

Pre-release, slices O1 and O2.
The behaviour is specified in [docs/o1-contract.md](docs/o1-contract.md) and, for `omnitient overview`, [docs/o2-overview-contract.md](docs/o2-overview-contract.md).
Starting implementation from a Claude Code session in the project is specified in [docs/start-contract.md](docs/start-contract.md).

Supported Python: 3.12 or newer (`requires-python = ">=3.12"`).
Tested: CPython 3.12, 3.13, 3.14 and 3.15 in CI, standard GIL builds from `actions/setup-python` (for example 3.12.15, 3.13.16, 3.14.8 and 3.15.0), on Linux only; 3.12.15 is the development baseline.
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

Record that a revision was accepted as delivered, and withdraw such a record if it was wrong.
This needs a **manifest v2** package: `omnitient new` creates one, and an existing v1 package stays v1 until you upgrade it explicitly (preview first, then apply with the file hash the preview printed).
Acceptance is provenance, not verification: it records who accepted which revision, why, and a snapshot of the evidence entries they chose, and it reads no artifact.
It does not depend on approvals, readiness or context, and changes none of them:

```bash
omnitient upgrade work/my-package --to-version 2
omnitient upgrade work/my-package --to-version 2 --apply --expected-file-sha256 HASH
omnitient closeout work/my-package --by "Your Name" --revision FULL_DIGEST \
    --evidence E1 E2 --reason "Why this revision is accepted" --operation-id C1
omnitient withdraw work/my-package --closeout-id C1 --by "Your Name" \
    --reason "Why it is withdrawn" --operation-id W1
```

Repeating a command with the same operation id and the same inputs replays the saved record and writes nothing.
See [docs/o3-closeout-contract.md](docs/o3-closeout-contract.md).

A worked example with unresolved decisions lives in [examples/](examples/).

See every package in one directory at once.
`overview` reads the immediate subdirectories of the directory you name, each with its own `work-package.json`, and shows each package's declared stage, its readiness for each handoff stage and what needs attention.
It only reads.
Discovery rejects symbolic links at the final component of the collection path, at immediate child entries and at manifests, and never follows them; links in ancestor components of the path you give resolve normally.
Context files that a package lists are read under O1's separate symlink rules.

```bash
omnitient overview work --root repo=$HOME/src/my-repo
omnitient overview work --root repo=$HOME/src/my-repo --json
```

Start implementing one package from a Claude Code session opened in the project.
The project skill `/omnitient-start PACKAGE [--root NAME=DIR]... [--checkout DIR]` runs `omnitient start preview`, which only reads and shows the package, revision, target checkout, declared inputs and proposed write scope, and waits for you before anything is written or edited.
After you agree, `omnitient start handoff` checks that what you were shown is unchanged (the package file, mappings and the target directory itself), then writes the same READY handoff as `render` to a new directory outside the target and verifies it.
It refuses if anything changed, and a blocked package gets a refusal and no handoff.
The target must be the session's working directory.
See [docs/start-contract.md](docs/start-contract.md).

```bash
omnitient start preview --root repo=$HOME/src/my-repo -- work/my-package
omnitient start handoff --root repo=$HOME/src/my-repo --expect-preview TOKEN --out /tmp/handoff -- work/my-package
```

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
| `start` | `preview`: read-only summary of starting implementation (package, revision, target checkout, inputs, write scope). `handoff`: after the preview is confirmed unchanged, write the verified READY handoff outside the target. |
| `overview` | Read-only summary of the packages in one explicit directory: declared stage, readiness per handoff stage, advisory next steps. |
| `closeout` | Record the acceptance of the present revision of a manifest v2 package, with a snapshot of the chosen evidence. |
| `withdraw` | Withdraw an earlier acceptance by its id. |
| `upgrade` | Preview, or with an explicit file hash apply, the upgrade of one package from manifest v1 to v2. |
| `schema` | Print a packaged JSON Schema: the original v1 one by default, or `--schema-version 2`. |

Exit codes: `0` success; `1` refused or invalid input; `2` usage error; `3` valid but not ready for the requested stage.
`closeout`, `withdraw` and `upgrade` never evaluate readiness and never exit `3`.
`overview` reports its own outcomes with `0` and `1` only; a usage error is still `2`, and it never exits `3`.
Blocked valid packages alone do not fail it, but a malformed, unreadable, non-regular or linked manifest or candidate, a duplicate package id, a refusal or a failure to deliver the report to standard output does.
The report is built completely before it is written, but a delivery failure can still leave the reader with partial output: the exit status `1` and the `error[overview.stdout]` line on stderr are the signal.

## Things worth knowing

- **Valid is not ready.** A package can be schema-valid and still blocked: for an implementation handoff it needs every blocking decision resolved, every required context file present with the expected digest, and a current approval.
- **A blocked handoff says so.** A planning handoff for an unready package is still generated and prominently marked BLOCKED with the open decisions.
  An implementation or review handoff is refused unless `--allow-blocked` asks for a document that is itself marked BLOCKED.
- **Approvals follow content.** Changing the objective, scope, decisions or context makes earlier approvals stale.
  Evidence, recommendations and the lifecycle stage are outside that revision.
- **Approvals are provenance, not authentication.** They record who said what, when; nothing stops another process running as you from editing the file.
- **Only listed files are read.** Paths must be relative and stay inside their root, including through symlinks.
  Files such as `.env`, key and credential files and anything under `.git` or `.ssh` are always refused, and so is a root mapped into such a directory.
- **Context is evidence, not instructions.** Embedded files and all package text are quoted literally, in fenced blocks and code spans, so under CommonMark they cannot add or hide sections; the handoff states that they grant no permission to run commands.
  This is structural separation, not protection against a persuasive text.
- **Recommendations are declarations.** A recommended model, effort or account is not verified availability and not authorization.
- **Reproducible output.** The handoff contains no timestamps or host paths; host details go only into the `.meta.json` sidecar, which is not meant to be committed.
- **Bounded.** Package files, embedded context and rendered output have fixed size limits; exceeding one is an error, never a silent cut.

## Documentation

- [docs/o1-contract.md](docs/o1-contract.md): the O1 contract, schema, readiness rules and acceptance plan.
- [docs/o2-overview-contract.md](docs/o2-overview-contract.md): the O2 contract for `omnitient overview`: discovery rules, output schema, completeness and limits.
- [docs/start-contract.md](docs/start-contract.md): `omnitient start` and the `/omnitient-start` project skill.
- [docs/development.md](docs/development.md): environment, checks, tests and conventions.
- [docs/ci.md](docs/ci.md): the CI workflow, runner selection and the steps for making the repository public.
- [docs/github-handoff.md](docs/github-handoff.md): the state of the GitHub repository, its templates and labels, and the independent review handoff.
