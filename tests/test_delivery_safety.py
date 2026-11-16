# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""No unintended actions, writer paths, capacity through the commands, concurrency."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnitient import cli, delivery
from omnitient.cli import main
from omnitient.limits import MAX_PACKAGE_BYTES
from omnitient.model import Evidence, WorkPackage
from omnitient.serialize import dumps
from omnitient.store import load_package
from test_closeout_cli import closeout_args, tree, withdraw_args
from test_delivery import accept, accept_size, deficient, padded

Run = Callable[..., tuple[int, str, str]]

SOURCE = Path(__file__).resolve().parent.parent / "src" / "omnitient"


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Run:
    def invoke(*args: object) -> tuple[int, str, str]:
        code = main([str(a) for a in args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke


@pytest.fixture
def v2_file(write_package: Callable[..., Path], v2_package: WorkPackage) -> Path:
    return write_package(v2_package)


# --- audit events (AC23) --------------------------------------------------------------------------

_events: list[tuple[str, tuple[Any, ...]]] | None = None


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if _events is not None:
        _events.append((event, args))


sys.addaudithook(_hook)


@contextlib.contextmanager
def audited() -> Iterator[list[tuple[str, tuple[Any, ...]]]]:
    global _events  # noqa: PLW0603
    _events = []
    try:
        yield _events
    finally:
        seen = _events
        _events = None
        assert seen is not None


FORBIDDEN_EVENTS = {
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.fork",
    "os.forkpty",
    "socket.connect",
    "socket.bind",
    "socket.getaddrinfo",
    "urllib.Request",
    "ctypes.dlopen",
    "os.scandir",
    "os.listdir",
    "os.walk",
    "glob.glob",
    "shutil.rmtree",
}


def test_no_command_spawns_connects_scans_or_reads_the_artifacts_evidence_names(
    run: Run,
    tmp_path: Path,
    repo: Path,
    write_package: Callable[..., Path],
    v2_package: WorkPackage,
    package: WorkPackage,
) -> None:
    v2_path = write_package(v2_package)
    v1_path = write_package(replace(package, work_package="wpv1"), "v1")
    artifact = repo / "docs" / "spec.md"  # E2 names this file; nothing may open it
    commands: list[list[object]] = [
        closeout_args(v2_path, evidence=("E1", "E2")),
        closeout_args(v2_path, evidence=("E1", "E2")),  # replay
        withdraw_args(v2_path),
        withdraw_args(v2_path),  # replay
        closeout_args(v2_path, op="C2", evidence=("E2", "E3")),
        ["show", v2_path, "--json"],
        ["upgrade", v1_path, "--to-version", "2"],
        ["upgrade", v1_path, "--to-version", "2", "--apply", "--expected-file-sha256",
         hashlib.sha256(v1_path.read_bytes()).hexdigest()],
    ]  # fmt: skip
    with audited() as events:
        for command in commands:
            assert run(*command)[0] == 0, command
    names = {name for name, _ in events}
    assert not names & FORBIDDEN_EVENTS, names & FORBIDDEN_EVENTS
    opened = [
        args[0] for name, args in events if name == "open" and args[0] is not None
    ]
    assert str(artifact) not in {str(p) for p in opened}
    assert not any(str(repo) in str(p) for p in opened)
    # Writes go only to the selected manifest's own temporary and lock files.
    allowed = {"work-package.json", "work-package.json.lock"}
    for name, args in events:
        if name != "open" or args[0] is None or isinstance(args[0], int):
            continue  # a descriptor handed to fdopen: its file was audited when opened
        path, mode, flags = args
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int)
            and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        )
        if writes:
            base = Path(str(path)).name
            assert base in allowed or base.startswith(".work-package.json.tmp-"), path
    assert not (tmp_path / "home").exists()


def test_the_new_code_imports_nothing_that_could_spawn_or_connect() -> None:
    forbidden = {
        "subprocess", "socket", "urllib", "http", "ssl", "shutil", "tempfile",
        "importlib", "runpy", "multiprocessing", "asyncio", "ctypes", "pickle",
        "glob", "requests",
    }  # fmt: skip
    for name in ("delivery.py", "model.py", "schema.py", "serialize.py", "ops.py"):
        tree_ = ast.parse((SOURCE / name).read_text(encoding="utf-8"))
        imported = {
            part
            for node in ast.walk(tree_)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for part in (
                [a.name.split(".")[0] for a in node.names]
                if isinstance(node, ast.Import)
                else [(node.module or "").split(".")[0]]
            )
        }
        assert not imported & forbidden, (name, imported & forbidden)


