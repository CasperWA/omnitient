# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Manifest format 2: parsing, serialization, digests and the schema resource."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import pytest

from omnitient import delivery, ops
from omnitient.model import DIGEST_SCHEMES, Acceptance, WorkPackage
from omnitient.schema import parse_package
from omnitient.serialize import (
    canonical_bytes,
    content_digest,
    covered_dict,
    dumps,
    snapshot_fingerprint,
    to_dict,
)

NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def accepted(
    pkg: WorkPackage, op: str = "C1", evidence: tuple[str, ...] = ("E1",)
) -> WorkPackage:
    request = delivery.acceptance_request(
        operation_id=op,
        actor="user:alice",
        revision=content_digest(pkg),
        evidence=evidence,
        reason="Accepted.",
    )
    return delivery.plan_acceptance(pkg, request, NOW).package


def withdrawn(pkg: WorkPackage, target: str = "C1", op: str = "W1") -> WorkPackage:
    request = delivery.withdrawal_request(
        operation_id=op, actor="user:bob", target=target, reason="Wrong.\nSecond line."
    )
    return delivery.plan_withdrawal(pkg, request, NOW).package


def problems(data: Any) -> list[str]:
    result = parse_package(json.dumps(data).encode("utf-8"))
    assert result.package is None
    return [d.format() for d in result.diagnostics]


def history(v2_package: WorkPackage) -> WorkPackage:
    pkg = accepted(v2_package, "C1", ("E1", "E2", "E3"))
    pkg = withdrawn(pkg)
    return accepted(pkg, "C2", ("E2",))


# --- format identity and digests (AC01, AC02) ---------------------------------


def test_new_packages_are_v2_with_an_empty_history() -> None:
    pkg = ops.new_package("demo", "init", "wp1", "T")
    assert pkg.schema_version == 2
    data = to_dict(pkg)
    assert data["schema_version"] == 2
    assert data["delivery"] == []
    assert list(data)[-1] == "delivery"
    parsed = parse_package(dumps(pkg))
    assert parsed.package == pkg


def test_v1_stays_v1_through_the_parser_and_the_serializer(
    package: WorkPackage,
) -> None:
    parsed = parse_package(dumps(package)).package
    assert parsed is not None
    assert parsed.schema_version == 1
    assert "delivery" not in to_dict(parsed)
    assert dumps(parsed) == dumps(package)


def test_the_digest_uses_the_packages_own_schema_version(
    package: WorkPackage, v2_package: WorkPackage
) -> None:
    assert covered_dict(package)["schema_version"] == 1
    assert covered_dict(v2_package)["schema_version"] == 2
    as_v1 = replace(v2_package, schema_version=1, delivery=())
    manual = {**covered_dict(v2_package), "schema_version": 1}
    assert content_digest(as_v1) == hashlib.sha256(canonical_bytes(manual)).hexdigest()
    assert content_digest(v2_package) != content_digest(as_v1)


def test_each_format_has_one_fixed_digest_scheme(
    package: WorkPackage, v2_package: WorkPackage
) -> None:
    assert package.digest_scheme == DIGEST_SCHEMES[1] == "covered-sha256-v1"
    assert v2_package.digest_scheme == DIGEST_SCHEMES[2] == "covered-sha256-v2"


def test_delivery_is_outside_the_covered_projection(v2_package: WorkPackage) -> None:
    assert "delivery" not in covered_dict(v2_package)
    digest = content_digest(v2_package)
    pkg = accepted(v2_package)
    assert content_digest(pkg) == digest
    assert content_digest(withdrawn(pkg)) == digest


def test_v2_history_round_trips_byte_for_byte(v2_package: WorkPackage) -> None:
    pkg = history(v2_package)
    raw = dumps(pkg)
    parsed = parse_package(raw)
    assert parsed.diagnostics == ()
    assert parsed.package == pkg
    assert dumps(parsed.package) == raw  # type: ignore[arg-type]


def test_the_stored_record_key_order_is_fixed(v2_package: WorkPackage) -> None:
    data = to_dict(withdrawn(accepted(v2_package)))
    acceptance, withdrawal = data["delivery"]
    assert list(acceptance) == [
        "operation_id",
        "kind",
        "actor",
        "recorded_at",
        "reason",
        "package_id",
        "revision",
        "evidence",
    ]
    assert list(acceptance["revision"]) == ["scheme", "digest"]
    assert list(withdrawal) == [
        "operation_id",
        "kind",
        "actor",
        "recorded_at",
        "reason",
        "target_acceptance_id",
    ]


