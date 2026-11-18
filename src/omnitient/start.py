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
_HEX64 = re.compile(r"[0-9a-f]{64}")

# What a preview token covers, in token order, as the owner is told about a change.
# The first EARLY facts are known before any context file is read; the last is the
# document those reads produce.
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
EARLY = len(FACT_NAMES) - 1


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
    def token(self) -> str:
        return ".".join((PREVIEW_TAG, *self.values))

    def changed_from(self, shown: Facts, *, early_only: bool = False) -> list[str]:
        """Names of the facts that differ from the ones previewed.

        ``early_only`` compares the facts known before any context is read.
        """
        count = EARLY if early_only else len(FACT_NAMES)
        return [
            name
            for name, now, then in zip(
                FACT_NAMES[:count],
                self.values[:count],
                shown.values[:count],
                strict=True,
            )
            if now != then
        ]


def _path_id(path: Path) -> str:
    return sha256_hex(os.fsencode(path))


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
) -> Facts:
    """The facts of a preview; without ``document`` the last one is empty."""
    return Facts(
        (
            revision,
            file_sha256,
            _path_id(package_file),
            roots_id(roots),
            _path_id(checkout.path),
            sha256_hex(f"{checkout.device}:{checkout.inode}".encode()),
            _path_id(destination),
            sha256_hex(document) if document is not None else "",
        )
    )


def parse_token(text: str) -> Facts:
    """The facts a token carries; ``ValueError`` when it is not a preview token."""
    prefix, *values = text.split(".")
    if (
        prefix != PREVIEW_TAG
        or len(values) != len(FACT_NAMES)
        or not all(_HEX64.fullmatch(v) for v in values)
    ):
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
    pkg: WorkPackage, report: Readiness, resolved: Sequence[ResolvedContext]
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
            "(read only), commits, pushes and publication"
        ),
        (
            "  enforcement: none by omnitient; the session's permissions and any "
            "sandbox decide what can be written, and a Git directory alone is no "
            "isolation"
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
) -> list[str]:
    """The owner-facing summary, one fact per line; every host path is escaped."""
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
            "checkout checks: path, directory identity and a .git entry only; "
            "the branch, HEAD and local modifications are not observed"
        ),
        f"checkout git entry: {'present' if checkout.git_marker else 'absent'}",
        (
            "checkout relation to mapped roots (information only; context roots "
            f"may differ from the target): {'; '.join(root_relations(checkout.path, roots))}"
        ),
        f"package file inside target checkout: {'yes' if inside else 'no'}",
        f"handoff directory: {where}",
        *_scope_lines(pkg, report, resolved),
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
