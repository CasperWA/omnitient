"""CLI behaviour: exit codes, no-clobber writes, read-only reads, full workflow."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from omnitient.cli import main
from omnitient.model import Decision, WorkPackage

Run = Callable[..., tuple[int, str, str]]


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Run:
    def invoke(*args: object) -> tuple[int, str, str]:
        code = main([str(a) for a in args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke


@pytest.fixture
def pkg_file(write_package: Callable[..., Path], package: WorkPackage) -> Path:
    return write_package(package)


def snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(p.relative_to(directory)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def readonly() -> Iterator[Callable[[Path], None]]:
    locked: list[Path] = []

    def lock(path: Path) -> None:
        path.chmod(0o555)
        locked.append(path)

    yield lock
    for path in locked:
        path.chmod(0o755)


def test_new_scaffolds_a_draft_and_refuses_to_overwrite(
    run: Run, tmp_path: Path
) -> None:
    target = tmp_path / "p" / "work-package.json"
    code, out, _ = run(
        "new",
        target,
        "--project",
        "demo",
        "--initiative",
        "init",
        "--work-package",
        "wp1",
        "--title",
        "T",
    )
    assert code == 0
    assert "stage draft, no approvals" in out
    stored = json.loads(target.read_text(encoding="utf-8"))
    assert stored["stage"] == "draft"
    assert stored["approvals"] == []
    before = target.read_bytes()
    code, _, err = run(
        "new",
        target,
        "--project",
        "demo",
        "--initiative",
        "init",
        "--work-package",
        "wp1",
        "--title",
        "Other",
    )
    assert code == 1
    assert "store.exists" in err
    assert target.read_bytes() == before


def test_new_rejects_bad_identifiers_without_creating_anything(
    run: Run, tmp_path: Path
) -> None:
    target = tmp_path / "bad.json"
    code, _, err = run(
        "new",
        target,
        "--project",
        "Bad Slug",
        "--initiative",
        "i",
        "--work-package",
        "w",
        "--title",
        "T",
    )
    assert code == 1
    assert "$.project" in err
    assert not target.exists()


def test_usage_errors_exit_2(run: Run) -> None:
    with pytest.raises(SystemExit) as info:
        run("render")
    assert info.value.code == 2
    with pytest.raises(SystemExit) as info:
        run("validate", "x", "--stage", "launch")
    assert info.value.code == 2


def test_invalid_package_reports_every_problem(run: Run, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {"schema": "omnitient/work-package", "schema_version": 1, "stray": 1}
        ),
        encoding="utf-8",
    )
    code, out, err = run("validate", bad)
    assert code == 1
    assert out == ""
    assert "unknown field" in err
    assert "is required" in err


def test_read_commands_do_not_mutate(
    run: Run, pkg_file: Path, repo: Path, readonly: Callable[[Path], None]
) -> None:
    directory = pkg_file.parent
    before = snapshot(directory)
    readonly(directory)
    for args in (
        ("validate", pkg_file, "--root", f"repo={repo}"),
        ("validate", pkg_file, "--root", f"repo={repo}", "--json"),
        ("show", pkg_file, "--root", f"repo={repo}"),
        ("show", pkg_file, "--root", f"repo={repo}", "--json"),
        ("schema",),
        (
            "render",
            pkg_file,
            "--stage",
            "planning",
            "--out",
            "-",
            "--format",
            "md",
            "--root",
            f"repo={repo}",
        ),
        (
            "render",
            pkg_file,
            "--stage",
            "planning",
            "--out",
            "-",
            "--format",
            "json",
            "--root",
            f"repo={repo}",
        ),
    ):
        assert run(*args)[0] == 0, args
    assert snapshot(directory) == before
    assert sorted(p.name for p in directory.iterdir()) == ["work-package.json"]


def test_validate_exit_code_follows_the_requested_stage(
    run: Run, pkg_file: Path, repo: Path
) -> None:
    root = ("--root", f"repo={repo}")
    assert run("validate", pkg_file, *root)[0] == 0
    assert run("validate", pkg_file, "--stage", "planning", *root)[0] == 0
    code, out, _ = run("validate", pkg_file, "--stage", "implementation", *root)
    assert code == 3
    assert "approval.missing" in out
    data = json.loads(run("validate", pkg_file, "--json", *root)[1])
    assert data["schema_valid"] is True
    assert data["readiness"]["planning"]["ready"] is True
    assert data["readiness"]["implementation"]["ready"] is False


def test_show_lists_decisions_context_and_readiness(
    run: Run,
    write_package: Callable[..., Path],
    package: WorkPackage,
    unresolved: Decision,
    repo: Path,
) -> None:
    path = write_package(replace(package, decisions=(*package.decisions, unresolved)))
    _, out, _ = run("show", path, "--root", f"repo={repo}")
    assert "demo/init/wp1" in out
    assert "D2 unresolved needed_by=implementation" in out
    assert "spec repo:docs/spec.md required embed [ok]" in out
    assert "implementation: BLOCKED" in out
    _, out, _ = run("show", path)  # no host mapping
    assert "[root_unmapped]" in out


def test_blocked_implementation_writes_nothing(
    run: Run,
    write_package: Callable[..., Path],
    package: WorkPackage,
    unresolved: Decision,
    repo: Path,
    tmp_path: Path,
) -> None:
    path = write_package(replace(package, decisions=(*package.decisions, unresolved)))
    out_dir = tmp_path / "handoffs"
    code, _, err = run(
        "render",
        path,
        "--stage",
        "implementation",
        "--out",
        out_dir,
        "--root",
        f"repo={repo}",
    )
    assert code == 3
    assert "decision.unresolved" in err
    assert "nothing was written" in err
    assert not out_dir.exists()


def test_allow_blocked_writes_a_clearly_blocked_document(
    run: Run,
    write_package: Callable[..., Path],
    package: WorkPackage,
    unresolved: Decision,
    repo: Path,
    tmp_path: Path,
) -> None:
    path = write_package(replace(package, decisions=(*package.decisions, unresolved)))
    out_dir = tmp_path / "handoffs"
    code, _, err = run(
        "render",
        path,
        "--stage",
        "implementation",
        "--out",
        out_dir,
        "--allow-blocked",
        "--root",
        f"repo={repo}",
    )
    assert code == 3
    assert "BLOCKED for implementation" in err
    md = (out_dir / "handoff-implementation.md").read_text(encoding="utf-8")
    assert "**Status: BLOCKED" in md
    assert "**Status: READY" not in md
    assert (
        json.loads(
            (out_dir / "handoff-implementation.json").read_text(encoding="utf-8")
        )["status"]
        == "blocked"
    )


def test_blocked_planning_handoff_is_generated_but_exits_3(
    run: Run,
    write_package: Callable[..., Path],
    package: WorkPackage,
    repo: Path,
    tmp_path: Path,
) -> None:
    early = Decision("D9", "What is the goal?", "unresolved", "me", "planning")
    path = write_package(replace(package, decisions=(early,)))
    out_dir = tmp_path / "handoffs"
    code, _, _ = run(
        "render",
        path,
        "--stage",
        "planning",
        "--out",
        out_dir,
        "--root",
        f"repo={repo}",
    )
    assert code == 3
    md = (out_dir / "handoff-planning.md").read_text(encoding="utf-8")
    assert md.splitlines()[2].startswith("**Status: BLOCKED")
    assert "What is the goal?" in md


def test_render_never_overwrites_without_force(
    run: Run, pkg_file: Path, repo: Path, tmp_path: Path
) -> None:
    out_dir = tmp_path / "handoffs"
    out_dir.mkdir()
    existing = out_dir / "handoff-planning.json"
    existing.write_text("precious", encoding="utf-8")
    args = (
        "render",
        pkg_file,
        "--stage",
        "planning",
        "--out",
        out_dir,
        "--root",
        f"repo={repo}",
    )
    code, _, err = run(*args)
    assert code == 1
    assert "output.exists" in err
    assert existing.read_text(encoding="utf-8") == "precious"
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "handoff-planning.json"
    ]  # all or nothing
    assert run(*args, "--force")[0] == 0
    assert (
        json.loads(existing.read_text(encoding="utf-8"))["schema"]
        == "omnitient/handoff"
    )


def test_render_to_stdout(run: Run, pkg_file: Path, repo: Path) -> None:
    code, out, _ = run(
        "render",
        pkg_file,
        "--stage",
        "planning",
        "--out",
        "-",
        "--format",
        "md",
        "--root",
        f"repo={repo}",
    )
    assert code == 0
    assert out.startswith("# Omnitient handoff: demo/init/wp1 (planning)")
    code, _, err = run(
        "render",
        pkg_file,
        "--stage",
        "planning",
        "--out",
        "-",
        "--root",
        f"repo={repo}",
    )
    assert code == 1
    assert "render.format" in err


def test_full_workflow_from_scaffold_to_ready_handoff(
    run: Run, repo: Path, tmp_path: Path
) -> None:
    path = tmp_path / "flow" / "work-package.json"
    roots = ("--root", f"repo={repo}")
    assert (
        run(
            "new",
            path,
            "--project",
            "demo",
            "--initiative",
            "init",
            "--work-package",
            "wp1",
            "--title",
            "Flow",
        )[0]
        == 0
    )
    assert (
        run(
            "context",
            "add",
            path,
            "repo:docs/spec.md",
            "--id",
            "spec",
            "--purpose",
            "the spec",
            "--git-revision",
            "a" * 40,
            *roots,
        )[0]
        == 0
    )
    assert (
        run("context", "add", path, "repo:docs/notes.md", "--optional", *roots)[0] == 0
    )

    code, _, err = run(
        "approve", path, "--stage", "implementation", "--by", "user:alice"
    )
    assert code == 1
    assert "content.incomplete" in err  # scaffold content is not approvable

    stored = json.loads(path.read_text(encoding="utf-8"))
    stored.update(objective="Do the flow.", scope=["Step one"], acceptance=["It flows"])
    path.write_text(json.dumps(stored), encoding="utf-8")

    code, _, err = run(
        "render", path, "--stage", "implementation", "--out", tmp_path / "early", *roots
    )
    assert code == 3
    assert "approval.missing" in err

    code, out, err = run(
        "approve",
        path,
        "--stage",
        "implementation",
        "--by",
        "user:alice",
        "--note",
        "scope ok",
        *roots,
    )
    assert code == 0
    assert "recorded A1" in out
    assert "not authentication" in err
    assert json.loads(path.read_text(encoding="utf-8"))["stage"] == "implementation"

    out_dir = tmp_path / "handoffs"
    code, _, _ = run(
        "render", path, "--stage", "implementation", "--out", out_dir, *roots
    )
    assert code == 0
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "handoff-implementation.json",
        "handoff-implementation.md",
        "handoff-implementation.meta.json",
    ]
    md = (out_dir / "handoff-implementation.md").read_text(encoding="utf-8")
    assert "**Status: READY for implementation.**" in md
    assert "The spec says: build the thing." in md
    meta = json.loads(
        (out_dir / "handoff-implementation.meta.json").read_text(encoding="utf-8")
    )
    assert meta["host_roots"] == {"repo": str(repo.resolve())}
    assert set(meta["outputs"]) == {
        "handoff-implementation.md",
        "handoff-implementation.json",
    }
    assert str(repo) not in md  # host paths live only in the sidecar

    # evidence appended later keeps the approval and the approved scope
    approved_bytes = json.loads(path.read_text(encoding="utf-8"))
    assert (
        run(
            "evidence",
            "add",
            path,
            "--kind",
            "pr",
            "--ref",
            "https://example.org/o/r/pull/1",
            "--by",
            "user:alice",
            "--run-id",
            "run-1",
            "--attempt-id",
            "att-1",
        )[0]
        == 0
    )
    after = json.loads(path.read_text(encoding="utf-8"))
    assert {k: v for k, v in after.items() if k != "evidence"} == {
        k: v for k, v in approved_bytes.items() if k != "evidence"
    }
    assert after["evidence"][0]["engine"] == {"run_id": "run-1", "attempt_id": "att-1"}
    assert run("validate", path, "--stage", "implementation", *roots)[0] == 0

    # accepting file drift is explicit and invalidates the approval
    (repo / "docs" / "spec.md").write_text("A new spec.\n", encoding="utf-8")
    assert run("validate", path, "--stage", "implementation", *roots)[0] == 3
    code, out, _ = run("context", "pin", path, "spec", *roots)
    assert code == 0
    assert "updated" in out
    code, out, _ = run("validate", path, "--stage", "implementation", *roots)
    assert code == 3
    assert "approval.stale" in out
    assert (
        json.loads(path.read_text(encoding="utf-8"))["approvals"][0]["id"] == "A1"
    )  # history kept


def test_approve_refuses_context_drift_when_roots_given(
    run: Run, pkg_file: Path, repo: Path
) -> None:
    (repo / "docs" / "spec.md").write_text("drift", encoding="utf-8")
    before = pkg_file.read_bytes()
    code, _, err = run(
        "approve",
        pkg_file,
        "--stage",
        "implementation",
        "--by",
        "me",
        "--root",
        f"repo={repo}",
    )
    assert code == 1
    assert "context.digest_mismatch" in err
    assert pkg_file.read_bytes() == before


def test_approve_requires_an_explicit_approver(run: Run, pkg_file: Path) -> None:
    with pytest.raises(SystemExit) as info:
        run("approve", pkg_file, "--stage", "implementation")
    assert info.value.code == 2


def test_update_refuses_while_locked_and_leaves_package_alone(
    run: Run, pkg_file: Path
) -> None:
    lock = pkg_file.with_name("work-package.json.lock")
    lock.write_text("held", encoding="utf-8")
    before = pkg_file.read_bytes()
    code, _, err = run("approve", pkg_file, "--stage", "planning", "--by", "me")
    assert code == 1
    assert "store.locked" in err
    assert pkg_file.read_bytes() == before
    assert lock.read_text(encoding="utf-8") == "held"


def test_context_add_refusals(run: Run, pkg_file: Path, repo: Path) -> None:
    before = pkg_file.read_bytes()
    assert (
        run(
            "context",
            "add",
            pkg_file,
            "repo:docs/spec.md",
            "--id",
            "spec",
            "--root",
            f"repo={repo}",
        )[0]
        == 1
    )  # duplicate id
    assert run("context", "add", pkg_file, "repo:docs/new.md")[0] == 1  # unmapped
    assert (
        run(
            "context", "add", pkg_file, "repo:docs/absent.md", "--root", f"repo={repo}"
        )[0]
        == 1
    )
    assert (
        run("context", "add", pkg_file, "repo:../x.md", "--root", f"repo={repo}")[0]
        == 1
    )
    assert run("context", "add", pkg_file, "nocolon", "--root", f"repo={repo}")[0] == 1
    (repo / ".env").write_text("TOKEN=x", encoding="utf-8")
    assert (
        run("context", "add", pkg_file, "repo:.env", "--root", f"repo={repo}")[0] == 1
    )
    assert pkg_file.read_bytes() == before


def test_roots_file_and_duplicate_mapping(
    run: Run, pkg_file: Path, repo: Path, tmp_path: Path
) -> None:
    roots_file = tmp_path / "roots.local.json"
    roots_file.write_text(json.dumps({"roots": {"repo": str(repo)}}), encoding="utf-8")
    assert (
        run("validate", pkg_file, "--roots-file", roots_file, "--stage", "planning")[0]
        == 0
    )
    code, _, err = run(
        "validate", pkg_file, "--roots-file", roots_file, "--root", f"repo={repo}"
    )
    assert code == 1
    assert "roots.duplicate" in err


def test_unknown_mapped_root_is_only_a_warning(
    run: Run, pkg_file: Path, repo: Path, tmp_path: Path
) -> None:
    code, _, err = run(
        "validate", pkg_file, "--root", f"repo={repo}", "--root", f"other={tmp_path}"
    )
    assert code == 0
    assert "roots.unknown" in err


def test_schema_command_prints_the_packaged_schema(run: Run) -> None:
    code, out, _ = run("schema")
    assert code == 0
    assert json.loads(out)["title"].startswith("Omnitient work package")


def test_package_directory_argument(run: Run, pkg_file: Path, repo: Path) -> None:
    assert run("validate", pkg_file.parent, "--root", f"repo={repo}")[0] == 0


def test_cli_touches_nothing_but_its_targets(
    run: Run, pkg_file: Path, repo: Path, tmp_path: Path
) -> None:
    before = snapshot(tmp_path)
    run(
        "render",
        pkg_file,
        "--stage",
        "planning",
        "--out",
        tmp_path / "o",
        "--root",
        f"repo={repo}",
    )
    after = snapshot(tmp_path)
    assert {k for k in after if k not in before} == {
        "o/handoff-planning.md",
        "o/handoff-planning.json",
        "o/handoff-planning.meta.json",
    }
    assert {
        k: v for k, v in after.items() if k in before
    } == before  # nothing else changed
    assert not (tmp_path / "home").exists()
