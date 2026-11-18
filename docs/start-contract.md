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

## 1. The commands

```bash
omnitient start preview [--root NAME=DIR]... [--roots-file FILE] [--checkout DIR] -- PACKAGE
omnitient start handoff [--root NAME=DIR]... [--roots-file FILE] [--checkout DIR] --expect-preview TOKEN --out DIR -- PACKAGE
```

`PACKAGE` is a package file or a directory holding `work-package.json`.
The root mappings are the explicit ones of o1-contract.md section 6.2; a mapping that is not supplied is never supplied by `start`.
The target checkout is `--checkout`, or the current working directory, resolved through symlinks.
It is refused (`start.checkout`, exit 1) when it does not exist, is not a directory or has a component named `.git`, `.ssh`, `.aws`, `.gnupg` or `secrets`.

Both steps evaluate readiness for the `implementation` stage once, with the O1 engine.
Only `start handoff` writes, and only inside `--out`.

### 1.1 Preview

Never writes.
It prints one fact per line and exits:

| Exit | Meaning |
| --- | --- |
| 0 | Ready, and the handoff renders within its bounds. No handoff is written. |
| 3 | Blocked. The blockers are printed; the inputs and write scope are withheld and there is no token. |
| 1 | Refused: invalid package or roots, an unusable checkout, or a handoff that cannot be rendered (for example over the output bound). |

The lines are the status, package id, title, declared stage, the full revision (content digest), the implementation approval (current, stale or none), the package file and its SHA-256, the mapped roots, the target checkout and its identity (device and inode), whether the checkout is the working directory of the command, what is checked about the checkout, whether it has a `.git` entry, its relation to each mapped root, whether the package file lies inside the checkout, and, when ready, the declared inputs, the proposed write scope, the rendered handoff size and the preview token.

**Declared inputs.**
Each context reference is listed by id, logical `root:path`, mode and whether it is required, with its fate in the handoff: `embedded`, `referenced (path and digest only)` or `not embedded (STATUS)`.
Their contents are never in the preview, and a blocked preview lists none.

**Proposed write scope.**
The files under the target checkout that the package `scope` needs, with the package `exclusions` listed as not to be changed.
It names what is outside the scope: the package file with its approvals and evidence, anything outside the checkout, mapped roots outside it (read only), commits, pushes and publication.
It states that Omnitient enforces nothing and that a Git directory alone is no isolation.
The session's permissions and any sandbox decide what can actually be written.

**What is checked about the checkout.**
Its resolved path, its directory identity, and whether a `.git` entry exists.
The branch, HEAD and local modifications are not observed, and there is no dirty-tree gate.

**The checkout and the roots.**
The target and the context roots may legitimately differ; a mapped root that is not the target is information, never a refusal.
The relation (the root itself, inside it, containing it, or none) is printed for the owner's judgement only.

**The working directory.**
The preview says whether the target is the working directory of the command, which for a session is the working directory it runs in.
A target elsewhere can be previewed, but implementation does not start from a session that is not working there: the owner opens a session in the target and previews again.

**Display and comparison are separate.**
Every host path, package text and diagnostic in the output is written as escapes, so a name cannot forge a line.
Escaped text is for reading only and is never compared.

### 1.2 Preview token

A ready preview ends with `preview token: omnitient-start-1.F1.F2.F3.F4.F5.F6`, six SHA-256 values taken from the exact facts the owner was shown:

| Fact | Fingerprint of |
| --- | --- |
| revision | the content digest |
| package file contents | the package file's SHA-256 |
| package file location | the resolved package path, as the filesystem encodes it |
| root mappings | the sorted name and resolved directory of every mapped root |
| target checkout location | the resolved target path |
| target checkout identity | the target's device and inode |

The token is local to this comparison.
It is not in any manifest, handoff or approval digest, and it contains no path.
It is not a credential: it only proves the same facts were observed.

### 1.3 Handoff

`start handoff` takes the token the owner confirmed.
A token that is not a preview token is a usage error (exit 2).

It first requires that the target is the working directory (`start.checkout_not_cwd`, exit 1).
It then recomputes the six facts from the package file, the mappings and the target, without reading any context file, and compares them with the token.
A difference means a material change: nothing is written and the command exits 1 with `start.changed`, naming the facts that differ and nothing else.
Because the confirmed fingerprints are compared, not a path resolved again now, a directory replaced at the same path, or a name that now links to another checkout, is a change.

Only then it reads the context, assesses readiness once and renders.
A blocked result exits 3 with the blocked preview and writes nothing; there is no `--allow-blocked` form.

