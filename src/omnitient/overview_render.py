# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Serialisation of an :class:`~omnitient.overview.Overview`; no I/O.

The JSON form follows docs/o2-overview-contract.md, section 5, and the text form
escapes everything a terminal could act on.
Both are bounded by ``MAX_OUTPUT_BYTES``: a result that does not fit is replaced
by a compact ``omitted`` refusal, never cut short.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omnitient.errors import Diagnostic
from omnitient.limits import MAX_OUTPUT_BYTES
from omnitient.overview import (
    SCHEMA,
    SCHEMA_VERSION,
    Action,
    Counts,
    Entry,
    Overview,
    StageReport,
)

_TITLE_WIDTH = 100
_MESSAGE_WIDTH = 200
_ELLIPSIS = "…"


@dataclass(frozen=True)
class Rendered:
    """The bytes to write and the overview they stand for.

    ``overview`` is the one that was delivered: the output-limit refusal when the
    requested form did not fit.
    """

    data: bytes
    overview: Overview


def _diagnostic(diag: Diagnostic) -> dict[str, Any]:
    return {
        "code": diag.code,
        "severity": diag.severity,
        "message": diag.message,
        "location": diag.location or None,
    }


def _action(action: Action) -> dict[str, Any]:
    return {
        "code": action.code,
        "diagnostics": [
            {"code": r.code, "location": r.location} for r in action.references
        ],
    }


def _stage(report: StageReport) -> dict[str, Any]:
    return {
        "ready": report.ready,
        "blockers": [_diagnostic(d) for d in report.blockers],
        "warnings": [_diagnostic(d) for d in report.warnings],
        "actions": [_action(a) for a in report.actions],
    }


def _entry(entry: Entry) -> dict[str, Any]:
    package = entry.package
    return {
        "path": entry.path,
        "status": entry.status,
        "package": (
            None
            if package is None
            else {
                "id": package.id,
                "title": package.title,
                "declared_stage": package.declared_stage,
                "content_digest": package.content_digest,
                "manifest_schema_version": package.manifest_schema_version,
                "digest_scheme": package.digest_scheme,
                "delivery": package.delivery.to_dict(),
            }
        ),
        "diagnostics": [_diagnostic(d) for d in entry.diagnostics],
        "readiness": (
            None
            if entry.readiness is None
            else {report.stage: _stage(report) for report in entry.readiness}
        ),
    }


def _counts(counts: Counts | None) -> dict[str, int] | None:
    if counts is None:
        return None
    return {
        "collection_entries": counts.collection_entries,
        "ignored": counts.ignored,
        "candidates": counts.candidates,
        "valid": counts.valid,
        "invalid": counts.invalid,
        "excluded": counts.excluded,
        "unassessed": counts.unassessed,
    }


def to_document(ov: Overview) -> dict[str, Any]:
    """The JSON document, with the contract's fixed key order."""
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "discovery_complete": ov.discovery_complete,
        "assessment_complete": ov.assessment_complete,
        "results_status": ov.results_status,
        "counts": _counts(ov.counts),
        "diagnostics": [_diagnostic(d) for d in ov.diagnostics],
        "entries": [_entry(e) for e in ov.entries],
    }


def dumps_json(ov: Overview) -> bytes:
    """Canonical JSON for ``ov``: ASCII only, two-space indent, one final LF."""
    text = json.dumps(to_document(ov), ensure_ascii=True, allow_nan=False, indent=2)
    return (text + "\n").encode("ascii")


def _over_limit(ov: Overview) -> Overview:
    return Overview(
        ov.discovery_complete,
        ov.assessment_complete,
        "omitted",
        None,
        (
            Diagnostic(
                "overview.output_limit",
                "error",
                f"the result is larger than {MAX_OUTPUT_BYTES} bytes, "
                "so no entries are shown",
                "output",
            ),
        ),
        (),
    )


def _bounded(ov: Overview, serialize: Callable[[Overview], bytes]) -> Rendered:
    data = serialize(ov)
    if len(data) <= MAX_OUTPUT_BYTES:
        return Rendered(data, ov)
    refusal = _over_limit(ov)
    return Rendered(serialize(refusal), refusal)


