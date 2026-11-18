# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Strict parsing and validation of work-package JSON.

``parse_package`` collects every problem it finds instead of stopping at the
first, and returns a package only when there are none.
The one exception is an unsupported ``schema_version``: the format of the rest
is then unknown, so it fails early with that single diagnostic (``schema.version``)
and does not interpret the other fields.
This module is the authority on schema validity; the packaged JSON Schema file
is documentation for editors.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from omnitient import rules
from omnitient.errors import Diagnostic
from omnitient.limits import (
    APPROVALS_MAX,
    BLOCK_MAX,
    CONTEXT_MAX,
    DECISIONS_MAX,
    DELIVERY_EVIDENCE_MAX,
    DELIVERY_MAX,
    DELIVERY_REASON_MAX_BYTES,
    EVIDENCE_MAX,
    LINE_MAX,
    LIST_MAX,
    MAX_PACKAGE_BYTES,
    PATH_MAX,
    RECOMMENDATIONS_MAX,
    ROOTS_MAX,
    SHORT_MAX,
)
from omnitient.model import (
    CONTEXT_MODES,
    DECISION_STATUSES,
    DELIVERY_KINDS,
    DIGEST_SCHEMES,
    EVIDENCE_KINDS,
    HANDOFF_STAGES,
    SCHEMA_ID,
    SCHEMA_VERSION_1,
    SCHEMA_VERSION_2,
    SCHEMA_VERSIONS,
    STAGES,
    Acceptance,
    Approval,
    ContextRef,
    Decision,
    DeliveryRecord,
    Dependency,
    EngineRef,
    Evidence,
    EvidenceSnapshot,
    GitProvenance,
    Limits,
    Recommendation,
    Revision,
    Root,
    Withdrawal,
    WorkPackage,
)
from omnitient.safeio import loads_strict
from omnitient.serialize import snapshot_fingerprint

T = TypeVar("T")

_MISSING: Any = object()


@dataclass(frozen=True)
class ParseResult:
    package: WorkPackage | None
    diagnostics: tuple[Diagnostic, ...]


