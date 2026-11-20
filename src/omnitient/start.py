# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Start-of-implementation preview and change check (docs/start-contract.md).

Nothing here touches the filesystem, the clock or the environment. The CLI
resolves the checkout and reads the package; this module only words what the
owner is shown and compares what they were shown with what is there now.
Readiness is never decided here: it comes from :mod:`omnitient.readiness`.

What the owner sees (escaped, for reading) and what is compared (hashes of the
exact bytes, for machines) are kept apart on purpose: a display form must never
be the thing that is compared, and a compared value must never be displayed raw.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from omnitient.context import ResolvedContext
from omnitient.errors import Diagnostic
from omnitient.model import HandoffStage, WorkPackage
from omnitient.overview_render import escape_text
from omnitient.readiness import Readiness
from omnitient.serialize import sha256_hex

START_STAGE: HandoffStage = "implementation"
HANDOFF_FILE = f"handoff-{START_STAGE}.md"
PROBE_FILE = f"handoff-{START_STAGE}.reader-probe.txt"

PREVIEW_TAG = "omnitient-start-2"
# Commit mode adds Git facts to the same eight, so its token has its own tag and a
# token of one mode can never be taken for the other.
COMMIT_TAG = "omnitient-start-3"
MODES = ("edit", "commit")
_HEX64 = re.compile(r"[0-9a-f]{64}")

# What a preview token covers, in token order, as the owner is told about a change.
# All but the rendered handoff are known before any context file is read; that one
# is the document those reads produce. Commit mode appends its Git facts.
FACT_NAMES = (
    "revision",
    "package file contents",
    "package file location",
    "root mappings",
    "target checkout location",
    "target checkout identity",
    "handoff directory",
    "rendered handoff",
)
RENDERED = len(FACT_NAMES) - 1  # the only fact that needs context to be read
GIT_FACT_NAMES = ("checkout git state", "git base commit", "branch and operations")
COMMIT_FACT_NAMES = FACT_NAMES + GIT_FACT_NAMES


@dataclass(frozen=True)
class Checkout:
    """The resolved target directory and what was read from it."""

    path: Path
    device: int
    inode: int
    git_marker: bool
    is_working_directory: bool


@dataclass(frozen=True)
class Destination:
    """The proposed directory for the handoff and why it cannot be used, if so."""

    path: Path
    problem: str | None


@dataclass(frozen=True)
class Facts:
    """Fingerprints of everything a preview showed, taken when it was shown.

    Each field is a SHA-256 of exact bytes (paths as the filesystem encodes
    them), so a later start compares what was confirmed, never a re-resolved
    path. Nothing here enters a manifest or an approval digest.
    """

    values: tuple[str, ...]

    @property
    def commit_mode(self) -> bool:
        return len(self.values) == len(COMMIT_FACT_NAMES)

    @property
    def token(self) -> str:
        return ".".join((COMMIT_TAG if self.commit_mode else PREVIEW_TAG, *self.values))

    def changed_from(self, shown: Facts, *, early_only: bool = False) -> list[str]:
        """Names of the facts that differ from the ones previewed.

        ``early_only`` leaves out the rendered handoff, the one fact that needs
        context files to be read. A token of the other mode differs as a whole.
        """
        if self.commit_mode != shown.commit_mode:
            return ["mode"]
        names = COMMIT_FACT_NAMES if self.commit_mode else FACT_NAMES
        return [
            name
            for i, (name, now, then) in enumerate(
                zip(names, self.values, shown.values, strict=True)
            )
            if now != then and not (early_only and i == RENDERED)
        ]

    def git_changed_from(self, shown: Facts) -> list[str]:
        """Names of the Git facts that differ; every fact of a token of the wrong mode."""
        if not (self.commit_mode and shown.commit_mode):
            return ["mode"]
        n = len(FACT_NAMES)
        return [
            name
            for name, now, then in zip(
                GIT_FACT_NAMES, self.values[n:], shown.values[n:], strict=True
            )
            if now != then
        ]


def _path_id(path: Path) -> str:
    return sha256_hex(os.fsencode(path))


def checkout_ids(checkout: Checkout) -> tuple[str, str]:
    """Fingerprints of the target's location and of its identity (device and inode)."""
    return (
        _path_id(checkout.path),
        sha256_hex(f"{checkout.device}:{checkout.inode}".encode()),
    )