def render_json(ov: Overview) -> Rendered:
    return _bounded(ov, dumps_json)


# --- text -----------------------------------------------------------------


def escape_text(text: str) -> str:
    """``text`` with every character a terminal could act on written as an escape.

    Control characters (including newline and tab), bidirectional and other format
    characters, surrogates, unassigned and private-use code points and spaces other
    than U+0020 become ``\\xNN``, ``\\uNNNN`` or ``\\UNNNNNNNN``; the backslash is
    doubled so an escape is never ambiguous.
    """
    out: list[str] = []
    for char in text:
        if char == "\\":
            out.append("\\\\")
        elif char == " " or char.isprintable():
            out.append(char)
        else:
            code = ord(char)
            width, letter = (2, "x") if code < 0x100 else (4, "u")
            if code > 0xFFFF:
                width, letter = 8, "U"
            out.append(f"\\{letter}{code:0{width}x}")
    return "".join(out)


def shorten(text: str, width: int) -> str:
    """``text`` limited to ``width`` source characters, marked, then escaped.

    Text longer than ``width`` is cut to ``width - 1`` source characters and
    followed by an ellipsis; the cut text is escaped afterwards. The limit counts
    characters of the text as written, so it is neither a terminal-column width
    nor a limit on the escaped length: escapes can make the result longer.
    """
    if len(text) > width:
        return escape_text(text[: width - 1]) + _ELLIPSIS
    return escape_text(text)


def _diag_line(diag: Diagnostic, indent: str) -> str:
    where = f" {escape_text(diag.location)}" if diag.location else ""
    message = shorten(diag.message, _MESSAGE_WIDTH)
    return f"{indent}{diag.severity}[{diag.code}]{where}: {message}"


def _refs(action: Action) -> str:
    if not action.references:
        return ""
    parts = [
        r.code if r.location is None else f"{r.code} {r.location}"
        for r in action.references
    ]
    return " <- " + ", ".join(escape_text(p) for p in parts)


def _entry_lines(entry: Entry) -> list[str]:
    lines = [f"[{entry.status}] {escape_text(entry.path)}"]
    package = entry.package
    if package is not None:
        lines.append(
            f'  {package.id}  "{shorten(package.title, _TITLE_WIDTH)}"  '
            f"declared stage: {package.declared_stage}  "
            f"revision: {package.content_digest[:12]}  "
            f"manifest: v{package.manifest_schema_version}"
        )
        lines.append(f"  delivery: {escape_text(package.delivery.describe())}")
    lines += [_diag_line(d, "  ") for d in entry.diagnostics]
    for report in entry.readiness or ():
        state = (
            "READY" if report.ready else f"BLOCKED ({len(report.blockers)} blocker(s))"
        )
        lines.append(f"  {report.stage}: {state}")
        lines += [_diag_line(d, "    ") for d in (*report.blockers, *report.warnings)]
        lines += [f"    advice: {a.code}{_refs(a)}" for a in report.actions]
    return lines


def dumps_text(ov: Overview) -> bytes:
    """Human-readable report for a delivered (not omitted) overview."""
    counts = ov.counts
    lines = ["omnitient overview"]
    if counts is not None:
        lines.append(
            f"entries {counts.collection_entries}: {counts.ignored} ignored, "
            f"{counts.candidates} candidate(s): {counts.valid} valid, "
            f"{counts.invalid} invalid, {counts.excluded} excluded, "
            f"{counts.unassessed} unassessed"
        )
    lines.append(
        f"discovery {'complete' if ov.discovery_complete else 'INCOMPLETE'}, "
        f"assessment {'complete' if ov.assessment_complete else 'INCOMPLETE'}, "
        f"results {ov.results_status}"
    )
    lines += [_diag_line(d, "") for d in ov.diagnostics]
    for entry in ov.entries:
        lines.append("")
        lines += _entry_lines(entry)
    lines += [
        "",
        "declared stages are as written, not verified; readiness is per handoff stage;",
        "dependencies, evidence and git provenance are not checked; advice is not a command.",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_text(ov: Overview) -> Rendered:
    return _bounded(ov, dumps_text)