class _Reader:
    """A strict view of one JSON object that records every problem."""

    def __init__(self, value: object, location: str, diags: list[Diagnostic]) -> None:
        self.location = location
        self.diags = diags
        self.seen: set[str] = set()
        self.obj: dict[str, Any]
        if isinstance(value, dict):
            self.obj = value
        else:
            self.error("", "must be an object")
            self.obj = {}

    def at(self, key: str = "") -> str:
        return f"{self.location}.{key}" if key else self.location

    def error(self, key: str, message: str, code: str = "schema.invalid") -> None:
        self.diags.append(Diagnostic(code, "error", message, self.at(key)))

    def raw(self, key: str, required: bool) -> Any:
        self.seen.add(key)
        if key in self.obj:
            return self.obj[key]
        if required:
            self.error(key, "is required", "schema.missing_field")
        return _MISSING

    def text(
        self,
        key: str,
        *,
        max_chars: int,
        multiline: bool = False,
        required: bool = True,
        allow_empty: bool = False,
    ) -> str | None:
        value = self.raw(key, required)
        if value is _MISSING:
            return None
        if not isinstance(value, str):
            self.error(key, "must be a string")
            return None
        if not allow_empty and not value.strip():
            self.error(key, "must not be empty")
            return None
        if len(value) > max_chars:
            self.error(key, f"is longer than {max_chars} characters")
            return None
        problem = rules.text_problem(value, multiline=multiline)
        if problem:
            self.error(key, problem)
            return None
        return value

    def pattern(
        self,
        key: str,
        regex: Any,
        description: str,
        *,
        required: bool = True,
    ) -> str | None:
        value = self.raw(key, required)
        if value is _MISSING:
            return None
        if not isinstance(value, str) or not regex.fullmatch(value):
            self.error(key, f"must be {description}")
            return None
        return value

    def choice(
        self, key: str, allowed: Sequence[T], *, required: bool = True
    ) -> T | None:
        value = self.raw(key, required)
        if value is _MISSING:
            return None
        if not isinstance(value, str) or value not in allowed:
            self.error(key, "must be one of: " + ", ".join(map(str, allowed)))
            return None
        return cast(T, value)

    def flag(self, key: str, default: bool) -> bool:
        value = self.raw(key, False)
        if value is _MISSING:
            return default
        if not isinstance(value, bool):
            self.error(key, "must be true or false")
            return default
        return value

    def integer(self, key: str, *, maximum: int) -> int | None:
        value = self.raw(key, False)
        if value is _MISSING:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self.error(key, "must be an integer")
            return None
        if not 1 <= value <= maximum:
            self.error(key, f"must be between 1 and {maximum}")
            return None
        return value

    def number(self, key: str, *, maximum: float) -> float | None:
        value = self.raw(key, False)
        if value is _MISSING:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.error(key, "must be a number")
            return None
        if not 0 < value <= maximum:
            self.error(key, f"must be greater than 0 and at most {maximum}")
            return None
        return float(value)

    def array(self, key: str, max_items: int) -> list[Any]:
        value = self.raw(key, False)
        if value is _MISSING:
            return []
        if not isinstance(value, list):
            self.error(key, "must be a list")
            return []
        if len(value) > max_items:
            self.error(key, f"has more than {max_items} items")
            return []
        return value

    def lines(self, key: str, max_items: int = LIST_MAX) -> tuple[str, ...]:
        out: list[str] = []
        for index, item in enumerate(self.array(key, max_items)):
            where = f"{key}[{index}]"
            if not isinstance(item, str) or not item.strip():
                self.error(where, "must be a non-empty string")
            elif len(item) > LINE_MAX:
                self.error(where, f"is longer than {LINE_MAX} characters")
            elif problem := rules.text_problem(item, multiline=False):
                self.error(where, problem)
            else:
                out.append(item)
        return tuple(out)

    def records(
        self,
        key: str,
        max_items: int,
        parse_one: Callable[[_Reader], T],
    ) -> tuple[T, ...]:
        return tuple(
            parse_one(_Reader(item, f"{self.at(key)}[{index}]", self.diags))
            for index, item in enumerate(self.array(key, max_items))
        )

    def child(self, key: str) -> _Reader | None:
        value = self.raw(key, False)
        if value is _MISSING:
            return None
        return _Reader(value, self.at(key), self.diags)

    def finish(self) -> None:
        for key in self.obj:
            if key not in self.seen:
                self.error(key, "unknown field", "schema.unknown_field")


def _s(value: str | None) -> str:
    return value if value is not None else ""


def _parse_root(r: _Reader) -> Root:
    name = r.pattern("name", rules.SLUG, "a slug (lowercase letters, digits, '-')")
    description = r.text(
        "description", max_chars=LINE_MAX, required=False, allow_empty=True
    )
    r.finish()
    return Root(_s(name), _s(description))


def _parse_dependency(r: _Reader) -> Dependency:
    package = r.pattern(
        "package", rules.PACKAGE_ID, "a package id 'project/initiative/work-package'"
    )
    note = r.text("note", max_chars=LINE_MAX, required=False)
    r.finish()
    return Dependency(_s(package), note)


def _parse_git(r: _Reader) -> GitProvenance:
    revision = r.pattern(
        "revision", rules.GIT_REVISION, "7-64 lowercase hex characters"
    )
    repository = r.text("repository", max_chars=LINE_MAX, required=False)
    if repository is not None and (problem := rules.repository_problem(repository)):
        r.error("repository", problem)
    ref = r.pattern("ref", rules.LABEL, "a ref label without spaces", required=False)
    r.finish()
    return GitProvenance(_s(revision), repository, ref)


def _parse_context(r: _Reader) -> ContextRef:
    ident = r.pattern(
        "id", rules.IDENT, "an identifier (letters, digits, '_', '-'; max 32)"
    )
    root = r.pattern("root", rules.SLUG, "a slug naming a declared root")
    path = r.text("path", max_chars=PATH_MAX)
    if path is not None:
        problem = rules.relative_path_problem(path) or rules.forbidden_reason(path)
        if problem:
            r.error("path", problem, "schema.unsafe_path")
    digest = r.pattern("sha256", rules.SHA256, "64 lowercase hex characters")
    required = r.flag("required", True)
    mode = r.choice("mode", CONTEXT_MODES, required=False) or "embed"
    purpose = r.text("purpose", max_chars=LINE_MAX, required=False, allow_empty=True)
    git_reader = r.child("git")
    git = _parse_git(git_reader) if git_reader else None
    r.finish()
    return ContextRef(
        _s(ident), _s(root), _s(path), _s(digest), required, mode, _s(purpose), git
    )


