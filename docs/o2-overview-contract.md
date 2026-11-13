<!--
SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
SPDX-FileCopyrightText: 2026 SINTEF Ocean AS

SPDX-License-Identifier: MIT
-->

# Omnitient O2 contract: work-package overview

Status: implemented by the O2 slice, on top of [the O1 contract](o1-contract.md).
This document is the specification the overview code and tests follow.
Where it is silent, O1 applies; it changes nothing in O1 (package schema version 1, commands, exit codes, diagnostics).

## 1. Purpose and boundaries

> In one invocation, show the packages in one project's explicit local collection, their declared stages, assessed handoff readiness and what needs human attention.

The overview is read-only.
It writes nothing, runs no process, contacts no network service and imports or executes nothing a manifest provides.

It does not follow evidence URLs, resolve dependencies, verify Git provenance, schedule or rank work, infer that anything is merged, complete or ready to launch, or group packages across projects.
A declared `done` is what the author wrote, not verified completion.
It compares neither `package.project` with a directory name nor one package with another except for duplicate package ids.

## 2. Command

```text
omnitient overview COLLECTION [--root NAME=ABSOLUTE_DIRECTORY ...] [--roots-file FILE] [--json]
```

`COLLECTION` is a directory chosen by the operator.
It is the only project boundary: there is no implicit collection, registry, Git discovery or automatic inclusion of examples.
One host mapping, validated exactly as in O1 (section 6.2 there), applies to every package in the collection.
Nothing maps a root automatically.
A required context file whose root is unmapped stays a blocker for that package.
Mapped roots may lie outside the collection and outside any checkout, under O1's rules.

## 3. Discovery

```text
work/
  o2-overview/work-package.json
  another-package/work-package.json
```

- Candidates are the **immediate child directories** of `COLLECTION` that hold an entry named exactly `work-package.json`.
  There is no recursion; a manifest directly in `COLLECTION` is not a candidate.
- Other entries, hidden ones included, are **ignored** and counted: regular files, other file types, and directories without that entry.
  Generated handoffs and their sidecars below a package directory are never reached.
- The listing is sorted by file-system **bytes** (`os.fsencode` of the name), not by locale.
  Creation and enumeration order never change the output.
  Names that are not valid UTF-8 are carried as surrogate escapes and written as JSON `\udcXX` escapes.

### 3.1 Links and containment

- Links in **ancestor** components of the supplied `COLLECTION` path resolve normally.
- A link at the **final component** of `COLLECTION` is refused (`overview.symlink`, a whole-command refusal).
- A link as an **immediate child entry** (to anything, including a dangling one) or as the **manifest** is not followed.
  The location is a candidate with status `excluded`, because nothing can be said about a package that may be behind the link.
- Discovery below the opened collection directory is descriptor-relative: each child directory and each manifest is looked at with an `lstat` and opened with `O_NOFOLLOW` relative to a descriptor already held, and the manifest is verified to be a regular file on the open descriptor before any of it is read.
  So a link at those components, including one substituted after the inventory, is refused rather than followed, and a FIFO is never opened blocking or read.
  This is the whole guarantee; it does not extend to other ways a file system can change under a reader (section 3.2).
- A manifest that is a directory, FIFO, device or socket is `excluded` (`overview.not_regular`) and never read.
- O1's own rules for context files (links inside an explicitly mapped root, forbidden names, limits) are unchanged and apply only to context.

### 3.2 Limits of the guarantee

There is no atomic snapshot and no general protection against a hostile concurrent file-system writer.
A process that changes the collection while it is read can produce a report that mixes states.
The overview defends against links at the components above and against unexpected file types; it adds no defence against renames, hard links, mounts or other substitutions.

## 4. Assessment

Candidates are assessed one at a time, in sorted order.
Each valid package goes through O1's `resolve_context` and `readiness.evaluate_all`; the resolved context (embedded text included) is dropped before the next package is read.
There is no second readiness engine, cache or concurrency.
This bounds memory, not latency: under O1's maxima the reads can still be large.

