# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Schema validation: strictness, diagnostics and the packaged JSON Schema."""

from __future__ import annotations

import json
from dataclasses import replace
from importlib import resources
from typing import Any

import pytest

from omnitient import ops
from omnitient.limits import MAX_PACKAGE_BYTES
from omnitient.model import Evidence, WorkPackage
from omnitient.schema import parse_package
from omnitient.serialize import dumps, to_dict


def parse_dict(data: Any) -> tuple[WorkPackage | None, list[str]]:
    result = parse_package(json.dumps(data).encode("utf-8"))
    return result.package, [d.format() for d in result.diagnostics]


def codes(raw: bytes) -> set[str]:
    return {d.code for d in parse_package(raw).diagnostics}


def test_round_trip_is_lossless(package: WorkPackage) -> None:
    parsed = parse_package(dumps(package))
    assert parsed.diagnostics == ()
    assert parsed.package == package
    assert dumps(parsed.package) == dumps(package)  # type: ignore[arg-type]


def test_scaffold_is_valid_draft_and_unapproved() -> None:
    pkg = ops.new_package("demo", "init", "wp1", "Title")
    parsed = parse_package(dumps(pkg))
    assert parsed.package is not None
    assert parsed.package.stage == "draft"
    assert parsed.package.approvals == ()
    assert parsed.package.decisions == ()


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"not json", "schema.syntax"),
        (b"\xff\xfe", "schema.syntax"),
        (b'{"a": 1, "a": 2}', "schema.syntax"),
        (b'{"schema_version": NaN}', "schema.syntax"),
        (b"[" * 100_000, "schema.syntax"),
        (b"[]", "schema.invalid"),
        (b"x" * (MAX_PACKAGE_BYTES + 1), "schema.too_large"),
    ],
)
def test_malformed_input_is_refused_with_a_code(raw: bytes, code: str) -> None:
    result = parse_package(raw)
    assert result.package is None
    assert code in {d.code for d in result.diagnostics}


def test_unknown_field_is_rejected(package: WorkPackage) -> None:
    data = to_dict(package)
    data["surprise"] = 1
    data["context"][0]["extra"] = True
    pkg, problems = parse_dict(data)
    assert pkg is None
    assert any("$.surprise" in p for p in problems)
    assert any("$.context[0].extra" in p for p in problems)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema", "other/thing"),
        ("schema_version", 2),
        ("schema_version", True),
        ("project", "Not A Slug"),
        ("work_package", ""),
        ("title", "two\nlines"),
        ("title", "x" * 201),
        ("stage", "launched"),
        ("objective", "bad \x07 control"),
        ("objective", "bidi " + chr(0x202E) + " override"),
        ("scope", "not a list"),
        ("scope", ["ok", ""]),
        ("scope", ["multi\nline"]),
        ("scope", ["x"] * 101),
    ],
)
def test_field_rules(package: WorkPackage, key: str, value: Any) -> None:
    data = to_dict(package)
    data[key] = value
    pkg, problems = parse_dict(data)
    assert pkg is None, problems
    assert problems


def test_missing_required_fields_are_all_reported() -> None:
    pkg, problems = parse_dict(
        {"schema": "omnitient/work-package", "schema_version": 1}
    )
    assert pkg is None
    missing = {p.split(":")[0] for p in problems if "is required" in p}
    assert len(missing) >= 6


def test_lone_surrogate_is_rejected(package: WorkPackage) -> None:
    raw = dumps(package).replace(b"Demo package", b"Demo \\ud800 package")
    assert parse_package(raw).package is None


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "/etc/passwd",
        "a/../b.md",
        "a//b.md",
        "./a.md",
        "~/a.md",
        "a\\b.md",
        ".env",
        "cfg/.env.local",
        "x/id_rsa",
        ".git/config",
        "secrets/token.txt",
        "keys/server.pem",
    ],
)
def test_unsafe_context_paths_are_schema_errors(
    package: WorkPackage, path: str
) -> None:
    data = to_dict(package)
    data["context"][0]["path"] = path
    pkg, problems = parse_dict(data)
    assert pkg is None
    assert any("$.context[0].path" in p for p in problems)


def test_context_must_use_declared_root(package: WorkPackage) -> None:
    data = to_dict(package)
    data["context"][0]["root"] = "elsewhere"
    assert "schema.undeclared_root" in codes(json.dumps(data).encode())


def test_duplicate_ids_are_rejected(package: WorkPackage) -> None:
    data = to_dict(package)
    data["context"].append(dict(data["context"][0]))
    assert "schema.duplicate_id" in codes(json.dumps(data).encode())


def test_dependency_rules(package: WorkPackage) -> None:
    data = to_dict(package)
    data["dependencies"] = [{"package": "demo/init/wp1"}]
    assert "schema.invalid" in codes(json.dumps(data).encode())
    data["dependencies"] = [{"package": "demo/init"}]
    assert parse_dict(data)[0] is None


