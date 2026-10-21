# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Strict parsing and validation of work-package JSON.

``parse_package`` collects every problem it finds instead of stopping at the
first, and returns a package only when there are none.
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
    EVIDENCE_KINDS,
    HANDOFF_STAGES,
    SCHEMA_ID,
    SCHEMA_VERSION,
    STAGES,
    Approval,
    ContextRef,
    Decision,
    Dependency,
    EngineRef,
    Evidence,
    GitProvenance,
    Limits,
    Recommendation,
    Root,
    WorkPackage,
)
from omnitient.safeio import loads_strict

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


def _parse_evidence(r: _Reader) -> Evidence:
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
    r.finish()
    return Evidence(
        _s(ident), kind or "report", _s(ref), _s(by), _s(at), summary, engine
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
    item: Evidence, roots: set[str], location: str, diags: list[Diagnostic]
) -> None:
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
        if not sep or path.startswith("//") or root not in roots:
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
    if version is not _MISSING and (
        isinstance(version, bool) or version != SCHEMA_VERSION
    ):
        r.error(
            "schema_version",
            f"unsupported version {version!r}; this Omnitient reads version {SCHEMA_VERSION}",
            "schema.version",
        )
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
    )
    r.finish()
    if not diags:
        _cross_check(pkg, diags)
    return ParseResult(None if diags else pkg, tuple(diags))