| Entry status | Meaning | `package` / `readiness` |
| --- | --- | --- |
| `valid` | The manifest loaded under the O1 schema and was assessed, even if every stage is blocked. | present |
| `invalid` | Reading, parsing or schema validation of the manifest failed. | `null` |
| `excluded` | A known disallowed link or non-regular manifest (section 3.1). | `null` |
| `unassessed` | A potential location could not be examined (for example a directory that cannot be opened or searched, or an entry that disappeared). | `null` |

Nothing about identity is invented for entries that are not `valid`.

**Declared stage and readiness are separate.**
`package.declared_stage` is the package's `stage` field.
`readiness` has `planning`, `implementation` and `review`, each assessed as in O1 section 5, so a package declared `done` with a stale approval shows `approval.stale` for implementation.
Readiness is handoff readiness only; the overview never says "ready to launch", "execute now", "merged" or "dependency-complete".

**Duplicate package ids.**
When two or more `valid` entries declare the same id, every one keeps its path, `package` and `readiness`, each gets an `overview.duplicate_id` error diagnostic (location `package.id`), and the collection gets one `overview.duplicate_id` error diagnostic per id that lists every path.
Nothing is deduplicated, no entry wins, and the duplicates are not O1 blockers.

**Unmapped roots.**
A root that is mapped but declared by no package in the collection produces one `roots.unknown` warning for the whole collection (location `roots`), not one per package.
It is reported only when usage can be established: discovery complete and every candidate valid.
An empty, fully understood collection reports every mapped root.

### 4.1 Advisory actions

Every stage report lists actions: deterministic explanations, not executable commands and not a schedule.
Each action is `{code, diagnostics: [{code, location}]}`; the references point at the stage's blockers and warnings, in O1 order, and identical `(code, location)` references are listed once per category.
The source diagnostics themselves are not deduplicated.
Categories appear in this fixed order, and only when they have a reference:

| Action | O1 diagnostic codes |
| --- | --- |
| `inspect_package` | `content.incomplete` |
| `resolve_decision` | `decision.unresolved`, `decision.open` |
| `review_context` | `context.digest_mismatch` (never an automatic pin) |
| `supply_context` | `context.root_unmapped`, `context.missing` |
| `seek_approval` | `approval.missing`, `approval.stale` |
| `inspect_diagnostic` | every other code (`context.unreadable`, `context.unsafe_path`, `context.too_large`, `context.not_text`) |

`handoff_ready` is appended, with an empty reference list, when the stage has no blockers.
It says only that O1 found no blocker.
Warnings produce advice but never change `ready` or the exit status.

## 5. JSON output

`--json` writes one document to stdout and nothing to stderr.
Keys are in the fixed order below; the text is `json.dumps(..., ensure_ascii=True, allow_nan=False, indent=2)` plus a final line feed.

| Key | Type | Meaning |
| --- | --- | --- |
| `schema` | string | `omnitient/overview` |
| `schema_version` | integer | `1` |
| `discovery_complete` | boolean | Section 6. |
| `assessment_complete` | boolean | Section 6. |
| `results_status` | string | `complete`, `partial` or `omitted` (section 6). |
| `counts` | object or `null` | Below; `null` for a whole-command refusal. |
| `diagnostics` | array | Collection-level diagnostics (below). |
| `entries` | array | One per candidate, sorted by file-system bytes of the directory name. |

`counts`, in this order: `collection_entries`, `ignored`, `candidates`, `valid`, `invalid`, `excluded`, `unassessed`.
All are non-negative integers.
They count immediate entries, hidden ones included, not files inside them.
Whenever `counts` is not `null`: `collection_entries = ignored + candidates` and `candidates = valid + invalid + excluded + unassessed`.
Candidate counts include excluded and unassessed locations.

An **entry** has `path` (collection-relative POSIX string, always `NAME/work-package.json`), `status`, `package`, `diagnostics` and `readiness`.
`package` has `id`, `title`, `declared_stage` and the full 64-character `content_digest`.
`readiness` maps `planning`, `implementation` and `review`, in that order, to `{ready, blockers, warnings, actions}`.
Entry `diagnostics` hold problems with the entry itself (manifest, schema, duplicate id); O1 readiness findings live only in the stage lists.

