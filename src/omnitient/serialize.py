"""Canonical serialisation, the stored file format and the content digest."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from omnitient.model import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    Approval,
    ContextRef,
    Decision,
    Dependency,
    Evidence,
    Recommendation,
    WorkPackage,
)


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def context_to_dict(ref: ContextRef) -> dict[str, Any]:
    return _drop_none(
        {
            "id": ref.id,
            "root": ref.root,
            "path": ref.path,
            "sha256": ref.sha256,
            "required": ref.required,
            "mode": ref.mode,
            "purpose": ref.purpose,
            "git": (
                _drop_none(
                    {
                        "revision": ref.git.revision,
                        "repository": ref.git.repository,
                        "ref": ref.git.ref,
                    }
                )
                if ref.git
                else None
            ),
        }
    )


def decision_to_dict(item: Decision) -> dict[str, Any]:
    return _drop_none(
        {
            "id": item.id,
            "question": item.question,
            "rationale": item.rationale,
            "status": item.status,
            "answer": item.answer,
            "source": item.source,
            "needed_by": item.needed_by,
            "applies_to_revision": item.applies_to_revision,
        }
    )


def dependency_to_dict(item: Dependency) -> dict[str, Any]:
    return _drop_none({"package": item.package, "note": item.note})


def recommendation_to_dict(item: Recommendation) -> dict[str, Any]:
    limits = (
        _drop_none(
            {
                "max_turns": item.limits.max_turns,
                "max_wall_minutes": item.limits.max_wall_minutes,
                "max_cost_usd": item.limits.max_cost_usd,
            }
        )
        if item.limits
        else None
    )
    return _drop_none(
        {
            "id": item.id,
            "stage": item.stage,
            "model": item.model,
            "effort": item.effort,
            "account": item.account,
            "limits": limits,
            "note": item.note,
        }
    )


def evidence_to_dict(item: Evidence) -> dict[str, Any]:
    engine = (
        _drop_none({"run_id": item.engine.run_id, "attempt_id": item.engine.attempt_id})
        if item.engine
        else None
    )
    return _drop_none(
        {
            "id": item.id,
            "kind": item.kind,
            "ref": item.ref,
            "summary": item.summary,
            "recorded_by": item.recorded_by,
            "recorded_at": item.recorded_at,
            "engine": engine,
        }
    )


def approval_to_dict(item: Approval) -> dict[str, Any]:
    return _drop_none(
        {
            "id": item.id,
            "stage": item.stage,
            "approved_by": item.approved_by,
            "approved_at": item.approved_at,
            "content_digest": item.content_digest,
            "note": item.note,
        }
    )


def covered_dict(pkg: WorkPackage) -> dict[str, Any]:
    """The approval-covered content (see docs/o1-contract.md, section 4.2)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "project": pkg.project,
        "initiative": pkg.initiative,
        "work_package": pkg.work_package,
        "title": pkg.title,
        "objective": pkg.objective,
        "scope": list(pkg.scope),
        "exclusions": list(pkg.exclusions),
        "acceptance": list(pkg.acceptance),
        "dependencies": [dependency_to_dict(d) for d in pkg.dependencies],
        "roots": [{"name": r.name, "description": r.description} for r in pkg.roots],
        "context": [context_to_dict(c) for c in pkg.context],
        "decisions": [decision_to_dict(d) for d in pkg.decisions],
    }


def to_dict(pkg: WorkPackage) -> dict[str, Any]:
    """The full stored form, in fixed key order."""
    covered = covered_dict(pkg)
    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "project": pkg.project,
        "initiative": pkg.initiative,
        "work_package": pkg.work_package,
        "title": pkg.title,
        "stage": pkg.stage,
        "objective": pkg.objective,
        "scope": covered["scope"],
        "exclusions": covered["exclusions"],
        "acceptance": covered["acceptance"],
        "dependencies": covered["dependencies"],
        "roots": covered["roots"],
        "context": covered["context"],
        "decisions": covered["decisions"],
        "recommendations": [recommendation_to_dict(r) for r in pkg.recommendations],
        "evidence": [evidence_to_dict(e) for e in pkg.evidence],
        "approvals": [approval_to_dict(a) for a in pkg.approvals],
    }


def canonical_bytes(data: Any) -> bytes:
    """Sorted keys, no insignificant whitespace, UTF-8."""
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_digest(pkg: WorkPackage) -> str:
    """The package revision: SHA-256 over the approval-covered content."""
    return hashlib.sha256(canonical_bytes(covered_dict(pkg))).hexdigest()


def dumps(pkg: WorkPackage) -> bytes:
    """The stored file form: two-space indent, fixed key order, final newline."""
    text = json.dumps(to_dict(pkg), indent=2, ensure_ascii=False) + "\n"
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