def _parse_decision(r: _Reader) -> Decision:
    ident = r.pattern(
        "id", rules.IDENT, "an identifier (letters, digits, '_', '-'; max 32)"
    )
    question = r.text("question", max_chars=LINE_MAX)
    rationale = r.text(
        "rationale",
        max_chars=BLOCK_MAX,
        multiline=True,
        required=False,
        allow_empty=True,
    )
    status = r.choice("status", DECISION_STATUSES)
    answer = r.text("answer", max_chars=BLOCK_MAX, multiline=True, required=False)
    source = r.text("source", max_chars=SHORT_MAX)
    needed_by = r.choice("needed_by", HANDOFF_STAGES)
    applies = r.pattern(
        "applies_to_revision",
        rules.LABEL,
        "a revision label without spaces",
        required=False,
    )
    r.finish()
    if status == "resolved" and answer is None and "answer" not in r.obj:
        r.error(
            "answer", "is required when status is 'resolved'", "schema.missing_field"
        )
    if status == "unresolved" and "answer" in r.obj:
        r.error("answer", "must be absent when status is 'unresolved'")
    return Decision(
        _s(ident),
        _s(question),
        status or "unresolved",
        _s(source),
        needed_by or "implementation",
        _s(rationale),
        answer,
        applies,
    )


def _parse_limits(r: _Reader) -> Limits:
    limits = Limits(
        r.integer("max_turns", maximum=100_000),
        r.integer("max_wall_minutes", maximum=100_000),
        r.number("max_cost_usd", maximum=1_000_000),
    )
    r.finish()
    return limits


def _parse_recommendation(r: _Reader) -> Recommendation:
    ident = r.pattern(
        "id", rules.IDENT, "an identifier (letters, digits, '_', '-'; max 32)"
    )
    stage = r.choice("stage", HANDOFF_STAGES)
    model = r.text("model", max_chars=SHORT_MAX, required=False)
    effort = r.text("effort", max_chars=SHORT_MAX, required=False)
    account = r.text("account", max_chars=SHORT_MAX, required=False)
    limits_reader = r.child("limits")
    limits = _parse_limits(limits_reader) if limits_reader else None
    note = r.text("note", max_chars=LINE_MAX, required=False)
    r.finish()
    return Recommendation(
        _s(ident), stage or "implementation", model, effort, account, limits, note
    )


def _parse_engine(r: _Reader) -> EngineRef:
    run_id = r.pattern("run_id", rules.ENGINE_ID, "an opaque engine identity")
    attempt_id = r.pattern(
        "attempt_id", rules.ENGINE_ID, "an opaque engine identity", required=False
    )
    r.finish()
    return EngineRef(_s(run_id), attempt_id)


def _read_evidence(r: _Reader) -> Evidence:
    """The fields of an evidence entry, without closing the reader."""
    ident = r.pattern(
        "id", rules.IDENT, "an identifier (letters, digits, '_', '-'; max 32)"
    )
    kind = r.choice("kind", EVIDENCE_KINDS)
    ref = r.text("ref", max_chars=PATH_MAX + 40)
    summary = r.text("summary", max_chars=LINE_MAX, required=False)
    by = r.text("recorded_by", max_chars=SHORT_MAX)
    at = r.text("recorded_at", max_chars=32)
    if at is not None and not rules.is_timestamp(at):
        r.error("recorded_at", "must be a UTC timestamp like 2026-01-31T12:00:00Z")
    engine_reader = r.child("engine")
    engine = _parse_engine(engine_reader) if engine_reader else None
    return Evidence(
        _s(ident), kind or "report", _s(ref), _s(by), _s(at), summary, engine
    )


def _parse_evidence(r: _Reader) -> Evidence:
    item = _read_evidence(r)
    r.finish()
    return item