A **diagnostic** is `{code, severity, message, location}`; `location` is always present, a string or `null`.
O1 readiness diagnostics keep their code, severity and location (a decision or context id, or `null`) and, with one exception, their message.
The exception is `context.unreadable`: O1 quotes operating-system error text, which the overview replaces by `<root>:<path>: the path or file could not be read`.
Schema diagnostics for an `invalid` entry are O1's, unchanged.
Overview locations are fixed symbols: `collection`, `roots`, `output`, `manifest`, `directory` and `package.id`.

| Code | Severity | Where | Meaning |
| --- | --- | --- | --- |
| `overview.collection_invalid` | error | collection refusal | The collection does not exist or is not a directory. |
| `overview.collection_unreadable` | error | collection refusal | It cannot be opened or listed, or listing failed partway. |
| `overview.symlink` | error | collection refusal or entry | A link that is not followed (section 3.1). |
| `overview.not_regular` | error | entry | The manifest is not a regular file. |
| `overview.discovery_unreadable` | error | entry | A potential location could not be examined. |
| `overview.manifest_unreadable` | error | entry | The manifest exists but cannot be read; the entry is `invalid`. |
| `overview.duplicate_id` | error | collection and entries | Section 4. |
| `overview.entry_limit` | error | collection refusal | More than 1000 immediate entries. |
| `overview.package_limit` | error | collection refusal | More than 100 candidate locations. |
| `overview.output_limit` | error | output refusal | The selected output exceeds 1,048,576 bytes. |
| `roots.invalid`, `roots.duplicate` | error | collection refusal | The host mapping is invalid (O1 codes). |
| `roots.unknown` | warning | collection | Section 4. |

Messages never contain host paths, resolved roots or host-root listings, and never quote operating-system error text.
For a host mapping error the message is one fixed sentence per code and does not say which root or file was wrong.
The code names the category, not the host condition: the overview does not claim that two different host failures, or the same one at two moments, give identical diagnostics.

## 6. Completeness, result status and exit status

- `discovery_complete` is `true` when every immediate entry was classified and no entry is an `excluded` link or `unassessed`, and the listing and the inventory bounds succeeded.
  An `excluded` non-regular manifest does not make discovery incomplete: the location is known and hides nothing.
- `assessment_complete` is `true` when discovery is complete and every candidate is `valid`.
- `results_status` describes the delivery of entries, not correctness or readiness.
  `complete`: discovery is complete, so every identified location is represented, with error entries where needed.
  `partial`: entries are delivered but coverage is not established (a link or unexamined location).
  `omitted`: no result entries were delivered because the command refused.

| Situation | discovery | assessment | `results_status` | `counts` | exit |
| --- | --- | --- | --- | --- | --- |
| Empty accessible collection | true | true | complete | zeros | 0 |
| Only valid packages, blocked or not | true | true | complete | counts | 0 |
| Warnings only | true | true | complete | counts | 0 |
| A malformed or unreadable manifest | true | false | complete | counts | 1 |
| A non-regular manifest | true | false | complete | counts | 1 |
| Valid duplicate ids | true | true | complete | counts | 1 |
| A link, or a location that could not be examined | false | false | partial | counts | 1 |
| Invalid mapping, invalid or unreadable collection, listing failure, `entry_limit`, `package_limit` | false | false | omitted | `null` | 1 |
| `output_limit` | as established | as established | omitted | `null` | 1 |

A refusal has `entries: []` and one diagnostic; it is never an arbitrary subset and never an empty successful collection.
Exit status is `0` or `1` for a delivered overview, `2` for an argument error (argparse, unchanged), and never `3`: there is no aggregate readiness.
A programming error is not turned into a report; it ends with a traceback.
If stdout breaks, the command exits `1` after one write attempt, with no retry; whatever the reader received may be cut short, and the overview publishes nothing anywhere else.

Preflight order: argument parsing, mapping validation, collection validation, bounded inventory, sorted assessments, duplicate and aggregate diagnostics, serialisation.
So an invalid mapping is reported even when the collection is also invalid, and discovery never starts after it.

## 7. Bounds

