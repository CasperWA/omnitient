# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Read-only overview of the packages in one explicit collection directory.

Each package is assessed with the O1 engine (``resolve_context`` and
``readiness.evaluate_all``) one at a time, so resolved context is released
before the next package is read.
This module builds a result; ``overview_render`` turns it into bytes or text and
``cli`` prints it. See docs/o2-overview-contract.md.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from omnitient import readiness
from omnitient.collection import (
    Candidate,
    CollectionRefused,
    Inventory,
    Probe,
    open_collection,
    probe_child,
    take_inventory,
)
from omnitient.context import HostMapping, build_host_mapping, resolve_context
from omnitient.errors import Diagnostic, OmnitientError
from omnitient.model import HANDOFF_STAGES, HandoffStage, Stage, WorkPackage
from omnitient.schema import parse_package
from omnitient.serialize import content_digest

SCHEMA = "omnitient/overview"
SCHEMA_VERSION = 1

EntryStatus = Literal["valid", "invalid", "excluded", "unassessed"]
ResultsStatus = Literal["complete", "partial", "omitted"]

# Fixed order of the advisory action categories.
ACTION_ORDER: tuple[str, ...] = (
    "inspect_package",
    "resolve_decision",
    "review_context",
    "supply_context",
    "seek_approval",
    "inspect_diagnostic",
)
HANDOFF_READY = "handoff_ready"

_ACTION_FOR_CODE = {
    "content.incomplete": "inspect_package",
    "decision.unresolved": "resolve_decision",
    "decision.open": "resolve_decision",
    "context.digest_mismatch": "review_context",
    "context.root_unmapped": "supply_context",
    "context.missing": "supply_context",
    "approval.missing": "seek_approval",
    "approval.stale": "seek_approval",
}

# A probe that is neither a package nor excluded/unassessed failed while reading.
_STATUS_FOR_KIND: dict[str, EntryStatus] = {
    "excluded": "excluded",
    "unassessed": "unassessed",
}

_ROOTS_MESSAGES = {
    "roots.duplicate": "a root name is mapped more than once; check --root and --roots-file",
    "roots.invalid": "the host mapping is invalid; check --root and --roots-file",
}


@dataclass(frozen=True)
class Reference:
    """A diagnostic an action points at: its code and its location (or ``None``)."""

    code: str
    location: str | None


@dataclass(frozen=True)
class Action:
    code: str
    references: tuple[Reference, ...]


@dataclass(frozen=True)
class StageReport:
    stage: HandoffStage
    ready: bool
    blockers: tuple[Diagnostic, ...]
    warnings: tuple[Diagnostic, ...]
    actions: tuple[Action, ...]


@dataclass(frozen=True)
class PackageSummary:
    id: str
    title: str
    declared_stage: Stage
    content_digest: str


@dataclass(frozen=True)
class Entry:
    path: str
    status: EntryStatus
    package: PackageSummary | None
    diagnostics: tuple[Diagnostic, ...]
    readiness: tuple[StageReport, ...] | None


@dataclass(frozen=True)
class Counts:
    collection_entries: int
    ignored: int
    candidates: int
    valid: int
    invalid: int
    excluded: int
    unassessed: int


@dataclass(frozen=True)
class Overview:
    discovery_complete: bool
    assessment_complete: bool
    results_status: ResultsStatus
    counts: Counts | None
    diagnostics: tuple[Diagnostic, ...]
    entries: tuple[Entry, ...]

    @property
    def succeeded(self) -> bool:
        """True when nothing needs the command to fail (exit 0).

        Blocked readiness never fails the command; an error diagnostic, an entry
        that is not valid, or incomplete coverage does.
        """
        return (
            self.results_status == "complete"
            and self.discovery_complete
            and self.assessment_complete
            and not any(d.severity == "error" for d in self.diagnostics)
            and all(
                e.status == "valid"
                and not any(d.severity == "error" for d in e.diagnostics)
                for e in self.entries
            )
        )


