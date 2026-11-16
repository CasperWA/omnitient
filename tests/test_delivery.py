# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Acceptance, withdrawal, replay, summaries and capacity, as pure operations."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnitient import delivery, ops, readiness
from omnitient.context import HostMapping, resolve_context
from omnitient.delivery import (
    RESERVE_LIMIT_BYTES,
    WITHDRAWAL_RESERVE_BYTES,
    acceptance_request,
    plan_acceptance,
    plan_withdrawal,
    reserve_of,
    summarize,
    withdrawal_request,
)
from omnitient.errors import OmnitientError
from omnitient.model import Acceptance, Decision, Evidence, WorkPackage
from omnitient.schema import parse_package
from omnitient.serialize import content_digest, dumps

NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
LATER = NOW + timedelta(days=1)


def make_request(pkg: WorkPackage, op: str = "C1", **kw: Any) -> Any:
    fields: dict[str, Any] = {
        "operation_id": op,
        "actor": "user:alice",
        "revision": content_digest(pkg),
        "evidence": ["E1"],
        "reason": "Accepted.",
    }
    fields.update(kw)
    return acceptance_request(**fields)


def accept(
    pkg: WorkPackage, op: str = "C1", at: datetime = NOW, **kw: Any
) -> WorkPackage:
    return plan_acceptance(pkg, make_request(pkg, op, **kw), at).package


def withdraw(
    pkg: WorkPackage,
    target: str = "C1",
    op: str = "W1",
    at: datetime = NOW,
    **kw: Any,
) -> WorkPackage:
    fields: dict[str, Any] = {
        "operation_id": op,
        "actor": "user:bob",
        "target": target,
        "reason": "Superseded.",
    }
    fields.update(kw)
    return plan_withdrawal(pkg, withdrawal_request(**fields), at).package


def codes(call: Callable[[], object]) -> set[str]:
    with pytest.raises(OmnitientError) as info:
        call()
    return {d.code for d in info.value.diagnostics}


def roundtrip(pkg: WorkPackage) -> WorkPackage:
    parsed = parse_package(dumps(pkg))
    assert parsed.diagnostics == ()
    assert parsed.package is not None
    return parsed.package


# --- eligibility (AC06) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("evidence", "objective"),
    [
        (
            Evidence("E1", "commit", "abcdef1", "me", "2026-01-02T03:04:05Z"),
            "Implement the feature.",
        ),
        (
            Evidence(
                "E1", "pr", "https://example.org/pull/9", "me", "2026-01-02T03:04:05Z"
            ),
            "Document the format.",
        ),
        (
            Evidence(
                "E1",
                "report",
                "https://example.org/report",
                "me",
                "2026-01-02T03:04:05Z",
            ),
            "Investigate whether X is feasible; the finding is negative.",
        ),
    ],
    ids=["code", "documentation", "investigation-report-only"],
)
def test_code_documentation_and_investigations_can_be_accepted(
    v2_package: WorkPackage, evidence: Evidence, objective: str
) -> None:
    pkg = replace(v2_package, evidence=(evidence,), objective=objective)
    new = accept(pkg)
    assert len(new.delivery) == 1
    assert roundtrip(new) == new


def test_no_pull_request_or_build_is_required(v2_package: WorkPackage) -> None:
    only_report = replace(v2_package, evidence=(v2_package.evidence[1],))
    assert accept(only_report, evidence=["E2"]).delivery


def test_the_record_holds_exactly_what_was_requested(v2_package: WorkPackage) -> None:
    new = accept(
        v2_package, "Closeout-1", reason="Because.\nTwice.", actor=" odd actor "
    )
    record = new.delivery[0]
    assert isinstance(record, Acceptance)
    assert record.operation_id == "Closeout-1"
    assert record.actor == " odd actor "
    assert record.reason == "Because.\nTwice."
    assert record.recorded_at == "2026-03-04T05:06:07Z"
    assert record.package_id == v2_package.package_id
    assert record.revision.scheme == "covered-sha256-v2"
    assert record.revision.digest == content_digest(v2_package)


def test_only_the_delivery_changes_when_accepting(v2_package: WorkPackage) -> None:
    new = accept(v2_package)
    assert replace(new, delivery=()) == v2_package
    assert new.stage == v2_package.stage
    assert new.approvals == v2_package.approvals


