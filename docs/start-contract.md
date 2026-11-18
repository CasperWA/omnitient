<!--
SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
SPDX-FileCopyrightText: 2026 SINTEF Ocean AS

SPDX-License-Identifier: MIT
-->

# Starting implementation from the project window

Status: implemented.
`omnitient start` and the project skill `/omnitient-start` connect one work package to one implementation session.
They add no readiness rule: readiness, context handling and the handoff are those of [o1-contract.md](o1-contract.md), and `start` calls the same code as `render`.
Nothing here records an approval, writes a package or runs an agent.

## 1. The command

```bash
omnitient start PACKAGE [--root NAME=DIR]... [--roots-file FILE] [--checkout DIR]
omnitient start PACKAGE ... --handoff --expect-revision DIGEST --expect-file-sha256 HASH --expect-checkout DIR
```

`PACKAGE` is a package file or a directory holding `work-package.json`.
The root mappings are the explicit ones of o1-contract.md section 6.2; a mapping that is not supplied is never supplied by `start`.
The target checkout is `--checkout`, or the current working directory, resolved through symlinks.
It is refused (`start.checkout`, exit 1) when it does not exist, is not a directory or has a component named `.git`, `.ssh`, `.aws`, `.gnupg` or `secrets`.

Both forms evaluate readiness for the `implementation` stage and never write a file.

### 1.1 Preview (default)

Prints one fact per line, with package text written as escapes (the escaping of `overview`), and exits:

| Exit | Meaning |
| --- | --- |
| 0 | Ready. No handoff is produced. |
| 3 | Blocked. The blockers are printed; the write scope is withheld and no handoff is produced. |
| 1 | Refused: invalid package or roots, an unusable checkout. |

The lines are the status, package id, title, declared stage, the full revision (content digest), the current, stale or missing implementation approval, the package file and its SHA-256, the target checkout, whether the checkout has a `.git` entry, its relation to each mapped root (the root itself, inside it, containing it, or none), whether the package file lies inside the checkout, and the proposed write scope.

The proposed write scope is the files under the target checkout that the package `scope` needs, with the package `exclusions` listed as not to be changed.
It names what is outside the scope: the package file with its approvals and evidence, anything outside the checkout, mapped roots outside it (read only), commits, pushes and publication.
It states that Omnitient enforces nothing and that a Git directory alone is no isolation.
The session's permissions and any sandbox decide what can actually be written.

### 1.2 Handoff (`--handoff`)

Needs all three `--expect-*` values, which are the revision, package file hash and checkout the preview printed.
They are accepted only with `--handoff`; anything else is a usage error (exit 2), as is a value that is not 64 lowercase hexadecimal characters.

If the revision, the file hash or the resolved checkout differs from what was previewed, nothing is produced and the command exits 1 with `start.changed`, which says what differed.
The package file hash also covers what the revision does not (stage, approvals, evidence, recommendations).

Otherwise it prints the preview and, when ready, the Markdown handoff of o1-contract.md section 8 between the lines `=== handoff-implementation.md begin ===` and `=== handoff-implementation.md end ===`.
The document is the bytes `render --stage implementation --format md --out -` produces.
A blocked package exits 3 with the preview only; there is no `--allow-blocked` form.

## 2. The skill

`.claude/skills/omnitient-start/SKILL.md` is a project-scoped skill.
The owner types `/omnitient-start PACKAGE [--root NAME=DIR]... [--checkout DIR]`.
It is owner-invoked only (`disable-model-invocation: true`) and pre-approves one tool pattern, `Bash(omnitient start *)`, which cannot write.
It grants no edit permission; the session's own configuration applies.

The skill runs the preview and shows it, then stops until the owner says to start for that displayed scope.
Then it runs the `--handoff` form with the previewed values and implements the handoff, or, on `start.changed`, shows the change and returns to the preview.
It never uses `--allow-blocked`, records an approval, edits the package or commits.
It does not use or change the controller-backed `/autonomous-development:implement-plan` workflow, which remains a separate owner-invoked option.

The arguments reach Omnitient as program arguments, never as shell text.
The skill tells the model to single-quote each one, put options first and write `--` before the package path, and it uses no `$(...)`, backticks or dynamic command injection.
Package text and embedded context are never part of a command.

Claude Code 2.1.327 discovers `.claude/skills/<name>/SKILL.md` and invokes it as `/<name>`; the frontmatter keys used are `name`, `description`, `argument-hint`, `disable-model-invocation` and `allowed-tools`.
Whether the skill loads in a given session was not exercised, because that needs a model.

## 3. Acceptance

| Criterion | Tests (`tests/test_start.py`) |
| --- | --- |
| The preview shows identity, revision, target checkout and write scope, and no handoff | `test_preview_shows_what_the_owner_decides_on_and_no_handoff`, `test_preview_defaults_to_the_working_directory` |
| The handoff is the renderer's READY output | `test_handoff_is_exactly_the_renderers_ready_output` |
| Missing or stale approval, unmapped root, drifted context and an open decision block, and hand over no work | `TestBlocked` |
| A changed package, revision or checkout is surfaced before anything starts | `TestChangeSincePreview` |
| An unusable or wrong checkout is refused or shown | `TestCheckout` |
| Spaces and shell metacharacters in arguments and package text are never executed | `TestArgumentsAreData` |
| Misuse of the expectations is a usage error | `TestUsage` |
| The skill is project scoped, owner invoked, read-only pre-approved and names only real flags | `TestSkill` |