def roots_id(roots: Mapping[str, Path]) -> str:
    entries = [
        name.encode() + b"\0" + os.fsencode(path)
        for name, path in sorted(roots.items())
    ]
    return sha256_hex(b"\0".join(entries))


def gather_facts(
    *,
    revision: str,
    file_sha256: str,
    package_file: Path,
    roots: Mapping[str, Path],
    checkout: Checkout,
    destination: Path,
    document: bytes | None = None,
    git: tuple[str, str, str] | None = None,
) -> Facts:
    """The facts of a preview; without ``document`` the rendered one is empty.

    ``git`` is the three Git facts of commit mode (:func:`git_facts`).
    """
    return Facts(
        (
            revision,
            file_sha256,
            _path_id(package_file),
            roots_id(roots),
            *checkout_ids(checkout),
            _path_id(destination),
            sha256_hex(document) if document is not None else "",
            *(git or ()),
        )
    )


def parse_token(text: str) -> Facts:
    """The facts a token carries; ``ValueError`` when it is not a preview token."""
    prefix, *values = text.split(".")
    sizes = {PREVIEW_TAG: len(FACT_NAMES), COMMIT_TAG: len(COMMIT_FACT_NAMES)}
    if sizes.get(prefix) != len(values) or not all(_HEX64.fullmatch(v) for v in values):
        raise ValueError("not a preview token printed by 'omnitient start preview'")
    return Facts(tuple(values))


def root_relations(checkout: Path, roots: Mapping[str, Path]) -> list[str]:
    """How the checkout relates to each mapped root (paths are already resolved)."""
    lines: list[str] = []
    for name, root in sorted(roots.items()):
        if root == checkout:
            lines.append(f"is the mapped root '{name}'")
        elif checkout.is_relative_to(root):
            lines.append(f"lies inside the mapped root '{name}'")
        elif root.is_relative_to(checkout):
            lines.append(f"contains the mapped root '{name}'")
    return lines or ["none"]


def approval_line(pkg: WorkPackage, digest: str) -> str:
    for_stage = [a for a in pkg.approvals if a.stage == START_STAGE]
    current = [a for a in for_stage if a.content_digest == digest]
    if current:
        a = current[-1]
        return (
            f"current ({a.id}, {START_STAGE}) by {escape_text(a.approved_by)} "
            f"at {a.approved_at}"
        )
    if for_stage:
        return "stale: recorded for an earlier revision"
    return "none recorded"


def _items(items: Sequence[str]) -> list[str]:
    return [f"    - {escape_text(item)}" for item in items] or ["    (none)"]


def _path(path: Path) -> str:
    return escape_text(str(path))


def input_lines(resolved: Sequence[ResolvedContext]) -> list[str]:
    """Which declared inputs the handoff embeds, by id, logical path and mode."""
    lines = []
    for item in resolved:
        ref = item.ref
        if item.text is not None:
            fate = "embedded"
        elif item.status == "ok":
            fate = "referenced (path and digest only)"
        else:
            fate = f"not embedded ({item.status})"
        need = "required" if ref.required else "optional"
        lines.append(
            f"    - {ref.id} {escape_text(f'{ref.root}:{ref.path}')} "
            f"{ref.mode} {need}: {fate}"
        )
    return lines or ["    (none)"]


def _scope_lines(
    pkg: WorkPackage,
    report: Readiness,
    resolved: Sequence[ResolvedContext],
    *,
    commit_mode: bool = False,
) -> list[str]:
    if not report.ready:
        # A blocked package must not hand over work an agent could act on.
        return ["proposed write scope: withheld while the package is blocked"]
    return [
        "declared inputs (contents are not shown here):",
        *input_lines(resolved),
        "proposed write scope:",
        "  may be written: files under the target checkout, as the package scope needs",
        "  package scope:",
        *_items(pkg.scope),
        "  package exclusions (not to be changed):",
        *_items(pkg.exclusions),
        (
            "  not in the scope: the package file with its approvals and evidence, "
            "anything outside the target checkout, mapped roots that lie outside it "
            "(read only), "
            + (
                "any commit but the one local commit listed under the Git operations, "
                if commit_mode
                else "commits, "
            )
            + "pushes and publication"
        ),
        (
            "  enforcement: none by omnitient; the session's permissions and any "
            "sandbox decide what can be written, and a Git directory alone is no "
            "isolation"
        ),
    ]