def _parse_snapshot(r: _Reader) -> EvidenceSnapshot:
    """A historical copy of an evidence entry: its own syntax and fingerprint only.

    It is deliberately not compared with the live evidence list, and a report's
    root need not still be declared (docs/o3-closeout-contract.md, section 6).
    """
    item = _read_evidence(r)
    fingerprint = r.pattern(
        "metadata_sha256", rules.SHA256, "64 lowercase hex characters"
    )
    r.finish()
    if fingerprint is not None and fingerprint != snapshot_fingerprint(item):
        r.error(
            "metadata_sha256",
            "does not match the SHA-256 of the snapshot's metadata",
            "schema.fingerprint",
        )
    _check_evidence_ref(item, None, r.location, r.diags)
    return EvidenceSnapshot(item, _s(fingerprint))


def _reason(r: _Reader) -> str:
    """Multi-line safe text of 1 to 8192 UTF-8 bytes, kept exactly as written."""
    value = r.raw("reason", True)
    if value is _MISSING:
        return ""
    if not isinstance(value, str):
        r.error("reason", "must be a string")
        return ""
    if not value.strip():
        r.error("reason", "must not be empty")
        return ""
    problem = rules.text_problem(value, multiline=True)
    if problem:
        r.error("reason", problem)
        return ""
    if len(value.encode("utf-8")) > DELIVERY_REASON_MAX_BYTES:
        r.error("reason", f"is longer than {DELIVERY_REASON_MAX_BYTES} UTF-8 bytes")
        return ""
    return value


def _parse_revision(r: _Reader) -> Revision:
    scheme = r.choice("scheme", (DIGEST_SCHEMES[SCHEMA_VERSION_2],))
    digest = r.pattern("digest", rules.SHA256, "64 lowercase hex characters")
    r.finish()
    return Revision(scheme or "", _s(digest))


def _parse_delivery(r: _Reader) -> DeliveryRecord:
    kind = r.choice("kind", DELIVERY_KINDS)
    operation_id = _s(
        r.pattern(
            "operation_id",
            rules.IDENT,
            "an identifier (an ASCII letter, then letters, digits, '_', '-'; max 32)",
        )
    )
    actor = _s(r.text("actor", max_chars=SHORT_MAX))
    at = r.text("recorded_at", max_chars=32)
    if at is not None and not rules.is_timestamp(at):
        r.error("recorded_at", "must be a UTC timestamp like 2026-01-31T12:00:00Z")
    recorded_at = _s(at)
    reason = _reason(r)
    if kind == "withdrawal":
        target = r.pattern(
            "target_acceptance_id", rules.IDENT, "the id of an acceptance"
        )
        r.finish()
        return Withdrawal(operation_id, actor, recorded_at, reason, _s(target))
    if kind is None:
        r.seen.update(r.obj)  # the unusable kind is the one problem to report
        return Withdrawal(operation_id, actor, recorded_at, reason, "")
    package_id = r.pattern(
        "package_id", rules.PACKAGE_ID, "a package id 'project/initiative/work-package'"
    )
    revision_reader = r.child("revision")
    if revision_reader is None:
        r.error("revision", "is required", "schema.missing_field")
        revision = Revision("", "")
    else:
        revision = _parse_revision(revision_reader)
    snapshots = r.records("evidence", DELIVERY_EVIDENCE_MAX, _parse_snapshot)
    if not snapshots and "evidence" in r.obj and isinstance(r.obj["evidence"], list):
        r.error("evidence", "must select at least one evidence entry")
    if "evidence" not in r.obj:
        r.raw("evidence", True)  # reports the missing field
    r.finish()
    return Acceptance(
        operation_id,
        actor,
        recorded_at,
        reason,
        _s(package_id),
        revision,
        snapshots,
    )


def _parse_approval(r: _Reader) -> Approval:
    ident = r.pattern(
        "id", rules.IDENT, "an identifier (letters, digits, '_', '-'; max 32)"
    )
    stage = r.choice("stage", HANDOFF_STAGES)
    by = r.text("approved_by", max_chars=SHORT_MAX)
    at = r.text("approved_at", max_chars=32)
    if at is not None and not rules.is_timestamp(at):
        r.error("approved_at", "must be a UTC timestamp like 2026-01-31T12:00:00Z")
    digest = r.pattern("content_digest", rules.SHA256, "64 lowercase hex characters")
    note = r.text("note", max_chars=LINE_MAX, required=False)
    r.finish()
    return Approval(
        _s(ident), stage or "implementation", _s(by), _s(at), _s(digest), note
    )


