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
| 3 | Valid input, but no usable launch instruction: the package is not ready for review, or (Codex track) the checkout is not the candidate or the repository has replacement refs. The reasons are printed, nothing is written. |
| 1 | Refused: an invalid package, an unusable repository or ref, a Git program that is untrusted or too old, an empty or ambiguous comparison, an output directory that cannot be used or that changed while it was prepared, or an instruction that would be too long. |
| 2 | Usage error. |

Every line of the output, and every diagnostic, is written with control and formatting characters as escapes, as for `start`.
The one exception is the launch instruction, which is already in a closed notation (section 4) and is printed on its own line.

### 1.1 What is read from Git

Git is asked a fixed, read-only set of questions: `version`, `rev-parse`, `symbolic-ref`, `merge-base` and `for-each-ref`.
Before any ref is resolved, the repository is checked:

- The Git program is the first acceptable `git` in the caller's `PATH`, used by its real path.
  A `PATH` entry that is empty or relative (it would mean the current directory) or that lies inside the repository is skipped.
  So is an entry whose program, directory or any directory above either is owned by someone other than root or the current user, or can be written by the group or by others.
  A sticky directory such as `/tmp` is accepted above the program, because others cannot rename or remove an entry they do not own, and every entry on the way is owner-checked.
  Access control lists, and a process that runs as the owner or as root, are not considered.
  A group-writable directory therefore counts as changeable by others, which a distribution's umask of 002 can produce for a personal `bin`.
  If only such entries hold a `git`, that is `review.git_untrusted`; if there is none, `review.git_missing`.
  Git itself then runs with a `PATH` of its own program's directory alone.
- Git must be version 2.32 or later (`review.git_version`), the first that can be told to ignore the user's configuration; older ones would apply it silently.
  The version line and the program's path are shown and recorded in the manifest.
- A repository with an `info/grafts` file is refused (`review.history_rewritten`): Git still reads it when replacement refs are off, it can rewrite parents, and so it can move a merge base.
- Replacement refs are noted, not refused, here; section 2.2 says what they mean for the Codex track.

Each ref is then resolved once, in this order:

1. Both refs must name a commit; a remote branch counts only as a local ref, because nothing is fetched (`review.ref_unresolved`).
   A name that Git finds ambiguous, such as a branch and a tag with the same name or a ref and an abbreviated commit id, is `review.ref_ambiguous`, with advice to give the full ref name (`refs/heads/NAME`, `refs/tags/NAME`) or the full commit id; Git would otherwise pick one silently.
2. The candidate commit and its tree are recorded as full object ids, and so is the basis commit and its tree.
3. With `--basis merge-base` (the default) the basis is the one merge base of the two commits, the comparison the review skill makes by default.
   No merge base is `review.no_merge_base`, and more than one is `review.ambiguous_base`, with advice to name the base commit and use `--basis exact`.
   With `--basis exact` the basis is the base commit itself.
4. A basis and a candidate with the same tree leave nothing to review (`review.empty_comparison`).
5. HEAD and the checked-out branch are observed, for the checkout notes of section 3.

From then on only these ids are used.
A branch that moves afterwards cannot change a prepared comparison, and preparing again names the new ids.

