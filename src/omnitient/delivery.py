# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Delivery records: requests, status, summaries, reserve and planning.

Everything here is pure: it takes a loaded package and returns a new one or a
verdict, and never touches a file, the clock (the caller passes the time), the
environment, a process or the network. Persistence is :mod:`omnitient.store`.
See docs/o3-closeout-contract.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from omnitient import ops, rules
from omnitient.errors import Diagnostic, OmnitientError, refusal
from omnitient.limits import (
    DELIVERY_EVIDENCE_MAX,
    DELIVERY_MAX,
    DELIVERY_REASON_MAX_BYTES,
    MAX_PACKAGE_BYTES,
    SHORT_MAX,
)
from omnitient.model import (
    SCHEMA_VERSION_2,
    Acceptance,
    DeliveryRecord,
    Revision,
    Withdrawal,
    WorkPackage,
)
from omnitient.readiness import _TODO_PLACEHOLDER
from omnitient.serialize import content_digest, delivery_to_dict, dumps, snapshot_of

UPGRADE_HINT = (
    "this package is manifest schema version 1, which cannot hold delivery "
    "records; upgrade it explicitly first: "
    "omnitient upgrade PACKAGE --to-version 2 (preview), then add --apply "
    "--expected-file-sha256 HASH"
)

# --- reserve ---------------------------------------------------------------


def _worst_case_withdrawal() -> Withdrawal:
    """The largest withdrawal the schema admits, for the serialized-size bound.

    Actor: 200 characters of 4 UTF-8 bytes each (2-byte escapes of a quote are
    smaller). Reason: 8192 bytes of one-byte characters that JSON escapes to two
    bytes (a quote, a backslash; newline and tab are the only controls allowed).
    Every other field is at its length maximum.
    """
    return Withdrawal(
        operation_id="W" * 32,
        actor="\U0001f600" * SHORT_MAX,
        recorded_at="2026-12-31T23:59:59Z",
        reason='"' * DELIVERY_REASON_MAX_BYTES,
        target_acceptance_id="A" * 32,
    )


def _reserve_bytes() -> int:
    """Largest increase of the stored size when one withdrawal is appended.

    It is measured with the real serializer, once for the first record (the
    empty array becomes a multi-line array) and once for a later record (a
    separator is added), and the larger value is used.
    """
    base = WorkPackage(
        "p",
        "i",
        "w",
        "t",
        "draft",
        "o",
        schema_version=SCHEMA_VERSION_2,
    )
    worst = _worst_case_withdrawal()
    first = len(dumps(replace(base, delivery=(worst,)))) - len(dumps(base))
    one = replace(base, delivery=(worst,))
    later = len(dumps(replace(base, delivery=(worst, worst)))) - len(dumps(one))
    return max(first, later)


WITHDRAWAL_RESERVE_BYTES: int = _reserve_bytes()
RESERVE_LIMIT_BYTES = MAX_PACKAGE_BYTES


@dataclass(frozen=True)
class Reserve:
    """Count and byte figures for the withdrawal reserve of one v2 package."""

    acceptances: int
    withdrawals: int
    active: int
    bytes_stored: int

    @property
    def records_reserved(self) -> int:
        """Records plus one reserved slot for every active acceptance: ``2A``."""
        return self.acceptances + self.withdrawals + self.active

    @property
    def bytes_reserved(self) -> int:
        return self.active * WITHDRAWAL_RESERVE_BYTES

    @property
    def count_deficit(self) -> int:
        return max(0, self.records_reserved - DELIVERY_MAX)

    @property
    def byte_deficit(self) -> int:
        return max(0, self.bytes_stored + self.bytes_reserved - RESERVE_LIMIT_BYTES)

    @property
    def compliant(self) -> bool:
        return not self.count_deficit and not self.byte_deficit

    def to_dict(self) -> dict[str, Any]:
        return {
            "records_used": self.acceptances + self.withdrawals,
            "records_reserved": self.records_reserved,
            "records_limit": DELIVERY_MAX,
            "bytes_stored": self.bytes_stored,
            "bytes_reserved": self.bytes_reserved,
            "bytes_limit": RESERVE_LIMIT_BYTES,
            "withdrawal_reserve_bytes": WITHDRAWAL_RESERVE_BYTES,
            "compliant": self.compliant,
        }

    def describe(self) -> str:
        return (
            f"records {self.records_reserved} of {DELIVERY_MAX} (including "
            f"{self.active} reserved withdrawal slot(s)); bytes "
            f"{self.bytes_stored} stored + {self.bytes_reserved} reserved of "
            f"{RESERVE_LIMIT_BYTES}"
        )