| Limit | Value |
| --- | --- |
| Candidate package locations | 100, inclusive |
| Immediate entries in the collection | 1,000, inclusive, hidden entries included |
| Selected output | 1,048,576 UTF-8 bytes, inclusive, counting the final line feed and JSON escapes |

The listing stops reading at 1,001 entries; the inventory stops at 101 candidates.
Exceeding either is a compact refusal, never an assessed subset.
The output is built in memory first; one that does not fit becomes the `output_limit` refusal and the other form is not tried.
All O1 package, context and roots-file limits stay.
There are no override flags and no new aggregate budget.

## 8. Text output

Without `--json` the report is for a terminal.
Every character a terminal could act on is written as an escape: control characters (newline and tab included), bidirectional and other format characters, surrogates, private-use and unassigned code points, and spaces other than U+0020 become `\xNN`, `\uNNNN` or `\UNNNNNNNN`, and a backslash is doubled.
Titles are limited to 100 source characters and diagnostic messages to 200, counted on the text as written, before escaping.
Text longer than the limit is cut to one character fewer and followed by `…`, which marks it as shortened; the cut text is then escaped.
The limits are therefore neither terminal-column widths nor limits on the escaped length: a title full of characters that need escapes can occupy more rendered characters than its limit.
Paths are never shortened.
The report states that declared stages are not verified and that dependencies, evidence and Git provenance are not checked.
It is encoded as UTF-8.
A refusal is reported on stderr in O1's `severity[code] location: message` form with nothing on stdout.

## 9. Privacy

The overview prevents **tool-generated** host-path disclosure and embeds no context: it prints no collection path, mapped directory or resolved path, and no context content.
Text the author wrote (titles, decision questions, file names inside the collection) is shown as written, safely escaped, even if it contains a path or something that looks like a secret.
This is not secret detection and nothing is redacted automatically.

## 10. Interpretations chosen

1. **Every link child is a candidate.**
   A link cannot be classified without following it, so a link to an ordinary file is also `excluded`, and a collection that contains one exits `1`.
2. **A non-regular manifest keeps discovery complete; a link does not.**
   The first is a known location that hides nothing; the second may hide a package.
3. **`partial` versus `omitted`.**
   `partial` is a delivered report whose coverage has a gap; `omitted` is a refusal.
4. **Warnings produce advice.**
   `decision.open` only ever occurs as a warning, so the advice mapping would otherwise have no use for it.
5. **A manifest that exists but cannot be read is `invalid`; a directory that cannot be opened or searched is `unassessed`.**
   The first read a manifest and failed; the second never got to one.
6. **Unreadable O1 context text is normalised in O2 only.**
   O1's behaviour is unchanged.
7. **Host mapping messages are fixed sentences.**
   They lose the root name that O1's message gives, in exchange for never echoing the operator's paths.
8. **The text report escapes and shortens; JSON is exact.**
   JSON carries the authored text unabridged (as JSON escapes).

## 11. Acceptance plan

Every item is an offline pytest test in `tests/test_overview.py` unless noted.