# --- fail closed on unknown fields and versions (AC01) -------------------------


@pytest.mark.parametrize("version", [0, 3, True, False, "2", 2.0, 1.5, None, [2]])
def test_other_schema_versions_are_refused(
    v2_package: WorkPackage, version: Any
) -> None:
    data = to_dict(v2_package)
    data["schema_version"] = version
    result = parse_package(json.dumps(data).encode("utf-8"))
    assert result.package is None
    assert "schema.version" in {d.code for d in result.diagnostics}


@pytest.mark.parametrize("spelling", ["2.0", "2e0", "2E0", "20e-1"])
def test_v2_refuses_float_spellings_of_two(
    v2_package: WorkPackage, spelling: str
) -> None:
    raw = dumps(v2_package).decode("utf-8")
    line = '"schema_version": 2'
    assert line in raw
    result = parse_package(raw.replace(line, f'"schema_version": {spelling}').encode())
    assert result.package is None
    assert "schema.version" in {d.code for d in result.diagnostics}


def test_v2_accepts_integer_two(v2_package: WorkPackage) -> None:
    result = parse_package(dumps(v2_package))
    assert result.package == v2_package
    assert result.package.schema_version == 2


def test_a_v1_reader_refuses_a_v2_file_with_the_existing_code(
    v2_package: WorkPackage,
) -> None:
    """The v1 parser used ``schema.version``; this is what an old reader reports."""
    data = to_dict(v2_package)
    data["schema_version"] = 3
    assert any("this Omnitient reads versions 1 and 2" in p for p in problems(data))


def test_delivery_is_not_allowed_in_v1(package: WorkPackage) -> None:
    data = to_dict(package)
    data["delivery"] = []
    assert any("$.delivery" in p and "unknown field" in p for p in problems(data))


def test_delivery_is_required_in_v2(v2_package: WorkPackage) -> None:
    data = to_dict(v2_package)
    del data["delivery"]
    assert any("$.delivery" in p and "required" in p for p in problems(data))


def test_unknown_fields_are_rejected_at_every_level(v2_package: WorkPackage) -> None:
    data = to_dict(history(v2_package))
    data["delivery"][0]["extra"] = 1
    data["delivery"][0]["revision"]["extra"] = 1
    data["delivery"][0]["evidence"][0]["extra"] = 1
    data["delivery"][1]["extra"] = 1
    found = problems(data)
    for where in (
        "$.delivery[0].extra",
        "$.delivery[0].revision.extra",
        "$.delivery[0].evidence[0].extra",
        "$.delivery[1].extra",
    ):
        assert any(where in p and "unknown field" in p for p in found), where


def test_withdrawal_fields_are_not_allowed_on_an_acceptance(
    v2_package: WorkPackage,
) -> None:
    data = to_dict(accepted(v2_package))
    data["delivery"][0]["target_acceptance_id"] = "C0"
    assert any("target_acceptance_id" in p for p in problems(data))


def test_an_unknown_kind_is_one_clear_problem(v2_package: WorkPackage) -> None:
    data = to_dict(accepted(v2_package))
    data["delivery"][0]["kind"] = "closeout"
    found = problems(data)
    assert any("$.delivery[0].kind" in p for p in found)
    assert not any("unknown field" in p for p in found)


# --- record rules (section 4 of the contract) ---------------------------------


def mutate(pkg: WorkPackage, edit: Any) -> list[str]:
    data = to_dict(pkg)
    edit(data["delivery"])
    return problems(data)


def test_duplicate_operation_ids_are_rejected(v2_package: WorkPackage) -> None:
    pkg = withdrawn(accepted(v2_package))
    found = mutate(pkg, lambda d: d[1].update(operation_id="C1"))
    assert any("duplicate id 'C1'" in p for p in found)


def test_the_operation_id_namespace_is_shared(v2_package: WorkPackage) -> None:
    pkg = accepted(v2_package, "C1")
    pkg = withdrawn(pkg, "C1", "W1")
    pkg = accepted(pkg, "C2", ("E1",))
    found = mutate(pkg, lambda d: d[2].update(operation_id="W1"))
    assert any("duplicate id 'W1'" in p for p in found)


@pytest.mark.parametrize("ident", ["", "1abc", "a b", "a" * 33, "é", "a.b", "a\n"])
def test_operation_ids_follow_the_identifier_pattern(
    v2_package: WorkPackage, ident: str
) -> None:
    found = mutate(accepted(v2_package), lambda d: d[0].update(operation_id=ident))
    assert any("operation_id" in p for p in found)


