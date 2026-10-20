"""Pure package transformations: scaffold, approve, add evidence, edit context.

Each function returns a new package.
Persistence, locking and re-validation happen in :mod:`omnitient.store`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from omnitient import readiness
from omnitient.context import ResolvedContext
from omnitient.errors import Diagnostic, OmnitientError, refusal
from omnitient.model import (
    Approval,
    ContextRef,
    EngineRef,
    Evidence,
    EvidenceKind,
    HandoffStage,
    Root,
    WorkPackage,
    stage_rank,
)
from omnitient.rules import TIMESTAMP_FORMAT
from omnitient.serialize import content_digest

PLACEHOLDER_OBJECTIVE = "TODO: state the objective of this work package."


def timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def next_id(prefix: str, existing: Collection[str]) -> str:
    number = len(existing) + 1
    while f"{prefix}{number}" in existing:
        number += 1
    return f"{prefix}{number}"


def new_package(
    project: str, initiative: str, work_package: str, title: str
) -> WorkPackage:
    """A draft, unapproved scaffold. Never contains approvals."""
    return WorkPackage(
        project=project,
        initiative=initiative,
        work_package=work_package,
        title=title,
        stage="draft",
        objective=PLACEHOLDER_OBJECTIVE,
    )


def add_approval(
    pkg: WorkPackage,
    *,
    stage: HandoffStage,
    approved_by: str,
    note: str | None,
    at: datetime,
    resolved: Sequence[ResolvedContext] | None = None,
) -> WorkPackage:
    """Record approval of the present revision for ``stage``.

    Refuses while a decision blocks ``stage`` or the content is incomplete;
    when ``resolved`` is given, also while required context is unusable.
    Raises ``stage`` to the approved stage, never lowers it.
    """
    report = readiness.evaluate(pkg, resolved or [], stage)
    blockers = [b for b in report.blockers if not b.code.startswith("approval.")]
    if blockers:
        raise OmnitientError(blockers)
    approval = Approval(
        id=next_id("A", {a.id for a in pkg.approvals}),
        stage=stage,
        approved_by=approved_by,
        approved_at=timestamp(at),
        content_digest=content_digest(pkg),
        note=note,
    )
    new_stage = stage if stage_rank(pkg.stage) < stage_rank(stage) else pkg.stage
    new = replace(pkg, stage=new_stage, approvals=(*pkg.approvals, approval))
    assert_history_preserved(pkg, new)
    return new


def add_evidence(
    pkg: WorkPackage,
    *,
    kind: EvidenceKind,
    ref: str,
    recorded_by: str,
    at: datetime,
    summary: str | None = None,
    run_id: str | None = None,
    attempt_id: str | None = None,
) -> WorkPackage:
    """Append evidence. Approved content and earlier records are untouched."""
    if attempt_id and not run_id:
        raise refusal("evidence.invalid", "--attempt-id requires --run-id")
    item = Evidence(
        id=next_id("E", {e.id for e in pkg.evidence}),
        kind=kind,
        ref=ref,
        recorded_by=recorded_by,
        recorded_at=timestamp(at),
        summary=summary,
        engine=EngineRef(run_id, attempt_id) if run_id else None,
    )
    new = replace(pkg, evidence=(*pkg.evidence, item))
    assert_history_preserved(pkg, new)
    return new


def assert_history_preserved(old: WorkPackage, new: WorkPackage) -> None:
    """Guard: ``new`` only appends evidence/approvals; approved content is unchanged."""
    ok = (
        content_digest(old) == content_digest(new)
        and new.evidence[: len(old.evidence)] == old.evidence
        and new.approvals[: len(old.approvals)] == old.approvals
        and new.recommendations == old.recommendations
    )
    if not ok:
        raise OmnitientError(
            Diagnostic(
                "store.history",
                "error",
                "update would rewrite approved content or history",
            )
        )


def add_context(pkg: WorkPackage, ref: ContextRef) -> WorkPackage:
    """Add a context reference, declaring its root if the package lacks it."""
    roots = pkg.roots
    if ref.root not in {r.name for r in roots}:
        roots = (*roots, Root(ref.root))
    return replace(pkg, roots=roots, context=(*pkg.context, ref))


def pin_context(pkg: WorkPackage, digests: Mapping[str, str]) -> WorkPackage:
    """Replace the expected digest of the named references."""
    known = {c.id for c in pkg.context}
    missing = sorted(set(digests) - known)
    if missing:
        raise refusal(
            "context.unknown", f"no context reference with id: {', '.join(missing)}"
        )
    return replace(
        pkg,
        context=tuple(
            replace(c, sha256=digests[c.id]) if c.id in digests else c
            for c in pkg.context
        ),
    )