def test_a_v1_package_is_refused_with_upgrade_guidance(package: WorkPackage) -> None:
    request = make_request(package, evidence=["E1"])
    with pytest.raises(OmnitientError) as info:
        plan_acceptance(package, request, NOW)
    assert [d.code for d in info.value.diagnostics] == ["delivery.requires_v2"]
    assert "omnitient upgrade PACKAGE --to-version 2" in str(info.value)


@pytest.mark.parametrize(
    "edit",
    [
        {"objective": "TODO: write this"},
        {"objective": "  todo "},
        {"scope": ()},
        {"acceptance": ()},
    ],
)
def test_structural_gaps_refuse(v2_package: WorkPackage, edit: dict[str, Any]) -> None:
    pkg = replace(v2_package, **edit)
    assert codes(lambda: accept(pkg)) == {"delivery.incomplete"}


def test_the_revision_must_be_the_present_one(v2_package: WorkPackage) -> None:
    assert codes(lambda: accept(v2_package, revision="0" * 64)) == {
        "delivery.revision_mismatch"
    }
    old = content_digest(v2_package)
    changed = replace(v2_package, title="Changed")
    assert codes(lambda: accept(changed, revision=old)) == {
        "delivery.revision_mismatch"
    }


def test_unknown_evidence_refuses_even_when_other_ids_exist(
    v2_package: WorkPackage,
) -> None:
    assert codes(lambda: accept(v2_package, evidence=["E1", "E9"])) == {
        "delivery.evidence_unknown"
    }
    bare = replace(v2_package, evidence=())
    assert codes(lambda: accept(bare)) == {"delivery.evidence_unknown"}


@pytest.mark.parametrize(
    "bad",
    [
        {"operation_id": ""},
        {"operation_id": "1x"},
        {"operation_id": "x" * 33},
        {"operation_id": "é"},
        {"actor": ""},
        {"actor": "  "},
        {"actor": "a\nb"},
        {"actor": "a" * 201},
        {"reason": ""},
        {"reason": "\n \t"},
        {"reason": "bad\x07"},
        {"reason": "a" * 8193},
        {"reason": "é" * 4097},
        {"revision": "A" * 64},
        {"revision": "a" * 63},
        {"evidence": []},
        {"evidence": ["E1", "E1"]},
        {"evidence": ["E1", "e 1"]},
        {"evidence": [f"E{i}" for i in range(21)]},
    ],
)
def test_malformed_request_fields_are_refused_before_anything_else(
    v2_package: WorkPackage, bad: dict[str, Any]
) -> None:
    assert codes(lambda: make_request(v2_package, **bad)) == {
        "delivery.request_invalid"
    }


def test_request_bounds_are_inclusive(v2_package: WorkPackage) -> None:
    ids = [f"E{i}" for i in range(20)]
    pkg = replace(
        v2_package,
        evidence=tuple(
            Evidence(i, "commit", "abcdef1", "me", "2026-01-02T03:04:05Z") for i in ids
        ),
    )
    new = accept(pkg, evidence=ids, actor="a" * 200, reason="é" * 4096)
    record = new.delivery[0]
    assert isinstance(record, Acceptance)
    assert len(record.evidence) == 20
    assert roundtrip(new) == new


