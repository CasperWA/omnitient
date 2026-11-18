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
from omnitient.model import Withdrawal, WorkPackage
from omnitient.serialize import dumps
from omnitient.store import load_package, update_package
from test_delivery import (
    accept,
    count_only_deficit,
    deficient,
    padded,
    withdraw,
)

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


# --- the recovery exception is a withdrawal and nothing else (review finding F-001) ----------------


def shrink(pkg: WorkPackage) -> WorkPackage:
    """Drop all but the first filler decision: a big, valid byte reduction."""
    return replace(pkg, decisions=pkg.decisions[:1])


def shrink_a_little(pkg: WorkPackage) -> WorkPackage:
    """Drop the last filler decision: less than the deficit, so it stays deficient."""
    return replace(pkg, decisions=pkg.decisions[:-1])


def bundled(
    extra: Callable[[WorkPackage], WorkPackage],
) -> Callable[[WorkPackage], WorkPackage]:
    return lambda pkg: extra(withdraw(pkg, "C0", "W1"))


def rewrite_history(pkg: WorkPackage) -> WorkPackage:
    first = replace(pkg.delivery[0], reason="rewritten")
    return replace(pkg, delivery=(first, *pkg.delivery[1:]))


def two_withdrawals(pkg: WorkPackage) -> WorkPackage:
    return withdraw(withdraw(pkg, "C0", "W1"), "C1", "W2")


REFUSED_RECOVERIES: dict[str, Callable[[WorkPackage], WorkPackage]] = {
    "shrinks to compliance": shrink,
    "shrinks and stays deficient": shrink_a_little,
    "shrinks and withdraws": bundled(shrink),
    "adds evidence and withdraws": bundled(evidence_adder),
    "retitles and withdraws": bundled(lambda p: replace(p, title="Another title")),
    "rewrites earlier history": lambda p: rewrite_history(withdraw(p, "C0", "W1")),
    "deletes earlier history": lambda p: replace(p, delivery=p.delivery[1:]),
    "withdraws twice": two_withdrawals,
}


@pytest.mark.parametrize("name", sorted(REFUSED_RECOVERIES))
def test_the_recovery_flag_authorizes_only_one_withdrawal(
    v2_package: WorkPackage, write_package: Callable[..., Path], name: str
) -> None:
    pkg = deficient(v2_package, 3)
    path = write_package(pkg)
    before = path.read_bytes()
    mutate = REFUSED_RECOVERIES[name]
    result = mutate(pkg)
    # Fixture precondition: the deficit rule alone would accept this result (it
    # is valid, fits the hard limits and reduces the byte deficit), so only the
    # withdrawal-only rule can be what refuses it.
    old, new = delivery.reserve_of(pkg), delivery.reserve_of(result)
    assert old is not None
    assert new is not None
    assert not old.compliant
    assert new.bytes_stored <= MAX_PACKAGE_BYTES
    if name.startswith("shrinks"):
        assert new.byte_deficit < old.byte_deficit
    # Both routes of the old check are exercised: a result that is compliant (the
    # fast path) and one that is only less deficient (the recovery branch).
    assert new.compliant == (name in {"shrinks to compliance", "shrinks and withdraws"})
    with pytest.raises(OmnitientError) as info:
        update_package(load_package(path), mutate, reserve_recovery=True)
    assert [d.code for d in info.value.diagnostics] == ["store.recovery"]
    assert path.read_bytes() == before
    assert stray(path.parent) == []


def test_a_shrinking_edit_that_stays_deficient_is_refused_without_the_flag_as_before(
    v2_package: WorkPackage, write_package: Callable[..., Path]
) -> None:
    pkg = deficient(v2_package, 3)
    path = write_package(pkg)
    before = path.read_bytes()
    result = delivery.reserve_of(shrink_a_little(pkg))
    assert result is not None
    assert not result.compliant  # still deficient, so an ordinary writer refuses it
    with pytest.raises(OmnitientError) as info:
        update_package(load_package(path), shrink_a_little)
    assert [d.code for d in info.value.diagnostics] == ["store.reserve"]
    assert path.read_bytes() == before


def test_the_flag_does_not_authorize_a_change_of_a_compliant_package(
    v2_package: WorkPackage, write_package: Callable[..., Path]
) -> None:
    """The compliant-result fast path must not skip the withdrawal-only rule."""
    path = write_package(accept(v2_package, "C1"))
    before = path.read_bytes()
    assert delivery.reserve_of(load_package(path).package).compliant  # type: ignore[union-attr]
    with pytest.raises(OmnitientError) as info:
        update_package(load_package(path), evidence_adder, reserve_recovery=True)
    assert [d.code for d in info.value.diagnostics] == ["store.recovery"]
    assert path.read_bytes() == before
    # An honest withdrawal with the flag still works on a compliant package.
    update_package(
        load_package(path), lambda p: withdraw(p, "C1", "W1"), reserve_recovery=True
    )
    assert isinstance(load_package(path).package.delivery[-1], Withdrawal)


def test_a_withdrawal_of_an_already_withdrawn_or_unknown_acceptance_is_not_recovery(
    v2_package: WorkPackage,
) -> None:
    """The library check itself, on results the parser would also refuse."""
    first = accept(replace(v2_package, title="A"), "C0")
    pkg = withdraw(accept(replace(first, title="B"), "C1"), "C0", "W0")
    again = replace(pkg.delivery[-1], operation_id="W9")  # withdraws C0 a second time
    unknown = replace(again, target_acceptance_id="C77")
    appended_acceptance = replace(pkg.delivery[0], operation_id="C9")
    for extra in (again, unknown, appended_acceptance):
        new = replace(pkg, delivery=(*pkg.delivery, extra))
        with pytest.raises(OmnitientError) as info:
            delivery.check_reserve(pkg, new, recovery=True, code="store.reserve")
        assert [d.code for d in info.value.diagnostics] == ["store.recovery"]


def test_a_count_only_deficit_is_not_recoverable_through_the_store(
    v2_package: WorkPackage, write_package: Callable[..., Path]
) -> None:
    pkg = count_only_deficit(v2_package)
    path = write_package(pkg)
    before = path.read_bytes()
    reserve = delivery.reserve_of(load_package(path).package)
    assert reserve is not None
    assert (reserve.count_deficit, reserve.byte_deficit) == (2, 0)
    withdrawal = replace(
        pkg.delivery[1], operation_id="W90", target_acceptance_id="C40"
    )
    assert isinstance(withdrawal, Withdrawal)
    with pytest.raises(OmnitientError) as info:
        update_package(
            load_package(path),
            lambda p: replace(p, delivery=(*p.delivery, withdrawal)),
            reserve_recovery=True,
        )
    assert [d.code for d in info.value.diagnostics] == ["store.reserve"]
    assert path.read_bytes() == before
    assert stray(path.parent) == []


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
