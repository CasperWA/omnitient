# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Shared fixtures. Every test runs offline in temporary directories."""

from __future__ import annotations

import hashlib
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnitient import ops
from omnitient.context import HostMapping, build_host_mapping
from omnitient.model import (
    ContextRef,
    Decision,
    Dependency,
    GitProvenance,
    Limits,
    Recommendation,
    Root,
    WorkPackage,
)
from omnitient.serialize import dumps

SPEC_TEXT = "# Spec\n\nThe spec says: build the thing.\n"
NOTES_TEXT = "Optional notes.\n"


def sha(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fail any test that tries to open a network connection."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "checkout-a"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "spec.md").write_text(SPEC_TEXT, encoding="utf-8")
    (root / "docs" / "notes.md").write_text(NOTES_TEXT, encoding="utf-8")
    return root


@pytest.fixture
def mapping(repo: Path) -> HostMapping:
    return build_host_mapping([f"repo={repo}"])


@pytest.fixture
def package() -> WorkPackage:
    """A complete package whose context digests match the ``repo`` fixture."""
    return WorkPackage(
        project="demo",
        initiative="init",
        work_package="wp1",
        title="Demo package",
        stage="draft",
        objective="Build the thing.\nSecond line.",
        scope=("Implement A", "Implement B"),
        exclusions=("No C",),
        acceptance=("A works",),
        dependencies=(Dependency("demo/init/wp0", "after wp0"),),
        roots=(Root("repo", "The demo repository"),),
        context=(
            ContextRef(
                "spec",
                "repo",
                "docs/spec.md",
                sha(SPEC_TEXT),
                True,
                "embed",
                "The specification",
                GitProvenance("a" * 40, "demo-repo", "main"),
            ),
            ContextRef(
                "notes", "repo", "docs/notes.md", sha(NOTES_TEXT), False, "embed", ""
            ),
        ),
        decisions=(
            Decision(
                "D1",
                "Which format?",
                "resolved",
                "user:alice",
                "implementation",
                rationale="Because.",
                answer="JSON",
            ),
        ),
        recommendations=(
            Recommendation(
                "R1",
                "implementation",
                model="some-model",
                effort="high",
                account="regular",
                limits=Limits(max_turns=50, max_cost_usd=5.0),
                note="advisory",
            ),
        ),
    )


@pytest.fixture
def unresolved() -> Decision:
    return Decision(
        "D2", "Which database?", "unresolved", "user:alice", "implementation"
    )


@pytest.fixture
def write_package(tmp_path: Path) -> Callable[..., Path]:
    def write(pkg: WorkPackage, name: str = "pkg") -> Path:
        directory = tmp_path / name
        directory.mkdir(exist_ok=True)
        path = directory / "work-package.json"
        path.write_bytes(dumps(pkg))
        return path

    return write


@pytest.fixture
def approved(package: WorkPackage) -> WorkPackage:
    return ops.add_approval(
        package,
        stage="implementation",
        approved_by="user:alice",
        note=None,
        at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
