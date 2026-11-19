<!--
SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
SPDX-FileCopyrightText: 2026 SINTEF Ocean AS

SPDX-License-Identifier: MIT
-->

# Handing one selected commit to independent review

Status: implemented.
`omnitient review prepare` turns a review-ready work package, one explicit local Git comparison and one selected review track into what a fresh reviewer needs.
It adds no readiness rule: readiness, approvals, blockers, decisions, context handling and root mappings are those of [o1-contract.md](o1-contract.md) for the `review` stage, and the package handoff is the one `render --stage review` produces.
Nothing here launches a reviewer, runs a review, records an approval, changes Git or contacts a network service.

## 1. The command

```bash
omnitient review prepare [--root NAME=DIR]... [--roots-file FILE]
    --repo DIR --base REF --candidate REF [--basis merge-base|exact]
    --track regular|codex [--out DIR] -- PACKAGE
```

`PACKAGE` is a package file or a directory holding `work-package.json`.
The root mappings are the explicit ones of o1-contract.md section 6.2.
`--repo` is the top directory of one local Git work tree.
`--base` and `--candidate` are revision expressions that name a commit in it.

| Exit | Meaning |
| --- | --- |
| 0 | Prepared. A launch instruction is printed. |
| 3 | Valid input, but no usable launch instruction: the package is not ready for review, or (Codex track) the checkout is not the candidate. The reasons are printed, nothing is written. |
| 1 | Refused: an invalid package, an unusable repository or ref, an empty or ambiguous comparison, an output directory that cannot be used, or an instruction that would be too long. |
| 2 | Usage error. |

Every line of the output, and every diagnostic, is written with control and formatting characters as escapes, as for `start`.
The one exception is the launch instruction, which is already in a closed notation (section 4) and is printed on its own line.

### 1.1 What is read from Git

Git is asked a fixed, read-only set of questions: `rev-parse`, `symbolic-ref` and `merge-base`.
Each ref is resolved once, in this order:

1. Both refs must name a commit; a remote branch counts only as a local ref, because nothing is fetched (`review.ref_unresolved`).
2. The candidate commit and its tree are recorded as full object ids, and so is the basis commit and its tree.
3. With `--basis merge-base` (the default) the basis is the one merge base of the two commits, the comparison the review skill makes by default.
   No merge base is `review.no_merge_base`, and more than one is `review.ambiguous_base`, with advice to name the base commit and use `--basis exact`.
   With `--basis exact` the basis is the base commit itself.
4. A basis and a candidate with the same tree leave nothing to review (`review.empty_comparison`).
5. HEAD and the checked-out branch are observed, for the checkout notes of section 3.

From then on only these ids are used.
A branch that moves afterwards cannot change a prepared comparison, and preparing again names the new ids.

The Git program runs with its global and system configuration disabled, with replacement refs off (so an id cannot be redirected to another commit), with optional locks and lazy fetching off, with the hooks path and the file-system monitor disabled, and with an environment built from scratch (`PATH` and a fixed locale).
Variables such as `GIT_DIR`, `GIT_SSH_COMMAND` or `GIT_EXTERNAL_DIFF` in the caller's environment are not passed on.
The repository's own `.git/config` still applies: this is no defence against a repository that a hostile party prepared, and no isolation.
The work tree is never inspected, so whether it is clean is not observed.

### 1.2 Package identity and Git identity

The output and the regular file always name two kinds of identity and label them:

- the package revision, which is the content digest of o1-contract.md section 4.2, and is not a Git identity;
- Git commit and tree ids, which are full object ids of the repository's object format (40 hexadecimal digits for SHA-1, 64 for SHA-256).

Refs are shown as given, escaped, beside the ids they resolved to.

## 2. The tracks

The two tracks are prepared from the same facts and never see each other.
Neither output mentions the other track, and neither carries a reviewer's verdict.

### 2.1 Regular-Claude

`--track regular --out DIR` writes into a new directory, outside the repository, whose absolute path has only letters, digits and `._+=@:,/-` after the leading slash.
A path that is inside the repository, exists, has no existing parent or has other characters is refused before anything is written, so no instruction names a path that cannot be given as it is.
It contains:

