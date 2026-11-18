# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Start-of-implementation preview and change check (docs/start-contract.md).

Nothing here touches the filesystem, the clock or the environment. The CLI
resolves the checkout and reads the package; this module only words what the
owner is shown and compares what they were shown with what is there now.
Readiness is never decided here: it comes from :mod:`omnitient.readiness`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from omnitient.errors import Diagnostic
from omnitient.model import HandoffStage, WorkPackage
from omnitient.overview_render import escape_text
from omnitient.readiness import Readiness

START_STAGE: HandoffStage = "implementation"

HANDOFF_BEGIN = "=== handoff-implementation.md begin ==="
HANDOFF_END = "=== handoff-implementation.md end ==="


@dataclass(frozen=True)
class Checkout:
    """The resolved target directory and the one fact read from it."""

    path: Path
    git_marker: bool


@dataclass(frozen=True)
class Shown:
    """What a preview showed, and what a later start must still find."""

    revision: str
    file_sha256: str
    checkout: Path


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
    return lines or ["matches no mapped root"]


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


def _scope_lines(pkg: WorkPackage, report: Readiness) -> list[str]:
    if not report.ready:
        # A blocked package must not hand over work an agent could act on.
        return ["proposed write scope: withheld while the package is blocked"]
    return [
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


def preview_lines(
    pkg: WorkPackage,
    digest: str,
    report: Readiness,
    *,
    package_file: Path,
    file_sha256: str,
    checkout: Checkout,
    roots: Mapping[str, Path],
) -> list[str]:
    """The owner-facing summary, one fact per line; package text is escaped."""
    status = (
        "READY"
        if report.ready
        else f"BLOCKED ({len(report.blockers)} blocker(s)); no handoff is produced"
    )
    inside = package_file.is_relative_to(checkout.path)
    return [
        f"omnitient start: {START_STAGE} preview",
        f"status: {status}",
        f"package: {escape_text(pkg.package_id)}",
        f"title: {escape_text(pkg.title)}",
        f"declared stage: {pkg.stage}",
        f"revision: {digest}",
        f"{START_STAGE} approval: {approval_line(pkg, digest)}",
        f"package file: {package_file}",
        f"package file sha256: {file_sha256}",
        f"target checkout: {checkout.path}",
        f"checkout git marker: {'present' if checkout.git_marker else 'absent'}",
        f"checkout relation to mapped roots: {'; '.join(root_relations(checkout.path, roots))}",
        f"package file inside target checkout: {'yes' if inside else 'no'}",
        *_scope_lines(pkg, report),
        *_diagnostics("blockers", report.blockers),
        *_diagnostics("warnings", report.warnings),
    ]


def changes_since_preview(
    expected: Shown, *, revision: str, file_sha256: str, checkout: Path
) -> list[str]:
    """What differs from the preview the owner saw; empty when nothing does."""
    found: list[str] = []
    if revision != expected.revision:
        found.append(f"revision was {expected.revision} and is now {revision}")
    if file_sha256 != expected.file_sha256:
        found.append(
            f"package file sha256 was {expected.file_sha256} and is now {file_sha256}"
        )
    if checkout != expected.checkout:
        found.append(f"target checkout was {expected.checkout} and is now {checkout}")
    return found