@dataclass(frozen=True)
class GitObservation:
    """What read-only Git questions answered about one checkout, and about the
    base and branch a commit-mode start was asked for.

    Everything is data: no field is an instruction, and a field that could not be
    observed is empty and says why in ``error`` or a ``*_problem``.
    """

    checkout: Path
    base_ref: str
    branch_name: str
    error: str | None = None  # Git could not be asked about this checkout at all
    branch: str | None = None  # None: detached
    head: str | None = None
    head_tree: str | None = None
    staged: tuple[str, ...] = ()
    unstaged: tuple[str, ...] = ()
    untracked: tuple[str, ...] = ()
    unmerged: tuple[str, ...] = ()
    in_progress: tuple[str, ...] = ()
    base: str | None = None
    base_tree: str | None = None
    base_problem: str | None = None
    branch_problem: str | None = None
    branch_id: str | None = None  # the commit an existing local branch points at


@dataclass(frozen=True)
class CommitPlan:
    """The branch action a commit-mode start would take, or why it cannot start."""

    action: str  # create, switch, stay or none
    problems: tuple[str, ...]
    files_change: (
        bool  # switching replaces tracked files: HEAD's tree is not the base's
    )

    @property
    def startable(self) -> bool:
        return not self.problems


def _count(paths: Sequence[str]) -> str:
    shown = ", ".join(escape_text(p) for p in paths[:5])
    more = f", and {len(paths) - 5} more" if len(paths) > 5 else ""
    return f"{len(paths)} path(s): {shown}{more}"


def plan_commit(
    obs: GitObservation, roots: Mapping[str, Path] | None = None
) -> CommitPlan:
    """Decide the branch action and every reason a commit-mode start is withheld.

    Pure: only the observation and the mapped roots are read. A dirty checkout, an
    unresolved base and a conflicting branch each withhold the start; none is
    repaired or worked around here.
    """
    problems: list[str] = []
    if obs.error:
        return CommitPlan(
            "none",
            (f"Git cannot be asked about the checkout: {escape_text(obs.error)}",),
            False,
        )
    if obs.unmerged:
        problems.append(f"the checkout has unmerged paths ({_count(obs.unmerged)})")
    if obs.in_progress:
        problems.append(
            "a Git operation is in progress in the checkout: "
            + ", ".join(obs.in_progress)
        )
    for label, paths in (
        ("staged changes", obs.staged),
        ("unstaged changes", obs.unstaged),
        ("untracked files", obs.untracked),
    ):
        if paths:
            problems.append(
                f"the checkout is not clean, it has {label} ({_count(paths)})"
            )
    if obs.head is None:
        problems.append("HEAD does not name a commit")
    if obs.base_problem:
        problems.append(f"the git base is unresolved: {escape_text(obs.base_problem)}")
    if obs.branch_problem:
        problems.append(
            f"the feature branch is unusable: {escape_text(obs.branch_problem)}"
        )
    action = "none"
    if obs.base is not None and not obs.branch_problem:
        if obs.branch_id is None:
            action = "create"
        elif obs.branch_id == obs.base:
            action = "stay" if obs.branch == obs.branch_name else "switch"
        else:
            problems.append(
                f"the feature branch '{escape_text(obs.branch_name)}' already exists "
                f"and points at {obs.branch_id}, not at the git base {obs.base}"
            )
    files_change = bool(
        obs.head_tree and obs.base_tree and obs.head_tree != obs.base_tree
    )
    if files_change and action != "none":
        overlap = [
            name
            for name, root in sorted((roots or {}).items())
            if root_relations(obs.checkout, {name: root}) != ["none"]
        ]
        if overlap:
            problems.append(
                "the tracked files of the checkout would change when the branch is "
                "entered (HEAD's tree is not the git base's), and the mapped root(s) "
                + ", ".join(repr(n) for n in overlap)
                + " are or overlap the checkout, so the handoff would describe files "
                "that are about to be replaced"
            )
    return CommitPlan(action, tuple(problems), files_change)


OPERATIONS_VERSION = "operations-1"


