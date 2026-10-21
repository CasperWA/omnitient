<!--
SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
SPDX-FileCopyrightText: 2026 SINTEF Ocean AS

SPDX-License-Identifier: MIT
-->

# Omnitient O1 contract and acceptance plan

Status: implemented by the O1 slice. This document is the specification the code and tests follow.
Omnitient is a local, deterministic CLI and library for preparing portable work packages and self-contained agent handoffs.
It makes no model calls, runs no agents and contacts no network service.

## 1. Purpose and boundaries

Omnitient preserves approved scope, decisions and the context needed to act on them in small inspectable files, and renders a handoff from them.

Omnitient owns its work-package metadata only.
It does not own, read or write the state of any autonomous-development engine, and it does not duplicate that engine's gates.
Evidence that mentions an engine run or attempt is an opaque identity, never a status.

Out of scope for O1: agent runners, model execution, review-track launching, account switching, Git synchronisation or remote retrieval, parsing provider transcripts, importing historical accounting, servers, databases, UIs and any generic workflow engine.

## 2. Terms

- **Package**: one JSON file, `work-package.json`, describing one unit of work.
- **Package id**: `project/initiative/work_package`, three slugs, independent of any host path.
- **Logical root**: a name declared by the package (for example `omnitient`). Context files are addressed as logical root plus relative path.
- **Host mapping**: a supplied map from logical root name to an absolute directory on this machine. It is never stored in the package.
- **Content digest** (revision): SHA-256 over the canonical form of the approval-covered content (section 4.2).
- **Handoff stage**: `planning`, `implementation` or `review`, in that order.
- **Schema-valid**: the file parses and satisfies section 3. Distinct from **ready**, which is evaluated per handoff stage (section 5).

## 3. Package schema (version 1)

The file is UTF-8 JSON with one top-level object.
Unknown fields, duplicate keys, `NaN`/`Infinity`, wrong types and out-of-range values are errors.
The machine-readable description ships as package data and is printed by `omnitient schema`.
The validator in `omnitient.schema` is authoritative where the two could differ.

| Field | Type | Rule |
| --- | --- | --- |
| `schema` | string | Exactly `omnitient/work-package`. |
| `schema_version` | integer | Exactly `1`. Other versions are refused. |
| `project`, `initiative`, `work_package` | slug | `^[a-z0-9][a-z0-9-]{0,62}$`. |
| `title` | string | One line, 1–200 characters. |
| `stage` | enum | `draft`, `planning`, `implementation`, `review`, `done`. Declared lifecycle position. |
| `objective` | text | Multi-line allowed, 1–8000 characters. |
| `scope`, `exclusions`, `acceptance` | list of line | Up to 100 items, each one line of 1–1000 characters. |
| `dependencies` | list of object | `{package: <package id>, note?: line}`. References only; never resolved or fetched. No self-reference, no duplicates. |
| `roots` | list of object | `{name: slug, description: line}`. Declares the logical roots context may use. |
| `context` | list of object | Up to 64 context references (section 3.1). |
| `decisions` | list of object | Up to 200 decision records (section 3.2). |
| `recommendations` | list of object | Up to 20 declared execution recommendations (section 3.3). |
| `evidence` | list of object | Up to 500 evidence references (section 3.4). Append-only through the CLI. |
| `approvals` | list of object | Up to 100 approval records (section 3.5). Append-only through the CLI. |

Identifiers inside lists (`id` fields) match `^[A-Za-z][A-Za-z0-9_-]{0,31}$` and are unique within their list.
Text and line fields reject control characters other than newline and tab (tab and newline only in multi-line text), bidirectional override characters and lone surrogates.

### 3.1 Context reference

| Field | Type | Rule |
| --- | --- | --- |
| `id` | id | Unique among context references. |
| `root` | slug | Must be declared in `roots`. |
| `path` | string | Relative POSIX path, rules in section 6.1. |
| `sha256` | string | 64 lowercase hex characters: the expected digest of the file's bytes. |
| `required` | boolean | Missing or drifted required context blocks readiness. |
| `mode` | enum | `embed` (contents included in the handoff) or `reference` (path and digest only). |
| `purpose` | line | Why the file is included. May be empty. |
| `git` | object, optional | Supplied provenance: `{revision: 7–64 hex, repository?: line, ref?: label}`. Recorded and displayed, never verified or fetched. |