def _check_unique(diags: list[Diagnostic], location: str, ids: Sequence[str]) -> None:
    seen: set[str] = set()
    for index, ident in enumerate(ids):
        if ident in seen:
            diags.append(
                Diagnostic(
                    "schema.duplicate_id",
                    "error",
                    f"duplicate id '{ident}'",
                    f"{location}[{index}]",
                )
            )
        seen.add(ident)


def _check_evidence_ref(
    item: Evidence, roots: set[str] | None, location: str, diags: list[Diagnostic]
) -> None:
    """Check an evidence reference; ``roots`` is ``None`` for a historical snapshot,
    where a report's root need not still be declared."""

    def bad(message: str) -> None:
        diags.append(Diagnostic("schema.invalid", "error", message, f"{location}.ref"))

    if item.kind == "commit":
        if not rules.GIT_REVISION.fullmatch(item.ref):
            bad("must be 7-64 lowercase hex characters for kind 'commit'")
    elif item.kind == "pr":
        if problem := rules.url_problem(item.ref):
            bad(f"{problem} (kind 'pr')")
    elif item.ref.startswith("https://"):
        if problem := rules.url_problem(item.ref):
            bad(problem)
    else:
        root, sep, path = item.ref.partition(":")
        declared = rules.SLUG.fullmatch(root) if roots is None else root in roots
        if not sep or path.startswith("//") or not declared:
            bad(
                "must be an https:// URL or 'declared-root:relative/path' for kind 'report'"
            )
        elif problem := rules.relative_path_problem(path) or rules.forbidden_reason(
            path
        ):
            bad(problem)


def _cross_check(pkg: WorkPackage, diags: list[Diagnostic]) -> None:
    roots = {r.name for r in pkg.roots}
    _check_unique(diags, "$.roots", [r.name for r in pkg.roots])
    _check_unique(diags, "$.context", [c.id for c in pkg.context])
    _check_unique(diags, "$.decisions", [d.id for d in pkg.decisions])
    _check_unique(diags, "$.recommendations", [r.id for r in pkg.recommendations])
    _check_unique(diags, "$.evidence", [e.id for e in pkg.evidence])
    _check_unique(diags, "$.approvals", [a.id for a in pkg.approvals])
    _check_unique(diags, "$.dependencies", [d.package for d in pkg.dependencies])
    for index, ref in enumerate(pkg.context):
        if ref.root not in roots:
            diags.append(
                Diagnostic(
                    "schema.undeclared_root",
                    "error",
                    f"root '{ref.root}' is not declared in 'roots'",
                    f"$.context[{index}].root",
                )
            )
    for index, dep in enumerate(pkg.dependencies):
        if dep.package == pkg.package_id:
            diags.append(
                Diagnostic(
                    "schema.invalid",
                    "error",
                    "a package cannot depend on itself",
                    f"$.dependencies[{index}].package",
                )
            )
    for index, item in enumerate(pkg.evidence):
        _check_evidence_ref(item, roots, f"$.evidence[{index}]", diags)
    _check_delivery(pkg, diags)


def _check_delivery(pkg: WorkPackage, diags: list[Diagnostic]) -> None:
    """History rules that need the whole list (docs/o3-closeout-contract.md, section 4)."""

    def bad(index: int, key: str, message: str) -> None:
        where = f"$.delivery[{index}]" + (f".{key}" if key else "")
        diags.append(Diagnostic("schema.delivery", "error", message, where))

    _check_unique(diags, "$.delivery", [d.operation_id for d in pkg.delivery])
    kinds: dict[str, str] = {}
    withdrawn: dict[str, str] = {}
    active: dict[str, str] = {}  # revision digest -> unwithdrawn acceptance id
    revisions: dict[str, str] = {}
    for index, record in enumerate(pkg.delivery):
        if isinstance(record, Acceptance):
            if record.package_id != pkg.package_id:
                bad(index, "package_id", "must be the id of the enclosing package")
            ids = [e.id for e in record.evidence]
            _check_unique(diags, f"$.delivery[{index}].evidence", ids)
            digest = record.revision.digest
            if digest in active:
                bad(
                    index,
                    "revision",
                    f"overlaps the unwithdrawn acceptance {active[digest]} "
                    "of the same revision",
                )
            active[digest] = record.operation_id
            revisions[record.operation_id] = digest
        else:
            target = record.target_acceptance_id
            if target not in kinds:
                bad(index, "target_acceptance_id", "must name an earlier record")
            elif kinds[target] != "acceptance":
                bad(index, "target_acceptance_id", "must name an acceptance")
            elif target in withdrawn:
                bad(
                    index,
                    "target_acceptance_id",
                    f"is already withdrawn by {withdrawn[target]}",
                )
            else:
                withdrawn[target] = record.operation_id
                if active.get(revisions[target]) == target:
                    del active[revisions[target]]
        kinds.setdefault(record.operation_id, record.kind)