def test_decision_answer_rules(package: WorkPackage, unresolved: Any) -> None:
    resolved = to_dict(package)
    del resolved["decisions"][0]["answer"]
    assert parse_dict(resolved)[0] is None

    blank = to_dict(package)
    blank["decisions"][0]["answer"] = "  "
    assert parse_dict(blank)[0] is None

    pending = to_dict(replace(package, decisions=(unresolved,)))
    pending["decisions"][0]["answer"] = "sneaky"
    assert parse_dict(pending)[0] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_turns", True),
        ("max_turns", 0),
        ("max_turns", 1.5),
        ("max_cost_usd", -1),
        ("max_cost_usd", "5"),
    ],
)
def test_recommendation_limits_are_validated(
    package: WorkPackage, field: str, value: Any
) -> None:
    data = to_dict(package)
    data["recommendations"][0]["limits"] = {field: value}
    assert parse_dict(data)[0] is None


@pytest.mark.parametrize(
    ("kind", "ref", "ok"),
    [
        ("commit", "abcdef1234", True),
        ("commit", "ABCDEF1", False),
        ("commit", "xyz", False),
        ("pr", "https://example.org/o/r/pull/3", True),
        ("pr", "http://example.org/o/r/pull/3", False),
        ("pr", "https://user:pw@example.org/o/r/pull/3", False),
        ("pr", "https://exa mple.org/x", False),
        ("report", "https://example.org/report", True),
        ("report", "repo:docs/spec.md", True),
        ("report", "other:docs/spec.md", False),
        ("report", "repo:../spec.md", False),
        ("report", "repo:.env", False),
        ("report", "ftp://example.org/x", False),
        # urlsplit raises ValueError for these; they must be diagnostics
        ("pr", "https://[::1", False),
        ("pr", "https://[not-an-address]/x", False),
        ("report", "https://exa]mple.org/", False),
        ("report", "https://[::1]:99999999999/x", False),
    ],
)
def test_evidence_reference_rules(
    package: WorkPackage, kind: str, ref: str, ok: bool
) -> None:
    item = Evidence("E1", kind, ref, "user:alice", "2026-01-02T03:04:05Z")  # type: ignore[arg-type]
    pkg = replace(package, evidence=(item,))
    assert (parse_package(dumps(pkg)).package is not None) is ok


def test_timestamps_must_be_utc_and_real(package: WorkPackage) -> None:
    for bad in (
        "2026-01-02 03:04:05",
        "2026-13-02T03:04:05Z",
        "2026-01-02T03:04:05+01:00",
    ):
        item = Evidence("E1", "commit", "abcdef1", "me", bad)
        assert parse_package(dumps(replace(package, evidence=(item,)))).package is None


def test_git_repository_with_password_is_rejected(package: WorkPackage) -> None:
    data = to_dict(package)
    data["context"][0]["git"]["repository"] = "https://bot:secret@example.org/r.git"
    assert parse_dict(data)[0] is None


@pytest.mark.parametrize("repository", ["https://[::1", "https://[bad]/r.git"])
def test_malformed_git_repository_url_is_a_diagnostic(
    package: WorkPackage, repository: str
) -> None:
    data = to_dict(package)
    data["context"][0]["git"]["repository"] = repository
    pkg, problems = parse_dict(data)
    assert pkg is None
    assert any("context[0].git.repository" in line for line in problems)


def test_packaged_json_schema_matches_the_model(package: WorkPackage) -> None:
    schema = json.loads(
        (resources.files("omnitient") / "data" / "work-package.schema.json").read_text(
            "utf-8"
        )
    )
    full = replace(
        package,
        evidence=(Evidence("E1", "commit", "abcdef1", "me", "2026-01-02T03:04:05Z"),),
    )
    stored = to_dict(full)
    assert set(schema["properties"]) == set(stored)
    assert set(schema["required"]) <= set(stored)
    for key in ("context", "decisions", "recommendations", "evidence", "approvals"):
        allowed = set(schema["properties"][key]["items"]["properties"])
        used = {k for item in stored[key] for k in item}
        assert used <= allowed, key


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("project", "demo\n"),
        ("initiative", "init\n"),
        ("work_package", "wp1\n"),
    ],
)
def test_identifiers_with_a_trailing_newline_are_rejected(
    package: WorkPackage, key: str, value: str
) -> None:
    data = to_dict(package)
    data[key] = value
    assert parse_dict(data)[0] is None


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("context", 0, "id"), "spec\n"),
        (("context", 0, "sha256"), "a" * 64 + "\n"),
        (("decisions", 0, "id"), "D1\n"),
        (("dependencies", 0, "package"), "demo/init/wp0\n"),
    ],
)
def test_nested_identifiers_with_a_trailing_newline_are_rejected(
    package: WorkPackage, path: tuple[str, int, str], value: str
) -> None:
    data = to_dict(package)
    data[path[0]][path[1]][path[2]] = value
    assert parse_dict(data)[0] is None