def test_operation_ids_are_case_sensitive(v2_package: WorkPackage) -> None:
    pkg = withdrawn(accepted(v2_package, "Abc"), "Abc", "abc")
    assert parse_package(dumps(pkg)).package == pkg


def test_a_withdrawal_must_name_an_earlier_acceptance(v2_package: WorkPackage) -> None:
    pkg = withdrawn(accepted(v2_package))
    assert any(
        "must name an earlier record" in p
        for p in mutate(pkg, lambda d: d[1].update(target_acceptance_id="Zed"))
    )
    # A reference that points forward is as bad as one that points nowhere.
    assert any("earlier record" in p for p in mutate(pkg, lambda d: d.reverse()))


def test_a_withdrawal_cannot_target_a_withdrawal_or_repeat(
    v2_package: WorkPackage,
) -> None:
    pkg = accepted(accepted_withdrawn := withdrawn(accepted(v2_package)), "C2", ("E1",))
    del accepted_withdrawn
    found = mutate(
        pkg,
        lambda d: d.append(
            {**d[1], "operation_id": "W2", "target_acceptance_id": "W1"}
        ),
    )
    assert any("must name an acceptance" in p for p in found)
    found = mutate(pkg, lambda d: d.append({**d[1], "operation_id": "W2"}))
    assert any("already withdrawn by W1" in p for p in found)


def test_overlapping_unwithdrawn_acceptances_of_one_revision_are_rejected(
    v2_package: WorkPackage,
) -> None:
    pkg = accepted(v2_package, "C1")
    found = mutate(pkg, lambda d: d.append({**d[0], "operation_id": "C2"}))
    assert any("overlaps the unwithdrawn acceptance C1" in p for p in found)


def test_the_same_revision_may_be_accepted_again_after_a_withdrawal(
    v2_package: WorkPackage,
) -> None:
    pkg = accepted(withdrawn(accepted(v2_package, "C1")), "C2")
    assert parse_package(dumps(pkg)).package == pkg


def test_an_acceptance_must_name_its_own_package(v2_package: WorkPackage) -> None:
    found = mutate(
        accepted(v2_package), lambda d: d[0].update(package_id="demo/init/other")
    )
    assert any("must be the id of the enclosing package" in p for p in found)


def test_renaming_a_package_with_history_no_longer_parses(
    v2_package: WorkPackage,
) -> None:
    data = to_dict(accepted(v2_package))
    data["work_package"] = "renamed"
    assert any("enclosing package" in p for p in problems(data))


@pytest.mark.parametrize(
    ("edit", "needle"),
    [
        (
            lambda d: d[0]["revision"].update(scheme="covered-sha256-v1"),
            "revision.scheme",
        ),
        (lambda d: d[0]["revision"].update(digest="A" * 64), "revision.digest"),
        (lambda d: d[0]["revision"].update(digest="a" * 63), "revision.digest"),
        (lambda d: d[0].pop("revision"), "revision"),
        (lambda d: d[0].update(evidence=[]), "at least one"),
        (lambda d: d[0].pop("evidence"), "evidence"),
        (lambda d: d[0].update(recorded_at="2026-13-01T00:00:00Z"), "recorded_at"),
        (lambda d: d[0].update(actor=" "), "actor"),
        (lambda d: d[0].update(actor="a\nb"), "actor"),
        (lambda d: d[0].update(reason="   "), "reason"),
        (lambda d: d[0].update(reason="a\rb"), "reason"),
        (lambda d: d[0].update(reason=5), "reason"),
        (lambda d: d[0].pop("reason"), "reason"),
    ],
)
def test_malformed_acceptance_fields_are_rejected(
    v2_package: WorkPackage, edit: Any, needle: str
) -> None:
    assert any(needle in p for p in mutate(accepted(v2_package), edit))


def test_actor_and_reason_bounds(v2_package: WorkPackage) -> None:
    base = accepted(v2_package)
    for actor, ok in (("a" * 200, True), ("a" * 201, False)):
        data = to_dict(base)
        data["delivery"][0]["actor"] = actor
        assert (parse_package(json.dumps(data).encode()).package is not None) is ok
    for reason, ok in (
        ("a" * 8192, True),
        ("a" * 8193, False),
        ("é" * 4096, True),  # 8192 bytes
        ("é" * 4097, False),
        ("\U0001f600" * 2048, True),
        ("\U0001f600" * 2049, False),
        ("é" * 8192, False),  # 8192 characters but 16384 bytes
    ):
        data = to_dict(base)
        data["delivery"][0]["reason"] = reason
        assert (
            parse_package(json.dumps(data).encode()).package is not None
        ) is ok, len(reason)


