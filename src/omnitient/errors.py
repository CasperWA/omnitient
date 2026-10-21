# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Diagnostics shared by validation, readiness and the CLI."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "blocker", "warning"]


@dataclass(frozen=True)
class Diagnostic:
    """One finding: a stable ``code``, a severity, a message and a location."""

    code: str
    severity: Severity
    message: str
    location: str = ""

    def to_dict(self) -> dict[str, str]:
        data = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.location:
            data["location"] = self.location
        return data

    def format(self) -> str:
        where = f" {self.location}:" if self.location else ""
        return f"{self.severity}[{self.code}]{where} {self.message}"


class OmnitientError(Exception):
    """A refusal that carries one or more diagnostics."""

    def __init__(self, diagnostics: Diagnostic | Iterable[Diagnostic]) -> None:
        self.diagnostics: tuple[Diagnostic, ...] = (
            (diagnostics,)
            if isinstance(diagnostics, Diagnostic)
            else tuple(diagnostics)
        )
        super().__init__("; ".join(d.format() for d in self.diagnostics))


def refusal(code: str, message: str, location: str = "") -> OmnitientError:
    """Build an :class:`OmnitientError` with a single error diagnostic."""
    return OmnitientError(Diagnostic(code, "error", message, location))
