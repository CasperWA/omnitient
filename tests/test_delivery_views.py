# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Delivery summaries in show, the overview and handoffs, for both formats."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from markdown_it import MarkdownIt

from omnitient import render
from omnitient.cli import main
from omnitient.context import build_host_mapping, resolve_context
from omnitient.limits import MAX_OUTPUT_BYTES, MAX_PACKAGE_BYTES
from omnitient.model import WorkPackage
from omnitient.serialize import content_digest, dumps
from omnitient.store import check_bundle, load_package
from test_delivery import NOW, accept, deficient, padded, withdraw

Run = Callable[..., tuple[int, str, str]]


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Run:
    def invoke(*args: object) -> tuple[int, str, str]:
        code = main([str(a) for a in args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke


# Distinct from every string in the fixtures, so absence from an output means something.
ACCEPTER = "Zed Accepter"
WITHDRAWER = "Yan Withdrawer"
REASON = "Quux rationale text"


def mixed(v2_package: WorkPackage) -> WorkPackage:
    """Withdrawn C1, active C2 at an earlier revision, active C3 at the present one."""
    pkg = withdraw(
        accept(v2_package, "C1", actor=ACCEPTER, reason=REASON),
        "C1",
        "W1",
        actor=WITHDRAWER,
        reason=REASON,
    )
    pkg = accept(
        replace(pkg, title="Second revision"),
        "C2",
        evidence=["E2"],
        actor=ACCEPTER,
        reason=REASON,
    )
    return accept(
        replace(pkg, title="Third revision"),
        "C3",
        evidence=["E3"],
        actor=ACCEPTER,
        reason=REASON,
    )


def show_json(run: Run, path: Path, *extra: object) -> dict[str, Any]:
    code, out, _ = run("show", path, "--json", *extra)
    assert code == 0
    return json.loads(out)  # type: ignore[no-any-return]


# --- show --json -------------------------------------------------------------------------------


def test_show_json_is_versioned_and_carries_the_full_history(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    pkg = mixed(v2_package)
    doc = show_json(run, write_package(pkg))
    assert list(doc)[:2] == ["schema", "schema_version"]
    assert (doc["schema"], doc["schema_version"]) == ("omnitient/show", 1)
    assert doc["manifest_schema_version"] == 2
    assert doc["digest_scheme"] == "covered-sha256-v2"
    assert doc["content_digest"] == content_digest(pkg)
    assert doc["counts"]["delivery"] == 4
    block = doc["delivery"]
    assert block["summary"] == {
        "state": "current",
        "current_revision_acceptance": "C3",
        "prior_revision_unwithdrawn_count": 1,
        "withdrawn_acceptance_count": 1,
        "acceptance_count": 3,
        "withdrawal_count": 1,
    }
    history = block["history"]
    assert [h["operation_id"] for h in history] == ["C1", "W1", "C2", "C3"]
    assert [h.get("state") for h in history] == ["withdrawn", None, "prior", "current"]
    assert history[0]["withdrawn_by"] == "W1"
    assert history[1]["target_acceptance_id"] == "C1"
    assert history[0]["reason"] == REASON  # full reasons and snapshots
    assert history[0]["evidence"][0]["metadata_sha256"]
    assert block["reserve"]["compliant"] is True
    assert block["reserve"]["records_reserved"] == 2 * 3
    assert block["diagnostics"] == []


def test_show_json_for_v1_is_latest_output_with_an_unavailable_summary(
    run: Run, write_package: Callable[..., Path], package: WorkPackage
) -> None:
    doc = show_json(run, write_package(package))
    assert (doc["schema"], doc["schema_version"]) == ("omnitient/show", 1)
    assert doc["manifest_schema_version"] == 1
    assert doc["digest_scheme"] == "covered-sha256-v1"
    assert doc["delivery"]["summary"]["state"] == "unavailable"
    assert doc["delivery"]["history"] == []
    assert doc["delivery"]["reserve"] is None
    assert doc["content_digest"] == content_digest(package)  # the legacy digest


def test_a_declared_done_package_is_not_accepted(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    doc = show_json(run, write_package(replace(v2_package, stage="done")))
    assert doc["stage"] == "done"
    assert doc["delivery"]["summary"]["state"] == "none"
    assert doc["delivery"]["summary"]["current_revision_acceptance"] is None


def test_an_invalid_package_gets_diagnostics_and_no_summary(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    path = write_package(accept(v2_package))
    data = json.loads(path.read_text())
    data["delivery"][0]["revision"]["digest"] = "xyz"
    path.write_text(json.dumps(data))
    code, out, err = run("show", path, "--json")
    assert (code, out) == (1, "")
    assert "schema.invalid" in err
    assert '"none"' not in err


def test_the_output_is_bounded_for_the_largest_package(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    pkg = deficient(v2_package, 2)  # a package close to the manifest limit
    assert len(dumps(pkg)) > MAX_PACKAGE_BYTES - 6000
    code, out, _ = run("show", write_package(pkg), "--json")
    assert code == 0
    assert len(out.encode()) < MAX_OUTPUT_BYTES
    assert len(json.loads(out)["delivery"]["history"]) == 2  # nothing truncated


def test_raw_context_drift_keeps_the_association_and_shows_the_mismatch(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage, repo: Path
) -> None:
    path = write_package(accept(v2_package))
    (repo / "docs" / "spec.md").write_text("changed\n")
    doc = show_json(run, path, "--root", f"repo={repo}")
    assert doc["delivery"]["summary"]["state"] == "current"
    assert doc["context"][0]["status"] == "digest_mismatch"
    assert doc["readiness"]["planning"] is False


def test_covered_content_changes_create_a_prior_association(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    path = write_package(accept(v2_package))
    assert (
        run("context", "pin", path, "spec", "--root", "repo=/x")[0] != 0
    )  # needs a real root
    moved = replace(load_package(path).package, title="Moved")
    path.write_bytes(dumps(moved))
    summary = show_json(run, path)["delivery"]["summary"]
    assert summary["state"] == "prior_only"
    assert summary["prior_revision_unwithdrawn_count"] == 1


# --- show (human) -------------------------------------------------------------------------------


def test_human_show_lists_ids_revisions_and_linkage_but_not_reasons(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    pkg = mixed(v2_package)
    code, out, _ = run("show", write_package(pkg))
    assert code == 0
    assert "manifest: schema v2, digest scheme covered-sha256-v2" in out
    assert "delivery: accepted at the current revision; current acceptance C3" in out
    assert "withdrawn 1" in out
    assert "C1 acceptance of revision" in out
    assert "[withdrawn, withdrawn by W1]" in out
    assert f"W1 withdrawal of C1 by {WITHDRAWER}" in out
    assert "[prior]" in out
    assert "[current]" in out
    assert REASON not in out  # reasons stay in the JSON form


def test_human_show_states_unavailable_for_v1(
    run: Run, write_package: Callable[..., Path], package: WorkPackage
) -> None:
    code, out, _ = run("show", write_package(package))
    assert code == 0
    assert "manifest: schema v1, digest scheme covered-sha256-v1" in out
    assert "delivery: unavailable" in out


def test_actors_are_escaped_in_terminal_output(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    pkg = accept(v2_package, actor="zero\u200bwidth \\ slash")
    code, out, _ = run("show", write_package(pkg))
    assert code == 0
    assert "\u200b" not in out
    assert "zero\\u200bwidth \\\\ slash" in out


def test_show_reports_a_reserve_deficit_without_failing(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    pkg = deficient(v2_package, 2)
    code, out, _ = run("show", write_package(pkg))
    assert code == 0
    assert "warning[delivery.reserve_deficit] delivery:" in out
    doc = show_json(run, write_package(pkg, "again"))
    assert doc["delivery"]["reserve"]["compliant"] is False
    assert doc["delivery"]["diagnostics"][0]["code"] == "delivery.reserve_deficit"


# --- exit semantics ------------------------------------------------------------------------------


def test_readiness_stage_and_exit_codes_are_unchanged_by_acceptance(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage, repo: Path
) -> None:
    unapproved = replace(v2_package, approvals=())
    path = write_package(unapproved)
    before = run(
        "validate", path, "--stage", "implementation", "--root", f"repo={repo}"
    )
    assert before[0] == 3
    path.write_bytes(dumps(accept(unapproved)))
    after = run("validate", path, "--stage", "implementation", "--root", f"repo={repo}")
    assert after == before


# --- overview ---------------------------------------------------------------------------------------


def overview_doc(
    run: Run, collection: Path, *extra: object
) -> tuple[int, dict[str, Any]]:
    code, out, _ = run("overview", collection, "--json", *extra)
    return code, json.loads(out)


def put(collection: Path, name: str, pkg: WorkPackage) -> None:
    (collection / name).mkdir(parents=True, exist_ok=True)
    (collection / name / "work-package.json").write_bytes(dumps(pkg))


def test_the_overview_shows_compact_summaries_for_both_formats(
    run: Run, tmp_path: Path, v2_package: WorkPackage, package: WorkPackage
) -> None:
    coll = tmp_path / "coll"
    put(coll, "a-v1", replace(package, work_package="wpa"))
    put(coll, "b-v2", mixed(v2_package))
    put(coll, "c-none", replace(v2_package, work_package="wp3"))
    code, doc = overview_doc(run, coll)
    assert code == 0
    assert (doc["schema"], doc["schema_version"]) == ("omnitient/overview", 2)
    v1, v2, none = (e["package"] for e in doc["entries"])
    assert (v1["manifest_schema_version"], v1["digest_scheme"]) == (
        1,
        "covered-sha256-v1",
    )
    assert v1["delivery"]["state"] == "unavailable"
    assert (v2["manifest_schema_version"], v2["digest_scheme"]) == (
        2,
        "covered-sha256-v2",
    )
    assert v2["delivery"] == {
        "state": "current",
        "current_revision_acceptance": "C3",
        "prior_revision_unwithdrawn_count": 1,
        "withdrawn_acceptance_count": 1,
        "acceptance_count": 3,
        "withdrawal_count": 1,
    }
    assert none["delivery"]["state"] == "none"
    # Compact: no reasons, actors or snapshots anywhere in the overview.
    text = json.dumps(doc)
    for forbidden in (REASON, ACCEPTER, WITHDRAWER, "metadata_sha256", "abcdef1"):
        assert forbidden not in text


def test_the_overview_text_states_manifest_and_delivery(
    run: Run, tmp_path: Path, v2_package: WorkPackage
) -> None:
    coll = tmp_path / "coll"
    put(coll, "p", mixed(v2_package))
    code, out, _ = run("overview", coll)
    assert code == 0
    assert "manifest: v2" in out
    assert "delivery: accepted at the current revision; current acceptance C3" in out


def test_an_overview_invalid_entry_has_no_delivery_claim(
    run: Run, tmp_path: Path, v2_package: WorkPackage
) -> None:
    coll = tmp_path / "coll"
    put(coll, "p", accept(v2_package))
    data = json.loads((coll / "p" / "work-package.json").read_text())
    data["delivery"][0]["extra"] = 1
    (coll / "p" / "work-package.json").write_text(json.dumps(data))
    code, doc = overview_doc(run, coll)
    assert code == 1
    entry = doc["entries"][0]
    assert entry["status"] == "invalid"
    assert entry["package"] is None


def test_the_overview_warns_about_a_reserve_deficit_without_failing(
    run: Run, tmp_path: Path, v2_package: WorkPackage
) -> None:
    coll = tmp_path / "coll"
    put(coll, "p", deficient(v2_package, 2))
    code, doc = overview_doc(run, coll)
    assert code == 0  # a warning, not an error
    diag = doc["entries"][0]["diagnostics"][0]
    assert (diag["code"], diag["severity"]) == ("delivery.reserve_deficit", "warning")


def test_overview_reads_do_not_change_the_collection(
    run: Run, tmp_path: Path, v2_package: WorkPackage
) -> None:
    coll = tmp_path / "coll"
    put(coll, "p", mixed(v2_package))
    before = (coll / "p" / "work-package.json").read_bytes()
    run("overview", coll)
    run("overview", coll, "--json")
    assert (coll / "p" / "work-package.json").read_bytes() == before
    assert [p.name for p in (coll / "p").iterdir()] == ["work-package.json"]


# --- handoffs ------------------------------------------------------------------------------------------


def handoff(
    pkg: WorkPackage, mapping_root: Path, stage: str = "planning"
) -> render.RenderedHandoff:
    mapping = build_host_mapping([f"repo={mapping_root}"])
    resolved = resolve_context(pkg, mapping)
    return render.render(render.build_handoff(pkg, resolved, stage))  # type: ignore[arg-type]


def headings(markdown: bytes) -> list[str]:
    tokens = MarkdownIt("commonmark", {"html": True}).parse(markdown.decode())
    out: list[str] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag == "h2":
            out.append(tokens[index + 1].content)
    return out


def test_handoffs_are_latest_version_for_both_input_formats(
    package: WorkPackage, v2_package: WorkPackage, repo: Path
) -> None:
    for pkg, version, scheme in (
        (package, 1, "covered-sha256-v1"),
        (v2_package, 2, "covered-sha256-v2"),
    ):
        rendered = handoff(pkg, repo)
        doc = json.loads(rendered.json)
        assert (doc["schema"], doc["schema_version"]) == ("omnitient/handoff", 2)
        assert doc["package"]["manifest_schema_version"] == version
        assert doc["package"]["digest_scheme"] == scheme
        md = rendered.markdown.decode()
        assert f"- Package schema: `omnitient/work-package` version {version}" in md
        assert f"- Digest scheme: `{scheme}`" in md
        assert "- Handoff schema: `omnitient/handoff` version 2" in md
        assert headings(rendered.markdown) == [
            f"{n}. {t}" for n, t in enumerate(render.SECTION_TITLES, start=1)
        ]
    assert render.SECTION_TITLES[9] == "Delivery"


def test_the_handoff_delivery_section_is_compact_and_separate_from_readiness(
    v2_package: WorkPackage, repo: Path
) -> None:
    pkg = mixed(v2_package)
    rendered = handoff(pkg, repo)
    doc = json.loads(rendered.json)
    block = doc["delivery"]
    assert block["state"] == "current"
    assert block["current_revision_acceptance"] == "C3"
    assert [r["operation_id"] for r in block["records"]] == ["C1", "W1", "C2", "C3"]
    assert "reason" not in json.dumps(block["records"])
    assert "evidence" not in json.dumps(block["records"])
    md = rendered.markdown.decode()
    assert "- Current-revision acceptance: `C3`" in md
    assert "`C1` acceptance of revision" in md
    assert "withdrawn by `W1`" in md
    assert "`W1` withdrawal of `C1`" in md
    assert "A declared stage of done is not an acceptance." in md
    for forbidden in (REASON, ACCEPTER, WITHDRAWER):
        assert forbidden not in md
    # Readiness is evaluated as before: acceptance changes none of it.
    assert (
        doc["readiness"]
        == json.loads(handoff(replace(pkg, delivery=()), repo).json)["readiness"]
    )


def test_handoff_output_is_deterministic_and_has_no_host_paths(
    v2_package: WorkPackage, repo: Path
) -> None:
    pkg = mixed(v2_package)
    one, two = handoff(pkg, repo), handoff(pkg, repo)
    assert one == two
    assert str(repo) not in one.markdown.decode()
    assert str(repo) not in one.json.decode()


def test_hostile_authored_text_cannot_reach_or_reshape_the_handoff(
    v2_package: WorkPackage, repo: Path
) -> None:
    hostile = "# Forged\n<!-- x"
    pkg = accept(v2_package, actor="`` # Forged", reason=hostile)
    rendered = handoff(pkg, repo)
    assert (
        "Forged" not in rendered.markdown.decode()
    )  # actors and reasons are not rendered
    assert headings(rendered.markdown) == [
        f"{n}. {t}" for n, t in enumerate(render.SECTION_TITLES, start=1)
    ]


def test_a_reserve_deficit_is_visible_in_the_handoff(
    v2_package: WorkPackage, repo: Path
) -> None:
    rendered = handoff(deficient(v2_package, 2), repo)
    assert "delivery.reserve_deficit" in rendered.markdown.decode()
    assert json.loads(rendered.json)["delivery"]["diagnostics"][0]["code"] == (
        "delivery.reserve_deficit"
    )


def test_the_bundle_format_is_unchanged_and_verifies_for_a_v2_package(
    run: Run,
    tmp_path: Path,
    write_package: Callable[..., Path],
    v2_package: WorkPackage,
    repo: Path,
) -> None:
    path = write_package(mixed(v2_package))
    out = tmp_path / "bundle"
    code, _, _ = run(
        "render", path, "--stage", "planning", "--out", out, "--root", f"repo={repo}"
    )
    assert code == 0
    manifest = json.loads((out / "handoff-planning.meta.json").read_text())
    assert (manifest["schema"], manifest["schema_version"]) == (
        "omnitient/handoff-manifest",
        1,
    )
    assert check_bundle(out, "handoff-planning.meta.json") == []
    # A later acceptance never rewrites a handoff that was already generated.
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    run(
        "closeout", path, "--by", "x", "--revision", content_digest(load_package(path).package),
        "--evidence", "E1", "--reason", "again", "--operation-id", "C9",
    )  # fmt: skip
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_nothing_is_padded_for_the_view_tests(v2_package: WorkPackage) -> None:
    """Guards the helper the size test relies on."""
    assert len(dumps(padded(v2_package, 10_000))) == 10_000
    assert accept(v2_package).delivery[0].recorded_at == "2026-03-04T05:06:07Z"
    assert NOW.year == 2026
