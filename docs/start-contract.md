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
omnitient start preview [--root NAME=DIR]... [--roots-file FILE] [--checkout DIR] [--out DIR] -- PACKAGE
omnitient start handoff [--root NAME=DIR]... [--roots-file FILE] [--checkout DIR] --expect-preview TOKEN --out DIR -- PACKAGE
```

`PACKAGE` is a package file or a directory holding `work-package.json`.
The root mappings are the explicit ones of o1-contract.md section 6.2; a mapping that is not supplied is never supplied by `start`.
The target checkout is `--checkout`, or the current working directory, resolved through symlinks.
It is refused (`start.checkout`, exit 1) when it does not exist, is not a directory or has a component named `.git`, `.ssh`, `.aws`, `.gnupg` or `secrets`.
`--out` is the proposed handoff directory (section 1.3).

Both steps evaluate readiness for the `implementation` stage once, with the O1 engine.
Only `start handoff` writes, and only inside `--out`.

### 1.1 Preview

Never writes.
It prints one fact per line and exits:

| Exit | Meaning |
| --- | --- |
| 0 | Ready, and the handoff renders within its bounds. No handoff is written. A token is printed only when the handoff can be started from here (below). |
| 3 | Blocked. The blockers are printed; the inputs and write scope are withheld and there is no token. |
| 1 | Refused: invalid package or roots, an unusable checkout, or a handoff that cannot be rendered (for example over the output bound). |

The lines are the status, package id, title, declared stage, the full revision (content digest), the implementation approval (current, stale or none), the package file and its SHA-256, the mapped roots, the target checkout and its identity (device and inode), whether the checkout is the working directory of the command, what is checked about the checkout, whether it has a `.git` entry, its relation to each mapped root, whether the package file lies inside the checkout, the proposed handoff directory, and, when ready, the declared inputs, the proposed write scope, the size and SHA-256 of the rendered handoff, a summary of its reading plan, and the preview token.

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
The preview says whether the target is the working directory of the command.
That is all it observes: it cannot see which directory a session was opened in, and a session runs the command from its own working directory only because the skill says so.

**Informational previews.**
A ready preview prints `no usable handoff token` with a reason, and no token, when any of these hold, so it can be read but not started from:

- the target is not the working directory of the command, which is how a target elsewhere is previewed: the owner runs the preview, and then the start, from the target directory;
- no `--out` was proposed, or the proposed directory is not usable (section 1.3);
- the handoff cannot be read under the bounded reading procedure (section 1.4).

**Display and comparison are separate.**
Every host path, package text and diagnostic of the `start` commands is written as escapes, so a name cannot forge a line: the preview, the delivery report, and every refusal, including those that come from loading the package, reading a roots file, resolving the checkout, publishing the bundle and the command line itself.
The other commands keep their output.
Escaped text is for reading only and is never compared.

### 1.2 Preview token

A startable preview ends with `preview token: omnitient-start-2.F1. ... .F8`, eight SHA-256 values taken from the exact facts the owner was shown:

| Fact | Fingerprint of |
| --- | --- |
| revision | the content digest |
| package file contents | the package file's SHA-256 |
| package file location | the resolved package path, as the filesystem encodes it |
| root mappings | the sorted name and resolved directory of every mapped root |
| target checkout location | the resolved target path |
| target checkout identity | the target's device and inode |
| handoff directory | the resolved proposed directory |
| rendered handoff | the bytes of the Markdown handoff the preview rendered |

The token is local to this comparison.
It is not in any manifest, handoff or approval digest, and it contains no path.
It is not a credential: it only proves the same facts were observed.

The rendered handoff is deterministic (o1-contract.md section 8.2), so its fingerprint binds the owner's consent to what will be delivered, not only to the package.
An optional input that becomes available or unavailable, or changes, keeps the package READY and changes the handoff; it is a material change.

### 1.3 Handoff

`start handoff` takes the token the owner confirmed.
A token that is not a preview token (a version other than `omnitient-start-2`, or not eight hexadecimal values) is a usage error (exit 2).

It first requires that the target is the working directory (`start.checkout_not_cwd`, exit 1).
It then recomputes the first seven facts from the package file, the mappings, the target and `--out`, without reading any context file, and compares them with the token.
A difference means a material change: nothing is written and the command exits 1 with `start.changed`, naming the facts that differ and nothing else.
Because the confirmed fingerprints are compared, not a path resolved again now, a directory replaced at the same path, or a name that now links to another checkout, is a change.
A different `--out` is a change.

It then checks the destination, reads the context, assesses readiness once and renders.
A blocked result exits 3 with the blocked preview and writes nothing; there is no `--allow-blocked` form.
The rendered handoff is compared with the eighth fact before anything is created, and a difference is `start.changed` naming `rendered handoff`.

**The handoff directory** must be new, outside the target checkout and in an existing parent directory.
A preview that proposes one that is not is informational.
The write step refuses an existing file or directory, even an empty one, with `start.out_exists`, and one inside the target with `start.out_inside_checkout`; it never writes into, replaces or removes anything that exists.
It creates the directory itself, writes the bundle, verifies it, and on a failure removes the directory only if it is still empty.
Its advice is limited to what this command supports: it has no `--force`, so every refusal says to preview again naming a new directory with `--out`.

The bundle is published and verified as `render --out` does (o1-contract.md section 8.4): the Markdown handoff `handoff-implementation.md`, a reader probe `handoff-implementation.reader-probe.txt` (section 1.4) and the manifest, which lists both with their SHA-256.
The Markdown is the bytes `render --stage implementation --format md` produces.
The manifest records host paths, as `render`'s does, and is not to be committed.

On success it prints the files, the byte count and SHA-256 of the handoff, a statement that it verified the stored bytes and does not verify what a reader's tool presents, and the reading plan.
It prints `handoff delivered` only after the bundle verified, and a render or write failure exits 1 before anything is reported as ready to read.

### 1.4 Reading the handoff

Two different things are established, and only the first is established by Omnitient.

**Stored bytes.**
The files on disk match the manifest's SHA-256 values when the command read them back.
This says nothing about what a reader's tool presents afterwards, and a digest of the source file, a byte count or a last line number cannot prove that a reader received the whole document.

**What a reader received.**
This is established by a reading procedure that checks the presented content against facts Omnitient prints.
The plan covers the document in pages of at most 200 lines and prints, for each page, its line range, the first 16 characters of its first line and the last 16 characters of its last line.
It prints every line longer than 1024 characters with its number, length and last 16 characters, and a reader probe: a file of lines of 64, 128, 256, 512 and 1024 characters, each ending in a marker with its length.
Lines are counted at a line feed only, and characters are code points, not bytes.

The plan is printed once, on stdout, after `handoff delivered`.
It is not in the bundle, so the text a session was shown is the only copy it has, and that text can arrive cut: a tool may drop the middle or the end of a long output.
The procedure therefore verifies the plan before following it.

**Verifying the plan.**
The plan is every output line from the `reading plan:` line to the end.
Nothing is read, and so nothing is edited, unless all of these hold, using only the lines as printed:

- exactly one `reading plan:` line, complete, as `reading plan: N lines in P pages of at most 200, K line(s) over 1024 characters`, identical to the `reading plan:` line of the preview the owner confirmed (a summary reading `cannot be established` is a stop);
- exactly one `reader probe:` line naming M, then probe lines 1 to M, each once, in order;
- exactly P page entries, numbered 1 to P once each, in order, with page 1 starting at line 1, every other page starting on the line after the previous one ends, no page over 200 lines, and page P ending at line N;
- exactly K long-line entries, for K distinct line numbers within 1 to N, each longer than 1024 characters;
- every line after the summary is one of those entries, complete with its closing `|`, so a cut, repeated or unrecognised line is a stop;
- every anchor decodes as below and names no character the reader cannot observe.

The checks use only the counts the plan already prints, so the summary is the declaration the entries are held to, and the confirmed preview is where the summary was first shown.
A summary that agrees with the preview and entries that agree with the summary are what show that a plan was not cut in the middle or at the end.
This is independent of whether the handoff itself is later read completely.

**Anchors.**
An anchor is printed between two `|` delimiters that are not part of it, so an anchor may itself contain `|`: in a page entry the head ends at the one `|, last line ends |`, and every other anchor ends at the last `|` of its line.
The anchor text is the first or last 16 characters of the line (all of it when shorter) in the notation of `escape_text` (`overview_render.py`), which the other commands use for the same purpose:

| Written | Is |
| --- | --- |
| `\\` | one backslash |
| `\xNN` | the character with that code point, two lowercase hexadecimal digits |
| `\uNNNN` | the same with four digits, for code points up to U+FFFF |
| `\UNNNNNNNN` | the same with eight digits, above U+FFFF |
| any other character | itself, including the vertical bar and the space |

A reader decodes from left to right and compares the decoded text, never the printed form.
Left to right keeps the notation unambiguous: the document's own characters `\x09` are printed `\\x09` and decode to a backslash and `x09`, and only `\x09` alone is a tab.
Any other backslash sequence (including uppercase hexadecimal, a short escape or a code point above U+10FFFF) is ambiguous and is a stop.

The decoded head is compared as the start, and the decoded tail as the end, of the line as the reader's tool shows it, after its line number and untrimmed.
What a reader can observe is fixed as follows:

- A tab (`\x09`) is compared as a tab; a tool that shows it as spaces fails the comparison, which is a stop.
- A carriage return (`\x0d`) is removed from the decoded anchor and from the shown line before comparing, because a tool may or may not show it. So the procedure does not tell CRLF from LF. A tool that starts a new line at a lone carriage return presents lines that are not the planned lines, so a page edge differs and the comparison fails.
- Any other escaped character (`\xNN` other than `\x09` and `\x0d`, `\uNNNN`, `\UNNNNNNNN`) is a control, space-like, formatting or unassigned character that a tool may show as nothing or as another character. The reader does not guess: it stops, naming the page or line and the code point.

**Reading.**

1. Read the probe file. The probe lines up to the one the plan names (the smallest that is at least as long as the longest ordinary line) must each be shown whole, ending in their marker. A reader that cuts them cannot show ordinary lines of that length whole.
2. Read each page in plan order, asking for exactly its line range. The line numbers shown must be that range with none missing and none repeated; the first line must start, and the last line end, as planned.
3. Every listed long line must end as planned.
4. Report that the plan was complete and the pages read and checks done to the owner before any edit. If anything fails or cannot be completed, stop before any edit and say exactly what could not be read.

A skipped range, a page that ends early, a page that was never read, a long line that the reader cuts, and a reader that cuts ordinary lines are each detected; `tests/test_start.py::TestReadingProcedure` shows each against real handoffs with a simulated reader, and shows that the round-one rule (the last line number reached equals the line count) accepted a skipped middle and an unread page.
`TestPlanIsVerifiedFirst` cuts the plan text itself, in the middle and at the end, with the handoff intact and the reader faithful: every cut stops the procedure before a file is read, and the round-two procedure, which followed whatever entries it found, accepted each of them.
`TestAnchorsAreLiteralText` prints real handoffs whose page edges and long lines carry a tab, single and doubled backslashes, the document's own `\x09`, `|`, carriage returns and a no-break space, and compares them as defined above; it also shows that comparing the printed form would have stopped a faithful reader.
The number of lines over 1024 characters is bounded (32); beyond that the plan is not established, the preview prints no token and the write step refuses (`start.unreadable`), so the owner can mark such an input as a reference instead of embedding it.

What still depends on live reader behavior, and cannot be established offline:

- the size a single read or a single command output can return, which is not measured here: a long output can arrive cut, which the plan check detects for the plan, and a page of 200 lines has no byte limit, so a reader that returns fewer lines or characters than asked fails the page or the long line, after the bundle is written and after consent;
- how the reader's tool normalises text: whether it shows tabs, carriage returns, trailing white space, non-ASCII and invisible characters as they are, and whether it counts characters or bytes or UTF-16 units, so that it could cut a non-ASCII ordinary line that the ASCII probe does not reveal;
- that the tool's output for the probe is the output it gives for the handoff, which the probe samples at the lengths of the longest ordinary line, not at every line;
- that it keeps its line numbers honest, and does not present blank or altered text for lines whose numbers, start and end still match (a limit of the check, shown in `test_what_the_procedure_cannot_see`);
- that the file is not changed between the write and the read.

A normalisation that makes an anchor differ, or a character the procedure will not guess at, stops the start safely, with the owner told which page or line it was; none of them can make the procedure pass on incomplete text.

## 2. The skill

`.claude/skills/omnitient-start/SKILL.md` is a project-scoped skill.
The owner types `/omnitient-start PACKAGE [--root NAME=DIR]... [--checkout DIR]`.
It is owner-invoked only (`disable-model-invocation: true`).
It pre-approves one tool pattern, `Bash(omnitient start preview *)`, which cannot write.
It does not pre-approve `start handoff`, which writes, or any edit; the session's own permission configuration applies to them, and the skill does not promise that a prompt appears.

The skill chooses a new handoff directory outside the target, runs the preview naming it, and shows the preview, including what is and is not checked about the target and the directory.
It stops on a blocked or refused preview and on an informational one, telling the owner what to do for each reason.
Otherwise it asks the owner whether to start for exactly the displayed package, revision, target, directory, rendered handoff and write scope, and stops if the owner declines, says anything else, or the runtime denies a step, without retrying elsewhere.
Only after the owner's go-ahead it runs `start handoff` with the same directory and the token, follows the reading procedure of section 1.4, and implements.
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
- Not exercised in a live session, because that needs a model: that `/omnitient-start` loads and substitutes its arguments, that the owner is asked and a denial stops the model, how `Bash(omnitient start preview *)` matches commands with pipes or redirections, whether a permission prompt appears for `start handoff` or an edit, that a session's Bash tool starts in its working directory, the reader tool's actual line and page behavior, and whether device and inode numbers are stable on every filesystem (an unstable one would show as a false `start.changed`).
- The static tests establish only what the skill's text says and that the commands behave as above; the reading tests use a simulated reader.

## 4. Acceptance

| Criterion | Tests (`tests/test_start.py`) |
| --- | --- |
| The preview shows identity, revision, target checkout, inputs, directory and write scope, and writes nothing | `TestPreview` |
| An informational preview prints no token: target not the working directory, no or unusable directory, unreadable handoff | `TestNoUsableToken` |
| The handoff is the renderer's READY output, in a new directory outside the target, verified, with a probe | `TestHandoff` |
| An existing file or directory is never reused or removed, and advice is one this command supports | `TestHandoff` |
| Missing or stale approval, unmapped root, drifted context and an open decision block, and hand over no work | `TestBlocked` |
| A change to the package, revision, location, mappings, checkout location or identity, or directory is refused before context is read; a changed rendered handoff is refused before anything is created | `TestChangeSincePreview` |
| An unusable checkout is refused; a root that differs from the target is information | `TestCheckout`, `TestPreview` |
| Host paths and control characters cannot forge lines of any `start` output, including refusals; other commands are unchanged | `TestDisplayIsEscaped` |
| Spaces and shell metacharacters in arguments and package text are never executed | `TestArgumentsAreData` |
| Misuse of the token or options is a usage error | `TestUsage` |
| The reading plan, probe and procedure detect skipped ranges, early ends, unread pages and cut lines | `TestReadingPlan`, `TestReadingProcedure` |
| A plan that arrives cut, in the middle or at the end, or inconsistent with the confirmed preview, stops the procedure before any file is read | `TestPlanIsVerifiedFirst` |
| Anchors are decoded from the `escape_text` notation and compared as literal text, including a backslash, a tab, a carriage return and a character the reader cannot observe | `TestAnchorsAreLiteralText` |
| The skill is project scoped, owner invoked, pre-approves only the preview, names only real flags and states its boundaries and procedure | `TestSkill` |
| Only the shipped skill is tracked under `.claude/` | `TestIgnoreBoundary` |
