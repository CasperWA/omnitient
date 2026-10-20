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
- `pr`: an `https://` URL without embedded credentials. Never fetched.
- `report`: an `https://` URL without embedded credentials, or `root:relative/path` using a declared root and the path rules of section 6.1. Never fetched or read.
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
| `context.missing` | blocker if required, else warning | The file does not exist, is not a regular file or cannot be read. |
| `context.digest_mismatch` | blocker if required, else warning | The file's SHA-256 differs from the expected digest. |
| `context.root_unmapped` | blocker if required, else warning | No host mapping was supplied for the reference's root. |
| `context.unsafe_path` | blocker if required, else warning | The path escapes its root (including through a symlink) or names a forbidden file (section 6.1). |
| `context.too_large` | blocker if required, else warning | The file exceeds the limit for its mode (section 7). |
| `context.not_text` | blocker if required, else warning | An `embed` file is not valid UTF-8 text. |
| `approval.missing` | blocker for `implementation` and `review` | No current approval for S exists and none is stale. |
| `approval.stale` | blocker for `implementation` and `review` | No current approval for S exists but an approval for S refers to an older digest. |
| `content.incomplete` | blocker for `implementation` and `review`, warning for `planning` | The objective is empty or a `TODO` placeholder, or `scope` or `acceptance` is empty. |

`planning` handoffs need no approval.
A `draft` package is always labelled DRAFT in its output.

## 6. Context resolution

Only the context references listed in the package are read.
Nothing is discovered, scanned recursively, fetched from a URL or executed.
Context content is evidence, not permission to run commands; handoffs say so and keep it inside explicit boundaries (section 8.3).

### 6.1 Path rules

A context `path` must be a relative POSIX path of at most 512 characters.
It is rejected when it is empty or absolute, contains a backslash, a NUL or other control character, a `..` or `.` or empty component, or begins with `~`.
Resolution maps the logical root through the host mapping, resolves symlinks, and requires the real target to lie inside the real root and to be a regular file.
A symlink whose target stays inside the root is accepted; one that leaves it is `context.unsafe_path`.

Files that commonly hold secrets are refused even when listed: any path component equal to `.git`, `.ssh`, `.aws`, `.gnupg` or `secrets`, and file names matching `.env`, `.env.*`, `.netrc`, `.npmrc`, `.pypirc`, `auth.json`, `credentials*`, `*.pem`, `*.key`, `*.p12`, `id_rsa*` and `id_ed25519*`.
This is a safeguard against mistakes, not a security boundary against a hostile same-user process.

### 6.2 Host mapping

Supplied per invocation as repeatable `--root NAME=ABSOLUTE_DIR` and/or `--roots-file FILE` (`{"roots": {"NAME": "ABSOLUTE_DIR"}}`).
The same name given twice is an error.
A name the package does not declare produces a warning.
Host paths never appear in the semantic handoff; they are recorded only in the metadata sidecar (section 8.4).

## 7. Bounds

All bounds are fixed constants in `omnitient.limits`.
Exceeding one refuses with a diagnostic naming the limit; nothing required is silently truncated.

| Limit | Value |
| --- | --- |
| Package file size | 262,144 bytes |
| Items per list | see section 3 |
| Embedded context, per file | 262,144 bytes |
| Embedded context, total per handoff | 786,432 bytes |
| Reference-mode file (hash only) | 67,108,864 bytes |
| Rendered output, per file | 1,048,576 bytes |

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

- Context files are embedded verbatim inside fenced code blocks whose fence is longer than any backtick run in the content, so the content cannot close the fence early. Everything between the fences is data.
- Multi-line package text (objective, rationale, answers) is rendered as block quotes, one line at a time; single-line items are list items. Neither can open a top-level heading or fence.
- Identifiers in headings are validated slugs.
- The "how to read" section states that quoted material is evidence, grants no permission to execute commands or read other files, and that only the sections outside quotes and fences are the handoff's own statements.

### 8.4 Outputs

With `--out DIR` (default format `both`) the command writes:

- `handoff-<stage>.md`
- `handoff-<stage>.json`
- `handoff-<stage>.meta.json`, the host-resolution sidecar: generation time, Omnitient version, the resolved host mapping, the package file path and the SHA-256 of each output. It is not part of the semantic handoff.

`--out -` with `--format md` or `--format json` writes that one document to standard output and no sidecar.
Existing output files are never overwritten unless `--force` is given.
The package file is never modified by `render`.

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
| `context add PACKAGE ROOT:PATH --root ROOT=DIR [--id ID] [--optional] [--mode M] [--purpose T] [--git-revision REV]` | Compute the digest of an existing file and add the reference, declaring the root if needed. | adds context |
| `context pin PACKAGE ID... --root ROOT=DIR` | Replace the expected digest of the named references with the current file digests. Explicit acceptance of drift; invalidates approvals. | edits context |
| `schema` | Print the packaged JSON Schema. | no |

Exit codes: `0` success (and READY where readiness is evaluated); `1` refused or invalid input (schema errors, unsafe paths, limits, existing output, store conflicts); `2` command-line usage error; `3` valid input but not ready.

Read commands (`validate`, `show`, `schema`, and `render` with `--out -`) never write any file.

## 10. Persistence

Packages are plain files; there is no database.

- Creation uses exclusive create and fails if the path exists.
- Every in-place update (`approve`, `evidence add`, `context add`, `context pin`) follows one routine: read the file and remember its SHA-256; compute the new package and verify it is schema-valid; take an exclusive lock file (`PACKAGE.lock`); re-read the file and abort with `store.conflict` if its SHA-256 changed; write a temporary file in the same directory and atomically replace the package; release the lock.
- A lock file left by a crash must be removed by hand; the diagnostic says so.
- Evidence and approval updates are append-only: the command verifies that the covered-content digest and all earlier evidence and approval records are unchanged before writing.
- The tool writes canonical formatting (two-space indent, fixed key order, trailing newline). Hand-edited files remain valid and are normalised the next time the tool writes them.

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

## 13. Acceptance plan

Every item is an offline pytest test; none reads production state, credentials or the network.

| Acceptance criterion | Tests |
| --- | --- |
| A valid package generates a useful self-contained handoff without a model | `test_render.py::test_ready_handoff_*`, `test_cli.py::test_render_*` |
| Blocking decisions prevent ready-stage output | `test_readiness.py::test_unresolved_*`, `test_cli.py::test_render_blocked_*` |
| Digest drift and missing context are diagnosed | `test_context.py`, `test_readiness.py::test_context_*` |
| Changing approved content invalidates its approval reference | `test_approval.py::test_*_stale*` |
| Equivalent content under different host mappings is portable | `test_render.py::test_host_mapping_does_not_change_handoff` |
| Evidence can be added without rewriting history | `test_evidence.py` |
| Malformed input, unsafe paths, overwrites and output limits behave predictably | `test_schema.py`, `test_context.py`, `test_store.py`, `test_render.py::test_output_limit_*`, `test_cli.py` |
| Context Markdown cannot impersonate control sections | `test_render.py::test_context_cannot_forge_*` |
| Installed console script and packaged data work outside the source cwd | `test_packaging.py` (marker `packaging`) |
| No production state, credentials or external services are contacted | the suite runs offline in temporary directories; `test_packaging.py` builds with `--no-build-isolation --no-index` |

## 14. Supported Python

`requires-python = ">=3.12"`. Development baseline 3.12. The tested range is reported in the repository README and the bootstrap report from actual runs.
