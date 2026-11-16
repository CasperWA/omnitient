# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""The update routine and the withdrawal reserve of manifest format 2."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnitient import delivery, ops
from omnitient.errors import OmnitientError
from omnitient.limits import MAX_PACKAGE_BYTES
from omnitient.model import WorkPackage
from omnitient.serialize import dumps
from omnitient.store import load_package, update_package
from test_delivery import accept, deficient, padded, withdraw

NOW = datetime(2026, 5, 6, tzinfo=UTC)


def evidence_adder(pkg: WorkPackage) -> WorkPackage:
    return ops.add_evidence(pkg, kind="commit", ref="abcdef2", recorded_by="x", at=NOW)


def stray(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name != "work-package.json")


def test_an_ordinary_write_that_crosses_the_reserve_line_is_refused_and_leaves_no_trace(
    v2_package: WorkPackage, write_package: Callable[..., Path]
) -> None:
    pkg = accept(v2_package, "C1")
    limit = MAX_PACKAGE_BYTES - delivery.WITHDRAWAL_RESERVE_BYTES
    tight = padded(pkg, limit - 100)  # compliant, with 100 bytes to spare
    path = write_package(tight)
    before = path.read_bytes()
    assert delivery.reserve_of(load_package(path).package).compliant  # type: ignore[union-attr]
    with pytest.raises(OmnitientError) as info:
        update_package(load_package(path), evidence_adder)  # adds more than 100 bytes
    assert [d.code for d in info.value.diagnostics] == ["store.reserve"]
    assert path.read_bytes() == before
    assert stray(path.parent) == []


def test_a_compliant_v2_package_keeps_its_reserve_through_every_writer(
    v2_package: WorkPackage, write_package: Callable[..., Path]
) -> None:
    path = write_package(accept(v2_package, "C1"))
    update_package(load_package(path), evidence_adder)
    update_package(
        load_package(path),
        lambda p: ops.add_approval(
            p, stage="implementation", approved_by="x", note=None, at=NOW
        ),
    )
    update_package(load_package(path), lambda p: ops.pin_context(p, {"spec": "e" * 64}))
    loaded = load_package(path)
    reserve = delivery.reserve_of(loaded.package)
    assert reserve is not None
    assert reserve.compliant
    assert reserve.active == 1
    assert len(loaded.package.delivery) == 1
    assert stray(path.parent) == []


def test_a_deficient_package_refuses_ordinary_writes_but_stays_readable(
    v2_package: WorkPackage, write_package: Callable[..., Path]
) -> None:
    pkg = deficient(v2_package, 2)
    path = write_package(pkg)
    loaded = load_package(path)  # inspectable
    before = path.read_bytes()
    with pytest.raises(OmnitientError) as info:
        update_package(loaded, evidence_adder)
    assert [d.code for d in info.value.diagnostics] == ["store.reserve"]
    assert path.read_bytes() == before
    assert stray(path.parent) == []


def test_the_recovery_exception_applies_only_to_a_withdrawal_that_reduces_the_deficit(
    v2_package: WorkPackage, write_package: Callable[..., Path]
) -> None:
    pkg = deficient(v2_package, 3)
    path = write_package(pkg)
    new = withdraw(pkg, "C0", "W1")  # the planner already applied the rule
    with pytest.raises(OmnitientError):
        update_package(load_package(path), lambda _p: new)  # no recovery flag: refused
    assert path.read_bytes() == dumps(pkg)
    update_package(load_package(path), lambda _p: new, reserve_recovery=True)
    saved = load_package(path).package
    assert saved == new
    before = delivery.reserve_of(pkg)
    after = delivery.reserve_of(saved)
    assert before is not None
    assert after is not None
    assert after.byte_deficit < before.byte_deficit
    # Writing something that does not reduce the deficit is still refused.
    with pytest.raises(OmnitientError):
        update_package(load_package(path), evidence_adder, reserve_recovery=True)


def test_v1_bounds_are_unchanged_and_have_no_reserve(
    package: WorkPackage, write_package: Callable[..., Path]
) -> None:
    near = padded(package, MAX_PACKAGE_BYTES - 400)
    path = write_package(near)
    assert len(path.read_bytes()) == MAX_PACKAGE_BYTES - 400
    update_package(load_package(path), evidence_adder)  # fine: no reserve for v1
    assert len(path.read_bytes()) <= MAX_PACKAGE_BYTES
    with pytest.raises(OmnitientError) as info:
        update_package(
            load_package(path),
            lambda p: replace(p, scope=(*p.scope, "x" * 900, "y" * 900)),
        )
    assert info.value.diagnostics[0].code == "schema.too_large"
