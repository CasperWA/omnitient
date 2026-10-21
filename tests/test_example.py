# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""The committed example stays valid, honest and reproducible."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnitient import render
from omnitient.context import build_host_mapping, resolve_context
from omnitient.store import load_package

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "examples" / "o2-git-pinned-context"

pytestmark = pytest.mark.skipif(
    not EXAMPLE.is_dir(), reason="examples are not part of an installed distribution"
)


def test_example_is_a_draft_with_no_approval_and_open_decisions() -> None:
    pkg = load_package(EXAMPLE).package
    assert pkg.stage == "draft"
    assert pkg.approvals == ()
    assert pkg.evidence == ()
    assert {d.status for d in pkg.decisions} == {"unresolved"}


def test_example_context_digests_are_current() -> None:
    pkg = load_package(EXAMPLE).package
    resolved = resolve_context(pkg, build_host_mapping([f"omnitient={REPO_ROOT}"]))
    stale = [(r.ref.id, r.status) for r in resolved if r.status != "ok"]
    assert (
        stale == []
    ), "pinned file changed: see examples/README.md, 'Keeping the example fresh'"


def test_example_handoff_is_blocked_and_reproducible() -> None:
    pkg = load_package(EXAMPLE).package
    resolved = resolve_context(pkg, build_host_mapping([f"omnitient={REPO_ROOT}"]))
    rendered = render.render(render.build_handoff(pkg, resolved, "planning"))
    committed = EXAMPLE / "handoffs"
    assert rendered.markdown == (committed / "handoff-planning.md").read_bytes()
    assert rendered.json == (committed / "handoff-planning.json").read_bytes()
    data = json.loads(rendered.json)
    assert data["status"] == "blocked"
    assert [b["location"] for b in data["readiness"]["blockers"]] == ["D1"]
    assert str(REPO_ROOT) not in rendered.markdown.decode("utf-8")


def test_example_implementation_handoff_is_not_ready() -> None:
    pkg = load_package(EXAMPLE).package
    resolved = resolve_context(pkg, build_host_mapping([f"omnitient={REPO_ROOT}"]))
    handoff = render.build_handoff(pkg, resolved, "implementation")
    assert not handoff.readiness.ready
    codes = {d.code for d in handoff.readiness.blockers}
    assert {"decision.unresolved", "approval.missing"} <= codes
