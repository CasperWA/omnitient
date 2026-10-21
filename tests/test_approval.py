# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Approval references follow the content revision, not the file."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from omnitient import ops, readiness
from omnitient.context import HostMapping, resolve_context
from omnitient.errors import OmnitientError
from omnitient.model import (
    ContextRef,
    Decision,
    HandoffStage,
    Limits,
    Recommendation,
    WorkPackage,
)
from omnitient.serialize import content_digest

AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def ready_for(
    pkg: WorkPackage, mapping: HostMapping, stage: HandoffStage = "implementation"
) -> bool:
    return readiness.evaluate(pkg, resolve_context(pkg, mapping), stage).ready


def approve(pkg: WorkPackage, stage: HandoffStage = "implementation") -> WorkPackage:
    return ops.add_approval(
        pkg, stage=stage, approved_by="user:alice", note=None, at=AT
    )


def test_approval_records_the_revision_and_makes_the_package_ready(
    package: WorkPackage, mapping: HostMapping
) -> None:
    pkg = approve(package)
    record = pkg.approvals[0]
    assert record.content_digest == content_digest(package)
    assert record.approved_at == "2026-01-02T03:04:05Z"
    assert pkg.stage == "implementation"
    assert ready_for(pkg, mapping)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: replace(p, objective="A different objective"),
        lambda p: replace(p, scope=(*p.scope, "More scope")),
        lambda p: replace(p, exclusions=()),
        lambda p: replace(p, acceptance=("Something else",)),
        lambda p: replace(p, title="Renamed"),
        lambda p: replace(p, dependencies=()),
        lambda p: replace(p, decisions=(replace(p.decisions[0], answer="YAML"),)),
        lambda p: replace(
            p,
            decisions=(
                *p.decisions,
                Decision("D2", "New?", "resolved", "me", "review", answer="yes"),
            ),
        ),
        lambda p: replace(
            p, context=(replace(p.context[0], sha256="0" * 64), p.context[1])
        ),
        lambda p: replace(
            p, context=(replace(p.context[0], required=False), p.context[1])
        ),
        lambda p: replace(
            p, context=(*p.context, ContextRef("extra", "repo", "docs/x.md", "1" * 64))
        ),
    ],
)
def test_changing_covered_content_makes_the_approval_stale(
    package: WorkPackage, mapping: HostMapping, change: object
) -> None:
    approved = approve(package)
    changed = change(approved)  # type: ignore[operator]
    assert content_digest(changed) != content_digest(approved)
    report = readiness.evaluate(
        changed, resolve_context(changed, mapping), "implementation"
    )
    assert "approval.stale" in {d.code for d in report.blockers}
    assert changed.approvals == approved.approvals  # history is kept


@pytest.mark.parametrize(
    "change",
    [
        lambda p: replace(p, stage="review"),
        lambda p: replace(
            p,
            recommendations=(
                Recommendation(
                    "R2", "review", model="other", limits=Limits(max_turns=3)
                ),
            ),
        ),
        lambda p: ops.add_evidence(
            p, kind="commit", ref="abcdef1", recorded_by="me", at=AT
        ),
    ],
)
def test_changing_uncovered_content_keeps_the_approval_current(
    package: WorkPackage, mapping: HostMapping, change: object
) -> None:
    approved = approve(package)
    changed = change(approved)  # type: ignore[operator]
    assert content_digest(changed) == content_digest(approved)
    assert ready_for(changed, mapping)


def test_approval_is_per_stage(package: WorkPackage, mapping: HostMapping) -> None:
    pkg = approve(package, "implementation")
    assert ready_for(pkg, mapping, "implementation")
    assert not ready_for(pkg, mapping, "review")
    assert ready_for(approve(pkg, "review"), mapping, "review")


def test_approval_ids_are_unique_and_stage_never_lowered(package: WorkPackage) -> None:
    pkg = approve(replace(package, stage="review"))
    assert pkg.stage == "review"
    again = approve(pkg)
    assert [a.id for a in again.approvals] == ["A1", "A2"]


def test_approval_refused_while_a_decision_blocks(
    package: WorkPackage, unresolved: Decision
) -> None:
    pkg = replace(package, decisions=(*package.decisions, unresolved))
    with pytest.raises(OmnitientError) as info:
        approve(pkg)
    assert info.value.diagnostics[0].code == "decision.unresolved"
    approve(pkg, "planning")  # not needed until implementation


def test_approval_refused_for_incomplete_content() -> None:
    with pytest.raises(OmnitientError):
        approve(ops.new_package("demo", "init", "wp1", "T"))


def test_approval_refused_when_supplied_context_drifted(
    package: WorkPackage, mapping: HostMapping, repo: object
) -> None:
    (repo / "docs" / "spec.md").write_text("drift", encoding="utf-8")  # type: ignore[operator]
    with pytest.raises(OmnitientError) as info:
        ops.add_approval(
            package,
            stage="implementation",
            approved_by="me",
            note=None,
            at=AT,
            resolved=resolve_context(package, mapping),
        )
    assert info.value.diagnostics[0].code == "context.digest_mismatch"


def test_scaffold_has_no_approval_and_approve_is_never_implicit() -> None:
    assert ops.new_package("demo", "init", "wp1", "T").approvals == ()