def reserve_of(pkg: WorkPackage) -> Reserve | None:
    """The reserve figures, or ``None`` for v1, which has none."""
    if pkg.schema_version < SCHEMA_VERSION_2:
        return None
    acceptances = sum(isinstance(d, Acceptance) for d in pkg.delivery)
    withdrawals = len(pkg.delivery) - acceptances
    return Reserve(acceptances, withdrawals, acceptances - withdrawals, len(dumps(pkg)))


def reserve_deficit_diagnostic(pkg: WorkPackage) -> Diagnostic | None:
    """A warning for a v2 package that is valid but short of its reserve."""
    reserve = reserve_of(pkg)
    if reserve is None or reserve.compliant:
        return None
    return Diagnostic(
        "delivery.reserve_deficit",
        "warning",
        "the package is short of its withdrawal reserve: "
        + reserve.describe()
        + "; only a withdrawal that reduces the byte deficit is accepted",
        "delivery",
    )


def check_reserve(
    old: WorkPackage, new: WorkPackage, *, recovery: bool = False, code: str
) -> None:
    """Refuse a v2 result that violates the reserve invariants.

    With ``recovery`` (a withdrawal only), a result that fits the hard limits and
    reduces the byte deficit of an already deficient package is accepted.
    """
    reserve = reserve_of(new)
    if reserve is None or reserve.compliant:
        return
    before = reserve_of(old)
    if (
        recovery
        and before is not None
        and not before.compliant
        and reserve.bytes_stored <= MAX_PACKAGE_BYTES
        and len(new.delivery) <= DELIVERY_MAX
        and reserve.byte_deficit < before.byte_deficit
    ):
        return
    raise refusal(
        code,
        "this would leave too little room to withdraw every active acceptance: "
        + reserve.describe()
        + ". Nothing was written; the limits cannot be overridden.",
        "delivery",
    )


# --- status and summary -----------------------------------------------------


@dataclass(frozen=True)
class AcceptanceStatus:
    """What one acceptance is, as observed in one loaded package."""

    record: Acceptance
    withdrawn_by: str | None
    current: bool  # its revision is the package's present revision

    @property
    def state(self) -> Literal["current", "prior", "withdrawn"]:
        if self.withdrawn_by is not None:
            return "withdrawn"
        return "current" if self.current else "prior"


def acceptance_statuses(pkg: WorkPackage) -> tuple[AcceptanceStatus, ...]:
    digest = content_digest(pkg)
    withdrawn = {
        d.target_acceptance_id: d.operation_id
        for d in pkg.delivery
        if isinstance(d, Withdrawal)
    }
    return tuple(
        AcceptanceStatus(d, withdrawn.get(d.operation_id), d.revision.digest == digest)
        for d in pkg.delivery
        if isinstance(d, Acceptance)
    )


SummaryState = Literal["current", "prior_only", "withdrawn_only", "none", "unavailable"]


@dataclass(frozen=True)
class DeliverySummary:
    state: SummaryState
    current_revision_acceptance: str | None
    prior_revision_unwithdrawn_count: int
    withdrawn_acceptance_count: int
    acceptance_count: int
    withdrawal_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "current_revision_acceptance": self.current_revision_acceptance,
            "prior_revision_unwithdrawn_count": self.prior_revision_unwithdrawn_count,
            "withdrawn_acceptance_count": self.withdrawn_acceptance_count,
            "acceptance_count": self.acceptance_count,
            "withdrawal_count": self.withdrawal_count,
        }

    def describe(self) -> str:
        """One line for people; the counts stay beside the state."""
        text = {
            "current": "accepted at the current revision",
            "prior_only": "accepted at a prior revision only",
            "withdrawn_only": "every acceptance withdrawn",
            "none": "no acceptance recorded",
            "unavailable": "unavailable (manifest v1 cannot hold delivery records)",
        }[self.state]
        current = self.current_revision_acceptance or "none"
        return (
            f"{text}; current acceptance {current}, "
            f"prior-revision unwithdrawn {self.prior_revision_unwithdrawn_count}, "
            f"withdrawn {self.withdrawn_acceptance_count}, "
            f"acceptances {self.acceptance_count}, "
            f"withdrawals {self.withdrawal_count}"
        )


