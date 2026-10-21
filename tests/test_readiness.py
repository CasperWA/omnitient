# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Readiness is evaluated per stage and is distinct from schema validity."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from omnitient import ops, readiness
from omnitient.context import HostMapping, resolve_context
from omnitient.model import Decision, HandoffStage, WorkPackage
from omnitient.readiness import Readiness


def evaluate(pkg: WorkPackage, mapping: HostMapping, stage: HandoffStage) -> Readiness:
    return readiness.evaluate(pkg, resolve_context(pkg, mapping), stage)


def codes(report: Readiness) -> set[str]:
    return {d.code for d in report.blockers}


def test_complete_unapproved_package_is_ready_for_planning_only(
    package: WorkPackage, mapping: HostMapping
) -> None:
    assert evaluate(package, mapping, "planning").ready
    impl = evaluate(package, mapping, "implementation")
    assert not impl.ready
    assert codes(impl) == {"approval.missing"}


def test_unresolved_decision_blocks_stage_it_is_needed_by(
    package: WorkPackage, mapping: HostMapping, unresolved: Decision
) -> None:
    pkg = replace(package, decisions=(*package.decisions, unresolved))
    assert evaluate(pkg, mapping, "planning").ready
    planning_warnings = {d.code for d in evaluate(pkg, mapping, "planning").warnings}
    assert "decision.open" in planning_warnings
    blocked = evaluate(pkg, mapping, "implementation")
    assert "decision.unresolved" in codes(blocked)
    assert blocked.blockers[0].location == "D2"
    assert "Which database?" in blocked.blockers[0].message


def test_planning_stage_decision_blocks_planning(
    package: WorkPackage, mapping: HostMapping
) -> None:
    early = Decision("D9", "Pick a goal?", "unresolved", "me", "planning")
    pkg = replace(package, decisions=(early,))
    assert codes(evaluate(pkg, mapping, "planning")) == {"decision.unresolved"}


def test_resolved_decision_does_not_block(
    approved: WorkPackage, mapping: HostMapping
) -> None:
    assert evaluate(approved, mapping, "implementation").ready


def test_required_context_problems_block_optional_ones_warn(
    approved: WorkPackage, mapping: HostMapping, repo: Path
) -> None:
    (repo / "docs" / "notes.md").unlink()
    report = evaluate(approved, mapping, "implementation")
    assert report.ready
    assert [d.code for d in report.warnings] == ["context.missing"]

    (repo / "docs" / "spec.md").write_text("drift", encoding="utf-8")
    report = evaluate(approved, mapping, "implementation")
    assert codes(report) == {"context.digest_mismatch"}
    assert "docs/spec.md" in report.blockers[0].message


def test_unmapped_root_blocks_required_context(approved: WorkPackage) -> None:
    report = evaluate(approved, HostMapping(), "implementation")
    assert codes(report) == {"context.root_unmapped"}


def test_content_incomplete(package: WorkPackage, mapping: HostMapping) -> None:
    todo = replace(package, objective="todo: later")
    assert "content.incomplete" in codes(evaluate(todo, mapping, "planning"))

    bare = replace(package, scope=(), acceptance=())
    assert evaluate(bare, mapping, "planning").ready
    assert [d.code for d in evaluate(bare, mapping, "planning").warnings] == [
        "content.incomplete"
    ]
    assert "content.incomplete" in codes(evaluate(bare, mapping, "implementation"))


@pytest.mark.parametrize(
    ("objective", "placeholder"),
    [
        ("TODO", True),
        ("  todo  ", True),
        ("TODO: state the objective of this work package.", True),
        ("Todo:later", True),
        ("Todoist integration for the team.", False),
        ("TODOs in module X must be removed.", False),
        ("TODO-list cleanup in the CLI.", False),
        ("Remove the last TODO: comments.", False),
    ],
)
def test_only_a_todo_placeholder_objective_is_incomplete(
    package: WorkPackage, mapping: HostMapping, objective: str, placeholder: bool
) -> None:
    report = evaluate(replace(package, objective=objective), mapping, "planning")
    assert ("content.incomplete" in codes(report)) is placeholder


def test_schema_valid_is_not_ready(mapping: HostMapping) -> None:
    scaffold = ops.new_package("demo", "init", "wp1", "T")
    assert not evaluate(scaffold, mapping, "planning").ready
    assert not evaluate(scaffold, mapping, "implementation").ready
