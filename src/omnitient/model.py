"""Immutable data model for work packages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SCHEMA_ID = "omnitient/work-package"
SCHEMA_VERSION = 1

Stage = Literal["draft", "planning", "implementation", "review", "done"]
HandoffStage = Literal["planning", "implementation", "review"]
ContextMode = Literal["embed", "reference"]
DecisionStatus = Literal["unresolved", "resolved"]
EvidenceKind = Literal["report", "pr", "commit"]

STAGES: tuple[Stage, ...] = ("draft", "planning", "implementation", "review", "done")
HANDOFF_STAGES: tuple[HandoffStage, ...] = ("planning", "implementation", "review")
CONTEXT_MODES: tuple[ContextMode, ...] = ("embed", "reference")
DECISION_STATUSES: tuple[DecisionStatus, ...] = ("unresolved", "resolved")
EVIDENCE_KINDS: tuple[EvidenceKind, ...] = ("report", "pr", "commit")


def stage_rank(stage: Stage) -> int:
    """Order of handoff stages; ``draft`` ranks below, ``done`` above them all."""
    return STAGES.index(stage)


@dataclass(frozen=True)
class Root:
    name: str
    description: str = ""


@dataclass(frozen=True)
class Dependency:
    package: str
    note: str | None = None


@dataclass(frozen=True)
class GitProvenance:
    revision: str
    repository: str | None = None
    ref: str | None = None


@dataclass(frozen=True)
class ContextRef:
    id: str
    root: str
    path: str
    sha256: str
    required: bool = True
    mode: ContextMode = "embed"
    purpose: str = ""
    git: GitProvenance | None = None


@dataclass(frozen=True)
class Decision:
    id: str
    question: str
    status: DecisionStatus
    source: str
    needed_by: HandoffStage
    rationale: str = ""
    answer: str | None = None
    applies_to_revision: str | None = None


@dataclass(frozen=True)
class Limits:
    max_turns: int | None = None
    max_wall_minutes: int | None = None
    max_cost_usd: float | None = None


@dataclass(frozen=True)
class Recommendation:
    id: str
    stage: HandoffStage
    model: str | None = None
    effort: str | None = None
    account: str | None = None
    limits: Limits | None = None
    note: str | None = None


@dataclass(frozen=True)
class EngineRef:
    run_id: str
    attempt_id: str | None = None


@dataclass(frozen=True)
class Evidence:
    id: str
    kind: EvidenceKind
    ref: str
    recorded_by: str
    recorded_at: str
    summary: str | None = None
    engine: EngineRef | None = None


@dataclass(frozen=True)
class Approval:
    id: str
    stage: HandoffStage
    approved_by: str
    approved_at: str
    content_digest: str
    note: str | None = None


@dataclass(frozen=True)
class WorkPackage:
    project: str
    initiative: str
    work_package: str
    title: str
    stage: Stage
    objective: str
    scope: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    roots: tuple[Root, ...] = ()
    context: tuple[ContextRef, ...] = ()
    decisions: tuple[Decision, ...] = ()
    recommendations: tuple[Recommendation, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    approvals: tuple[Approval, ...] = ()

    @property
    def package_id(self) -> str:
        return f"{self.project}/{self.initiative}/{self.work_package}"
