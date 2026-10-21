# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""CLI behaviour: exit codes, no-clobber writes, read-only reads, full workflow."""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from conftest import sha
from omnitient.cli import _truncate, main
from omnitient.model import Decision, WorkPackage
from omnitient.store import (
    MANIFEST_SCHEMA,
    IncompleteBundleError,
    check_bundle,
    read_bundle,
)

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


def render_args(pkg_file: Path, repo: Path, out_dir: Path, *extra: object) -> tuple:
    return (
        "render",
        pkg_file,
        "--stage",
        "planning",
        "--out",
        out_dir,
        "--root",
        f"repo={repo}",
        *extra,
    )


def test_render_publishes_a_bundle_whose_manifest_verifies(
    run: Run, pkg_file: Path, repo: Path, tmp_path: Path
) -> None:
    out_dir = tmp_path / "handoffs"
    code, out, _ = run(*render_args(pkg_file, repo, out_dir))
    assert code == 0
    names = [
        "handoff-planning.md",
        "handoff-planning.json",
        "handoff-planning.meta.json",
    ]
    assert [
        Path(line.removeprefix("wrote ")).name for line in out.splitlines()
    ] == names
    manifest = json.loads((out_dir / names[2]).read_text(encoding="utf-8"))
    assert manifest["schema"] == MANIFEST_SCHEMA
    assert sorted(manifest["outputs"]) == sorted(names[:2])
    assert check_bundle(out_dir, names[2]) == []
    (out_dir / names[0]).write_text("tampered", encoding="utf-8")
    assert any("differs" in p for p in check_bundle(out_dir, names[2]))