def test_no_other_active_acceptance_of_the_same_revision(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(v2_package, "C1")
    assert codes(lambda: accept(pkg, "C2")) == {"delivery.duplicate_acceptance"}
    with pytest.raises(OmnitientError, match="C1"):
        accept(pkg, "C2")


# --- readiness independence (AC07) ------------------------------------------------------


def test_diagnostics_do_not_gate_acceptance_and_are_left_unchanged(
    v2_package: WorkPackage, mapping: HostMapping
) -> None:
    broken = replace(
        v2_package,
        approvals=(),
        decisions=(
            *v2_package.decisions,
            Decision("D9", "Still open?", "unresolved", "alice", "planning"),
        ),
        context=(replace(v2_package.context[0], sha256="0" * 64),),
    )
    before = readiness.evaluate_all(broken, resolve_context(broken, mapping))
    assert not before["implementation"].ready
    new = accept(broken)
    assert len(new.delivery) == 1
    after = readiness.evaluate_all(new, resolve_context(new, mapping))
    assert after == before
    assert new.stage == broken.stage


def test_a_stale_approval_stays_stale(
    v2_package: WorkPackage, approved: WorkPackage
) -> None:
    pkg = replace(approved, schema_version=2, evidence=v2_package.evidence)
    stale = replace(pkg, title="Edited after approval")
    new = accept(stale)
    assert new.approvals == stale.approvals
    assert "approval.stale" in {
        d.code for d in readiness.evaluate(new, (), "implementation").blockers
    }


# --- request equality, replay and conflict (AC13, AC14) ---------------------------------


def test_exact_replay_returns_the_original_record_and_changes_nothing(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(v2_package, "C1", at=NOW, evidence=["E2", "E1"])
    plan = plan_acceptance(pkg, make_request(pkg, evidence=["E1", "E2"]), LATER)
    assert plan.outcome == "replayed"
    assert plan.package is pkg
    assert plan.record is pkg.delivery[0]
    assert plan.record.recorded_at == "2026-03-04T05:06:07Z"  # not LATER


def test_evidence_order_alone_is_equivalent_but_repeats_still_refuse(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(v2_package, "C1", evidence=["E3", "E1"])
    one = plan_acceptance(pkg, make_request(pkg, evidence=["E1", "E3"]), NOW)
    two = plan_acceptance(pkg, make_request(pkg, evidence=["E3", "E1"]), NOW)
    assert one.outcome == two.outcome == "replayed"
    assert codes(lambda: make_request(pkg, evidence=["E1", "E1", "E3"])) == {
        "delivery.request_invalid"
    }


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"actor": "user:alice "}, "actor"),
        ({"actor": "User:alice"}, "actor"),
        ({"reason": "accepted."}, "reason"),
        ({"reason": "Accepted. "}, "reason"),
        ({"reason": "Accepted.\n"}, "reason"),
        ({"evidence": ["E1", "E2"]}, "evidence"),
        ({"evidence": ["E2"]}, "evidence"),
        ({"revision": "1" * 64}, "revision"),
    ],
)
def test_any_other_difference_is_a_conflict_with_no_write(
    v2_package: WorkPackage, change: dict[str, Any], field: str
) -> None:
    pkg = accept(v2_package, "C1")
    request = make_request(pkg, **change)
    with pytest.raises(OmnitientError) as info:
        plan_acceptance(pkg, request, NOW)
    assert [d.code for d in info.value.diagnostics] == ["delivery.conflict"]
    assert field in str(info.value)


def test_unicode_is_never_normalized(v2_package: WorkPackage) -> None:
    composed, decomposed = "café", "café"
    pkg = accept(v2_package, "C1", reason=composed)
    assert codes(
        lambda: plan_acceptance(pkg, make_request(pkg, reason=decomposed), NOW)
    ) == {"delivery.conflict"}


def test_the_same_id_with_another_kind_is_a_conflict(v2_package: WorkPackage) -> None:
    pkg = withdraw(accept(v2_package, "C1"), "C1", "W1")
    assert codes(lambda: plan_acceptance(pkg, make_request(pkg, "W1"), NOW)) == {
        "delivery.conflict"
    }
    request = withdrawal_request(operation_id="C1", actor="x", target="C1", reason="r")
    assert codes(lambda: plan_withdrawal(pkg, request, NOW)) == {"delivery.conflict"}