def refused(
    diagnostic: Diagnostic,
    *,
    discovery_complete: bool = False,
    assessment_complete: bool = False,
) -> Overview:
    """An overview whose entries were withheld, with one explaining diagnostic."""
    return Overview(
        discovery_complete,
        assessment_complete,
        "omitted",
        None,
        (diagnostic,),
        (),
    )


def _location(text: str) -> str | None:
    return text or None


def classify_action(code: str) -> str:
    return _ACTION_FOR_CODE.get(code, "inspect_diagnostic")


def build_actions(
    blockers: Sequence[Diagnostic], warnings: Sequence[Diagnostic]
) -> tuple[Action, ...]:
    """Advisory actions for one stage, in the fixed category order.

    Identical references are listed once per category; the diagnostics they come
    from are not deduplicated. A stage without blockers ends with ``handoff_ready``.
    """
    refs: dict[str, dict[Reference, None]] = defaultdict(dict)
    for diag in (*blockers, *warnings):
        refs[classify_action(diag.code)][
            Reference(diag.code, _location(diag.location))
        ] = None
    actions = [Action(c, tuple(refs[c])) for c in ACTION_ORDER if c in refs]
    if not blockers:
        actions.append(Action(HANDOFF_READY, ()))
    return tuple(actions)


def _normalize(diag: Diagnostic, pkg: WorkPackage) -> Diagnostic:
    """Replace the one O1 message that quotes operating-system error text."""
    if diag.code != "context.unreadable":
        return diag
    ref = next((c for c in pkg.context if c.id == diag.location), None)
    prefix = f"{ref.root}:{ref.path}: " if ref else ""
    return Diagnostic(
        diag.code,
        diag.severity,
        f"{prefix}the path or file could not be read",
        diag.location,
    )


def _stage_report(report: readiness.Readiness, pkg: WorkPackage) -> StageReport:
    blockers = tuple(_normalize(d, pkg) for d in report.blockers)
    warnings = tuple(_normalize(d, pkg) for d in report.warnings)
    return StageReport(
        report.stage,
        report.ready,
        blockers,
        warnings,
        build_actions(blockers, warnings),
    )


def _assess(
    path: str, raw: bytes, mapping: HostMapping
) -> tuple[Entry, WorkPackage | None]:
    result = parse_package(raw)
    if result.package is None:
        return Entry(path, "invalid", None, tuple(result.diagnostics), None), None
    pkg = result.package
    resolved = resolve_context(pkg, mapping)
    reports = readiness.evaluate_all(pkg, resolved)
    del resolved  # embedded context can be large; keep nothing of it
    summary = PackageSummary(pkg.package_id, pkg.title, pkg.stage, content_digest(pkg))
    stages = tuple(_stage_report(reports[s], pkg) for s in HANDOFF_STAGES)
    return Entry(path, "valid", summary, (), stages), pkg


def _entry_for(
    candidate: Candidate, probe: Probe, mapping: HostMapping
) -> tuple[Entry, WorkPackage | None]:
    if probe.kind == "package" and probe.raw is not None:
        return _assess(candidate.path, probe.raw, mapping)
    status: EntryStatus = _STATUS_FOR_KIND.get(probe.kind, "invalid")
    diags = (probe.diagnostic,) if probe.diagnostic else ()
    return Entry(candidate.path, status, None, diags, None), None


def _flag_duplicates(
    entries: list[Entry],
) -> tuple[list[Entry], list[Diagnostic]]:
    by_id: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        if entry.package is not None:
            by_id[entry.package.id].append(entry.path)
    duplicated = {i: paths for i, paths in by_id.items() if len(paths) > 1}
    collection: list[Diagnostic] = []
    for ident in sorted(duplicated):
        paths = ", ".join(duplicated[ident])
        collection.append(
            Diagnostic(
                "overview.duplicate_id",
                "error",
                f"package id {ident} is declared by {len(duplicated[ident])} "
                f"manifests: {paths}",
                "collection",
            )
        )
    flagged = [
        (
            Entry(
                e.path,
                e.status,
                e.package,
                (
                    *e.diagnostics,
                    Diagnostic(
                        "overview.duplicate_id",
                        "error",
                        f"package id {e.package.id} is also declared by another manifest "
                        "in this collection",
                        "package.id",
                    ),
                ),
                e.readiness,
            )
            if e.package is not None and e.package.id in duplicated
            else e
        )
        for e in entries
    ]
    return flagged, collection