def summarize(pkg: WorkPackage) -> DeliverySummary:
    """The compact summary shown by ``show``, the overview and handoffs.

    Derived in the order current, prior only, withdrawn only, none. A declared
    ``stage`` of ``done`` is never an acceptance.
    """
    statuses = acceptance_statuses(pkg)
    withdrawals = len(pkg.delivery) - len(statuses)
    current = next(
        (s.record.operation_id for s in statuses if s.state == "current"), None
    )
    prior = sum(s.state == "prior" for s in statuses)
    withdrawn = sum(s.state == "withdrawn" for s in statuses)
    state: SummaryState
    if pkg.schema_version < SCHEMA_VERSION_2:
        state = "unavailable"
    elif current is not None:
        state = "current"
    elif prior:
        state = "prior_only"
    elif withdrawn:
        state = "withdrawn_only"
    else:
        state = "none"
    return DeliverySummary(state, current, prior, withdrawn, len(statuses), withdrawals)


def history_dict(pkg: WorkPackage) -> list[dict[str, Any]]:
    """The full typed history for ``show --json``, with each record's status."""
    by_id = {s.record.operation_id: s for s in acceptance_statuses(pkg)}
    out: list[dict[str, Any]] = []
    for record in pkg.delivery:
        item = delivery_to_dict(record)
        if isinstance(record, Acceptance):
            status = by_id[record.operation_id]
            item["state"] = status.state
            item["withdrawn_by"] = status.withdrawn_by
        out.append(item)
    return out


def compact_records(pkg: WorkPackage) -> list[dict[str, Any]]:
    """Ids and linkage only, for the overview and handoffs: no reasons, no snapshots."""
    by_id = {s.record.operation_id: s for s in acceptance_statuses(pkg)}
    out: list[dict[str, Any]] = []
    for record in pkg.delivery:
        if isinstance(record, Acceptance):
            status = by_id[record.operation_id]
            out.append(
                {
                    "operation_id": record.operation_id,
                    "kind": "acceptance",
                    "revision": record.revision.digest,
                    "state": status.state,
                    "withdrawn_by": status.withdrawn_by,
                }
            )
        else:
            out.append(
                {
                    "operation_id": record.operation_id,
                    "kind": "withdrawal",
                    "target_acceptance_id": record.target_acceptance_id,
                }
            )
    return out


def delivery_dict(pkg: WorkPackage) -> dict[str, Any]:
    """The compact delivery block of the overview and handoffs."""
    diag = reserve_deficit_diagnostic(pkg)
    return {
        **summarize(pkg).to_dict(),
        "records": compact_records(pkg),
        "diagnostics": [diag.to_dict()] if diag else [],
    }


# --- requests ---------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceRequest:
    operation_id: str
    actor: str
    revision: str
    evidence_ids: tuple[str, ...]  # distinct, sorted: an unordered set
    reason: str


@dataclass(frozen=True)
class WithdrawalRequest:
    operation_id: str
    actor: str
    target: str
    reason: str


def _common_problems(operation_id: str, actor: str, reason: str) -> list[Diagnostic]:
    problems: list[Diagnostic] = []

    def bad(location: str, message: str) -> None:
        problems.append(
            Diagnostic("delivery.request_invalid", "error", message, location)
        )

    if not rules.IDENT.fullmatch(operation_id):
        bad(
            "operation-id",
            "must be an ASCII letter followed by letters, digits, '_' or '-' "
            "(1 to 32 characters, case-sensitive)",
        )
    if not actor.strip():
        bad("by", "must not be blank")
    elif len(actor) > SHORT_MAX:
        bad("by", f"is longer than {SHORT_MAX} characters")
    elif problem := rules.text_problem(actor, multiline=False):
        bad("by", problem)
    if not reason.strip():
        bad("reason", "must not be blank")
    elif problem := rules.text_problem(reason, multiline=True):
        bad("reason", problem)
    elif len(reason.encode("utf-8")) > DELIVERY_REASON_MAX_BYTES:
        bad("reason", f"is longer than {DELIVERY_REASON_MAX_BYTES} UTF-8 bytes")
    return problems