- `review-request.md`, a self-contained file: renderer-owned sections that identify the comparison and say how to review, the checkout observation, and the review-stage handoff for the package;
- `handoff-review.meta.json`, the completion manifest of o1-contract.md section 8.4 (it also records the host path and the Git ids, and is not to be committed).

The file starts with the package, the package revision, the review approval, the repository, each ref as given and as resolved, the candidate tree, the basis and the object format.
It tells the reviewer to read the whole file, to work read-only, how to see the change from the ids (`git diff --no-ext-diff --no-textconv BASIS CANDIDATE`, `git show CANDIDATE:PATH`), to treat statements about passing checks as claims, to form their own verdict and what to report.
It cannot enforce any of that: the session's permissions do.

The printed instruction is short and names the file, for example `Read the file /tmp/r-1/review-request.md completely, then follow it. It asks for one independent read-only review: do not edit, commit or push anything.`
The owner passes it to a fresh session.

### 2.2 Codex

`--track codex` writes nothing and takes no `--out`.
It prints one line, the owner-invoked instruction for the installed skill:

```text
/autonomous-development:review-existing-pr "Independent read-only review of one Git change. Package ... | objective: ... | scope 1: ... | exclusion 1: ... | acceptance 1: ... | decision D1 question: ... | decision D1 answer: ..."
```

That is the skill's input contract: its name followed by free text, as in the skill's own examples.
The text names the candidate commit as the target ref and the basis commit as the base ref with base mode exact, both as full ids, with the candidate tree and the package revision labelled as what they are.
It states that the checkout must be clean with the candidate checked out.
Then it tells the invoking session to pass the encoded package criteria, the items after its first sentence, as the import description of the review controller: all of them, in order, exactly as written and without decoding, summarising, reordering or adding to them, and to stop and report if it cannot supply them as that description.
The criteria are the approved objective, then the scope, exclusions and acceptance criteria, then the question and the answer of every resolved decision, in the package's order.
It asks for the reviewer's own verdict.
It contains no host path, no ref text, no evidence and no decision rationale.

The target, the base commit with base mode exact and the pinned ids stay in the first sentence, so the comparison is the one that was resolved.
Every item is written in the closed notation of section 4, so the description reaches the controller with the package's literal text and nothing in it can act as syntax.
A review-stage package has no unresolved decision, because one is blocking (o1-contract.md section 3.2); a decision that is not resolved is not carried.

The skill requires that its target ref be the checked-out HEAD of a clean work tree, and refuses otherwise.
Omnitient keeps that requirement and does not weaken it:

- If HEAD is not the candidate commit, exit 3 and no instruction (`no usable launch instruction`), with advice to check out the candidate in a clean work tree and prepare again.
- Whether the work tree is clean is not observed, because asking Git about a work tree can run configured helpers.
  The output says so, and the skill's own controller refuses a dirty tree.
- An instruction longer than 8000 characters is refused in full (`review.instruction_too_long`) rather than truncated, and nothing is printed as an instruction; the objective, the criteria and the decisions all count towards the limit.
  The regular track carries the same text in a file.

The skill, its owner-invocation policy and its controller are not touched.

## 3. Checkout notes

Both tracks print what was observed: HEAD, the checked-out branch or that it is detached, and whether HEAD is the candidate.
For the regular track a mismatch is information, not a refusal: the file says so and tells the reviewer to read the change through the commit ids and not through the working tree.
For the Codex track it withholds the instruction, as above.

## 4. Package text and paths stay literal