def commit_operations(obs: GitObservation, plan: CommitPlan) -> list[str]:
    """The Git operations a go-ahead authorizes, in order. Shown and fingerprinted."""
    branch, base = escape_text(obs.branch_name), obs.base or "(unresolved)"
    first = {
        "create": (
            f"create the local branch '{branch}' at the git base commit {base} and "
            "switch to it (git switch -c)"
        ),
        "switch": (
            f"switch to the existing local branch '{branch}', which already points at "
            f"the git base commit {base} (git switch)"
        ),
        "stay": (
            f"stay on the checked-out local branch '{branch}', which already points "
            f"at the git base commit {base} (no branch change)"
        ),
    }.get(plan.action, "no branch action: the start is withheld")
    return [
        first,
        (
            "stage by explicit path only the files the package scope needs, and "
            "inspect the staged diff (never a whole-tree add)"
        ),
        (
            "run the package's checks and the repository's formatters and hooks; "
            "when they fail or rewrite files, inspect what they changed, repair "
            "in-scope failures, rerun the affected checks on the resulting content "
            "and restage only the intended paths, repeating with hooks enabled"
        ),
        (
            "make exactly one local commit on that branch with hooks enabled "
            "(no --no-verify, no --amend, no second commit)"
        ),
    ]


COMMIT_NOT_AUTHORIZED = (
    "a push, fetch, pull request or any publication; a merge, rebase, reset, "
    "stash, tag, amend or force; deleting, moving or switching to any other "
    "branch; changing Git configuration; bypassing a hook; a commit in another "
    "repository; recording an approval; and anything that would disturb work that "
    "is not this package's (the exact blocker is reported and the state kept)"
)


def git_facts(obs: GitObservation, plan: CommitPlan) -> tuple[str, str, str]:
    """Fingerprints of the Git facts shown, taken from the exact bytes observed."""

    def enc(*parts: object) -> bytes:
        return b"\0".join(
            os.fsencode(str(p)) if not isinstance(p, bytes) else p for p in parts
        )

    state = enc(
        obs.checkout,
        obs.error or "",
        obs.branch or "",
        obs.head or "",
        obs.head_tree or "",
        "|".join(obs.staged),
        "|".join(obs.unstaged),
        "|".join(obs.untracked),
        "|".join(obs.unmerged),
        "|".join(obs.in_progress),
        obs.branch_id or "",
        obs.branch_problem or "",
    )
    base = enc(
        obs.base_ref, obs.base or "", obs.base_tree or "", obs.base_problem or ""
    )
    operations = enc(
        OPERATIONS_VERSION,
        obs.branch_name,
        plan.action,
        obs.base or "",
        *commit_operations(obs, plan),
        COMMIT_NOT_AUTHORIZED,
    )
    return sha256_hex(state), sha256_hex(base), sha256_hex(operations)


def _paths(label: str, paths: Sequence[str]) -> str:
    return f"{label}: " + (_count(paths) if paths else "none")


def commit_lines(
    obs: GitObservation, plan: CommitPlan, digest: str, *, branch_ref: str
) -> list[str]:
    """What the owner is shown before consent in commit mode, one fact per line.

    The package revision and the Git base are separate lines with separate
    meanings; neither stands in for the other.
    """
    head = obs.head or "none (no commit, or Git could not be asked)"
    if obs.error:
        branch = "unknown"
    else:
        branch = escape_text(obs.branch) if obs.branch else "none (detached HEAD)"
    if obs.branch_id is None:
        state = "does not exist"
    elif obs.branch_id == obs.base:
        state = "exists and points at the git base commit"
    else:
        state = f"exists and points at {obs.branch_id}"
    switch = (
        "unknown"
        if not (obs.head_tree and obs.base_tree)
        else (
            "yes, the tracked files become the git base's"
            if plan.files_change
            else "no, HEAD and the git base have the same tree"
        )
    )
    return [
        (
            "git commit mode: the session, not omnitient, performs every Git write; "
            "this preview writes nothing"
        ),
        f"package revision: {digest} (content digest of the work package; not a Git object)",
        f"git base ref: {escape_text(obs.base_ref)}",
        (
            f"git base commit: {obs.base} (a Git commit, not the package revision; "
            "available in this checkout)"
            if obs.base
            else "git base commit: UNRESOLVED"
        ),
        f"git work tree: {_path(obs.checkout)}",
        f"observed branch: {branch}",
        f"observed HEAD: {head}",
        _paths("staged changes", obs.staged),
        _paths("unstaged changes", obs.unstaged),
        _paths("untracked files", obs.untracked),
        f"unmerged paths: {_count(obs.unmerged) if obs.unmerged else 'none'}",
        "git operation in progress: "
        + (", ".join(obs.in_progress) if obs.in_progress else "none"),
        f"feature branch: {escape_text(branch_ref)}",
        f"feature branch state: {state}",
        f"proposed branch action: {plan.action}",
        f"tracked files change when the branch is entered: {switch}",
        "operations a go-ahead authorizes, in this order, and only these:",
        *(f"  {n}. {text}" for n, text in enumerate(commit_operations(obs, plan), 1)),
        f"not authorized: {COMMIT_NOT_AUTHORIZED}",
        (
            "git commit mode: STARTABLE, subject to a recheck of these facts "
            "immediately before the first Git write"
            if plan.startable
            else f"git commit mode: WITHHELD ({len(plan.problems)} reason(s))"
        ),
    ]