### 3.2 Decision record

| Field | Type | Rule |
| --- | --- | --- |
| `id` | id | Unique among decisions. |
| `question` | line | 1–1000 characters. |
| `rationale` | text | Why the question matters or why the answer was chosen. May be empty. |
| `status` | enum | `unresolved` or `resolved`. |
| `answer` | text | Required and non-empty when `resolved`; must be absent when `unresolved`. |
| `source` | line | Actor or source of the current state: who raised the question, or who or what supplied the answer. Required. |
| `needed_by` | enum | The handoff stage (`planning`, `implementation`, `review`) at which resolution becomes necessary. |
| `applies_to_revision` | label, optional | The target-repository revision (commit id, tag or branch label) the decision was made against. Informational; not verified. |

A decision is **blocking for stage S** when it is `unresolved` and `needed_by` is not later than S.

### 3.3 Declared execution recommendation

Recommendations are advisory.
They are not verified availability and not authorization to execute.
Every handoff states this next to them.

Fields: `id`; `stage` (the handoff stage it applies to); `model` (line, optional); `effort` (line, optional); `account` (line, optional, a constraint such as "regular account only", never a credential); `limits` (optional object with `max_turns` integer, `max_wall_minutes` integer, `max_cost_usd` number, all positive); `note` (line, optional).

### 3.4 Evidence reference

Fields: `id`; `kind` (`report`, `pr` or `commit`); `ref`; `summary` (line, optional); `recorded_by` (line); `recorded_at` (UTC timestamp `YYYY-MM-DDTHH:MM:SSZ`); `engine` (optional object `{run_id, attempt_id?}`).

- `commit`: 7–64 lowercase hex characters.
- `pr`: a well-formed `https://` URL without embedded credentials. Never fetched.
- `report`: a well-formed `https://` URL without embedded credentials, or `root:relative/path` using a declared root and the path rules of section 6.1. Never fetched or read.
A URL that cannot be parsed (for example an unclosed `[` in the host or a port that is not a number in range) is a schema error, as is a `git.repository` that is a malformed or credential-bearing URL.
- `engine.run_id` and `engine.attempt_id`: `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`. Opaque identities. They never mark a run complete and have no effect on readiness.

### 3.5 Approval record

Fields: `id`; `stage` (handoff stage); `approved_by` (line); `approved_at` (UTC timestamp); `content_digest` (64 hex, the revision approved); `note` (line, optional).

## 4. Revisions and approval semantics

### 4.1 What an approval means

An approval record is **workflow provenance**: a named person said, at a time, that the content with a given digest is approved for a given handoff stage.
It is not authentication and not protection against another process running as the same user, which can edit the file or the record.
Omnitient does not sign approvals or authenticate the approver.
`--by` is required and recorded as given; it is never defaulted from the environment.

### 4.2 Approval-covered content

The content digest covers: `schema_version`, `project`, `initiative`, `work_package`, `title`, `objective`, `scope`, `exclusions`, `acceptance`, `dependencies`, `roots`, `context` (including expected digests and git provenance) and `decisions` (including answers and status).
It is computed over canonical JSON (UTF-8, sorted keys, no insignificant whitespace, list order preserved).

It does **not** cover `stage`, `recommendations`, `evidence` and `approvals`.
So evidence appended later, an advancing `stage` and revised advisory recommendations never invalidate an approval and never rewrite approved scope.
Any change to covered content changes the digest, which makes earlier approvals **stale** for the new revision.
Stale approvals stay in the file as history; they simply no longer count.

An approval for stage S is **current** when its `stage` is S and its `content_digest` equals the package's present digest.

### 4.3 Approving

`omnitient approve` appends one approval for the present digest.
It refuses unless the package is schema-valid and has no decision that is blocking for the approved stage.
When a host mapping is supplied it also refuses on digest mismatch or missing required context; without a mapping the context is not verified at approval time, and the readiness check still verifies it at handoff time.
It raises `stage` to the approved stage if `stage` is lower (so `draft` becomes `implementation`), and never lowers it.
Scaffolding never approves.