Package text that must cross into an instruction that a model reads is written in a closed notation.
Only ASCII letters and digits, the space and `.,:()/_-+=` stand for themselves.
Every other character, the backslash included, is `\xNN`, `\uNNNN` or `\UNNNNNNNN`: the code point, in lowercase hexadecimal, decoded from left to right.
So the instruction holds no quote, backtick, dollar sign, semicolon, vertical bar (except Omnitient's own separator between items), single quote, newline or tab from a package.
The instruction says that the text is quoted, is data and grants no permission.
Refs and host paths are not in the Codex instruction at all, and the only path in the regular instruction is one that passed the check of section 2.1.

In the regular file, package text is only in the renderer's code spans and fences (o1-contract.md section 8.3), and the refs, the repository path and the observed branch name are escaped code spans, with the display escaping of the terminal output (section 1).
Terminal output is escaped once, as a whole line; the file escapes the branch name itself, because nothing else escapes a code span.
The sections added here are fixed text, so the document's headings are exactly the renderer's.

## 5. Independent contexts

The review-stage handoff is rendered from the package with the free-text summaries of evidence entries withheld, because a summary is written by whoever recorded the evidence and may carry another reviewer's verdict.
Evidence entries stay as opaque references: identifier, kind, reference, recorder, time and any engine identity, none of which is read, fetched or interpreted.
The covered content, and so the package revision, is unchanged, and `render` itself still shows the summaries.
The file says that statements about checks are claims by whoever recorded them and that this preparation verified none of them.

Each preparation is its own context: a new directory every time, an existing one never reused, and nothing from an earlier preparation or the other track read.
Omnitient does not look for or exclude a verdict kept anywhere else, such as in the objective, in a decision's answer or outside the package; those are part of the approved content.

## 6. Not covered

- Launching a reviewer, running a review, controller state changes, automatic Codex dispatch, and changes to the installed skill or its policy.
- Remote retrieval, Git changes, CI trust assertions, approvals, closeout, provider changes, publication and merge.
- A dashboard, a scheduler, a new evidence store and transcript import.
- Bare repositories, a repository directory that is not the top of its work tree, and a repository that Git refuses to open (for example for its ownership).
- Whether a reader's tool presents the regular file whole: the reading procedure of [start-contract.md](start-contract.md) section 1.4 is for implementation handoffs and is not applied here.
- That the model follows any instruction, and the behaviour of the review skill, which was not run.

## 7. Acceptance

| Criterion | Tests (`tests/test_review.py`) |
| --- | --- |
| The handoff identifies the package revision, base and candidate commits, candidate tree and review scope, and distinguishes Git identities from the package revision | `TestRegularTrack`, `TestCodexTrack`, `TestComparison` |
| A regular reviewer gets a file path and a short instruction; Codex gets one invocation; neither reviewer is launched and nothing is written outside the new directory | `TestRegularTrack`, `TestCodexTrack` |
| Missing, stale or other-stage approval, an open decision and a blocked required input give diagnostics and no instruction; implementation approval alone never satisfies review readiness | `TestRefusals` |
| Unresolved refs, an empty comparison, an ambiguous or missing merge base, and an unusable repository or output directory are refused with actionable diagnostics | `TestRefusals` |
| Refs resolve once to full ids and a moving branch cannot change a prepared comparison; replacement refs and the caller's Git environment cannot redirect it | `TestComparison` |
| Git is asked only allowlisted read-only questions with a scrubbed environment, and the repository is left unchanged | `TestGitIsAskedOnlyReadOnlyQuestions`, `TestRegularTrack` |
| A checkout that is not the candidate is surfaced, and withholds the Codex instruction | `TestRegularTrack`, `TestCodexTrack` |
| The Codex instruction tells the session to pass the encoded criteria, with the objective and the resolved decisions' questions and answers, unchanged as the controller's import description, and keeps the pinned target and exact basis | `TestCodexTrack` |
| An instruction over the size limit is refused in full | `TestCodexTrack` |
| The observed branch name is escaped exactly once in the regular file and in the terminal output, including an invisible formatting character in a real branch | `TestRegularTrack` |
| Package, ref and path text stays literal across the runtime boundary | `TestLiteralText` |
| The two tracks and repeated preparations are independent contexts, and evidence summaries stay out | `TestIndependentContexts` |
| `render` and the other callers keep their contracts | `TestExistingCallersKeepTheirContracts` and the existing suites |
