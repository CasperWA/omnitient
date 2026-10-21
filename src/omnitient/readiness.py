# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Readiness of a package for a handoff stage (docs/o1-contract.md, section 5)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from omnitient.context import ResolvedContext
from omnitient.errors import Diagnostic
from omnitient.model import (
    HANDOFF_STAGES,
    Approval,
    HandoffStage,
    WorkPackage,
    stage_rank,
)
from omnitient.serialize import content_digest

# A placeholder objective is ``TODO`` alone or ``TODO:`` and a note, in any
# letter case; "TODOs in X" and "Todoist" are real objectives.
_TODO_PLACEHOLDER = re.compile(r"TODO(?::|$)", re.IGNORECASE)


@dataclass(frozen=True)
class Readiness:
    stage: HandoffStage
    blockers: tuple[Diagnostic, ...]
    warnings: tuple[Diagnostic, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers


def approval_is_current(pkg: WorkPackage, approval: Approval) -> bool:
    return approval.content_digest == content_digest(pkg)


def decision_blocks(pkg: WorkPackage, stage: HandoffStage) -> list[Diagnostic]:
    """Unresolved decisions that are needed by ``stage``, as blockers."""
    return [
        Diagnostic(
            "decision.unresolved",
            "blocker",
            f"unresolved decision needed by {d.needed_by}: {d.question}",
            d.id,
        )
        for d in pkg.decisions
        if d.status == "unresolved" and stage_rank(d.needed_by) <= stage_rank(stage)
    ]


def evaluate(
    pkg: WorkPackage, resolved: Sequence[ResolvedContext], stage: HandoffStage
) -> Readiness:
    blockers: list[Diagnostic] = []
    warnings: list[Diagnostic] = []

    blockers.extend(decision_blocks(pkg, stage))
    warnings.extend(
        Diagnostic(
            "decision.open",
            "warning",
            f"unresolved decision not yet needed (needed by {d.needed_by}): {d.question}",
            d.id,
        )
        for d in pkg.decisions
        if d.status == "unresolved" and stage_rank(d.needed_by) > stage_rank(stage)
    )

    for item in resolved:
        if item.status == "ok":
            continue
        required = item.ref.required
        diag = Diagnostic(
            f"context.{item.status}",
            "blocker" if required else "warning",
            f"{item.ref.root}:{item.ref.path}: {item.detail}",
            item.ref.id,
        )
        (blockers if required else warnings).append(diag)

    if _TODO_PLACEHOLDER.match(pkg.objective.strip()):
        blockers.append(
            Diagnostic(
                "content.incomplete", "blocker", "objective is a TODO placeholder"
            )
        )
    gaps = [
        text
        for text, empty in (
            ("scope is empty", not pkg.scope),
            ("acceptance criteria are empty", not pkg.acceptance),
        )
        if empty
    ]
    if gaps:
        message = "; ".join(gaps)
        if stage == "planning":
            warnings.append(Diagnostic("content.incomplete", "warning", message))
        else:
            blockers.append(Diagnostic("content.incomplete", "blocker", message))

    if stage != "planning":
        digest = content_digest(pkg)
        for_stage = [a for a in pkg.approvals if a.stage == stage]
        if not any(a.content_digest == digest for a in for_stage):
            if for_stage:
                stale = ", ".join(a.id for a in for_stage)
                blockers.append(
                    Diagnostic(
                        "approval.stale",
                        "blocker",
                        f"approval(s) {stale} for '{stage}' refer to an earlier revision; "
                        f"current revision is {digest[:12]}",
                    )
                )
            else:
                blockers.append(
                    Diagnostic(
                        "approval.missing",
                        "blocker",
                        f"no approval recorded for '{stage}' at revision {digest[:12]}",
                    )
                )
    return Readiness(stage, tuple(blockers), tuple(warnings))


def evaluate_all(
    pkg: WorkPackage, resolved: Sequence[ResolvedContext]
) -> dict[HandoffStage, Readiness]:
    return {stage: evaluate(pkg, resolved, stage) for stage in HANDOFF_STAGES}