## 5. Readiness

`validate` and `show` report schema validity and, separately, readiness for each handoff stage.
`render` evaluates readiness for the requested stage.

A package is **ready for stage S** when it is schema-valid and has no blocker below.

| Code | Severity | Condition |
| --- | --- | --- |
| `decision.unresolved` | blocker | A decision is blocking for S. Other unresolved decisions are listed as warnings. |
| `context.missing` | blocker if required, else warning | The file does not exist or is not a regular file (a FIFO or device is not read). |
| `context.unreadable` | blocker if required, else warning | The path cannot be resolved (for example a symlink loop or a directory without search permission), or the file cannot be opened or read (for example permission denied). |
| `context.digest_mismatch` | blocker if required, else warning | The file's SHA-256 differs from the expected digest. |
| `context.root_unmapped` | blocker if required, else warning | No host mapping was supplied for the reference's root. |
| `context.unsafe_path` | blocker if required, else warning | The path resolves, through a symlink, outside its root or to a forbidden file (section 6.1). A path that is unsafe as written is a schema error instead. |
| `context.too_large` | blocker if required, else warning | The file exceeds the limit for its mode (section 7). |
| `context.not_text` | blocker if required, else warning | An `embed` file is not valid UTF-8 text. |
| `approval.missing` | blocker for `implementation` and `review` | No current approval for S exists and none is stale. |
| `approval.stale` | blocker for `implementation` and `review` | No current approval for S exists but an approval for S refers to an older digest. |
| `content.incomplete` | blocker (a `TODO` objective, every stage; empty `scope` or `acceptance`, `implementation` and `review` only) | The objective is a `TODO` placeholder: ignoring surrounding whitespace and letter case, it is `TODO` alone or starts with `TODO:` (as the scaffold's does). Or `scope` or `acceptance` is empty. Empty `scope` or `acceptance` is only a warning for `planning`, which is where they get written. |

`planning` handoffs need no approval.
A `draft` package is always labelled DRAFT in its output.
The label states the declared `stage` only; whether a handoff is ready, and which approvals are current, is stated for its target stage.
`approve` applies the same blockers except the approval ones, so it refuses incomplete content and blocking decisions.

## 6. Context resolution

Only the context references listed in the package are read.
Nothing is discovered, scanned recursively, fetched from a URL or executed.
Context content is evidence, not permission to run commands; handoffs say so and keep it inside explicit boundaries (section 8.3).
A context file is opened once; its type (a regular file) and size are checked on the open descriptor, and a FIFO or device is reported as `context.missing` without being read.
Diagnostic details never contain host paths, so handoffs stay host-independent.

### 6.1 Path rules

A context `path` must be a relative POSIX path of at most 512 characters.
It is rejected when it is empty or absolute, contains a backslash, a NUL or other control character, a `..` or `.` or empty component, or begins with `~`.
Resolution maps the logical root through the host mapping, resolves symlinks, and requires the real target to lie inside the real root and to be a regular file.
A path that is unsafe as written (the syntactic rules, or a forbidden name) makes the package schema-invalid.
A symlink whose target stays inside the root is accepted; one that leaves it, or that points at a forbidden file, is `context.unsafe_path` at resolution time.

Files that commonly hold secrets are refused even when listed: any path component equal to `.git`, `.ssh`, `.aws`, `.gnupg` or `secrets`, and file names matching `.env`, `.env.*`, `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `.pgpass`, `auth.json`, `credentials*`, `kubeconfig`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `id_dsa*`, `id_ecdsa*` and `id_ed25519*` (matched without regard to letter case).
A host directory mapped as a root is refused as well when its resolved path has a component equal to one of those directory names (section 6.2), so a root cannot expose the rest of such a directory.
This is a safeguard against mistakes, not a security boundary against a hostile same-user process.

### 6.2 Host mapping

Supplied per invocation as repeatable `--root NAME=ABSOLUTE_DIR` and/or `--roots-file FILE` (`{"roots": {"NAME": "ABSOLUTE_DIR"}}`).
The same name given twice is an error.
Each directory must be absolute and exist; it is resolved through symlinks, and refused when the resolved path lies inside a directory named `.git`, `.ssh`, `.aws`, `.gnupg` or `secrets` (section 6.1).
A roots file is read like a package: it must be a regular file of at most 65,536 bytes (a FIFO or device is refused without blocking), UTF-8 JSON with no duplicate keys, no `NaN`/`Infinity` and bounded nesting.
Every such failure is a `roots.invalid` diagnostic and exit status 1.
A name the package does not declare produces a warning.
Host paths never appear in the semantic handoff; they are recorded only in the metadata sidecar (section 8.4).

## 7. Bounds

All bounds are fixed constants in `omnitient.limits`.
Exceeding one refuses with a diagnostic naming the limit; nothing required is silently truncated.

| Limit | Value |
| --- | --- |
| Package file size | 262,144 bytes |
| Roots file size | 65,536 bytes |
| Items per list | see section 3; `roots` at most 16 |
| Embedded context, per file | 262,144 bytes |
| Embedded context, total per handoff | 786,432 bytes |
| Reference-mode file (hash only) | 67,108,864 bytes |
| Rendered output, per file | 1,048,576 bytes |
| Completion manifest, when read back | 1,048,576 bytes |
| Files listed by a completion manifest | 8 |

The rendered-output bound applies to each document that is produced: with `--format md` or `--format json`, the other form is not rendered, and its size cannot refuse the command.
An optional (`required: false`) reference that exceeds an embed limit is listed as not embedded, with the reason.
A required reference that exceeds a limit blocks readiness, and `render` produces no output rather than a short one.

## 8. Handoff generation

### 8.1 Command behaviour

`omnitient render PACKAGE --stage S ...` generates a Markdown handoff and a structured JSON handoff with the same semantic content, without any model call.

- If the package is ready for S, the handoff is marked READY.
- If it is not ready and S is `planning`, the handoff is still generated and marked BLOCKED, listing every blocker (the missing decisions first), and the command exits 3.
- If it is not ready and S is `implementation` or `review`, nothing is written and the command exits 3 with the blockers, unless `--allow-blocked` is given, in which case a BLOCKED document is written (still exit 3). Omnitient never emits READY for a package that is not ready.
- Context whose digest does not match, or that is missing, is never embedded; the handoff names it with its status.

### 8.2 Determinism

The Markdown and JSON handoffs contain no timestamps generated at render time, no host paths, no Omnitient version and no environment data.
Identical package content and identical context file contents produce byte-identical output, regardless of the host mapping used.
Timestamps stored inside the package (approvals, evidence) are inputs and appear as such.

### 8.3 Markdown structure and untrusted content

The document has a fixed sequence of numbered `##` control sections: status, how to read this handoff, objective, scope and acceptance, decisions, dependencies, execution recommendations, evidence, approvals, context, provenance.

The supported interpretation is **CommonMark** (0.31), with raw HTML enabled, which is the strictest reading for this purpose.
Author-controlled text never enters the document as Markdown:

- A single-line value (title, scope, exclusions and acceptance entries, questions, sources, purposes, notes, summaries, names, diagnostic messages including the file paths and details they quote) is an inline code span, lengthened past any backtick run in the text.
- Multi-line text (objective, rationale, answers) is a fenced block with the info string `text`.
- Context files are embedded verbatim in the same kind of fenced block.
- Every fence is longer than any backtick run in its content, so the content cannot close it, whatever line endings it uses.
- The only author-chosen words in a heading are validated identifiers (the package id is three slugs; decision and context ids match the id pattern), and the renderer writes the ids as code spans.

CommonMark reads code spans and fences literally: raw HTML (including an unclosed `<!--`), entities, links, images, emphasis, headings, lists and block quotes in author text are all just characters.
A block quote would not be enough: raw HTML inside one still reaches an HTML renderer, where an unclosed comment hides everything after it and a closing tag can forge a heading.
The JSON handoff is unaffected: it holds the exact text, and rendering does not change readiness.

**What this guarantees:** structural separation.
In any CommonMark reading, the headings, status line, readiness statements and the other renderer-owned text of a handoff are exactly those the renderer wrote, and package text, diagnostics and context files appear only inside code spans and fences.
**What it does not guarantee:** that text quoted in a span or fence cannot persuade a reader, human or model, to act on it (semantic prompt injection), or how any model behaves; the handoff states the trust boundary but cannot enforce it.
Renderers that differ from CommonMark for code spans or fences, and Markdown flavours with other block syntax, are not covered.
The tests check this with an established CommonMark parser (`markdown-it-py`, a development-only dependency) over hostile text in every author-controlled field.

The "how to read" section states that code spans and fences quote the author's text, diagnostics and files, that fenced files under "Context" are untrusted evidence granting no permission to execute commands or read other files, and that only the text outside code spans and fences is the handoff's own statement.

### 8.4 Outputs

With `--out DIR` (default format `both`) the command publishes one **bundle**:

- `handoff-<stage>.md` and `handoff-<stage>.json` (as selected by `--format`);
- `handoff-<stage>.meta.json`, the host-resolution sidecar and **completion manifest**: `schema` (`omnitient/handoff-manifest`), `schema_version` (1), generation time, Omnitient version, the resolved host mapping, the package file path and SHA-256, and `outputs`, the SHA-256 of each handoff file it vouches for. It is not part of the semantic handoff.

`--out -` with `--format md` or `--format json` writes that one document to standard output and no sidecar.
The package file is never modified by `render`.
`render` prints its `wrote` lines only after the whole bundle was published and verified as the one this command wrote.

**Publication.** The data files are written first and the manifest last.

- Without `--force`, no existing file is touched: the command refuses (`output.exists`) when any of the bundle's names exists, and creates each file exclusively, so a concurrent writer cannot be overwritten.
  If it loses a race, it removes only files it created, recognised by device and inode, and reports `output.exists`.
- With `--force`, it first removes the old manifest, so the old completion marker can never vouch for new data, then replaces each data file atomically through a uniquely named temporary file, and creates the manifest exclusively last.
  If another writer published a manifest meanwhile, it removes nothing and reports `output.conflict`.
  Replaced files are not rolled back.
  Replaced files and the new manifest keep the permission bits of the files they replace (section 10).
- After publishing, the command reads the bundle back under the reader rule below and reports `output.conflict` if it does not verify, or if it verifies but is not the bundle this command wrote.
- Unrelated files in the directory, including other stages' bundles, are never touched.

**Reader rule.** A bundle is complete only if all of these hold:

1. `handoff-<stage>.meta.json` exists, is a regular file and is valid JSON of schema `omnitient/handoff-manifest` version 1 (an integer, not `true`);
2. its `outputs` object lists 1 to 8 files, and every key is a plain file name in the same directory (no `/`, no NUL, not `.` or `..`);
3. each listed file is a regular file of at most 1,048,576 bytes whose SHA-256 equals the listed value.

Files not listed are not part of the bundle.
A copy of a handoff without its sidecar cannot be verified by this rule; the sidecar holds host paths and is not meant to be committed, so committed handoffs (such as the example's) are plain documents.

`omnitient.store.read_bundle` implements the rule and returns what it verified: the parsed manifest and the bytes of each listed file.
It reads the manifest once and each listed file once, and hashes exactly the bytes it returns.
So everything it returns matches one manifest, written by one writer, even if another writer replaces files while or after it reads: a file replaced before it is read no longer matches the manifest, and the read fails instead of mixing two writes.
Use the returned bytes.
Verifying a bundle and then opening its files again by name is not covered, because a `--force` run can replace them in between.
`omnitient.store.check_bundle` reports the same problems without returning data; an empty result says only that the files verified while they were read.
A successful read says nothing about what is on disk afterwards.

**Interrupted or mixed bundles.** An interrupted write leaves data files and no manifest, or a manifest that fails to parse or verify.
Treat any bundle that fails the reader rule as incomplete: do not use it.
Recover by running the same `render` command again with `--force` (this works whether the manifest is missing, stale or mismatched); alternatively delete the bundle's files by hand.
A no-clobber run that finds data files without a manifest says that an earlier write was probably interrupted.
Concurrent `--force` runs for the same bundle are not serialised.
Either or both of them can fail with `output.conflict`, and the files they leave can be a mixture of both writers' data with no manifest that verifies it.
A run that succeeded can have its bundle replaced or broken by the other run afterwards: success means only that the bundle it wrote verified when it read it back.
In every case, a reader that uses the bytes `read_bundle` returns gets one writer's complete set of files or a refusal, never a mixture.
Re-run the command to recover.
There is no lock file, journal or rollback.

### 8.5 JSON handoff

`schema` is `omnitient/handoff`, `schema_version` 1.
Top-level keys: `target_stage`, `status` (`ready` or `blocked`), `draft`, `readiness` (`ready`, `blockers`, `warnings`), `package` (identity, `stage`, `content_digest`, title, objective, scope, exclusions, acceptance, dependencies, roots, decisions), `recommendations` (`declared`, plus `availability: "unverified"` and `execution_authorization: "none"`), `evidence`, `approvals` (each with `current`), `context` (per reference: metadata, `status`, `bytes`, `content` or `null`) and `handling` (the untrusted-context statement).

## 9. CLI

Console script: `omnitient` (also `python -m omnitient`).

| Command | Behaviour | Mutates |
| --- | --- | --- |
| `new PATH --project P --initiative I --work-package W --title T` | Scaffold a draft package at `PATH` (a `.json` file path). Refuses if it exists. No approvals, no decisions. | creates `PATH` |
| `validate PACKAGE [--stage S] [--root ...]` | Schema diagnostics, then readiness for every stage; with `--stage`, the exit code follows that stage. | no |
| `show PACKAGE [--root ...] [--json]` | Compact inspection: identity, digest, counts, decisions, context status, approvals (current or stale), evidence, readiness. | no |
| `render PACKAGE --stage S [--out DIR\|-] [--format md\|json\|both] [--allow-blocked] [--force] [--root ...]` | Section 8. | writes outputs only |
| `approve PACKAGE --stage S --by NAME [--note T] [--root ...]` | Section 4.3. | appends an approval |
| `evidence add PACKAGE --kind K --ref R --by NAME [--summary T] [--run-id ID] [--attempt-id ID]` | Append an evidence reference. | appends evidence |
| `context add PACKAGE ROOT:PATH --root ROOT=DIR [--id ID] [--optional] [--mode M] [--purpose T] [--git-revision REV]` | Compute the digest of an existing file under the rules of its mode (a binary or oversized file cannot be added as `embed`) and add the reference, declaring the root if needed. | adds context |
| `context pin PACKAGE ID... --root ROOT=DIR` | Replace the expected digest of the named references with the current file digests. Explicit acceptance of drift; invalidates approvals; supplied `git` provenance is left for the author to update. | edits context |
| `schema` | Print the packaged JSON Schema. | no |

Exit codes: `0` success (and READY where readiness is evaluated); `1` refused or invalid input (schema errors, unsafe paths, limits, existing output, store and output conflicts); `2` command-line usage error; `3` valid input but not ready.

Read commands (`validate`, `show`, `schema`, and `render` with `--out -`) never write any file.

## 10. Persistence

Packages are plain files; there is no database.

- Creation uses exclusive create and fails if the path exists.
- Every file the tool reads that someone else controls (package, roots file, context file, and the package again during an update) is read through one routine: opened without blocking, required to be a regular file by checking the open descriptor, and read up to its bound.
  A FIFO, device or directory is refused, never read.
- Every in-place update (`approve`, `evidence add`, `context add`, `context pin`) follows one routine: read the file and remember its SHA-256; compute the new package and verify it is schema-valid; take an exclusive lock file (`PACKAGE.lock`) and remember its device and inode; re-read the file with the same type and size bounds and abort with `store.conflict` if it is no longer a regular file, is larger than the package limit or its SHA-256 changed; write a uniquely named temporary file in the same directory and atomically replace the package; remove the lock file if it is still the one this update created.
- A lock file left by a crash must be removed by hand; the diagnostic says so, and says to check first whether the crashed update already changed the package, since repeating an `approve` or `evidence add` appends a second record.
- A replaced file (a package during an update, or a handoff file or manifest under `render --force`) keeps the read, write and execute bits of the file it replaces; the temporary file gets them before any data is written.
  Set-user-ID, set-group-ID and sticky bits are never copied, and the owner, group, ACLs and extended attributes are not carried over: the new file belongs to the user running the command.
  A new file (a scaffold, a lock file, or an output that did not exist) is created with mode 0666 minus the umask.
- Temporary files are created exclusively under names that include the process id and random bytes, and a failed creation never removes anything.
  Cleanup, including the release of a lock, removes only a file the operation created, recognised by device and inode.
  A crash can leave a `.NAME.tmp-*` file, which is safe to delete.
- Evidence and approval updates are append-only: the command verifies that the covered-content digest and all earlier evidence and approval records are unchanged before writing.
- The tool writes canonical formatting (two-space indent, fixed key order, trailing newline). Hand-edited files remain valid and are normalised the next time the tool writes them.
- Handoff bundles are published as described in section 8.4.

**Trust and concurrency assumptions.** The files belong to one user on a local POSIX filesystem.
The checks resolve symlinks and test file types, but a process of the same user that swaps files between a check and an open, or deletes the files the tool created, is not defended against; neither are lock files on filesystems with unreliable exclusive create.
Approvals are provenance, not authentication (section 4.1).
Windows was neither considered nor tested.

## 11. Host-independence

The package contains logical root names only, never checkout or virtualenv paths.
The same package and the same context contents rendered with two different host mappings produce identical handoffs.

## 12. Interpretations chosen

These resolve phrases in the brief that admit more than one reading; each is reversible.

1. **"Applicable revision" of a decision** is the target-repository revision the answer was made against (`applies_to_revision`), not a package revision counter. The package revision itself is the content digest.
2. **Recommendations are outside the approved revision**, because they are advisory and changing them does not change approved scope.
3. **`stage` is outside the approved revision** and is advanced by `approve`; readiness never relies on it.
4. **Decisions are edited by hand** in O1; there are no decision subcommands. Any edit changes the digest.
5. **Review handoffs** require a current approval for the `review` stage, as implementation handoffs require one for `implementation`.
6. **The completion manifest is the existing sidecar.** `handoff-<stage>.meta.json` already listed the output digests, so it is written last and verified by readers; no new file, command or lock was added. A dedicated `verify` command, if wanted, is future work.
7. **Author text is quoted as code, not escaped.** Code spans and fences are literal in CommonMark without an escaping table to maintain; the cost is that prose such as scope entries reads as code in a rendered view. Escaping individual characters would keep the prose but depends on knowing every construct a renderer treats specially.

## 13. Acceptance plan

Every item is an offline pytest test; none reads production state, credentials or the network.

| Acceptance criterion | Tests |
| --- | --- |
| A valid package generates a useful self-contained handoff without a model | `test_render.py::test_ready_handoff_is_useful_and_self_contained`, `test_cli.py::test_full_workflow_from_scaffold_to_ready_handoff` |
| Blocking decisions prevent ready-stage output | `test_readiness.py::test_unresolved_decision_blocks_stage_it_is_needed_by`, `test_render.py::test_blocking_decision_is_prominent_and_not_ready`, `test_cli.py::test_blocked_implementation_writes_nothing`, `test_cli.py::test_blocked_planning_handoff_is_generated_but_exits_3` |
| Digest drift and missing context are diagnosed | `test_context.py::test_digest_drift_is_diagnosed_and_content_withheld`, `test_context.py::test_missing_file`, `test_readiness.py::test_required_context_problems_block_optional_ones_warn`, `test_render.py::test_digest_drift_withholds_content_and_says_so` |
| Changing approved content invalidates its approval reference | `test_approval.py::test_changing_covered_content_makes_the_approval_stale`, `test_approval.py::test_changing_uncovered_content_keeps_the_approval_current` |
| Equivalent content under different host mappings is portable | `test_render.py::test_host_mapping_does_not_change_handoff`, `test_render.py::test_output_is_reproducible` |
| Evidence can be added without rewriting history | `test_evidence.py`, `test_render.py::test_evidence_changes_only_the_evidence_section`, `test_cli.py::test_full_workflow_from_scaffold_to_ready_handoff` |
| Malformed input, unsafe paths, overwrites and output limits behave predictably | `test_schema.py` (including malformed URLs), `test_context.py` (symlinks, forbidden files, size limits, host mapping, roots-file duplicate keys, nesting and FIFOs), `test_store.py`, `test_render.py::test_output_limit_refuses_instead_of_truncating`, `test_render.py::test_only_the_requested_format_is_bounded`, `test_cli.py::test_markdown_only_render_is_not_refused_for_the_json_size`, `test_cli.py::test_render_never_overwrites_without_force` |
| Reads of files that others control are bounded and never block; cleanup touches only files the operation created | `test_store.py::test_reread_during_update_is_bounded_like_the_first_read`, `test_store.py::test_reread_during_update_does_not_block_on_a_fifo`, `test_store.py::test_preexisting_file_at_the_old_temporary_name_survives_an_update`, `test_store.py::test_exhausted_temporary_names_fail_without_deleting_the_colliding_file`, `test_context.py::TestHostMapping` |
| A handoff bundle is complete only when its manifest verifies; replacement and races never leave a blessed mixture, and a reader uses the bytes it verified | `test_store.py` (publication order, losing races, interrupted replacement, reader rule, `test_a_replacement_while_reading_fails_instead_of_mixing`, `test_read_bundle_returns_the_bytes_it_verified`), `test_cli.py::test_render_publishes_a_bundle_whose_manifest_verifies`, `test_cli.py::test_forced_render_replaces_a_bundle_and_keeps_unrelated_files`, `test_cli.py::test_render_reports_an_interrupted_bundle_and_force_completes_it`, `test_cli.py::test_concurrent_forced_renders_can_both_fail_and_never_verify_a_mixture`, `test_cli.py::test_a_forced_render_whose_bundle_was_replaced_does_not_claim_it` |
| Replacing a file keeps its permission bits, never special bits; a lock is released only by the update that took it | `test_store.py::test_update_keeps_the_package_file_mode`, `test_store.py::test_replacement_never_copies_special_mode_bits`, `test_store.py::test_forced_bundle_keeps_the_modes_of_the_files_it_replaces`, `test_store.py::test_new_files_follow_the_umask`, `test_store.py::test_a_failed_mode_change_leaves_the_package_and_no_stray_files`, `test_store.py::test_lock_release_removes_only_the_lock_this_update_took` |
| Context Markdown and package text cannot impersonate or hide control sections under CommonMark (parsed with `markdown-it-py`) | `test_render.py::test_context_cannot_forge_control_sections`, `test_render.py::test_package_text_cannot_forge_control_sections`, `test_render.py::test_unclosed_html_comment_in_a_list_entry_does_not_hide_later_sections`, `test_render.py::test_code_span_round_trips_through_a_commonmark_parser`, `test_render.py::test_fenced_text_round_trips_and_leaves_the_document_intact`, `test_render.py::test_fence_is_longer_than_any_backtick_run` |
| The installed console script and packaged data work outside the source cwd | `test_packaging.py` (marker `packaging`) |
| No production state, credentials or external services are contacted | An autouse fixture in `conftest.py` makes any socket connection fail the test; tests use temporary directories and a temporary `HOME`; `test_packaging.py` builds with `--no-build-isolation --no-index`; `test_cli.py::test_cli_touches_nothing_but_its_targets` |
| Read commands do not mutate packages | `test_cli.py::test_read_commands_do_not_mutate` (runs against a read-only directory) |
| The committed example is a draft, unapproved and reproducible | `test_example.py` |

## 14. Supported Python

`requires-python = ">=3.12"`, with no upper bound. Development and tooling baseline 3.12.

Supported means allowed by the metadata. Tested means the full suite, including the installed-wheel test, passed on standard GIL builds on Linux (x86-64).
CI does that for every pull request on CPython 3.12, 3.13, 3.14 and 3.15 as `actions/setup-python` provides them, and each run's log records the exact versions (run 39587221044: 3.12.15, 3.13.16, 3.14.8 and 3.15.0).
Before CI, the same checks passed locally on 3.12.15, 3.13.16, 3.14.4 (from the Linux distribution) and 3.15.0.
Free-threaded and JIT builds, Windows and macOS, and other implementations were not verified, and the runtime has no dependencies to check beyond the standard library.