def test_read_paths_make_no_files_even_in_a_read_only_directory(
    run: Run, v2_file: Path
) -> None:
    run(*closeout_args(v2_file))
    before = tree(v2_file.parent)
    v2_file.parent.chmod(0o555)
    try:
        for command in (
            ["show", v2_file],
            ["show", v2_file, "--json"],
            ["validate", v2_file],
            ["upgrade", v2_file, "--to-version", "2"],
            ["schema", "--schema-version", "2"],
        ):
            assert run(*command)[0] == 0, command
    finally:
        v2_file.parent.chmod(0o755)
    assert tree(v2_file.parent) == before


# --- competing writers (AC15) ----------------------------------------------------------------------


def test_a_competing_withdrawal_wins_and_the_stale_one_is_refused(
    run: Run, v2_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run(*closeout_args(v2_file))[0] == 0
    real = cli.update_package

    def other_first(loaded: Any, mutate: Any, **kw: Any) -> Any:
        monkeypatch.setattr(cli, "update_package", real)
        assert run(*withdraw_args(v2_file, op="Other"))[0] == 0
        return real(loaded, mutate, **kw)

    monkeypatch.setattr(cli, "update_package", other_first)
    code, _, err = run(*withdraw_args(v2_file, op="Mine"))
    assert code == 1
    assert "store.conflict" in err
    ids = [d.operation_id for d in load_package(v2_file).package.delivery]
    assert ids == ["C1", "Other"]  # one withdrawal, no lost record, no retry


# --- capacity through the commands (AC17, AC18) -------------------------------------------------------


def test_the_byte_boundary_through_the_command(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    # The size of the record the command will write (closeout_args' default reason).
    sized = accept(v2_package, reason="Accepted after reading the report.")
    grown = len(dumps(sized)) - len(dumps(v2_package))
    assert grown >= accept_size(v2_package)
    limit = MAX_PACKAGE_BYTES - delivery.WITHDRAWAL_RESERVE_BYTES - grown
    path = write_package(padded(v2_package, limit), "at")
    assert run(*closeout_args(path))[0] == 0
    assert (
        len(path.read_bytes()) + delivery.WITHDRAWAL_RESERVE_BYTES == MAX_PACKAGE_BYTES
    )
    over = write_package(padded(v2_package, limit + 1), "over")
    before = tree(over.parent)
    code, out, err = run(*closeout_args(over))
    assert (code, out) == (1, "")
    assert "delivery.capacity" in err
    assert tree(over.parent) == before  # nothing truncated, nothing forced


@pytest.mark.parametrize(
    ("reason", "accepted"),
    [
        ("a" * 8192, True),
        ("a" * 8193, False),
        ("é" * 4096, True),
        ("é" * 4097, False),
        ("\U0001f600" * 2048, True),
        ('"' * 8192, True),
        ("\\\n" * 4096, True),
        ("\t\n" * 4095 + "xy", True),
    ],
)
def test_reason_bounds_through_the_commands_with_escaping_and_multibyte_text(
    run: Run, v2_file: Path, reason: str, accepted: bool
) -> None:
    code, _, err = run(*closeout_args(v2_file, reason=reason))
    assert (code == 0) is accepted, err
    if accepted:
        stored = load_package(v2_file).package.delivery[0].reason
        assert stored == reason  # preserved exactly
        # The reserved withdrawal for it still fits, with the worst reason.
        worst = run(*withdraw_args(v2_file, reason='"' * 8192, by="\U0001f600" * 200))
        assert worst[0] == 0, worst[2]
        saved = load_package(v2_file).package
        reserve = delivery.reserve_of(saved)
        assert reserve is not None
        assert reserve.compliant
    else:
        assert "delivery.request_invalid" in err
        assert load_package(v2_file).package.delivery == ()


def test_twenty_evidence_ids_are_accepted_and_twenty_one_refused(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    ids = tuple(f"E{i:02d}" for i in range(21))
    pkg = replace(
        v2_package,
        evidence=tuple(
            Evidence(i, "commit", "abcdef1", "me", "2026-01-02T03:04:05Z") for i in ids
        ),
    )
    path = write_package(pkg)
    code, _, err = run(*closeout_args(path, evidence=ids))
    assert code == 1
    assert "at most 20" in err
    assert load_package(path).package.delivery == ()
    assert run(*closeout_args(path, evidence=ids[:20]))[0] == 0
    assert len(load_package(path).package.delivery[0].evidence) == 20  # type: ignore[union-attr]


# --- reserve through every writer (AC19) -------------------------------------------------------------------


def test_every_supported_writer_keeps_delivery_and_the_reserve(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage, repo: Path
) -> None:
    path = write_package(accept(v2_package, "C1"))
    original = load_package(path).package.delivery
    root = ("--root", f"repo={repo}")
    assert (
        run(
            "evidence", "add", path, "--kind", "commit", "--ref", "abcdef2", "--by", "x"
        )[0]
        == 0
    )
    assert run("approve", path, "--stage", "implementation", "--by", "x", *root)[0] == 0
    assert (
        run("context", "add", path, "repo:docs/notes.md", "--id", "extra", *root)[0]
        == 0
    )
    assert run("context", "pin", path, "spec", *root)[0] == 0
    saved = load_package(path).package
    assert saved.delivery == original
    assert saved.schema_version == 2
    reserve = delivery.reserve_of(saved)
    assert reserve is not None
    assert (reserve.active, reserve.compliant) == (1, True)
    assert [p.name for p in path.parent.iterdir()] == ["work-package.json"]


def test_v1_writers_are_unchanged(
    run: Run, write_package: Callable[..., Path], package: WorkPackage, repo: Path
) -> None:
    path = write_package(package)
    assert (
        run(
            "evidence", "add", path, "--kind", "commit", "--ref", "abcdef2", "--by", "x"
        )[0]
        == 0
    )
    assert (
        run(
            "approve",
            path,
            "--stage",
            "implementation",
            "--by",
            "x",
            "--root",
            f"repo={repo}",
        )[0]
        == 0
    )
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert "delivery" not in data
    loaded = load_package(path).package
    assert loaded.schema_version == 1
    assert loaded.approvals[-1].content_digest == __import__(
        "omnitient.serialize", fromlist=["x"]
    ).content_digest(package)


def test_ordinary_writers_refuse_a_deficient_package_but_it_stays_inspectable(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    path = write_package(deficient(v2_package, 2))
    before = tree(path.parent)
    assert run("show", path)[0] == 0
    code, out, err = run(
        "evidence", "add", path, "--kind", "commit", "--ref", "abcdef2", "--by", "x"
    )
    assert (code, out) == (1, "")
    assert "store.reserve" in err
    code, _, err = run(*closeout_args(path, op="C9", evidence=("E1",)))
    assert code == 1
    assert tree(path.parent) == before


def test_the_recovery_withdrawal_through_the_command(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    pkg = deficient(v2_package, 2)
    path = write_package(pkg)
    before = delivery.reserve_of(pkg)
    code, out, err = run(*withdraw_args(path, target="C0", op="W1"))
    assert code == 0, err
    assert out.startswith("saved withdrawal W1")
    after = delivery.reserve_of(load_package(path).package)
    assert before is not None
    assert after is not None
    assert after.byte_deficit < before.byte_deficit
    if not after.compliant:  # a remaining deficit stays visible
        assert "delivery.reserve_deficit" in run("show", path)[1]
    assert len(path.read_bytes()) <= MAX_PACKAGE_BYTES


def test_a_recovery_withdrawal_over_the_hard_limit_is_refused(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    pkg = padded(deficient(v2_package, 2), MAX_PACKAGE_BYTES - 20)
    path = write_package(pkg)
    before = tree(path.parent)
    code, _, err = run(*withdraw_args(path, target="C0", op="W1"))
    assert code == 1
    assert "delivery.capacity" in err
    assert tree(path.parent) == before


def test_the_clock_is_only_read_once_per_command(
    run: Run, v2_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded time is the actual recording time, taken by the command."""
    fixed = datetime(2031, 2, 3, 4, 5, 6, tzinfo=UTC)
    monkeypatch.setattr(cli, "_now", lambda: fixed)
    assert run(*closeout_args(v2_file))[0] == 0
    assert (
        load_package(v2_file).package.delivery[0].recorded_at == "2031-02-03T04:05:06Z"
    )
    assert dumps(load_package(v2_file).package) == v2_file.read_bytes()