def test_a_reason_is_kept_exactly(v2_package: WorkPackage) -> None:
    reason = '  Line one.\n\tIndented "quoted" \\ back  \n'
    request = delivery.acceptance_request(
        operation_id="C1",
        actor="x",
        revision=content_digest(v2_package),
        evidence=["E1"],
        reason=reason,
    )
    pkg = delivery.plan_acceptance(v2_package, request, NOW).package
    parsed = parse_package(dumps(pkg)).package
    assert parsed is not None
    assert parsed.delivery[0].reason == reason


def test_history_has_a_hard_limit_of_100_records(v2_package: WorkPackage) -> None:
    pkg = withdrawn(accepted(v2_package))
    data = to_dict(pkg)
    record = data["delivery"][1]
    data["delivery"] = [{**record, "operation_id": f"W{i}"} for i in range(101)]
    assert any("has more than 100 items" in p for p in problems(data))


def test_the_history_order_is_kept_and_never_sorted_by_time(
    v2_package: WorkPackage,
) -> None:
    pkg = accepted(v2_package, "C2")
    pkg = withdrawn(pkg, "C2", "W2")
    pkg = accepted(pkg, "C1")
    data = to_dict(pkg)
    data["delivery"][0]["recorded_at"] = "2030-01-01T00:00:00Z"  # later than the rest
    parsed = parse_package(json.dumps(data).encode()).package
    assert parsed is not None
    assert [d.operation_id for d in parsed.delivery] == ["C2", "W2", "C1"]


# --- evidence snapshots (AC08, AC09) ---------------------------------------------