def parse_package(raw: bytes) -> ParseResult:
    """Parse and validate package bytes; ``package`` is ``None`` on any error."""
    if len(raw) > MAX_PACKAGE_BYTES:
        return ParseResult(
            None,
            (
                Diagnostic(
                    "schema.too_large",
                    "error",
                    f"package is {len(raw)} bytes; the limit is {MAX_PACKAGE_BYTES}",
                ),
            ),
        )
    try:
        data = loads_strict(raw.decode("utf-8"))
    except ValueError as exc:
        return ParseResult(
            None, (Diagnostic("schema.syntax", "error", f"not valid JSON: {exc}"),)
        )

    diags: list[Diagnostic] = []
    r = _Reader(data, "$", diags)
    schema = r.raw("schema", True)
    if schema is not _MISSING and schema != SCHEMA_ID:
        r.error("schema", f"must be '{SCHEMA_ID}'")
    version = r.raw("schema_version", True)
    format_version = SCHEMA_VERSION_1
    if version is not _MISSING:
        if (
            isinstance(version, bool)
            or not isinstance(version, int | float)
            or version not in SCHEMA_VERSIONS
            # Legacy v1 accepted any JSON number equal to 1 (1.0, 1e0); that is
            # kept. v2 is new and requires a true integer.
            or (isinstance(version, float) and version != SCHEMA_VERSION_1)
        ):
            r.error(
                "schema_version",
                f"unsupported version {version!r}; this Omnitient reads versions "
                + " and ".join(str(v) for v in SCHEMA_VERSIONS),
                "schema.version",
            )
            return ParseResult(None, tuple(diags))
        format_version = int(version)
    slug = "a slug (lowercase letters, digits, '-'; max 63)"
    pkg = WorkPackage(
        project=_s(r.pattern("project", rules.SLUG, slug)),
        initiative=_s(r.pattern("initiative", rules.SLUG, slug)),
        work_package=_s(r.pattern("work_package", rules.SLUG, slug)),
        title=_s(r.text("title", max_chars=SHORT_MAX)),
        stage=r.choice("stage", STAGES) or "draft",
        objective=_s(r.text("objective", max_chars=BLOCK_MAX, multiline=True)),
        scope=r.lines("scope"),
        exclusions=r.lines("exclusions"),
        acceptance=r.lines("acceptance"),
        dependencies=r.records("dependencies", LIST_MAX, _parse_dependency),
        roots=r.records("roots", ROOTS_MAX, _parse_root),
        context=r.records("context", CONTEXT_MAX, _parse_context),
        decisions=r.records("decisions", DECISIONS_MAX, _parse_decision),
        recommendations=r.records(
            "recommendations", RECOMMENDATIONS_MAX, _parse_recommendation
        ),
        evidence=r.records("evidence", EVIDENCE_MAX, _parse_evidence),
        approvals=r.records("approvals", APPROVALS_MAX, _parse_approval),
        schema_version=format_version,
        delivery=(_required_delivery(r) if format_version >= SCHEMA_VERSION_2 else ()),
    )
    r.finish()
    if not diags:
        _cross_check(pkg, diags)
    return ParseResult(None if diags else pkg, tuple(diags))


def _required_delivery(r: _Reader) -> tuple[DeliveryRecord, ...]:
    """The ``delivery`` array of a v2 package, which must be present."""
    r.raw("delivery", True)  # reports a missing field; the array is read below
    return r.records("delivery", DELIVERY_MAX, _parse_delivery)