def test_replay_is_decided_before_the_new_operation_gates(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(v2_package, "C1", evidence=["E1"])
    # The revision drifts and the live evidence disappears: the replay still works.
    drifted = replace(pkg, title="Drifted", evidence=())
    plan = plan_acceptance(drifted, make_request(pkg), LATER)
    assert plan.outcome == "replayed"
    assert plan.status.state == "prior"
    assert plan.record is pkg.delivery[0]
    # A replay does not accept the current revision.
    assert len(plan.package.delivery) == 1


def test_a_replay_after_withdrawal_reports_it_and_never_reactivates(
    v2_package: WorkPackage,
) -> None:
    pkg = withdraw(accept(v2_package, "C1"), "C1", "W1")
    plan = plan_acceptance(pkg, make_request(pkg), LATER)
    assert plan.outcome == "replayed"
    assert plan.status.state == "withdrawn"
    assert plan.status.withdrawn_by == "W1"
    assert plan.package is pkg


def test_a_replay_after_reacceptance_is_about_the_original(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(withdraw(accept(v2_package, "C1"), "C1", "W1"), "C2", evidence=["E2"])
    plan = plan_acceptance(pkg, make_request(pkg, "C1"), LATER)
    assert plan.record.operation_id == "C1"
    assert plan.status.state == "withdrawn"
    assert summarize(pkg).current_revision_acceptance == "C2"


def test_a_replay_works_at_capacity_and_in_a_deficit(v2_package: WorkPackage) -> None:
    pkg = accept(v2_package, "C1")
    filler = padded(pkg, RESERVE_LIMIT_BYTES - WITHDRAWAL_RESERVE_BYTES + 5000)
    reserve = reserve_of(filler)
    assert reserve is not None
    assert not reserve.compliant
    assert (
        plan_acceptance(filler, make_request(v2_package), LATER).outcome == "replayed"
    )


def test_an_exact_withdrawal_replay_writes_nothing(v2_package: WorkPackage) -> None:
    pkg = withdraw(accept(v2_package, "C1"), "C1", "W1", at=NOW)
    plan = plan_withdrawal(
        pkg,
        withdrawal_request(
            operation_id="W1", actor="user:bob", target="C1", reason="Superseded."
        ),
        LATER,
    )
    assert plan.outcome == "replayed"
    assert plan.package is pkg
    assert plan.record is pkg.delivery[1]


def test_a_changed_withdrawal_request_conflicts(v2_package: WorkPackage) -> None:
    pkg = withdraw(accept(v2_package, "C1"), "C1", "W1")
    for change in ({"actor": "user:Bob"}, {"reason": "Superseded"}):
        request = withdrawal_request(
            **{
                "operation_id": "W1",
                "actor": "user:bob",
                "target": "C1",
                "reason": "Superseded.",
                **change,
            }
        )
        assert codes(lambda r=request: plan_withdrawal(pkg, r, NOW)) == {  # type: ignore[misc]
            "delivery.conflict"
        }
    other = accept(pkg, "C2", evidence=["E2"])
    request = withdrawal_request(
        operation_id="W1", actor="user:bob", target="C2", reason="Superseded."
    )
    assert codes(lambda: plan_withdrawal(other, request, NOW)) == {"delivery.conflict"}


# --- withdrawal (AC10, AC11) -------------------------------------------------------------


def test_a_withdrawal_appends_one_record_and_keeps_everything(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(v2_package, "C1")
    new = withdraw(pkg, at=LATER, reason="Line 1\nLine 2")
    assert new.delivery[0] is pkg.delivery[0]
    withdrawal = new.delivery[1]
    assert withdrawal.kind == "withdrawal"
    assert withdrawal.target_acceptance_id == "C1"  # type: ignore[union-attr]
    assert withdrawal.recorded_at == "2026-03-05T05:06:07Z"
    assert withdrawal.reason == "Line 1\nLine 2"
    assert replace(new, delivery=pkg.delivery) == pkg
    assert content_digest(new) == content_digest(pkg)
    assert summarize(new).current_revision_acceptance is None


def test_a_prior_revision_acceptance_can_be_withdrawn_from_an_incomplete_package(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(v2_package, "C1")
    moved = replace(pkg, objective="TODO: rewrite", scope=(), acceptance=())
    assert not readiness.evaluate(moved, (), "implementation").ready
    new = withdraw(moved, "C1", "W1")
    assert summarize(new).state == "none" or summarize(new).state == "withdrawn_only"
    assert summarize(new).withdrawn_acceptance_count == 1


def test_withdrawal_errors(v2_package: WorkPackage) -> None:
    pkg = withdraw(accept(v2_package, "C1"), "C1", "W1")
    pkg = accept(pkg, "C2", evidence=["E2"])

    def go(target: str, op: str = "W2") -> Callable[[], object]:
        return lambda: withdraw(pkg, target, op)

    assert codes(go("Nope")) == {"delivery.target_unknown"}
    assert codes(go("W1")) == {"delivery.target_not_acceptance"}
    assert codes(go("C1")) == {"delivery.already_withdrawn"}
    with pytest.raises(OmnitientError, match="W1"):
        withdraw(pkg, "C1", "W2")
    for bad in ({"actor": " "}, {"reason": ""}, {"op": "9"}):
        assert codes(lambda b=bad: withdraw(pkg, "C2", **b)) == {  # type: ignore[misc]
            "delivery.request_invalid"
        }
    assert codes(lambda: withdraw(pkg, "a b", "W2")) == {"delivery.request_invalid"}


def test_a_v1_package_has_no_target_to_withdraw(package: WorkPackage) -> None:
    assert codes(lambda: withdraw(package)) == {"delivery.target_unknown"}


def test_withdrawals_do_not_cascade_or_touch_other_acceptances(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(v2_package, "C1")
    moved = replace(pkg, title="Revision two")
    moved = accept(moved, "C2", evidence=["E2"])
    new = withdraw(moved, "C1", "W1")
    summary = summarize(new)
    assert summary.current_revision_acceptance == "C2"
    assert summary.withdrawn_acceptance_count == 1


# --- reacceptance (AC12) -----------------------------------------------------------------


def test_reacceptance_creates_a_new_record_with_fresh_inputs(
    v2_package: WorkPackage,
) -> None:
    first = accept(v2_package, "C1", at=NOW, evidence=["E1"], reason="First.")
    gone = withdraw(first, "C1", "W1", at=NOW)
    again = accept(gone, "C2", at=LATER, evidence=["E2", "E3"], reason="Second.")
    old, withdrawal, new = again.delivery
    assert old is first.delivery[0]
    assert withdrawal is gone.delivery[1]
    assert isinstance(new, Acceptance)
    assert new.recorded_at != old.recorded_at  # type: ignore[union-attr]
    assert [e.id for e in new.evidence] == ["E2", "E3"]
    assert new.reason == "Second."
    summary = summarize(again)
    assert summary.state == "current"
    assert summary.current_revision_acceptance == "C2"
    assert (summary.acceptance_count, summary.withdrawal_count) == (2, 1)
    assert codes(lambda: accept(again, "C3")) == {"delivery.duplicate_acceptance"}


def test_a_new_revision_needs_its_own_explicit_acceptance(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(v2_package, "C1")
    moved = replace(pkg, scope=(*pkg.scope, "More"))
    assert summarize(moved).state == "prior_only"
    assert summarize(moved).current_revision_acceptance is None
    assert summarize(moved).prior_revision_unwithdrawn_count == 1
    again = accept(moved, "C2")
    assert summarize(again).current_revision_acceptance == "C2"
    assert summarize(again).prior_revision_unwithdrawn_count == 1


def test_returning_to_an_earlier_revision_makes_its_acceptance_current_again(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(v2_package, "C1")
    away = replace(pkg, title="Away")
    assert summarize(away).state == "prior_only"
    assert summarize(replace(away, title=pkg.title)).state == "current"


# --- summaries and diagnostics (AC20) ----------------------------------------------------


def test_summary_states_and_counts(
    v2_package: WorkPackage, package: WorkPackage
) -> None:
    assert summarize(v2_package).to_dict() == {
        "state": "none",
        "current_revision_acceptance": None,
        "prior_revision_unwithdrawn_count": 0,
        "withdrawn_acceptance_count": 0,
        "acceptance_count": 0,
        "withdrawal_count": 0,
    }
    only = withdraw(accept(v2_package, "C1"), "C1", "W1")
    assert summarize(only).state == "withdrawn_only"
    mixed = accept(only, "C2", evidence=["E3"])
    assert summarize(mixed).to_dict() == {
        "state": "current",
        "current_revision_acceptance": "C2",
        "prior_revision_unwithdrawn_count": 0,
        "withdrawn_acceptance_count": 1,
        "acceptance_count": 2,
        "withdrawal_count": 1,
    }
    moved = replace(mixed, title="Moved")
    assert summarize(moved).state == "prior_only"
    assert summarize(moved).withdrawn_acceptance_count == 1
    v1 = summarize(package)
    assert v1.state == "unavailable"
    assert v1.acceptance_count == 0


def test_a_declared_done_stage_is_not_an_acceptance(v2_package: WorkPackage) -> None:
    done = replace(v2_package, stage="done")
    assert summarize(done).state == "none"
    assert summarize(done).current_revision_acceptance is None


def test_the_description_keeps_the_counts_beside_the_state(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(withdraw(accept(v2_package, "C1"), "C1", "W1"), "C2")
    text = summarize(replace(pkg, title="Moved")).describe()
    assert "prior revision only" in text
    assert "prior-revision unwithdrawn 1" in text
    assert "withdrawn 1" in text


def test_the_full_history_marks_each_acceptances_state(v2_package: WorkPackage) -> None:
    pkg = accept(withdraw(accept(v2_package, "C1"), "C1", "W1"), "C2")
    items = delivery.history_dict(replace(pkg, title="Moved"))
    assert [(i["operation_id"], i.get("state")) for i in items] == [
        ("C1", "withdrawn"),
        ("W1", None),
        ("C2", "prior"),
    ]
    assert items[0]["withdrawn_by"] == "W1"
    assert json.dumps(items)  # plain JSON


# --- history independence (AC09) ---------------------------------------------------------


def test_live_evidence_changes_do_not_touch_history(v2_package: WorkPackage) -> None:
    pkg = accept(v2_package, "C1", evidence=["E1", "E2"])
    edited = replace(pkg, evidence=(replace(pkg.evidence[0], summary="edited"),))
    assert edited.delivery == pkg.delivery
    assert roundtrip(edited) == edited
    assert summarize(edited).state == "current"  # evidence is outside the digest


def test_context_changes_create_a_prior_association_without_touching_history(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(v2_package, "C1")
    repinned = ops.pin_context(pkg, {"spec": "f" * 64})
    assert content_digest(repinned) != content_digest(pkg)
    assert repinned.delivery == pkg.delivery
    assert summarize(repinned).state == "prior_only"


# --- capacity (AC17, AC18, AC19) ----------------------------------------------------------


def padded(pkg: WorkPackage, target_bytes: int) -> WorkPackage:
    """``pkg`` with decisions whose text brings its stored size to ``target_bytes``."""
    base = replace(pkg, decisions=())
    size = len(dumps(base))
    filler: list[Decision] = []
    index = 0
    while True:
        index += 1
        decision = Decision(f"P{index}", "q", "resolved", "s", "planning", "x", "y")
        trial = replace(base, decisions=(*filler, decision))
        need = target_bytes - len(dumps(trial))
        if need <= 0:
            break
        room = 7000
        grown = replace(decision, rationale="x" * (1 + min(need, room)))
        filler.append(grown)
        size = len(dumps(replace(base, decisions=tuple(filler))))
        if size == target_bytes:
            break
    out = replace(base, decisions=tuple(filler))
    gap = target_bytes - len(dumps(out))
    if gap and filler:
        last = filler[-1]
        filler[-1] = replace(last, rationale=last.rationale + "x" * gap)
        out = replace(base, decisions=tuple(filler))
    assert len(dumps(out)) == target_bytes
    return out


def test_the_reserve_constant_is_the_largest_withdrawal_the_schema_admits(
    v2_package: WorkPackage,
) -> None:
    worst = delivery._worst_case_withdrawal()
    assert len(worst.actor) == 200
    assert len(worst.reason.encode()) == 8192
    base = accept(v2_package, "C1")
    actual = [
        len(dumps(replace(pkg, delivery=(*pkg.delivery, worst)))) - len(dumps(pkg))
        for pkg in (v2_package, base)
    ]
    assert max(actual) == WITHDRAWAL_RESERVE_BYTES
    # Any real withdrawal is smaller, so it fits inside its reserved headroom.
    real = withdraw(
        base, "C1", "W1", reason="\n" * 8191 + "x", actor="\U0001f600" * 200
    )
    assert len(dumps(real)) - len(dumps(base)) <= WITHDRAWAL_RESERVE_BYTES


@pytest.mark.parametrize(
    "reason",
    [
        "a" * 8192,
        '"' * 8192,
        "\\" * 8192,
        "\n" * 8191 + "x",
        "\t" * 8191 + "x",
        "é" * 4096,
        "\U0001f600" * 2048,
        "☃" * 2730 + "ab",
    ],
)
def test_no_admissible_reason_exceeds_the_reserve(
    v2_package: WorkPackage, reason: str
) -> None:
    base = accept(v2_package, "C1")
    new = withdraw(base, "C1", "W1", reason=reason, actor="\U0001f600" * 200)
    grown = len(dumps(new)) - len(dumps(base))
    assert grown <= WITHDRAWAL_RESERVE_BYTES
    assert roundtrip(new) == new


def test_the_reserve_counts_every_active_acceptance(v2_package: WorkPackage) -> None:
    one = accept(v2_package, "C1")
    two = accept(replace(one, title="Two"), "C2")
    r1, r2 = reserve_of(one), reserve_of(two)
    assert r1 is not None
    assert r2 is not None
    assert (r1.active, r2.active) == (1, 2)  # a prior revision's acceptance counts
    assert r2.bytes_reserved == 2 * WITHDRAWAL_RESERVE_BYTES
    assert reserve_of(withdraw(two, "C1", "W1")).active == 1  # type: ignore[union-attr]


def test_v1_has_no_reserve(package: WorkPackage) -> None:
    assert reserve_of(package) is None
    assert delivery.reserve_deficit_diagnostic(package) is None


def accept_size(pkg: WorkPackage) -> int:
    return len(dumps(accept(pkg))) - len(dumps(pkg))


def test_the_byte_boundary_is_exact(v2_package: WorkPackage) -> None:
    grown = accept_size(v2_package)
    limit = RESERVE_LIMIT_BYTES
    # After the acceptance: stored + one reserve must be <= the limit.
    at = padded(v2_package, limit - WITHDRAWAL_RESERVE_BYTES - grown)
    new = accept(at)
    reserve = reserve_of(new)
    assert reserve is not None
    assert reserve.bytes_stored + reserve.bytes_reserved == limit
    assert reserve.compliant
    over = padded(v2_package, limit - WITHDRAWAL_RESERVE_BYTES - grown + 1)
    assert codes(lambda: accept(over)) == {"delivery.capacity"}


def test_a_withdrawal_at_the_boundary_always_fits(v2_package: WorkPackage) -> None:
    grown = accept_size(v2_package)
    at = accept(
        padded(v2_package, RESERVE_LIMIT_BYTES - WITHDRAWAL_RESERVE_BYTES - grown)
    )
    new = withdraw(at, "C1", "W1", reason="é" * 4096, actor="\U0001f600" * 200)
    assert len(dumps(new)) <= RESERVE_LIMIT_BYTES
    reserve = reserve_of(new)
    assert reserve is not None
    assert reserve.compliant
    assert reserve.active == 0
    # The withdrawal replaced its worst-case reserve by its actual bytes, so the
    # unused part of the reserve is now plain headroom.
    actual = len(dumps(new)) - len(dumps(at))
    assert RESERVE_LIMIT_BYTES - len(dumps(new)) == WITHDRAWAL_RESERVE_BYTES - actual


def test_the_maxima_do_not_fit_together(v2_package: WorkPackage) -> None:
    long_ref = "https://example.org/" + "a" * 470
    evidence = tuple(
        Evidence(
            f"E{i}",
            "pr",
            long_ref,
            "\U0001f600" * 200,
            "2026-01-02T03:04:05Z",
            summary="\U0001f600" * 1000,
        )
        for i in range(20)
    )
    pkg = replace(v2_package, evidence=evidence)
    ids = [e.id for e in evidence]
    big = "\U0001f600" * 2048
    first = accept(pkg, "C1", evidence=ids, reason=big, actor="\U0001f600" * 200)
    assert len(dumps(first)) + WITHDRAWAL_RESERVE_BYTES < RESERVE_LIMIT_BYTES
    second_base = replace(first, title="Another revision")
    codes_seen = codes(
        lambda: accept(
            second_base, "C2", evidence=ids, reason=big, actor="\U0001f600" * 200
        )
    )
    assert codes_seen == {"delivery.capacity"}
    # Nothing is truncated or overridden: even a small request is refused here.
    assert codes(
        lambda: accept(second_base, "C2", evidence=["E0"], reason="short")
    ) == {"delivery.capacity"}


def test_the_count_ceiling_counts_reserved_slots(v2_package: WorkPackage) -> None:
    """Fifty acceptances fit only if withdrawals have used their slots: 2A <= 100."""
    pkg = v2_package
    for n in range(50):
        pkg = accept(pkg, f"C{n}", evidence=["E1"])
        reserve = reserve_of(pkg)
        assert reserve is not None
        assert reserve.acceptances == n + 1
        if n < 49:
            pkg = withdraw(pkg, f"C{n}", f"W{n}", reason="r", actor="a")
    pkg = withdraw(pkg, "C49", "W49", reason="r", actor="a")
    reserve = reserve_of(pkg)
    assert reserve is not None
    assert (reserve.acceptances, reserve.withdrawals, reserve.active) == (50, 50, 0)
    assert reserve.records_reserved == 100
    assert len(pkg.delivery) == 100
    assert reserve.compliant
    assert roundtrip(pkg) == pkg
    # A 51st acceptance event would need 2 * 51 = 102 slots.
    assert codes(lambda: accept(pkg, "C50", evidence=["E1"])) == {"delivery.capacity"}


def test_reacceptance_consumes_a_used_and_a_reserved_slot(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(v2_package, "C1")
    r0 = reserve_of(pkg)
    pkg = withdraw(pkg, "C1", "W1")
    r1 = reserve_of(pkg)
    pkg = accept(pkg, "C2")
    r2 = reserve_of(pkg)
    assert r0 is not None
    assert r1 is not None
    assert r2 is not None
    assert r0.records_reserved == 2
    assert r1.records_reserved == 2  # the withdrawal used the reserved slot
    assert r2.records_reserved == 4  # a new used and a new reserved slot


def test_the_hundred_record_parse_limit_leaves_no_room_for_a_withdrawal_reserve(
    v2_package: WorkPackage,
) -> None:
    pkg = v2_package
    for n in range(50):
        pkg = withdraw(accept(pkg, f"C{n}"), f"C{n}", f"W{n}")
    assert len(pkg.delivery) == 100
    data = json.loads(dumps(pkg))
    data["delivery"].append({**data["delivery"][1], "operation_id": "Extra"})
    assert parse_package(json.dumps(data).encode()).package is None


# --- the recovery exception (AC19) ---------------------------------------------------------


def deficient(v2_package: WorkPackage, n_active: int = 2) -> WorkPackage:
    """A format-valid package short of its reserve, as an external edit could leave it.

    ``n_active`` acceptances are active and the stored size is 5000 bytes under the
    hard limit, so the deficit is larger than one withdrawal's reserve.
    """
    pkg = v2_package
    for n in range(n_active):
        pkg = accept(replace(pkg, title=f"Rev {n}"), f"C{n}", evidence=["E1"])
    out = padded(pkg, RESERVE_LIMIT_BYTES - 5000)
    reserve = reserve_of(out)
    assert reserve is not None
    assert reserve.byte_deficit > WITHDRAWAL_RESERVE_BYTES
    return out


def test_an_over_reserve_package_is_inspectable_but_refuses_growth(
    v2_package: WorkPackage,
) -> None:
    pkg = deficient(v2_package)
    reserve = reserve_of(pkg)
    assert reserve is not None
    assert not reserve.compliant
    assert len(dumps(pkg)) <= RESERVE_LIMIT_BYTES  # still a valid, readable file
    assert parse_package(dumps(pkg)).package == pkg
    diag = delivery.reserve_deficit_diagnostic(pkg)
    assert diag is not None
    assert diag.code == "delivery.reserve_deficit"
    assert diag.severity == "warning"
    assert codes(lambda: accept(replace(pkg, title="Another"), "C9")) == {
        "delivery.capacity"
    }
    assert summarize(pkg).acceptance_count == 2


def test_a_deficit_reducing_withdrawal_is_accepted_and_a_remaining_deficit_is_kept(
    v2_package: WorkPackage,
) -> None:
    pkg = deficient(v2_package, 3)
    before = reserve_of(pkg)
    assert before is not None
    new = withdraw(pkg, "C0", "W1")
    after = reserve_of(new)
    assert after is not None
    assert after.byte_deficit < before.byte_deficit
    assert len(dumps(new)) <= RESERVE_LIMIT_BYTES
    if not after.compliant:
        assert delivery.reserve_deficit_diagnostic(new) is not None
    # Ordinary growth gets no bypass, even after the recovery.
    if not after.compliant:
        with pytest.raises(OmnitientError):
            delivery.check_reserve(
                new, replace(new, title="x" * 10), code="store.reserve"
            )


def test_a_withdrawal_that_exceeds_the_hard_limit_is_refused(
    v2_package: WorkPackage,
) -> None:
    pkg = deficient(v2_package, 2)
    size = len(dumps(pkg))
    tight = padded(pkg, RESERVE_LIMIT_BYTES - 20)
    assert size < len(dumps(tight)) <= RESERVE_LIMIT_BYTES
    assert codes(lambda: withdraw(tight, "C0", "W1", reason="r" * 100)) == {
        "delivery.capacity"
    }


def test_a_count_only_deficit_cannot_be_reduced_by_a_withdrawal() -> None:
    """2A cannot fall by withdrawing, so no recovery exists for it."""
    assert delivery.Reserve(51, 0, 51, 1000).count_deficit == 2
    after = delivery.Reserve(51, 1, 50, 1100)
    assert after.count_deficit == 2


def test_other_writers_preserve_the_reserve_from_compliant_input(
    v2_package: WorkPackage,
) -> None:
    pkg = accept(v2_package, "C1")
    now = datetime(2026, 5, 6, tzinfo=UTC)
    updates = [
        ops.add_evidence(pkg, kind="commit", ref="abcdef2", recorded_by="x", at=now),
        ops.add_approval(
            pkg, stage="implementation", approved_by="x", note=None, at=now
        ),
        ops.pin_context(pkg, {"spec": "e" * 64}),
        ops.add_context(pkg, replace(pkg.context[1], id="extra", path="docs/other.md")),
    ]
    for new in updates:
        assert new.delivery == pkg.delivery
        reserve = reserve_of(new)
        assert reserve is not None
        assert reserve.active == 1
        assert reserve.compliant
        assert roundtrip(new) == new