def _unknown_roots(
    mapping: HostMapping, packages: Iterable[WorkPackage]
) -> list[Diagnostic]:
    declared = {r.name for p in packages for r in p.roots}
    return [
        Diagnostic(
            "roots.unknown",
            "warning",
            f"root '{name}' is mapped but not declared by any package in the collection",
            "roots",
        )
        for name in sorted(set(mapping.roots) - declared)
    ]


def build_mapping(
    root_pairs: Sequence[str], roots_file: Path | None
) -> HostMapping | Overview:
    """The host mapping, or a refused overview with a host-neutral diagnostic."""
    try:
        return build_host_mapping(root_pairs, roots_file)
    except OmnitientError as exc:
        code = exc.diagnostics[0].code if exc.diagnostics else "roots.invalid"
    except (ValueError, RuntimeError):
        # Input that O1 does not catch (a NUL in a mapped path; on Python 3.12 a
        # symbolic-link loop), still an invalid mapping rather than a crash.
        code = "roots.invalid"
    message = _ROOTS_MESSAGES.get(code, _ROOTS_MESSAGES["roots.invalid"])
    return refused(Diagnostic(code, "error", message, "roots"))


def build_overview(collection: Path, mapping: HostMapping) -> Overview:
    """Inventory ``collection`` and assess its candidates in sorted order."""
    try:
        with open_collection(collection) as root_fd:
            inventory = take_inventory(root_fd)
            return _assess_inventory(root_fd, inventory, mapping)
    except CollectionRefused as exc:
        return refused(exc.diagnostic)


def _assess_inventory(
    root_fd: int, inventory: Inventory, mapping: HostMapping
) -> Overview:
    entries: list[Entry] = []
    valid_packages: list[WorkPackage] = []
    for candidate in inventory.candidates:
        probe = candidate.probe
        if probe.kind == "package":
            # Read now, not at inventory time, so only one manifest is held at once.
            probe = probe_child(root_fd, candidate.name, read=True)
        entry, pkg = _entry_for(candidate, probe, mapping)
        entries.append(entry)
        if pkg is not None:
            valid_packages.append(pkg)

    flagged, diagnostics = _flag_duplicates(entries)
    counts = Counts(
        inventory.entries,
        inventory.ignored,
        len(flagged),
        sum(e.status == "valid" for e in flagged),
        sum(e.status == "invalid" for e in flagged),
        sum(e.status == "excluded" for e in flagged),
        sum(e.status == "unassessed" for e in flagged),
    )
    # A link or an unexamined location may hide a package; a manifest that is
    # not a regular file hides nothing (section 6).
    discovery = not any(_hides_packages(e) for e in flagged)
    assessment = discovery and counts.valid == counts.candidates
    if discovery and assessment:
        diagnostics.extend(_unknown_roots(mapping, valid_packages))
    return Overview(
        discovery,
        assessment,
        "complete" if discovery else "partial",
        counts,
        tuple(diagnostics),
        tuple(flagged),
    )


def _hides_packages(entry: Entry) -> bool:
    if entry.status == "unassessed":
        return True
    return entry.status == "excluded" and any(
        d.code == "overview.symlink" for d in entry.diagnostics
    )


def collection_overview(
    collection: Path, root_pairs: Sequence[str], roots_file: Path | None
) -> Overview:
    """The whole preflight: mapping, collection, inventory, assessments, aggregates."""
    mapping = build_mapping(root_pairs, roots_file)
    if isinstance(mapping, Overview):
        return mapping
    return build_overview(collection, mapping)


__all__ = [
    "ACTION_ORDER",
    "HANDOFF_READY",
    "SCHEMA",
    "SCHEMA_VERSION",
    "Action",
    "Counts",
    "Entry",
    "Overview",
    "PackageSummary",
    "Reference",
    "StageReport",
    "build_actions",
    "build_overview",
    "collection_overview",
    "refused",
]
