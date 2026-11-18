<!--
SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
SPDX-FileCopyrightText: 2026 SINTEF Ocean AS

SPDX-License-Identifier: MIT
-->

# Omnitient O3 contract: delivery records (R1)

Status: the technical contract for the O3 slice, release R1, on top of [the O1 contract](o1-contract.md) and [the O2 contract](o2-overview-contract.md).
Where this document is silent, O1 and O2 apply.
Where it states a different rule for a point O1 or O2 describes (the manifest version, the digest scheme, the handoff, show and overview output versions, the `schema` command and the update routine's reserve check), this document is the specification and O1 and O2 describe the earlier behaviour.

Omnitient is early-development software that is used only to track its own development until a full product v1 exists.
The **manifest schema version** (1 or 2) is a separate axis from any product release number.

The acceptance criteria are AC01 to AC24 in section 12.
The map from each criterion to its tests and results is in [o3-verification-map.md](o3-verification-map.md).

## 1. Purpose and boundaries

> Let a person record, in the package itself, that a revision of the work was accepted as delivered, withdraw that record, and see both.

A **delivery record** is workflow provenance, like an approval (O1, section 4.1): a named actor said, at a recorded time, with a stated reason and a selection of evidence references, that a revision was accepted, or that an earlier acceptance is withdrawn.
It is not identity authentication, not a tamper-proof audit trail, not verification of any artifact, and not permission for any further execution.
No command reads a referenced artifact, fetches a URL, runs a process, calls a model or touches Git.

Acceptance is available for code, documentation and investigations alike.
There is no universal requirement for a pull request, a commit or a passing build.
A negative or inconclusive investigation can fulfil the agreed objective; the owner judges that, the tool does not.

Missing or stale approvals, a context mismatch and unresolved handoff decisions do **not** gate acceptance.
Their diagnostics stay visible and unchanged: acceptance does not clear, waive or represent them as passed.
Acceptance never changes `stage`, never records an approval, never repins context and never completes a dependency.

## 2. Terms

- **Manifest format**: the package's own `schema_version`, 1 or 2.
- **Digest scheme**: the label that names how a revision was computed (section 3).
- **Revision**: a digest scheme and the covered-content digest it names.
- **Delivery history**: the ordered `delivery` array of a v2 package.
- **Acceptance**, **withdrawal**: the two kinds of delivery record (section 5).
- **Active acceptance**: an acceptance that no withdrawal targets.
- **Current-revision acceptance**: an active acceptance whose revision is the package's present revision.
- **Prior-revision acceptance**: an active acceptance of another, earlier revision.
- **Operation id**: the caller-chosen id of a delivery record and its only identity.

## 3. Manifest formats and the digest

| Format | `schema_version` | Digest scheme label | Delivery history |
| --- | --- | --- | --- |
| v1 | `1` | `covered-sha256-v1` | not allowed (`delivery` is an unknown field) |
| v2 | `2` | `covered-sha256-v2` | required `delivery` array, possibly empty |

- The scheme is determined by the manifest format, never configured, and there is no registry of algorithms.
  Both schemes are SHA-256 over the canonical JSON of the same covered projection (O1, section 4.2).
- The covered projection is unchanged except that `schema_version` carries **the package's own value**: `1` for v1 and `2` for v2.
  A v1 package therefore keeps its exact legacy digest, and no global version constant is hashed for all packages.
- `delivery` is **outside** the covered projection, like `stage`, `recommendations`, `evidence` and `approvals`.
  Appending or withdrawing an acceptance never changes the revision it describes.
- The model preserves the format of the package it was parsed from, and every serializer writes that format.
  A v1 package stays v1 through every supported write (`approve`, `evidence add`, `context add`, `context pin`) until it is explicitly upgraded (section 9).
- The parser is strict: unknown fields, unknown versions, malformed data, duplicate keys and out-of-range values are errors.
  A reader that knows only v1 refuses v2 with the existing `schema.version` diagnostic.
- Packages created by `omnitient new` are v2 and start with an empty `delivery` array.
- There is no bulk migration, no downgrade, no incidental upgrade and no round trip that drops history.
- The existing v1 JSON Schema resource is preserved byte for byte.
  A second resource describes v2, and `omnitient schema --schema-version 2` prints it (section 10).
  The Python parser stays authoritative where a schema and the parser could differ.

### 3.1 Upgrade consequences for approvals

An explicit v1 to v2 upgrade changes the covered `schema_version`, so it creates a new revision.
Earlier approvals are retained exactly as written and become stale for it.
Whether an old approval is still applicable evidence of the old revision is separate from whether it authorizes the upgraded revision: it does not.
Nothing is approved, repinned or completed by an upgrade.

## 4. Delivery history

`delivery` is one ordered array of records discriminated by `kind`.
Append order is the order of the array.
Nothing sorts it by timestamp or claims clock-based chronology.

Common fields, in stored order: `operation_id`, `kind`, `actor`, `recorded_at`, `reason`.

| Field | Rule |
| --- | --- |
| `operation_id` | `^[A-Za-z][A-Za-z0-9_-]{0,31}$`: an ASCII letter, then letters, digits, `_` or `-`, 1 to 32 characters, case-sensitive. The stable record id; there is no second identity. One namespace across acceptances and withdrawals of one package. |
| `kind` | `acceptance` or `withdrawal`. |
| `actor` | One line of safe text (O1, section 3), not blank, at most 200 characters. |
| `recorded_at` | UTC timestamp `YYYY-MM-DDTHH:MM:SSZ`, the actual recording time. |
| `reason` | Safe multi-line text (newline and tab allowed), not blank, at most 8192 UTF-8 bytes. Preserved exactly: no trimming or normalization. |

An **acceptance** adds, in stored order:

| Field | Rule |
| --- | --- |
| `package_id` | Must equal the enclosing package's id. |
| `revision` | `{scheme, digest}`: the scheme label of the package format (`covered-sha256-v2`) and 64 lowercase hex characters. |
| `evidence` | 1 to 20 evidence snapshots (section 6), distinct ids. |

A **withdrawal** adds `target_acceptance_id`: the `operation_id` of an earlier acceptance.

The parser rejects: unknown fields, duplicate operation ids, malformed records, a withdrawal whose target is absent or appears later in the array, a withdrawal whose target is not an acceptance, a second withdrawal of one target, an acceptance whose `package_id` differs from the package, and an acceptance that overlaps an **earlier unwithdrawn** acceptance of the same revision.
Renaming a package's identity after delivery history exists is unsupported, because every acceptance names the package id: the renamed package no longer parses, and no operation transfers an acceptance to another identity.

## 5. Operations

### 5.1 Acceptance

`omnitient closeout PACKAGE --by ACTOR --revision DIGEST --evidence E1 [E2 ...] --reason TEXT --operation-id ID`

A **new** acceptance requires all of:

1. a structurally valid v2 package (a v1 package is refused with the upgrade command to run);
2. a non-placeholder objective (O1, section 5, `content.incomplete`), a non-empty `scope` and non-empty `acceptance` criteria;
3. an explicit actor, a non-blank reason, an operation id and a revision, each valid under section 4;
4. a revision equal to the package's **present** revision;
5. 1 to 20 distinct evidence ids, each an existing entry of the package's live evidence;
6. capacity for the acceptance and its possible future withdrawal (section 7);
7. no other active acceptance of that revision.

It appends exactly one record, with the actual recording time and snapshots of the selected evidence.
Nothing else in the package changes.

### 5.2 Withdrawal

`omnitient withdraw PACKAGE --closeout-id ID --by ACTOR --reason TEXT --operation-id ID`

A withdrawal is an explicit, append-only record that targets an existing active acceptance by its exact id.
It requires an actor, the actual recording time and a non-blank reason.
It preserves the acceptance and its snapshots and ends its **active** display.

- It may target an acceptance of a **prior** revision, and may be recorded while the present objective, scope or acceptance criteria are incomplete or the package is blocked, as long as the package, the request and the target are structurally valid.
- A target that does not exist, or is a withdrawal, is refused.
- A withdrawal cannot be withdrawn, nothing cascades, and there is no reopening workflow.
- A new operation id that targets an already withdrawn acceptance is refused, and the refusal names the earlier withdrawal.

### 5.3 Reacceptance

After a withdrawal, a fresh explicit acceptance of the same revision is allowed while that revision is still current and the normal acceptance requirements are met.
It is a new record with a new operation id, a fresh evidence selection, a fresh reason and a fresh time.
The original acceptance and its withdrawal are preserved and the old record is never reactivated.
The rule is *at most one active acceptance per revision*, not one acceptance ever.

## 6. Evidence snapshots

An acceptance does not point at live evidence; it carries a copy of each selected entry.

- Selected ids that are repeated or unknown are refused, and the selection is stored sorted by id.
  The selected ids are the ids of the snapshots: there is no second, separately mutable selection.
- A snapshot holds the entry's metadata as stored by O1 (`id`, `kind`, `ref`, optional `summary`, `recorded_by`, `recorded_at`, optional `engine`), with absent optional fields absent, and `metadata_sha256`.
- `metadata_sha256` is the SHA-256 of the canonical JSON (O1, section 4.2: sorted keys, no insignificant whitespace, UTF-8) of that metadata, without the fingerprint itself.
- A referenced artifact is never read, fetched or verified.
  A snapshot says what the evidence entry said, not that anything it names exists or is true.
- When a package is parsed, each historical snapshot is validated for its own syntax and fingerprint, **not** for equality with the live evidence list.
  Later edits to or removal of live evidence do not rewrite, invalidate or revalidate history.
  A report whose root is no longer declared remains valid in a snapshot; selecting live evidence for a *new* acceptance follows the current rules.

## 7. Capacity

Fixed ceilings, with no truncation and no override:

| Ceiling | Value |
| --- | --- |
| Serialized manifest plus the withdrawal reserve | 262,144 bytes |
| Acceptance and withdrawal records, **including reserved withdrawal slots** | 100 |
| Evidence ids per acceptance | 20 |
| Reason of either operation | 8192 UTF-8 bytes |

One bounded withdrawal is reserved for **every active acceptance**, including those of prior revisions.
Let `A` be the number of acceptances, `W` the number of withdrawals and `U = A - W` the active ones.

**Count invariant:** `A + W + U <= 100`, which is `2A <= 100`.
At most 50 acceptance events fit, reacceptances included; the byte limit can bind earlier.
A withdrawal consumes the target's reserved slot, not a new one, so it never changes `2A`.

**Byte invariant:** `B + sum(R_i) <= 262144`, where `B` is the size of the stored serialization and every `R_i` is the constant `WITHDRAWAL_RESERVE_BYTES`, so the sum is `U * WITHDRAWAL_RESERVE_BYTES`.
`WITHDRAWAL_RESERVE_BYTES` is the size of the largest withdrawal the schema admits when appended to the stored form (indentation, separators, key names, all field maxima and worst-case JSON escaping, with the largest first-record overhead), derived by the serializer's own routine and verified at the boundary by tests.
The maxima do not fit jointly in the ceiling, so the byte invariant, not the count, is what bounds a package that holds large records.
Nothing is stored as padding and nothing is fabricated: a real withdrawal replaces its reserve by its actual bytes and releases the unused headroom.

All supported v2 writers (acceptance, withdrawal, `approve`, `evidence add`, `context add`, `context pin`, `upgrade`) refuse a result that violates the invariants.
V1 bounds are unchanged and v1 has no reserve.

### 7.1 Recovery exception

A v2 package edited outside Omnitient can be valid yet short of its reserve.
It stays inspectable: `show`, `overview` and the handoffs report a `delivery.reserve_deficit` diagnostic with the numbers.
Ordinary growth and new acceptances get no bypass and refuse.
A **withdrawal** is accepted in that state only if the resulting manifest fits the hard limits (262,144 bytes, 100 records), it meets every other withdrawal rule, and it reduces the byte deficit.
Any deficit that remains is still reported.
A deficit in the record count alone cannot be reduced by a withdrawal (`2A` does not change), so no withdrawal is accepted for it.
Recovery from arbitrary manual edits or disk failures is not promised.

## 8. Retry, replay and conflict

A command line is a **request**.
Two requests are equal when their parsed inputs are equal:

- evidence ids compare as a set, after repeated ids were rejected;
- actor, reason, operation id, kind, revision and target compare exactly: no trimming, no case folding, no Unicode normalization;
- shell quoting and JSON key order do not matter once parsed;
- generated timestamps, live evidence, current readiness and current content are never substituted into a saved request.

The order of checks is fixed:

1. Load and parse the package, then validate the request's syntax and repeated ids.
2. If the operation id exists, compare the request to the saved record **before** any new-operation gate (revision, live evidence, capacity, uniqueness).
3. Otherwise apply the gates of section 5.

| Situation | Outcome |
| --- | --- |
| New eligible operation id | One record appended, reported as **saved**. |
| Existing id, equal request | **Replayed**: the original record and the status observed in the loaded snapshot. No write, no new timestamp, no new snapshot, no temporary or lock file, even at capacity. |
| Existing id, different request | `delivery.conflict`; nothing written. |
| Equal replay after the revision drifted | The historical record; it does not accept the current revision. |
| Equal replay after withdrawal or reacceptance | The original record with its withdrawn status; never reactivated. |
| New id for a revision that already has an active acceptance | Refused. |
| New id for a reacceptance after withdrawal | Normal new-acceptance validation. |
| Equal withdrawal replay | The original withdrawal; no write. |
| New id targeting a withdrawn acceptance | Refused, naming the earlier withdrawal. |
| The package changed while the command ran | Refused (`store.conflict`); no automatic merge or retry. |

An unreadable or malformed package never yields a replay.
A replay reports the status of the loaded snapshot, not a promise about later concurrent changes.

## 9. Upgrade

```text
omnitient upgrade PACKAGE --to-version 2
omnitient upgrade PACKAGE --to-version 2 --apply --expected-file-sha256 FULL64HEX
```

**Preview** reads and validates the manifest only.
It reports the package id, the old and new format, the old and new revision (scheme and digest), how many approvals become stale for the new revision, the size of the resulting file and the SHA-256 of the file as it is on disk now.
It evaluates no readiness, resolves no context, reads no artifact, calls no Git and writes nothing.

**Apply** needs the explicit expected file hash, from the preview.
It validates, then uses the ordinary update routine (section 11) whose hash comparison under the lock is the guard.
It changes only the format and adds the empty `delivery` array; authored content, `stage`, pins, evidence and approvals are preserved.
Invalid, unsupported, oversized or changed input is refused and left unchanged.
There is no bulk mode, no force and no downgrade, and no automatic upgrade.

An already v2 package: a preview reports that no upgrade is needed, and an apply whose expected hash equals the current file hash reports **unchanged** and writes nothing.
An apply with any other hash, including the old pre-upgrade hash after a successful upgrade, is refused and directs the operator to inspect the package.
No past-operation receipt is fabricated.

Before upgrading a package that matters, keep its pre-upgrade bytes in Git history.
That is an operator prerequisite; the command does not use Git.

## 10. Commands and read surfaces

| Command | Behaviour | Mutates |
| --- | --- | --- |
| `closeout` | Section 5.1. | appends one acceptance |
| `withdraw` | Section 5.2. | appends one withdrawal |
| `upgrade` | Section 9. | with `--apply` only |
| `schema [--schema-version {1,2}]` | Prints the v1 resource by default (unchanged), or the v2 resource. | no |

Exit codes keep their O1 meaning: `0` success, `1` refused or invalid, `2` usage error, `3` valid input but not ready (where readiness is evaluated).
`closeout`, `withdraw` and `upgrade` never evaluate readiness and never exit `3`.
There is no automatic operation-id generation, and no general query or preview subsystem.

The outcome line distinguishes **saved**, **replayed** and (for `upgrade`) **unchanged**.
Success is reported only after the file was replaced.
If writing standard output fails after the file was replaced, the command reports on standard error that the change **is saved** and that the output failed, exits `1`, never says nothing changed, never rolls back and never appends again; running the same command again replays it safely.

### 10.1 Delivery summary

`show`, `overview` and newly rendered handoffs carry a compact delivery summary, separate from stage and readiness:

| Key | Meaning |
| --- | --- |
| `state` | `current`, `prior_only`, `withdrawn_only`, `none`, or `unavailable` for a v1 package |
| `current_revision_acceptance` | the id of the active acceptance of the present revision, or `null` |
| `prior_revision_unwithdrawn_count` | active acceptances of other revisions |
| `withdrawn_acceptance_count` | acceptances that a withdrawal targets |
| `acceptance_count`, `withdrawal_count` | records of each kind |

The state is derived in the order *current*, *prior only*, *withdrawn only*, *none*, and the counts are always shown with it, so a mixed history is not hidden.
A package that does not validate yields diagnostics, never a trusted `none`.
A declared stage `done` alone is not an acceptance.

An acceptance stays *associated* with the revision it names.
Raw context that changed without a repin does not change the revision: the acceptance stays associated while readiness shows the mismatch.
The summary describes that association, not a fresh verification of anything.

`show --json` additionally carries the full typed history (every record, with reasons and snapshots) and the reserve figures; human `show` lists ids, revision association and withdrawal linkage.
Overview and handoffs carry only the summary and ids.
No history is truncated silently, and no generated host path or authority claim is added.
Authored text is quoted by the O1 rules in Markdown and escaped by the O2 rules in the overview text.

### 10.2 Output versions

| Output | Version |
| --- | --- |
| Handoff JSON (`omnitient/handoff`) | `2` |
| Overview JSON (`omnitient/overview`) | `2` |
| `show --json` (`omnitient/show`) | `1`, newly versioned |
| Handoff bundle manifest (`omnitient/handoff-manifest`) | `1`, unchanged |

Only the latest output form exists for both input formats; there is no legacy output mode.
The output version is independent of the package version and its digest.
The Markdown handoff states the **actual** input format.
A handoff gets a new `Delivery` section after `Approvals`; handoffs generated earlier are never rewritten.
Existing exit statuses, readiness and the exact-byte bundle verification are unchanged.

## 11. Persistence

Every write keeps the O1 routine (section 10 there): the new package is computed and serialised and parse-validated before locking; under the exclusive lock file the file is read again and its SHA-256 compared with the one first read; a unique sibling temporary file is written and synced and replaces the package; only files the operation created are removed.
This is optimistic concurrency, not a transformation under the lock.
Mode, path and cleanup rules and the cooperative-writer assumption are unchanged; there is no generalized transaction framework and no defence against a hostile process of the same user.
There is no promise of durability beyond the file system's.

- Snapshots for one operation are prepared from the one loaded package, so no snapshot mixes two reads.
  The full-file comparison also protects evidence and approvals, which the covered digest does not.
- Schema, history preservation and the reserve are validated before anything is written.
  Existing delivery records keep their fields, values and order in every write.
- An acceptance or withdrawal leaves every unrelated field unchanged.
  Context operations can legitimately change the covered revision, and keep the history.
- Previews and every read path create no lock, temporary or manifest file.

## 12. Acceptance criteria

These are requirements, not claims that they have passed.

| Id | Criterion |
| --- | --- |
| AC01 | **Format separation.** v1 reads and writes keep the legacy digest and supported serialization; v2 history round-trips; unknown fields and versions fail closed; making v2 the default for new packages does not alter existing ones. |
| AC02 | **Digest transition.** An explicit upgrade changes the revision and stales retained approvals; unrelated v1 digests are unchanged; appending or withdrawing delivery records leaves the covered revision unchanged; no approval is synthesized. |
| AC03 | **Upgrade preview.** Deterministic proposed change and the actual file hash; bytes and file system unchanged; no context, artifact, Git or network activity. |
| AC04 | **Upgrade apply.** A matching guard changes only the selected valid v1 fixture and preserves unrelated fields, history and pins; evidence-only and other drift refuses; over-limit input refuses. |
| AC05 | **Upgrade retry.** The old hash after a successful apply refuses with inspection guidance; v2 preview and no-op behaviour are truthful; no duplicated migration and no fabricated receipt. |
| AC06 | **Acceptance eligibility.** Code, documentation, report-only and investigation fixtures are supported; structural gaps, bad or missing request fields, a wrong revision and v1 refuse; no universal PR or CI gate. |
| AC07 | **Readiness independence.** Missing or stale approval, a context mismatch and unresolved handoff decisions do not prevent an eligible acceptance; diagnostics and stage stay unchanged. |
| AC08 | **Snapshot integrity.** 1 to 20 distinct existing ids; unknown, repeated and malformed ids refuse; metadata, provenance and fingerprints are exact; optional-field absence is deterministic; no artifact is read. |
| AC09 | **History independence.** Editing or removing live evidence neither rewrites history nor revalidates snapshot equality; covered changes create a prior-revision association; unpinned raw-context drift stays separately visible. |
| AC10 | **Withdrawal.** Appends the exact target, actor, time and reason; preserves the original, history, pins and stage; the withdrawn acceptance ceases to display as active; a prior-revision target, incomplete current content and blocked readiness are allowed. |
| AC11 | **Withdrawal errors.** An unknown or non-acceptance target, a duplicate withdrawal under a new id and malformed request fields refuse; there is no undo of a withdrawal and no cascade. |
| AC12 | **Reacceptance.** The same current revision can be accepted again under a new id with normal validation; the original and its withdrawal are retained; a new revision needs its own explicit acceptance; uniqueness holds. |
| AC13 | **Request equality.** Evidence order alone is equivalent; repeated ids still refuse; whitespace and case differences in actor or reason, and differences in kind, revision, target or selection, conflict; nothing is normalized. |
| AC14 | **True replay.** The original id, time and snapshots are returned with no file-system write, at the limits and after revision drift, evidence drift or withdrawal; the observed status is right and an acceptance is never reactivated. |
| AC15 | **Concurrency.** A stale snapshot, competing acceptance or withdrawal and lock cases refuse without a lost record or an automatic retry; snapshots never mix reads. |
| AC16 | **Persistence failures.** A failure before the replacement preserves the original and respects the mode and cleanup rules; successful persistence followed by an output failure is distinguished, replays safely, and is neither rolled back nor duplicated. |
| AC17 | **Count bounds.** The 100-record ceiling includes reserved slots; at most 50 acceptance events; a withdrawal consumes its reserved slot; a reacceptance consumes a new used-and-reserved pair. |
| AC18 | **Byte and text bounds.** 262,144 bytes including the reserve, 20 references and 8192 UTF-8 bytes of reason are tested at and beyond the bounds, with multibyte and escaping cases; no truncation or override; the reserve bound is demonstrated. |
| AC19 | **Reserve preservation.** Every supported v2 write path preserves the reserve from compliant input and consumes only its target's reserve; v1 is unchanged; externally edited over-reserve input is inspectable, and only a hard-limit-compliant, deficit-reducing withdrawal uses the recovery exception, with the remaining deficit visible. |
| AC20 | **Read diagnostics.** Current, prior, withdrawn and none, and mixed history, are correct; invalid is not none; a declared `done` is not an acceptance; readiness, stage and exit semantics are unchanged. |
| AC21 | **Output formats.** Latest versioned show, overview and handoff for both input formats; the actual package version; full history inspectable and summaries compact; historical files untouched; exact-byte bundle verification preserved. |
| AC22 | **Safe bounded rendering.** Actor, reason and metadata are quoted or escaped safely; outputs are deterministic and bounded; no silent history truncation, no new host paths and no authority claims. |
| AC23 | **No unintended actions.** The new operations have no network, subprocess, model, agent, artifact-fetch or Git activity; read paths do not mutate; a mutation touches only the selected manifest and its documented temporary and lock files; nothing is bulk or collection-wide. The rule applies to Omnitient's operations, not to development commands such as running the tests. |
| AC24 | **Regression and synthetic lifecycle.** Focused and full local checks cover v1, v2, the schema resources, installation and the O1 and O2 behaviour; the complete lifecycle (upgrade, acceptance, withdrawal, replay, reacceptance, failure) is exercised on synthetic packages before any real package is upgraded or accepted. |

## 13. Interpretations chosen

These resolve details the ratified contract leaves open; each is reversible.

1. **Labels and names.** The scheme labels are `covered-sha256-v1` and `covered-sha256-v2`; the array is `delivery`; a snapshot's fingerprint is `metadata_sha256`; a revision is `{scheme, digest}`; `--revision` takes the digest and the scheme follows from the package format.
2. **Reserve constant.** One conservative `WITHDRAWAL_RESERVE_BYTES` serves every active acceptance, rather than a per-target value.
3. **Count deficit.** A deficit that only the record count shows cannot be reduced by a withdrawal and gets no recovery.
4. **v1 summary.** A v1 package reports state `unavailable` with zero counts, not `none`, because it cannot hold records.
5. **Default schema.** `omnitient schema` keeps printing the v1 resource so existing callers see the same bytes.
6. **O1 text.** `docs/o1-contract.md` is not edited: the committed example package pins it. This document states what it changes.
7. **Committed example handoff.** The output version bump changes what the renderer produces for the committed example, whose test compares the committed bytes; the example's handoff is regenerated as its README describes.
8. **Delivery stdout failure.** Exit status `1` with a distinct `delivery.stdout` diagnostic on standard error.
