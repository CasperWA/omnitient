# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Immutable data model for work packages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SCHEMA_ID = "omnitient/work-package"
SCHEMA_VERSION_1 = 1
SCHEMA_VERSION_2 = 2
SCHEMA_VERSIONS: tuple[int, ...] = (SCHEMA_VERSION_1, SCHEMA_VERSION_2)
LATEST_SCHEMA_VERSION = SCHEMA_VERSION_2

# The digest scheme follows the manifest format and is never configured
# (docs/o3-closeout-contract.md, section 3).
DIGEST_SCHEMES: dict[int, str] = {
    SCHEMA_VERSION_1: "covered-sha256-v1",
    SCHEMA_VERSION_2: "covered-sha256-v2",
}

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
DeliveryKind = Literal["acceptance", "withdrawal"]
DELIVERY_KINDS: tuple[DeliveryKind, ...] = ("acceptance", "withdrawal")


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
class Revision:
    """A digest scheme label and the covered-content digest it names."""

    scheme: str
    digest: str


@dataclass(frozen=True)
class EvidenceSnapshot:
    """A copy of one evidence entry, taken when an acceptance was recorded.

    ``evidence`` holds the entry's metadata exactly as it was stored;
    ``metadata_sha256`` is the fingerprint of its canonical serialization.
    """

    evidence: Evidence
    metadata_sha256: str

    @property
    def id(self) -> str:
        return self.evidence.id


@dataclass(frozen=True)
class Acceptance:
    operation_id: str
    actor: str
    recorded_at: str
    reason: str
    package_id: str
    revision: Revision
    evidence: tuple[EvidenceSnapshot, ...]
    kind: Literal["acceptance"] = "acceptance"


@dataclass(frozen=True)
class Withdrawal:
    operation_id: str
    actor: str
    recorded_at: str
    reason: str
    target_acceptance_id: str
    kind: Literal["withdrawal"] = "withdrawal"


DeliveryRecord = Acceptance | Withdrawal


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
    # The manifest format this package was read from or created as. Delivery
    # history exists only in format 2; format 1 keeps ``delivery`` empty.
    schema_version: int = SCHEMA_VERSION_1
    delivery: tuple[DeliveryRecord, ...] = ()

    @property
    def digest_scheme(self) -> str:
        return DIGEST_SCHEMES[self.schema_version]

    @property
    def package_id(self) -> str:
        return f"{self.project}/{self.initiative}/{self.work_package}"