def _diagnostics(title: str, items: Sequence[Diagnostic]) -> list[str]:
    if not items:
        return []
    return [f"{title}:", *(f"  {escape_text(d.format())}" for d in items)]


PAGE_LINES = 200
PROBE_LENGTHS = (64, 128, 256, 512, 1024)
ORDINARY_MAX = PROBE_LENGTHS[-1]
MAX_LONG_LINES = 32
ANCHOR = 16


@dataclass(frozen=True)
class Page:
    number: int
    first: int
    last: int
    head: str
    tail: str


@dataclass(frozen=True)
class LongLine:
    number: int
    length: int
    tail: str


@dataclass(frozen=True)
class ReadingPlan:
    """What a reader must see to show it received the whole stored document.

    This describes the bytes on disk. It cannot prove what a reader's tool
    presented; it gives a reader things to check that skipped ranges and cut
    lines would break (see docs/start-contract.md section 1.4).
    """

    lines: int
    pages: tuple[Page, ...]
    long_lines: tuple[LongLine, ...]
    probe_lines: int  # probe lines 1..probe_lines must be shown whole

    @property
    def establishable(self) -> bool:
        return len(self.long_lines) <= MAX_LONG_LINES


def reading_plan(document: bytes) -> ReadingPlan:
    lines = document.decode("utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    pages = tuple(
        Page(
            number=i // PAGE_LINES + 1,
            first=i + 1,
            last=min(i + PAGE_LINES, len(lines)),
            head=lines[i][:ANCHOR],
            tail=lines[min(i + PAGE_LINES, len(lines)) - 1][-ANCHOR:],
        )
        for i in range(0, len(lines), PAGE_LINES)
    )
    long_lines = tuple(
        LongLine(n, len(line), line[-ANCHOR:])
        for n, line in enumerate(lines, start=1)
        if len(line) > ORDINARY_MAX
    )
    longest = max((len(line) for line in lines if len(line) <= ORDINARY_MAX), default=0)
    needed = next(
        (i for i, n in enumerate(PROBE_LENGTHS, 1) if n >= longest), len(PROBE_LENGTHS)
    )
    return ReadingPlan(len(lines), pages, long_lines, needed)


def probe_marker(length: int) -> str:
    return f"-END-{length}"


