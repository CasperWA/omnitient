# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Evidence is append-only and never touches approved content or engine state."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from omnitient import ops, readiness
from omnitient.errors import OmnitientError
from omnitient.model import Evidence, WorkPackage
from omnitient.serialize import content_digest, to_dict

AT = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def test_appending_evidence_keeps_everything_else_identical(
    approved: WorkPackage,
) -> None:
    before = to_dict(approved)
    new = ops.add_evidence(
        approved,
        kind="pr",
        ref="https://example.org/o/r/pull/7",
        recorded_by="user:alice",
        at=AT,
        summary="Implementation PR",
        run_id="run-123",
        attempt_id="attempt-2",
    )
    after = to_dict(new)
    assert content_digest(new) == content_digest(approved)
    assert new.approvals == approved.approvals
    for key in before:
        if key != "evidence":
            assert after[key] == before[key], key
    assert after["evidence"] == [
        {
            "id": "E1",
            "kind": "pr",
            "ref": "https://example.org/o/r/pull/7",
            "summary": "Implementation PR",
            "recorded_by": "user:alice",
            "recorded_at": "2026-03-04T05:06:07Z",
            "engine": {"run_id": "run-123", "attempt_id": "attempt-2"},
        }
    ]


def test_evidence_accumulates_in_order(package: WorkPackage) -> None:
    pkg = ops.add_evidence(
        package, kind="commit", ref="abcdef1", recorded_by="me", at=AT
    )
    pkg = ops.add_evidence(
        pkg, kind="report", ref="https://example.org/r", recorded_by="me", at=AT
    )
    assert [e.id for e in pkg.evidence] == ["E1", "E2"]
    assert [e.ref for e in pkg.evidence] == ["abcdef1", "https://example.org/r"]


def test_attempt_without_run_is_refused(package: WorkPackage) -> None:
    with pytest.raises(OmnitientError):
        ops.add_evidence(
            package,
            kind="commit",
            ref="abcdef1",
            recorded_by="me",
            at=AT,
            attempt_id="a1",
        )


def test_engine_identities_are_opaque_and_have_no_effect(approved: WorkPackage) -> None:
    new = ops.add_evidence(
        approved, kind="commit", ref="abcdef1", recorded_by="me", at=AT, run_id="run-1"
    )
    assert set(to_dict(new)["evidence"][0]["engine"]) == {
        "run_id"
    }  # no status of any kind
    assert readiness.evaluate(new, [], "planning") == readiness.evaluate(
        approved, [], "planning"
    )
    assert readiness.evaluate(new, [], "implementation") == readiness.evaluate(
        approved, [], "implementation"
    )


def test_history_guard_detects_rewrites(approved: WorkPackage) -> None:
    with_evidence = ops.add_evidence(
        approved, kind="commit", ref="abcdef1", recorded_by="me", at=AT
    )
    rewritten = replace(
        with_evidence,
        evidence=(Evidence("E1", "commit", "1234567", "me", "2026-03-04T05:06:07Z"),),
    )
    with pytest.raises(OmnitientError):
        ops.assert_history_preserved(with_evidence, rewritten)
    with pytest.raises(OmnitientError):
        ops.assert_history_preserved(with_evidence, replace(with_evidence, scope=()))
    with pytest.raises(OmnitientError):
        ops.assert_history_preserved(
            with_evidence, replace(with_evidence, approvals=())
        )