The Git program runs with its global and system configuration disabled, with replacement refs off (so an id cannot be redirected to another commit), with optional locks and lazy fetching off, with every transport refused, with the hooks path and the file-system monitor disabled, and with an environment built from scratch (`PATH` and a fixed locale).
Every transport is refused twice: `protocol.allow=never` is the default policy, and an empty `GIT_ALLOW_PROTOCOL`, which lists no allowed transport and overrides any setting of the repository, is what a `protocol.file.allow=always` in the repository's own configuration cannot relax.
Without it, a partial clone whose configuration said so would still fetch a missing object from its promisor remote on a Git that does not know `GIT_NO_LAZY_FETCH` (before 2.44): `TestTransportsCannotBeRelaxedByTheRepository` shows the fetch and its refusal.
An object that is missing locally is never fetched, and the ref that needs it is `review.ref_unresolved`, with advice to fetch it yourself first.
A partial clone whose commits are present works.
Variables such as `GIT_DIR`, `GIT_SSH_COMMAND` or `GIT_EXTERNAL_DIFF` in the caller's environment are not passed on.
The program's answers that name a path (the Git directories) are read as bytes, only the newline that ends the line is removed, and the bytes are decoded as the file system names them, so a leading or trailing space or a name that is not UTF-8 is the directory that exists and not another.
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
"Outside the repository" means outside its work tree and outside its Git directories, which lie elsewhere for a linked work tree (the shared directory) or a separate Git directory.
A path that is inside either, exists, has no existing parent or has other characters is refused before anything is written, so no instruction names a path that cannot be given as it is.
The directory has exactly the mode 0700 and its files exactly 0600, whatever the umask: the command sets the bits on the open directory and files, so a restrictive umask (even one that masks the owner's own bits) cannot leave the owner without access, and a permissive one cannot widen it.
They hold package content and a host path, and the parent may be shared.
Only the owner (and root) can read them, so whoever reads them must do so as that owner, or the owner arranges access, for example by copying the file or changing the modes; this command never does.
Access control lists and a default ACL of the parent are the file system's and are not examined.
Ordinary `render` keeps its own modes.
The destination is bound by identity, not by a path.
The parent is opened once, and the identity of it and of every directory above it is compared with the repository's top directory and Git directories (so a parent that is in the repository by a link, a bind or a swap is refused, not only one whose path says so).
The directory is created in that open parent, opened without following links and checked to be what `mkdir` made, and every file is created, read back and removed relative to it.
Required access: the identity checks open the existing parent and walk up through every directory above it, so the parent and each of its ancestors need read and search (execute) permission for the user running the command, and the parent also needs write permission, because the new directory is created in it.
The command does not change permissions or fall back to another location; where the access is missing, the owner arranges it or chooses another parent.
So a path that is replaced meanwhile (a parent swapped for a link into the repository, the new directory renamed away) cannot redirect a write: the bundle goes into the directory that was selected, and the final check that the path still names it fails with `review.out_changed`, which names no instruction.
Cleanup is bound the same way: only a file that is still the one this command created (device and inode) is removed from that directory, and the directory is removed only if it is still the one created and is empty; anything else, such as a file another writer put in its place, is left alone.
Residual: a writer with the command's own rights who replaces a name between the check and the removal is not prevented.
It contains:

- `review-request.md`, a self-contained file: renderer-owned sections that identify the comparison and say how to review, the checkout observation, and the review-stage handoff for the package;
- `handoff-review.meta.json`, the completion manifest of o1-contract.md section 8.4 (it also records the host path and the Git ids, and is not to be committed).

The file starts with a paragraph that applies the reader rule of o1-contract.md section 8.4 to it, then the package, the package revision, the review approval, the repository, each ref as given and as resolved, the candidate tree, the basis and the object format.
The paragraph says that `handoff-review.meta.json`, beside the file, must be a regular file of valid JSON of schema `omnitient/handoff-manifest` with `schema_version` the integer 1, whose `outputs` object lists 1 to 8 plain file names including `review-request.md`, and that each listed file must be a regular file of at most 1048576 bytes whose SHA-256 equals the 64 lowercase hexadecimal digits listed.
It says that unlisted files are not part of the bundle, that a failed or impossible check is a stop, and that passing shows that the stored bytes match the manifest and not what the reader's tool presented.
The rule uses the manifest the command wrote last (section 8.4), so a file that was cut, completed by another writer or edited in the middle with the right last line fails, which the last line alone cannot show; the command prints the file's SHA-256 beside the instruction for the owner.
It still cannot prove integrity against a writer who can replace the file and the manifest together, or what the reader's tool showed (section 6).
The page-by-page reading procedure of start-contract.md section 1.4 is not applied.

The rest of the file tells the reviewer to read the whole file, to work read-only, how to see the change from the ids, to treat statements about passing checks as claims, to form their own verdict and what to report.
The two Git commands give the reader the interpretation that resolved the ids:

```text
env -i LC_ALL=C GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_ATTR_NOSYSTEM=1 GIT_OPTIONAL_LOCKS=0 GIT_NO_LAZY_FETCH=1 GIT_TERMINAL_PROMPT=0 GIT_NO_REPLACE_OBJECTS=1 GIT_ALLOW_PROTOCOL= PATH=GITDIR GIT --no-pager --no-replace-objects --no-optional-locks -c core.fsmonitor=false -c core.hooksPath=/dev/null -c protocol.allow=never -c core.warnAmbiguousRefs=true -C REPO diff --no-ext-diff --no-textconv BASIS CANDIDATE
env -i ... -C REPO show --no-textconv CANDIDATE:PATH
```

`env -i` clears the reader's environment, so `GIT_DIR`, `GIT_CONFIG_*`, `GIT_REPLACE_REF_BASE`, `GIT_GRAFT_FILE` or a lazy-fetch setting of the session cannot redirect them; the variables and settings are those that resolved the ids (one list in `review.py`, used by both).
`GIT` is the absolute path of the Git program that resolved them and `GITDIR` its directory, and `REPO` is the repository's top directory, each as one shell word (quoted when needed, spaces kept).
So the commands run in the selected repository, read the commits the ids were resolved to, and never fetch: in a partial clone a blob that is not there is an error, which the file tells the reader to report, not to fetch.
A repository, program or Git directory path with a control or formatting character cannot be given that way and is `review.repo_not_literal`.
A reader on another machine, or after the program was removed, cannot run them; it stops and reports.
Nothing here enforces that the reader uses these commands: the session's permissions and the reader's compliance do.

The file ends with a last line, `End of review request` and the first twelve characters of the candidate commit, followed by a full stop.
It is written whole before the completion manifest, and a file can stop anywhere if the preparation is interrupted; the last line is a quick sign of that, and the reader rule decides.

The printed instruction is short and names the file, for example `Read the file /tmp/r-1/review-request.md completely, then follow it. It asks for one independent read-only review: do not edit, commit or push anything. First verify the bundle as the file's first paragraph says: handoff-review.meta.json beside it must list the file with its SHA-256, and the last line must be "End of review request 0123456789ab.". Stop and report if either check fails or cannot be made.`
The owner passes it to a fresh session.
A reader that cannot compute a SHA-256 cannot establish the rule and stops.

### 2.2 Codex

`--track codex` writes nothing and takes no `--out`.
It prints one line, the owner-invoked instruction for the installed skill:

```text
/autonomous-development:review-existing-pr "Independent read-only review of one Git change. Package ... | objective: ... | scope 1: ... | exclusion 1: ... | acceptance 1: ... | decision D1 question: ... | decision D1 answer: ..."
```

That is the skill's input contract: its name followed by free text, as in the skill's own examples.
The text names the candidate commit as the target ref and the basis commit as the base ref with base mode exact, both as full ids, with the candidate tree and the package revision labelled as what they are.
It first defines the notation of section 4 in words, then names the selected repository, the top directory in which the ids were resolved, as the text between the first less-than sign and the following greater-than sign after the sentence that says so (neither character can occur inside the notation), to be decoded and compared exactly, spaces included, so a leading or trailing space of the path is part of it.
It tells the session to stop and report if its work tree is another directory.
It states that the checkout must be clean with the candidate checked out.
Then it tells the invoking session to pass the encoded package criteria as the import description of the review controller: all of them, in order, exactly as written and without decoding, summarising, reordering or adding to them, and to stop and report if it cannot supply them as that description.
The boundary is defined by the first actual field separator: the criteria begin immediately after the first occurrence in the instruction of the three-character separator space, vertical bar, space, which no earlier text contains, and end immediately before the instruction's final double quote.
Their first item is labelled `objective`, they are separated from each other by the same separator, and the first separator is not part of them.
The criteria are the approved objective, then the scope, exclusions and acceptance criteria, then the question and the answer of every resolved decision, in the package's order.
Spaces at the very end of the criteria are written as `\x20`, because a controller may trim them.
After the import and before any review phase, the instruction asks the session to check the record the controller keeps: its review target must be the candidate commit and its base the base commit with base mode exact, it must record no uncommitted changes at import, and its accepted spec must show the criteria completely and without a truncation notice; otherwise the session stops and reports.
It asks for the reviewer's own verdict.
It contains no ref text, no evidence and no decision rationale, and no host path except the repository's top directory in the notation of section 4.

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

The skill's input has no way to turn replacement refs off, and its controller reads the change with the repository's own Git settings, so a replacement ref could make a pinned id mean another commit when it is read:

- If the repository has any replacement ref (`refs/replace/`), exit 3 and no instruction, with advice to remove it or use the regular track, whose commands keep replacement refs off.
- Replacement refs that the caller's environment adds when the controller runs (for example another `GIT_REPLACE_REF_BASE`) cannot be observed here.

The skill, its owner-invocation policy and its controller are not touched.

### 2.3 Codex: what is known when

The Codex track involves three moments, and the output keeps them apart (`observed at preparation`, `checked by the review controller when the review runs, not here`, `not established by this preparation or by the controller`).

| Question | At preparation (this command) | At review time (the skill's `import-pr` and later phases, from the skill text and a trial run of the controller) |
| --- | --- | --- |
| Is the candidate the checked-out HEAD? | Observed once. | Required and enforced: `import-pr` refuses another HEAD, re-checks the target before it publishes, and refuses `codex` and `evaluate` after drift. |
| Is the work tree clean? | Not observed (section 1.1). | Required and enforced, unconditionally, at import and at each drift check. The instruction asks the session to confirm that the record shows no uncommitted changes at import. |
| Are the pinned ids what the controller reviews? | The ids are resolved with replacement refs off. | The controller records its target and base commits and the base mode; the instruction asks the session to compare them with the pinned ids. |
| Does the criteria text arrive? | The size limit (section 2.2) keeps it within the controller's 8000-character description limit. | It arrives as the import description and is shown in the accepted spec; the instruction asks the session to confirm the criteria are complete and show no truncation notice, and to stop otherwise. |
| Are there replacement refs? | None under `refs/replace/` and no graft file, or no instruction. | Not detected and not disabled. |

The last row is a gap that this package cannot close.
A trial run of the controller (checked-out candidate, clean work tree) imported a candidate whose commit a replacement ref made after preparation had changed, with rc 0 and the replaced commit message in its artifacts, while the same run with `GIT_NO_REPLACE_OBJECTS=1` in the environment of the controller read the real commit.
The controller passes its caller's variables to its own Git, but the environment of the Codex execution it starts is minimised and does not carry that variable, so Git commands that the reviewing model itself runs would still apply a replacement ref.
Because the supported invocation is an owner-typed instruction with a fixed description and no environment, no wording of the instruction can establish this guarantee, and the instruction does not ask the session to run its own Git, which the skill does not grant.
The smallest owner decision, which this package leaves open, is one of:

- accept the residual for the Codex track: the instruction is still withheld where a replacement ref exists at preparation, and a ref created in the window between preparation and review is not seen;
- or ask for the controller change, outside this package and in the plugin's own repository: set `GIT_NO_REPLACE_OBJECTS=1` among the git hardening that `apply_git_hardening` in the plugin's `scripts/state.py` applies, which feeds both the controller's own Git and the minimised Codex environment (and, if wanted, refuse an import when `refs/replace/` is non-empty);
- or use the regular track for any review that must be exact about object interpretation.

Nothing about reviewer permissions or context boundaries changes: no raw Git is asked of the session, no scan or fetch is added, and the skill and controller are not touched.

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
Refs are not in the Codex instruction at all, its only host path is the repository's top directory in this notation, and the only path in the regular instruction is one that passed the check of section 2.1.

In the regular file, package text is only in the renderer's code spans and fences (o1-contract.md section 8.3), and the refs, the repository path and the observed branch name are escaped code spans, with the display escaping of the terminal output (section 1).
Terminal output is escaped once, as a whole line; the file escapes the branch name itself, because nothing else escapes a code span.
The sections added here are fixed text, so the document's headings are exactly the renderer's.

## 5. Independent contexts

The review-stage handoff is rendered from the package with three free-text fields withheld: the summaries of evidence entries, the notes of approvals and the notes of execution recommendations.
Each is written by whoever recorded it and may carry another reviewer's verdict or a preference about the review.
Nothing else is removed.
What stays, as approved package content, is the title, objective, scope, exclusions and acceptance criteria, dependency notes, each decision with its question, answer, rationale and source, the purposes and embedded files of the context, the identifiers, kinds, references and recorders of evidence entries, and the records of approvals and recommendations.
Any of those can hold a verdict or a preference, and Omnitient does not look for one.
Delivery records carry no free text in the handoff.
Evidence entries stay as opaque references: identifier, kind, reference, recorder, time and any engine identity, none of which is read, fetched or interpreted.
Approvals and recommendations stay as records (identifier, stage, person, time, state, model and limits) without their notes.
The covered content, and so the package revision, is unchanged, the package file is never modified, and `render` itself still shows the summaries and notes.
The file says that statements about checks are claims by whoever recorded them and that this preparation verified none of them.

Each preparation is its own context: a new directory every time, an existing one never reused, and nothing from an earlier preparation or the other track read.
Omnitient does not look for or exclude a verdict kept anywhere else, such as in the objective, in a decision's answer or outside the package; those are part of the approved content.
The manifest beside the regular file names the package file, and the package file still holds the withheld text.
The file tells the reviewer not to open it, nor anything an evidence reference names; that is an instruction, and a reviewer with read access to those files can still read them.
So the withholding is of the handoff's text, not of the files on the host.

## 6. Not covered

- Launching a reviewer, running a review, controller state changes, automatic Codex dispatch, and changes to the installed skill or its policy.
- Remote retrieval, Git changes, CI trust assertions, approvals, closeout, provider changes, publication and merge.
- A dashboard, a scheduler, a new evidence store and transcript import.
- Bare repositories, a repository directory that is not the top of its work tree, and a repository that Git refuses to open (for example for its ownership).
- Whether a reader's tool presents the regular file whole: the reader rule (section 2.1) establishes the stored bytes, not what a tool presented, and the page-by-page reading procedure of [start-contract.md](start-contract.md) section 1.4 is for implementation handoffs and is not applied here.
- Integrity against a writer who replaces the file and the manifest together, or a reader that cannot compute a SHA-256.
- Replacement refs that are created after preparation and the Codex runtime's own Git use (section 2.3).
- That the model follows any instruction, and the behaviour of the review skill, which was not run.
- A reader that does not use the commands of section 2.1, or runs on another host, and a Git program that is replaced between the check and its start by someone with the owner's own rights.
- Another work tree of the same repository (for a linked work tree, the main one) as an output location, a shallow clone's missing history beyond the no-merge-base refusal, and a Git older than 2.32.
- A writer with the command's own rights replacing a name in the destination between a check and a removal: writes and removals are bound to the opened directory (section 2.1), but a name inside it can still be swapped by such a writer.
- Access control lists on the output and on the Git program's location.
- Which diagnostic comes first when a package is not ready and a ref is also bad: a ref refusal (exit 1) is reported before blockers (exit 3), and a second run shows the other.

## 7. Acceptance

| Criterion | Tests (`tests/test_review.py`) |
| --- | --- |
| The handoff identifies the package revision, base and candidate commits, candidate tree and review scope, and distinguishes Git identities from the package revision | `TestRegularTrack`, `TestCodexTrack`, `TestComparison` |
| A regular reviewer gets a file path and a short instruction; Codex gets one invocation; neither reviewer is launched and nothing is written outside the new directory | `TestRegularTrack`, `TestCodexTrack` |
| Missing, stale or other-stage approval, an open decision and a blocked required input give diagnostics and no instruction; implementation approval alone never satisfies review readiness | `TestRefusals` |
| Unresolved refs, an empty comparison, an ambiguous or missing merge base, and an unusable repository or output directory are refused with actionable diagnostics | `TestRefusals` |
| Refs resolve once to full ids and a moving branch cannot change a prepared comparison; replacement refs and the caller's Git environment cannot redirect it | `TestComparison` |
| The reader's Git commands name the selected repository and keep replacement refs off, and give the pinned change when run from elsewhere | `TestReaderKeepsTheSelectedComparison` |
| The Codex track is pointed at the selected repository and withheld where replacement refs or a graft file exist | `TestReaderKeepsTheSelectedComparison` |
| Git is asked only allowlisted read-only questions with a scrubbed environment, and the repository is left unchanged | `TestGitIsAskedOnlyReadOnlyQuestions`, `TestRegularTrack` |
| The Git program is trusted (never from the checkout, a relative entry or a shared directory), recent enough and recorded | `TestTrustedGitProgram` |
| The program, its directory and every directory above are the owner's or root's and not group- or other-writable | `TestTrustedProgramLocation` |
| Nothing is fetched, in a partial clone either, and a repository's protocol-specific setting cannot relax it | `TestNothingIsFetched`, `TestTransportsCannotBeRelaxedByTheRepository` |
| The reader's commands run in their own environment, with the program and settings that resolved the ids | `TestReaderRunsInItsOwnEnvironment`, `TestReaderKeepsTheSelectedComparison` |
| Significant whitespace and non-text bytes in Git paths are kept, and the encoded Codex repository path is defined and delimited | `TestSignificantWhitespaceInPaths` |
| An ambiguous ref is refused with advice | `TestAmbiguousRefs` |
| Output is refused inside the repository's Git directories, writes and cleanup follow the selected directory, and a destination that changed gives no instruction | `TestOutputNearGitMetadata`, `TestDestinationIdentity`, `TestBundleAtDescriptor` |
| Output is exactly private whatever the umask, and ordinary render keeps its modes | `TestOutputPermissions`, `TestOutputPermissionsUnderRestrictiveUmasks` |
| A reader applies the O1 manifest and SHA-256 rule, with a short owner instruction, and tells a complete bundle from a partial or altered file | `TestPartialFileIsNotACompleteBundle` |
| The Codex track separates preparation-time observations from controller checks and states the gap | `TestCodexBoundaryAtReviewTime` |
| The withheld fields and what approved content can still contain are stated exactly | `TestIndependenceWording`, `TestNotesAreWithheldFromIndependentHandoffs` |
| Approval and recommendation notes are withheld from the independent view, with the records, the revision and ordinary render unchanged | `TestNotesAreWithheldFromIndependentHandoffs`, `TestIndependentContexts` |
| The start of the encoded description is the first field separator | `TestImportDescriptionBoundary` |
| A checkout that is not the candidate is surfaced, and withholds the Codex instruction | `TestRegularTrack`, `TestCodexTrack` |
| The Codex instruction tells the session to pass the encoded criteria, with the objective and the resolved decisions' questions and answers, unchanged as the controller's import description, and keeps the pinned target and exact basis | `TestCodexTrack` |
| An instruction over the size limit is refused in full | `TestCodexTrack` |
| The observed branch name is escaped exactly once in the regular file and in the terminal output, including an invisible formatting character in a real branch | `TestRegularTrack` |
| Package, ref and path text stays literal across the runtime boundary | `TestLiteralText` |
| The two tracks and repeated preparations are independent contexts, and evidence summaries stay out | `TestIndependentContexts` |
| `render` and the other callers keep their contracts | `TestExistingCallersKeepTheirContracts` and the existing suites |
