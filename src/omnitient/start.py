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

PREVIEW_TAG = "omnitient-start-1"
_HEX64 = re.compile(r"[0-9a-f]{64}")

# What a preview token covers, in token order, as the owner is told about a change.
FACT_NAMES = (
    "revision",
    "package file contents",
    "package file location",
    "root mappings",
    "target checkout location",
    "target checkout identity",
)


@dataclass(frozen=True)
class Checkout:
    """The resolved target directory and what was read from it."""

    path: Path
    device: int
    inode: int
    git_marker: bool
    is_working_directory: bool


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

    def changed_from(self, shown: Facts) -> list[str]:
        """Names of the facts that differ from the ones previewed."""
        return [
            name
            for name, now, then in zip(
                FACT_NAMES, self.values, shown.values, strict=True
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
) -> Facts:
    return Facts(
        (
            revision,
            file_sha256,
            _path_id(package_file),
            roots_id(roots),
            _path_id(checkout.path),
            sha256_hex(f"{checkout.device}:{checkout.inode}".encode()),
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
    token: str | None,
    handoff_bytes: int | None,
) -> list[str]:
    """The owner-facing summary, one fact per line; every host path is escaped."""
    status = (
        "READY"
        if report.ready
        else f"BLOCKED ({len(report.blockers)} blocker(s)); no handoff is produced"
    )
    inside = package_file.is_relative_to(checkout.path)
    here = checkout.is_working_directory
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
        (
            "target checkout is the working directory of this command "
            f"and so of the session: {'yes' if here else 'NO'}"
        ),
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
        *_scope_lines(pkg, report, resolved),
        *_diagnostics("blockers", report.blockers),
        *_diagnostics("warnings", report.warnings),
    ]
    if report.ready and handoff_bytes is not None:
        lines.append(f"handoff renders: {handoff_bytes} bytes")
    if report.ready and not here:
        lines.append(
            "this target is not the session's working directory, so implementation "
            "does not start from this session: open a session there and preview again"
        )
    if report.ready and token is not None:
        lines.append(f"preview token: {token}")
    return lines