def probe_document() -> bytes:
    """Lines of known lengths, each ending in a marker a cut line would lose."""
    out = []
    for n in PROBE_LENGTHS:
        head, tail = f"PROBE-{n}-", probe_marker(n)
        fill = ("0123456789" * (n // 10 + 1))[: n - len(head) - len(tail)]
        out.append(head + fill + tail)
    return ("\n".join(out) + "\n").encode()


def _anchor(text: str) -> str:
    return f"|{escape_text(text)}|"


def plan_summary(plan: ReadingPlan) -> str:
    if not plan.establishable:
        return (
            f"cannot be established: {len(plan.long_lines)} lines exceed "
            f"{ORDINARY_MAX} characters and the limit is {MAX_LONG_LINES}"
        )
    return (
        f"{plan.lines} lines in {len(plan.pages)} pages of at most {PAGE_LINES}, "
        f"{len(plan.long_lines)} line(s) over {ORDINARY_MAX} characters"
    )


def plan_lines(plan: ReadingPlan) -> list[str]:
    """The plan as printed after delivery: one fact per line, anchors delimited by |."""
    return [
        f"reading plan: {plan_summary(plan)}",
        (
            "reader probe: probe file lines 1 to "
            f"{plan.probe_lines} must each be shown whole"
        ),
        *(
            f"  probe line {i}: {n} characters, ends {_anchor(probe_marker(n))}"
            for i, n in enumerate(PROBE_LENGTHS[: plan.probe_lines], 1)
        ),
        *(
            (
                f"page {pg.number}: lines {pg.first}-{pg.last}, "
                f"first line starts {_anchor(pg.head)}, last line ends {_anchor(pg.tail)}"
            )
            for pg in plan.pages
        ),
        *(
            f"long line {ll.number}: {ll.length} characters, ends {_anchor(ll.tail)}"
            for ll in plan.long_lines
        ),
    ]


def withheld_reasons(
    checkout: Checkout, destination: Destination | None, plan: ReadingPlan
) -> list[str]:
    """Why a ready preview must not print a token; empty when it may."""
    reasons = []
    if not checkout.is_working_directory:
        reasons.append(
            "the target is not the working directory of this command, so "
            "implementation does not start from here: run the preview from the "
            "target directory"
        )
    if destination is None:
        reasons.append("no handoff directory was proposed with --out")
    elif destination.problem:
        reasons.append(f"the proposed handoff directory {destination.problem}")
    if not plan.establishable:
        reasons.append(
            "the handoff cannot be read under the bounded reading procedure "
            f"({plan_summary(plan)})"
        )
    return reasons


def preview_lines(
    pkg: WorkPackage,
    digest: str,
    report: Readiness,
    *,
    package_file: Path,
    file_sha256: str,
    checkout: Checkout,
    roots: Mapping[str, Path],
    resolved: Sequence[ResolvedContext],
    destination: Destination | None,
    document: bytes | None,
    plan: ReadingPlan | None,
    token: str | None,
    withheld: Sequence[str],
    git_lines: Sequence[str] | None = None,
) -> list[str]:
    """The owner-facing summary, one fact per line; every host path is escaped.

    ``git_lines`` is given in commit mode only (:func:`commit_lines`).
    """
    commit_mode = git_lines is not None
    status = (
        "READY"
        if report.ready
        else f"BLOCKED ({len(report.blockers)} blocker(s)); no handoff is produced"
    )
    inside = package_file.is_relative_to(checkout.path)
    here = checkout.is_working_directory
    if destination is None:
        where = "none proposed (--out)"
    else:
        state = destination.problem and f"NOT USABLE, it {destination.problem}"
        where = f"{_path(destination.path)} ({state or 'new, outside the target'})"
    lines = [
        f"omnitient start: {START_STAGE} preview",
        f"status: {status}",
        f"package: {escape_text(pkg.package_id)}",
        f"title: {escape_text(pkg.title)}",
        f"declared stage: {pkg.stage}",
        f"revision: {digest}",
        f"{START_STAGE} approval: {approval_line(pkg, digest)}",
        f"package file: {_path(package_file)}",
        f"package file sha256: {file_sha256}",
        "mapped roots:",
        *(f"    {name} = {_path(path)}" for name, path in sorted(roots.items())),
        f"target checkout: {_path(checkout.path)}",
        f"target checkout identity: device {checkout.device}, inode {checkout.inode}",
        f"target checkout is the working directory of this command: {'yes' if here else 'NO'}",
        (
            "checkout checks: path, directory identity, a .git entry and the Git "
            "state listed under the git commit mode lines"
            if commit_mode
            else "checkout checks: path, directory identity and a .git entry only; "
            "the branch, HEAD and local modifications are not observed"
        ),
        f"checkout git entry: {'present' if checkout.git_marker else 'absent'}",
        (
            "checkout relation to mapped roots (information only; context roots "
            f"may differ from the target): {'; '.join(root_relations(checkout.path, roots))}"
        ),
        f"package file inside target checkout: {'yes' if inside else 'no'}",
        f"handoff directory: {where}",
        *(git_lines or ()),
        *_scope_lines(pkg, report, resolved, commit_mode=commit_mode),
        *_diagnostics("blockers", report.blockers),
        *_diagnostics("warnings", report.warnings),
    ]
    if report.ready and document is not None and plan is not None:
        lines += [
            f"handoff renders: {len(document)} bytes, sha256 {sha256_hex(document)}",
            f"reading plan: {plan_summary(plan)}",
        ]
    if report.ready:
        lines += [f"no usable handoff token: {reason}" for reason in withheld]
    if report.ready and token is not None and not withheld:
        lines.append(f"preview token: {token}")
    return lines