def acceptance_request(
    *,
    operation_id: str,
    actor: str,
    revision: str,
    evidence: Sequence[str],
    reason: str,
) -> AcceptanceRequest:
    """Validate a request's syntax; every problem is reported, nothing is repaired."""
    problems = _common_problems(operation_id, actor, reason)
    if not rules.SHA256.fullmatch(revision):
        problems.append(
            Diagnostic(
                "delivery.request_invalid",
                "error",
                "must be the 64 lowercase hex characters of a revision digest",
                "revision",
            )
        )
    if not evidence:
        problems.append(
            Diagnostic(
                "delivery.request_invalid",
                "error",
                "select at least one evidence id",
                "evidence",
            )
        )
    elif len(evidence) > DELIVERY_EVIDENCE_MAX:
        problems.append(
            Diagnostic(
                "delivery.request_invalid",
                "error",
                f"selects {len(evidence)} evidence ids; at most {DELIVERY_EVIDENCE_MAX}",
                "evidence",
            )
        )
    for ident in evidence:
        if not rules.IDENT.fullmatch(ident):
            problems.append(
                Diagnostic(
                    "delivery.request_invalid",
                    "error",
                    f"{ident!r} is not an evidence id",
                    "evidence",
                )
            )
    repeated = sorted({i for i in evidence if list(evidence).count(i) > 1})
    if repeated:
        problems.append(
            Diagnostic(
                "delivery.request_invalid",
                "error",
                f"evidence id(s) selected more than once: {', '.join(repeated)}",
                "evidence",
            )
        )
    if problems:
        raise OmnitientError(problems)
    return AcceptanceRequest(
        operation_id, actor, revision, tuple(sorted(evidence)), reason
    )


def withdrawal_request(
    *, operation_id: str, actor: str, target: str, reason: str
) -> WithdrawalRequest:
    problems = _common_problems(operation_id, actor, reason)
    if not rules.IDENT.fullmatch(target):
        problems.append(
            Diagnostic(
                "delivery.request_invalid",
                "error",
                "must be the id of an acceptance",
                "closeout-id",
            )
        )
    if problems:
        raise OmnitientError(problems)
    return WithdrawalRequest(operation_id, actor, target, reason)


# --- planning -----------------------------------------------------------------

Outcome = Literal["saved", "replayed"]


@dataclass(frozen=True)
class Planned:
    """The result of planning one operation against one loaded package.

    For ``replayed`` the package is the loaded one and nothing must be written.
    ``status`` is the observed status of the acceptance involved: the record
    itself for an acceptance, its target for a withdrawal.
    """

    outcome: Outcome
    package: WorkPackage
    record: DeliveryRecord
    status: AcceptanceStatus


def _find(pkg: WorkPackage, operation_id: str) -> DeliveryRecord | None:
    return next((d for d in pkg.delivery if d.operation_id == operation_id), None)


def _status_of(pkg: WorkPackage, acceptance_id: str) -> AcceptanceStatus:
    return next(
        s for s in acceptance_statuses(pkg) if s.record.operation_id == acceptance_id
    )


def _conflict(existing: DeliveryRecord, fields: Sequence[str]) -> OmnitientError:
    return refusal(
        "delivery.conflict",
        f"operation id {existing.operation_id} already exists as a "
        f"{existing.kind} and the request differs in: {', '.join(fields)}. "
        "Nothing was written. Use the original request to replay it, or a new "
        "operation id for a new operation.",
        existing.operation_id,
    )