def test_forced_render_replaces_a_bundle_and_keeps_unrelated_files(
    run: Run, pkg_file: Path, repo: Path, tmp_path: Path
) -> None:
    out_dir = tmp_path / "handoffs"
    assert run(*render_args(pkg_file, repo, out_dir))[0] == 0
    (out_dir / "handoff-implementation.md").write_text(
        "another stage", encoding="utf-8"
    )
    (out_dir / "notes.txt").write_text("mine", encoding="utf-8")
    (repo / "docs" / "spec.md").write_text("changed\n", encoding="utf-8")
    code, _, err = run(*render_args(pkg_file, repo, out_dir, "--force"))
    assert code == 3  # the changed spec no longer matches its pinned digest
    assert "context.digest_mismatch" in err
    assert check_bundle(out_dir, "handoff-planning.meta.json") == []
    assert "changed" not in (out_dir / "handoff-planning.md").read_text(
        encoding="utf-8"
    )
    assert (out_dir / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert (out_dir / "handoff-implementation.md").read_text(
        encoding="utf-8"
    ) == "another stage"


def test_render_reports_an_interrupted_bundle_and_force_completes_it(
    run: Run, pkg_file: Path, repo: Path, tmp_path: Path
) -> None:
    out_dir = tmp_path / "handoffs"
    out_dir.mkdir()
    (out_dir / "handoff-planning.md").write_text("half a bundle", encoding="utf-8")
    code, _, err = run(*render_args(pkg_file, repo, out_dir))
    assert code == 1
    assert "output.exists" in err
    assert "interrupted" in err
    assert (out_dir / "handoff-planning.md").read_text(
        encoding="utf-8"
    ) == "half a bundle"
    assert run(*render_args(pkg_file, repo, out_dir, "--force"))[0] == 0
    assert check_bundle(out_dir, "handoff-planning.meta.json") == []


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


def test_markdown_only_render_is_not_refused_for_the_json_size(
    run: Run,
    repo: Path,
    write_package: Callable[..., Path],
    package: WorkPackage,
) -> None:
    # A newline takes one byte in Markdown and two in JSON (an escaped \n), so
    # two files within every embed limit give a JSON handoff over the bound.
    blob = b"\n" * 262_144
    refs = []
    for index in range(2):
        (repo / f"blank{index}.txt").write_bytes(blob)
        refs.append(
            replace(
                package.context[1],
                id=f"B{index}",
                path=f"blank{index}.txt",
                sha256=sha(blob),
            )
        )
    pkg = write_package(replace(package, context=tuple(refs)))
    common = ("render", pkg, "--stage", "planning", "--root", f"repo={repo}")
    code, out, _ = run(*common, "--out", "-", "--format", "md")
    assert code == 0
    assert len(out.encode("utf-8")) > 2 * len(blob)  # both files are embedded
    code, _, err = run(*common, "--out", "-", "--format", "json")
    assert code == 1
    assert "output.too_large" in err
    assert "JSON handoff" in err


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


def test_malformed_url_is_a_refusal_not_a_traceback(run: Run, pkg_file: Path) -> None:
    before = pkg_file.read_bytes()
    code, _, err = run(
        "evidence",
        "add",
        pkg_file,
        "--kind",
        "pr",
        "--ref",
        "https://[::1",
        "--by",
        "me",
    )
    assert code == 1
    assert "not a well-formed URL" in err
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


def test_context_add_applies_embed_rules_and_pin_notes_git(
    run: Run, pkg_file: Path, repo: Path
) -> None:
    (repo / "docs" / "blob.bin").write_bytes(b"\xff\x00")
    roots = ("--root", f"repo={repo}")
    code, _, err = run("context", "add", pkg_file, "repo:docs/blob.bin", *roots)
    assert code == 1
    assert "context.not_text" in err
    assert (
        run(
            "context",
            "add",
            pkg_file,
            "repo:docs/blob.bin",
            "--mode",
            "reference",
            *roots,
        )[0]
        == 0
    )
    (repo / "docs" / "spec.md").write_text("changed\n", encoding="utf-8")
    code, out, err = run(
        "context", "pin", pkg_file, "spec", *roots
    )  # spec carries git provenance
    assert code == 0
    assert "updated" in out
    assert "git provenance is not updated" in err


def test_truncate_marks_only_real_cuts() -> None:
    assert _truncate("short", 20) == "short"
    assert _truncate("one\ntwo", 20) == "one…"
    assert _truncate("x" * 30, 10) == "x" * 9 + "…"


class Turnstile:
    """Runs scripted file operations of named threads in one fixed order.

    Each step is (thread name, event, file name), where the event is
    ``unlink``, ``replace``, ``create`` or ``read``. Operations that are not in
    the rest of the script run freely.
    """

    def __init__(self, script: list[tuple[str, str, str]]) -> None:
        self.script = script
        self.position = 0
        self.condition = threading.Condition()

    @contextlib.contextmanager
    def turn(self, event: str, path: Any) -> Iterator[None]:
        step = (threading.current_thread().name, event, Path(os.fsdecode(path)).name)
        if step not in self.script[self.position :]:
            yield
            return
        with self.condition:
            if not self.condition.wait_for(
                lambda: self.script[self.position] == step, timeout=10
            ):
                raise AssertionError(f"{step} never got its turn")
        try:
            yield
        finally:
            with self.condition:
                self.position += 1
                self.condition.notify_all()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_open, real_replace, real_unlink = os.open, os.replace, os.unlink

        def gated_open(path: Any, flags: int, *args: Any, **kw: Any) -> int:
            with self.turn("create" if flags & os.O_CREAT else "read", path):
                return real_open(path, flags, *args, **kw)

        def gated_replace(src: Any, dst: Any, *args: Any, **kw: Any) -> None:
            with self.turn("replace", dst):
                real_replace(src, dst, *args, **kw)

        def gated_unlink(path: Any, *args: Any, **kw: Any) -> None:
            with self.turn("unlink", path):
                real_unlink(path, *args, **kw)

        monkeypatch.setattr(os, "open", gated_open)
        monkeypatch.setattr(os, "replace", gated_replace)
        monkeypatch.setattr(os, "unlink", gated_unlink)


def forced_renders(
    pkg_a: Path, pkg_b: Path, repo: Path, out_dir: Path
) -> dict[str, int | AssertionError]:
    """Run ``render --force`` for A and B at once; their exit codes by name.

    A thread that dies of any other exception has no entry, which fails the
    comparison in the test.
    """
    results: dict[str, int | AssertionError] = {}

    def render_as(name: str, pkg: Path) -> None:
        try:
            results[name] = main(
                [str(a) for a in render_args(pkg, repo, out_dir, "--force")]
            )
        except AssertionError as exc:  # a step that never got its turn
            results[name] = exc

    threads = [
        threading.Thread(target=render_as, name=name, args=(name, pkg))
        for name, pkg in (("A", pkg_a), ("B", pkg_b))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


MD, JS, META = (f"handoff-planning.{ext}" for ext in ("md", "json", "meta.json"))


def test_concurrent_forced_renders_can_both_fail_and_never_verify_a_mixture(
    pkg_file: Path,
    repo: Path,
    tmp_path: Path,
    write_package: Callable[..., Path],
    package: WorkPackage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = write_package(replace(package, title="Other writer"), name="other")
    out_dir = tmp_path / "handoffs"
    assert main([str(a) for a in render_args(pkg_file, repo, out_dir)]) == 0
    Turnstile(
        [
            ("A", "unlink", META),
            ("B", "unlink", META),
            ("A", "replace", MD),
            ("A", "replace", JS),
            ("B", "replace", MD),
            ("B", "replace", JS),
            ("A", "create", META),
            ("B", "create", META),  # A's manifest is there: B reports a conflict
            ("A", "read", META),  # A's manifest, B's data: A reports a conflict
        ]
    ).install(monkeypatch)
    assert forced_renders(pkg_file, other, repo, out_dir) == {"A": 1, "B": 1}
    with pytest.raises(IncompleteBundleError):
        read_bundle(out_dir, META)


def test_a_forced_render_whose_bundle_was_replaced_does_not_claim_it(
    pkg_file: Path,
    repo: Path,
    tmp_path: Path,
    write_package: Callable[..., Path],
    package: WorkPackage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = write_package(replace(package, title="Other writer"), name="other")
    out_dir = tmp_path / "handoffs"
    assert main([str(a) for a in render_args(pkg_file, repo, out_dir)]) == 0
    Turnstile(
        [
            ("A", "unlink", META),
            ("A", "replace", MD),
            ("A", "replace", JS),
            ("A", "create", META),
            ("B", "unlink", META),
            ("B", "replace", MD),
            ("B", "replace", JS),
            ("B", "create", META),
            ("B", "read", META),
            ("A", "read", META),  # a complete bundle, but B's
        ]
    ).install(monkeypatch)
    assert forced_renders(pkg_file, other, repo, out_dir) == {"A": 1, "B": 0}
    bundle = read_bundle(out_dir, META)
    assert b"Other writer" in bundle.files[MD]