| Criterion | Tests |
| --- | --- |
| Empty collection and exact envelope; ignored entries; shallow exact-name discovery | `test_empty_collection_has_the_exact_envelope`, `test_only_immediate_children_with_the_exact_manifest_name_are_candidates` |
| No recursive fixture or handoff inclusion | `test_generated_handoffs_and_fixtures_below_a_package_are_not_candidates` |
| Final root, child and manifest links refused; ancestor links allowed; races | `test_a_link_at_the_collection_root_is_refused`, `test_links_in_ancestor_components_resolve_normally`, `test_links_at_child_and_manifest_level_are_excluded_and_not_followed`, `test_a_link_swapped_in_after_inventory_is_not_followed`, `test_reading_a_manifest_refuses_links_and_fifos_that_appear_after_the_check` |
| Non-regular manifests, unreadable paths, no unsafe reads | `test_a_manifest_that_is_not_a_regular_file_is_excluded_without_being_read`, `test_a_manifest_swapped_for_a_fifo_after_inventory_is_never_blocked_on`, `test_unreadable_child_directory_and_manifest_with_permission_bits` (skipped as root), `test_unreadable_paths_by_fault_injection_even_where_modes_are_ignored`, `test_entry_that_cannot_be_examined_is_unassessed_and_peers_continue`, `test_manifest_that_vanishes_or_fails_midway_is_reported_not_guessed`, `test_invalid_collections_get_a_bounded_refusal`, `test_unreadable_collection_directory` (skipped as root), `test_unreadable_collection_by_fault_injection`, `test_a_collection_path_that_cannot_be_used_at_all_is_a_refusal` |
| Mixed valid, malformed and unreadable candidates; duplicate ids | `test_mixed_valid_malformed_and_unreadable_candidates_in_one_report`, `test_duplicate_ids_keep_every_path_and_assessment_and_fail_the_command` |
| Declared stage versus readiness | `test_declared_done_with_a_stale_approval_is_not_ready` |
| Context, decisions and advice | `test_context_and_decisions_map_to_advice_in_the_fixed_category_order`, `test_identical_action_references_are_listed_once_but_diagnostics_are_not_merged`, `test_warnings_alone_keep_a_stage_ready_and_the_exit_zero`, `test_other_context_problems_map_to_inspect_diagnostic`, `test_unreadable_context_message_is_normalized_and_keeps_code_and_location` |
| Parity with the O1 commands | `test_overview_agrees_with_validate` |
| Host mapping validation and aggregate unused-root warnings | `test_invalid_mapping_aborts_before_discovery_and_is_host_neutral`, `test_roots_file_validation_is_o1_validation`, `test_unknown_mapped_roots_are_reported_once_for_the_whole_collection`, `test_unused_root_warnings_are_suppressed_when_usage_cannot_be_established` |
| Exact envelopes, nullability, counts, completeness, exits | `test_json_envelope_key_order_types_and_nullability`, `test_completeness_combinations_and_exit_status`, `test_valid_blocked_packages_exit_zero_and_never_three` |
| Terminal controls, authored text, unusual file names, host-neutral output | `test_escape_text_covers_controls_bidi_surrogates_and_the_backslash`, `test_text_output_escapes_invisible_characters_and_marks_shortened_text`, `test_text_diagnostic_messages_escape_authored_text`, `test_unusual_file_names_in_json_and_text`, `test_output_never_contains_host_paths_or_context_content`, `test_text_report_is_deterministic_and_states_what_it_does_not_verify` |
| Stable output regardless of creation or enumeration order | `test_listing_is_sorted_by_bytes_and_ignores_creation_and_enumeration_order` |
| 100 and 101 candidates; 1000 and 1001 entries | `test_one_hundred_candidates_are_accepted_and_a_hundred_and_one_refused`, `test_a_thousand_entries_are_accepted_and_a_thousand_and_one_refused`, `test_entry_limit_stops_listing_instead_of_reading_everything` |
| At and over 1,048,576 serialised bytes | `test_json_at_the_output_limit_is_delivered_and_one_byte_more_is_not`, `test_json_size_boundary_is_inclusive_of_the_final_line_feed`, `test_over_limit_output_is_a_bounded_refusal_that_keeps_the_flags` |
| Failure during enumeration; preflight precedence; stdout and programming errors | `test_failure_partway_through_the_listing_is_a_whole_refusal`, `test_preflight_order_is_mapping_before_collection`, `test_broken_stdout_reports_a_nonzero_exit_without_retrying`, `test_programming_errors_are_not_reported_as_partial_results` |
| No mutation, network, subprocess or manifest code | `test_overview_makes_no_writes_spawns_nothing_and_opens_no_sockets`, `test_the_overview_modules_import_nothing_that_could_spawn_or_connect`, `test_a_child_process_leaves_the_tree_byte_identical_with_bytecode_disabled`, `test_manifest_code_is_never_imported_or_executed` |
| Existing commands unchanged; command in the installed distribution | `test_the_overview_command_is_listed_and_o1_commands_are_unchanged`, the unchanged O1 modules, `test_packaging.py` (marker `packaging`) |

The read-only test compares content, type, permission bits, size and modification time before and after, and ignores access times: an operating system updating `atime` when a file is read is not an application write.