def test_a_snapshot_carries_exact_metadata_and_its_fingerprint(
    v2_package: WorkPackage,
) -> None:
    pkg = accepted(v2_package, "C1", ("E3", "E1", "E2"))
    snapshots = pkg.delivery[0].evidence  # type: ignore[union-attr]
    assert [s.id for s in snapshots] == ["E1", "E2", "E3"]
    stored = to_dict(pkg)["delivery"][0]["evidence"]
    live = {e["id"]: e for e in to_dict(v2_package)["evidence"]}
    for item in stored:
        fingerprint = item.pop("metadata_sha256")
        assert item == live[item["id"]]
        assert (
            fingerprint
            == hashlib.sha256(
                json.dumps(
                    item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest()
        )
    # Optional fields stay absent rather than null.
    assert "summary" not in stored[0]
    assert "engine" not in stored[0]
    assert stored[1]["engine"] == {"run_id": "run-1", "attempt_id": "attempt-1"}


def test_a_tampered_snapshot_fingerprint_is_rejected(v2_package: WorkPackage) -> None:
    data = to_dict(accepted(v2_package))
    data["delivery"][0]["evidence"][0]["ref"] = "abcdef2"
    assert any("metadata_sha256" in p and "does not match" in p for p in problems(data))


def test_snapshots_are_not_compared_with_live_evidence(v2_package: WorkPackage) -> None:
    pkg = accepted(v2_package, "C1", ("E1", "E2"))
    edited = replace(
        pkg,
        evidence=(replace(pkg.evidence[0], ref="abcdef2", summary="changed"),),
    )
    parsed = parse_package(dumps(edited))
    assert parsed.diagnostics == ()
    assert parsed.package.delivery == pkg.delivery  # type: ignore[union-attr]


def test_a_report_root_need_not_remain_declared_for_a_snapshot(
    v2_package: WorkPackage,
) -> None:
    pkg = accepted(v2_package, "C1", ("E2",))
    no_roots = replace(pkg, roots=(), context=(), evidence=())
    assert parse_package(dumps(no_roots)).diagnostics == ()
    # The live rule is unchanged: a live report still needs a declared root.
    live = replace(no_roots, evidence=pkg.evidence)
    assert parse_package(dumps(live)).package is None


def test_snapshot_syntax_is_still_checked(v2_package: WorkPackage) -> None:
    pkg = accepted(v2_package, "C1", ("E1",))
    snapshot = pkg.delivery[0].evidence[0]  # type: ignore[union-attr]
    bad = replace(snapshot.evidence, ref="not-hex!")
    data = to_dict(pkg)
    data["delivery"][0]["evidence"][0] = {
        **to_dict(replace(pkg, evidence=(bad,)))["evidence"][0],
        "metadata_sha256": snapshot_fingerprint(bad),
    }
    assert any("kind 'commit'" in p for p in problems(data))


def test_repeated_snapshot_ids_and_the_selection_limit(v2_package: WorkPackage) -> None:
    pkg = accepted(v2_package, "C1", ("E1",))
    data = to_dict(pkg)
    one = data["delivery"][0]["evidence"][0]
    data["delivery"][0]["evidence"] = [one, one]
    assert any("duplicate id 'E1'" in p for p in problems(data))
    data["delivery"][0]["evidence"] = [{**one, "id": f"E{i}"} for i in range(21)]
    found = problems(data)
    assert any("more than 20 items" in p for p in found)


def test_the_stored_selection_is_the_snapshot_ids(v2_package: WorkPackage) -> None:
    pkg = accepted(v2_package, "C1", ("E2", "E1"))
    record = pkg.delivery[0]
    assert isinstance(record, Acceptance)
    assert "evidence_ids" not in to_dict(pkg)["delivery"][0]
    assert [s.id for s in record.evidence] == ["E1", "E2"]


# --- upgrade transformation (AC02) -------------------------------------------------


def test_upgrade_changes_the_revision_and_keeps_everything_else(
    package: WorkPackage, approved: WorkPackage
) -> None:
    new = ops.upgrade_to_v2(approved)
    assert new.schema_version == 2
    assert new.delivery == ()
    assert content_digest(new) != content_digest(approved)
    assert new.approvals == approved.approvals
    assert replace(new, schema_version=1) == approved
    assert all(a.content_digest != content_digest(new) for a in new.approvals)
    # The v1 original keeps its own digest, and an unrelated v1 package is unchanged.
    assert content_digest(approved) == content_digest(package)


def test_upgrade_never_synthesizes_an_approval_or_changes_the_stage(
    package: WorkPackage,
) -> None:
    new = ops.upgrade_to_v2(package)
    assert new.approvals == ()
    assert new.stage == package.stage


def test_a_v2_package_cannot_be_upgraded_again(v2_package: WorkPackage) -> None:
    with pytest.raises(Exception, match="already manifest v2"):
        ops.upgrade_to_v2(v2_package)


# --- JSON Schema resources --------------------------------------------------------


def _schema(name: str) -> dict[str, Any]:
    text = (resources.files("omnitient") / "data" / name).read_text("utf-8")
    return json.loads(text)  # type: ignore[no-any-return]


def test_the_v1_schema_resource_is_unchanged_and_the_v2_one_is_distinct() -> None:
    v1 = _schema("work-package.schema.json")
    v2 = _schema("work-package.v2.schema.json")
    assert v1["properties"]["schema_version"] == {"const": 1}
    assert "delivery" not in v1["properties"]
    assert v2["properties"]["schema_version"] == {"const": 2}
    assert "delivery" in v2["required"]
    assert v1["$id"] != v2["$id"]


def test_the_v2_schema_matches_the_model(v2_package: WorkPackage) -> None:
    schema = _schema("work-package.v2.schema.json")
    pkg = withdrawn(accepted(v2_package, "C1", ("E1", "E2", "E3")))
    stored = to_dict(pkg)
    assert set(schema["properties"]) == set(stored)
    assert set(schema["required"]) <= set(stored)
    by_kind = {
        variant["properties"]["kind"]["const"]: variant
        for variant in schema["properties"]["delivery"]["items"]["oneOf"]
    }
    for record in stored["delivery"]:
        variant = by_kind[record["kind"]]
        assert set(record) == set(variant["properties"])
        assert set(variant["required"]) <= set(record)
        if record["kind"] == "acceptance":
            item = variant["properties"]["evidence"]["items"]
            for snapshot in record["evidence"]:
                assert set(snapshot) <= set(item["properties"])
                assert set(item["required"]) <= set(snapshot)
            assert set(record["revision"]) == set(
                variant["properties"]["revision"]["properties"]
            )


def test_a_copy_of_a_valid_v2_file_with_extra_whitespace_still_parses(
    v2_package: WorkPackage,
) -> None:
    data = json.loads(dumps(history(v2_package)))
    reordered = json.dumps(copy.deepcopy(data), indent=4, sort_keys=True)
    parsed = parse_package(reordered.encode("utf-8"))
    assert parsed.diagnostics == ()
    # Hand-edited files are normalised when the tool next writes them.
    assert dumps(parsed.package) == dumps(history(v2_package))  # type: ignore[arg-type]
    assert canonical_bytes(data) == canonical_bytes(to_dict(history(v2_package)))