`--out` must be outside the target checkout (`start.out_inside_checkout`), so the handoff is never part of the write scope.
The bundle is published and verified exactly as `render --out` does (o1-contract.md section 8.4): the Markdown handoff `handoff-implementation.md` and its manifest, never replacing an existing bundle.
The Markdown is the bytes `render --stage implementation --format md` produces.
The manifest records host paths, as `render`'s does, and is not to be committed.

On success it prints the handoff file, its byte count, its newline count, its SHA-256 and the manifest.
It prints `handoff delivered` only after the bundle verified, and a render or write failure exits 1 before the owner is told anything is ready to read.

Delivery is complete only when the consumer has read the whole file.
The newline count is what a consumer checks: the last line number reached while reading the file equals it.
Omnitient verifies the bytes it wrote; it cannot verify what a reader received.
If the reader cannot read all of it, or the numbers differ, implementation must not start.

## 2. The skill

`.claude/skills/omnitient-start/SKILL.md` is a project-scoped skill.
The owner types `/omnitient-start PACKAGE [--root NAME=DIR]... [--checkout DIR]`.
It is owner-invoked only (`disable-model-invocation: true`).
It pre-approves one tool pattern, `Bash(omnitient start preview *)`, which cannot write.
It does not pre-approve `start handoff`, which writes, or any edit; the session's own permission configuration applies to them, and the skill does not promise that a prompt appears.

The skill runs the preview and shows it, including what is and is not checked about the target.
It stops on a blocked or refused preview, and when the target is not the session's working directory.
Otherwise it proposes a handoff directory outside the target, asks the owner whether to start for exactly the displayed scope and stops if the owner declines, says anything else, or the runtime denies a step, without retrying elsewhere.
Only after the owner's go-ahead it runs `start handoff` with the token, reads the whole file and checks the newline count, and implements.
On `start.changed` it shows the change and previews again.
It never uses `--allow-blocked`, records an approval, edits the package or commits.
It does not use or change the controller-backed `/autonomous-development:implement-plan` workflow, which remains a separate owner-invoked option.

The arguments reach Omnitient as program arguments, never as shell text.
The skill tells the model to single-quote each one, put options first, write `--` before the package path and run from the session's working directory, and it uses no `$(...)`, backticks or dynamic command injection.
Package text and embedded context are never part of a command.

Claude Code 2.1.327 discovers `.claude/skills/<name>/SKILL.md` and invokes it as `/<name>`; the frontmatter keys used are `name`, `description`, `argument-hint`, `disable-model-invocation` and `allowed-tools`.

Tracking: the repository's `.gitignore` un-ignores `.claude/` and then ignores everything in it except `.claude/skills/omnitient-start/`, so settings, worktrees, agent memory and other skills stay out of the repository even where a global rule hides `.claude/`.

## 3. Not covered, and not verified

- Delivery status (O3) is not in the preview; delivery records stay independent of readiness.
- The branch, HEAD and local modifications of the checkout are not observed.
- A second checkout is not isolated by anything here.
- Not exercised in a live session, because that needs a model: that `/omnitient-start` loads and substitutes its arguments, that the owner is asked and a denial stops the model, how `Bash(omnitient start preview *)` matches commands with pipes or redirections, whether a permission prompt appears for `start handoff` or an edit, and that a session's Bash tool starts in its working directory.
- The static tests establish only what the skill's text says and that the commands behave as above.

## 4. Acceptance

| Criterion | Tests (`tests/test_start.py`) |
| --- | --- |
| The preview shows identity, revision, target checkout, inputs and write scope, and writes nothing | `TestPreview` |
| The handoff is the renderer's READY output, written outside the target and verified, with a count the reader checks | `TestHandoff` |
| Missing or stale approval, unmapped root, drifted context and an open decision block, and hand over no work | `TestBlocked` |
| A change to the package, revision, location, mappings or the checkout's location or identity is refused before context is read | `TestChangeSincePreview` |
| An unusable checkout is refused; a root that differs from the target is information | `TestCheckout`, `TestPreview` |
| Host paths cannot forge lines of the output | `TestDisplayIsEscaped`, `TestCheckout` |
| Spaces and shell metacharacters in arguments and package text are never executed | `TestArgumentsAreData` |
| Misuse of the token or options is a usage error | `TestUsage` |
| The skill is project scoped, owner invoked, pre-approves only the preview, names only real flags and states its boundaries | `TestSkill` |
| Only the shipped skill is tracked under `.claude/` | `TestIgnoreBoundary` |