def plan_acceptance(
    pkg: WorkPackage, request: AcceptanceRequest, at: datetime
) -> Planned:
    """Replay an equal request, or append one acceptance if every gate allows it."""
    existing = _find(pkg, request.operation_id)
    if existing is not None:
        if not isinstance(existing, Acceptance):
            raise _conflict(existing, ["kind"])
        differs = [
            name
            for name, same in (
                ("actor", existing.actor == request.actor),
                ("reason", existing.reason == request.reason),
                ("revision", existing.revision.digest == request.revision),
                (
                    "evidence",
                    tuple(sorted(e.id for e in existing.evidence))
                    == request.evidence_ids,
                ),
            )
            if not same
        ]
        if differs:
            raise _conflict(existing, differs)
        return Planned(
            "replayed", pkg, existing, _status_of(pkg, existing.operation_id)
        )

    if pkg.schema_version < SCHEMA_VERSION_2:
        raise refusal("delivery.requires_v2", UPGRADE_HINT)
    gaps = [
        text
        for text, bad in (
            (
                "objective is a TODO placeholder",
                _TODO_PLACEHOLDER.match(pkg.objective.strip()),
            ),
            ("scope is empty", not pkg.scope),
            ("acceptance criteria are empty", not pkg.acceptance),
        )
        if bad
    ]
    if gaps:
        raise OmnitientError(
            Diagnostic(
                "delivery.incomplete",
                "error",
                "the package cannot be accepted: " + "; ".join(gaps),
            )
        )
    current = content_digest(pkg)
    if request.revision != current:
        raise refusal(
            "delivery.revision_mismatch",
            f"the request names revision {request.revision[:12]} but the package's "
            f"present revision is {current[:12]}; accept the present revision "
            "explicitly after inspecting it",
            "revision",
        )
    live = {e.id: e for e in pkg.evidence}
    unknown = [i for i in request.evidence_ids if i not in live]
    if unknown:
        raise refusal(
            "delivery.evidence_unknown",
            f"no evidence entry with id: {', '.join(unknown)}",
            "evidence",
        )
    for status in acceptance_statuses(pkg):
        if status.withdrawn_by is None and status.record.revision.digest == current:
            raise refusal(
                "delivery.duplicate_acceptance",
                f"revision {current[:12]} already has the active acceptance "
                f"{status.record.operation_id}; withdraw it first to accept again",
                status.record.operation_id,
            )
    record = Acceptance(
        operation_id=request.operation_id,
        actor=request.actor,
        recorded_at=ops.timestamp(at),
        reason=request.reason,
        package_id=pkg.package_id,
        revision=Revision(pkg.digest_scheme, current),
        evidence=tuple(snapshot_of(live[i]) for i in request.evidence_ids),
    )
    new = replace(pkg, delivery=(*pkg.delivery, record))
    check_reserve(pkg, new, code="delivery.capacity")
    ops.assert_history_preserved(pkg, new)
    return Planned("saved", new, record, _status_of(new, record.operation_id))


def plan_withdrawal(
    pkg: WorkPackage, request: WithdrawalRequest, at: datetime
) -> Planned:
    """Replay an equal request, or append one withdrawal of an active acceptance."""
    existing = _find(pkg, request.operation_id)
    if existing is not None:
        if not isinstance(existing, Withdrawal):
            raise _conflict(existing, ["kind"])
        differs = [
            name
            for name, same in (
                ("actor", existing.actor == request.actor),
                ("reason", existing.reason == request.reason),
                ("target", existing.target_acceptance_id == request.target),
            )
            if not same
        ]
        if differs:
            raise _conflict(existing, differs)
        return Planned(
            "replayed", pkg, existing, _status_of(pkg, existing.target_acceptance_id)
        )

    target = _find(pkg, request.target)
    if target is None:
        detail = (
            " (manifest v1 holds no delivery history)"
            if pkg.schema_version < SCHEMA_VERSION_2
            else ""
        )
        raise refusal(
            "delivery.target_unknown",
            f"there is no acceptance with id {request.target}{detail}",
            "closeout-id",
        )
    if not isinstance(target, Acceptance):
        raise refusal(
            "delivery.target_not_acceptance",
            f"{request.target} is a {target.kind}, not an acceptance; a withdrawal "
            "cannot be withdrawn",
            "closeout-id",
        )
    status = _status_of(pkg, request.target)
    if status.withdrawn_by is not None:
        raise refusal(
            "delivery.already_withdrawn",
            f"acceptance {request.target} was already withdrawn by "
            f"{status.withdrawn_by}",
            "closeout-id",
        )
    record = Withdrawal(
        operation_id=request.operation_id,
        actor=request.actor,
        recorded_at=ops.timestamp(at),
        reason=request.reason,
        target_acceptance_id=request.target,
    )
    new = replace(pkg, delivery=(*pkg.delivery, record))
    check_reserve(pkg, new, recovery=True, code="delivery.capacity")
    ops.assert_history_preserved(pkg, new)
    return Planned("saved", new, record, _status_of(new, request.target))
