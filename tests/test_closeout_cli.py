# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""closeout, withdraw and upgrade as commands, on synthetic packages only."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from omnitient import cli, ops
from omnitient.cli import main
from omnitient.model import WorkPackage
from omnitient.schema import parse_package
from omnitient.serialize import content_digest, dumps
from omnitient.store import load_package

Run = Callable[..., tuple[int, str, str]]

NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")


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


def tree(directory: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        str(p.relative_to(directory)): (
            p.read_bytes(),
            p.stat().st_mtime_ns,
            stat.S_IMODE(p.stat().st_mode),
        )
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def closeout_args(
    pkg: Path, *, op: str = "C1", evidence: tuple[str, ...] = ("E1",), **kw: str
) -> list[object]:
    digest = kw.pop("revision", content_digest(load_package(pkg).package))
    return [
        "closeout",
        pkg,
        "--by",
        kw.pop("by", "user:alice"),
        "--revision",
        digest,
        "--evidence",
        *evidence,
        "--reason",
        kw.pop("reason", "Accepted after reading the report."),
        "--operation-id",
        op,
    ]


def withdraw_args(
    pkg: Path, *, target: str = "C1", op: str = "W1", **kw: str
) -> list[object]:
    return [
        "withdraw",
        pkg,
        "--closeout-id",
        target,
        "--by",
        kw.pop("by", "user:bob"),
        "--reason",
        kw.pop("reason", "Found a problem."),
        "--operation-id",
        op,
    ]


# --- closeout and replay ---------------------------------------------------------------


def test_closeout_saves_exactly_one_record_and_nothing_else(
    run: Run, v2_file: Path, v2_package: WorkPackage
) -> None:
    code, out, err = run(*closeout_args(v2_file, evidence=("E2", "E1")))
    assert code == 0
    assert out.startswith("saved acceptance C1:")
    assert "evidence E1, E2" in out
    assert "not authentication" in err
    saved = load_package(v2_file).package
    assert replace(saved, delivery=()) == v2_package
    assert len(saved.delivery) == 1
    assert dumps(saved) == v2_file.read_bytes()


def test_closeout_on_v1_refuses_with_upgrade_guidance(
    run: Run, write_package: Callable[..., Path], package: WorkPackage
) -> None:
    path = write_package(package)
    before = path.read_bytes()
    code, out, err = run(
        "closeout", path, "--by", "a", "--revision", content_digest(package),
        "--evidence", "E1", "--reason", "r", "--operation-id", "C1",
    )  # fmt: skip
    assert (code, out) == (1, "")
    assert "delivery.requires_v2" in err
    assert "omnitient upgrade PACKAGE --to-version 2" in err
    assert path.read_bytes() == before


def test_an_exact_replay_writes_nothing_even_into_a_read_only_directory(
    run: Run, v2_file: Path
) -> None:
    assert run(*closeout_args(v2_file))[0] == 0
    before = tree(v2_file.parent)
    v2_file.parent.chmod(0o555)
    try:
        code, out, _ = run(*closeout_args(v2_file))
    finally:
        v2_file.parent.chmod(0o755)
    assert code == 0
    assert out.startswith("replayed acceptance C1:")
    assert "nothing was written" in out
    assert tree(v2_file.parent) == before  # contents, times and modes unchanged


def test_a_replay_keeps_the_original_time_and_snapshots(
    run: Run, v2_file: Path
) -> None:
    run(*closeout_args(v2_file))
    original = load_package(v2_file).package.delivery[0]
    code, out, _ = run(*closeout_args(v2_file))
    assert code == 0
    assert original.recorded_at in out
    assert load_package(v2_file).package.delivery[0] == original


def test_a_changed_request_conflicts_and_writes_nothing(
    run: Run, v2_file: Path
) -> None:
    run(*closeout_args(v2_file))
    before = tree(v2_file.parent)
    code, out, err = run(
        *closeout_args(v2_file, reason="Accepted after reading the report. ")
    )
    assert (code, out) == (1, "")
    assert "delivery.conflict" in err
    assert "reason" in err
    assert tree(v2_file.parent) == before


def test_replay_after_drift_and_withdrawal_reports_the_observed_status(
    run: Run, v2_file: Path
) -> None:
    run(*closeout_args(v2_file))
    # Drift: the package's covered content changes after the acceptance.
    drifted = replace(load_package(v2_file).package, title="Changed title")
    v2_file.write_bytes(dumps(drifted))
    code, out, _ = run(
        *closeout_args(
            v2_file,
            revision=content_digest(load_package(v2_file).package)[:0] + "0" * 64,
        )
    )
    assert code == 1  # a different request (revision) is a conflict, not a replay
    original_revision = load_package(v2_file).package.delivery[0].revision.digest  # type: ignore[union-attr]
    code, out, _ = run(*closeout_args(v2_file, revision=original_revision))
    assert code == 0
    assert out.startswith("replayed")
    assert "prior revision" in out
    # A withdrawal, then the same replay: withdrawn, never reactivated.
    assert run(*withdraw_args(v2_file))[0] == 0
    code, out, _ = run(*closeout_args(v2_file, revision=original_revision))
    assert code == 0
    assert "withdrawn by W1" in out
    assert len(load_package(v2_file).package.delivery) == 2


def test_closeout_refusals_leave_the_file_unchanged(run: Run, v2_file: Path) -> None:
    before = tree(v2_file.parent)
    cases = {
        "delivery.revision_mismatch": closeout_args(v2_file, revision="0" * 64),
        "delivery.evidence_unknown": closeout_args(v2_file, evidence=("E1", "E9")),
        "delivery.request_invalid": closeout_args(v2_file, reason="  "),
    }
    for code_name, args in cases.items():
        code, out, err = run(*args)
        assert (code, out) == (1, ""), code_name
        assert code_name in err
    assert tree(v2_file.parent) == before


def test_missing_arguments_are_usage_errors(v2_file: Path) -> None:
    for args in (
        ["closeout", v2_file],
        [
            "closeout",
            v2_file,
            "--by",
            "a",
            "--revision",
            "x",
            "--reason",
            "r",
            "--operation-id",
            "C",
        ],
        ["withdraw", v2_file, "--by", "a"],
    ):
        with pytest.raises(SystemExit) as info:
            main([str(a) for a in args])
        assert info.value.code == 2


def test_the_command_does_not_default_the_actor_or_the_operation_id(
    v2_file: Path,
) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "closeout",
                str(v2_file),
                "--revision",
                "0" * 64,
                "--evidence",
                "E1",
                "--reason",
                "r",
                "--operation-id",
                "C",
            ]
        )
    with pytest.raises(SystemExit):
        main(
            [
                "closeout",
                str(v2_file),
                "--by",
                "a",
                "--revision",
                "0" * 64,
                "--evidence",
                "E1",
                "--reason",
                "r",
            ]
        )


# --- withdraw and reacceptance ------------------------------------------------------------


def test_withdraw_reaccept_and_replays(run: Run, v2_file: Path) -> None:
    assert run(*closeout_args(v2_file))[0] == 0
    code, out, _ = run(*withdraw_args(v2_file, reason="Line 1\nLine 2"))
    assert code == 0
    assert out.startswith("saved withdrawal W1 of acceptance C1")
    assert "withdrawn by W1" in out
    # Exact replay of the withdrawal writes nothing.
    before = tree(v2_file.parent)
    code, out, _ = run(*withdraw_args(v2_file, reason="Line 1\nLine 2"))
    assert (code, out.startswith("replayed withdrawal W1")) == (0, True)
    assert tree(v2_file.parent) == before
    # A new id for the withdrawn target refuses and names the earlier withdrawal.
    code, _, err = run(*withdraw_args(v2_file, op="W2"))
    assert code == 1
    assert "delivery.already_withdrawn" in err
    assert "W1" in err
    # Reacceptance of the same revision under a new id.
    assert run(*closeout_args(v2_file, op="C2", evidence=("E3",)))[0] == 0
    history = load_package(v2_file).package.delivery
    assert [d.operation_id for d in history] == ["C1", "W1", "C2"]
    # The original acceptance's replay is about the original, never reactivated.
    code, out, _ = run(*closeout_args(v2_file))
    assert "withdrawn by W1" in out


def test_withdrawal_errors(run: Run, v2_file: Path) -> None:
    run(*closeout_args(v2_file))
    run(*withdraw_args(v2_file))
    before = tree(v2_file.parent)
    for args, expected in (
        (withdraw_args(v2_file, target="Nope", op="W2"), "delivery.target_unknown"),
        (
            withdraw_args(v2_file, target="W1", op="W2"),
            "delivery.target_not_acceptance",
        ),
        (withdraw_args(v2_file, op="W2"), "delivery.already_withdrawn"),
        (withdraw_args(v2_file, op="W2", by=" "), "delivery.request_invalid"),
        (withdraw_args(v2_file, op="C1", target="C1"), "delivery.conflict"),
    ):
        code, out, err = run(*args)
        assert (code, out) == (1, ""), expected
        assert expected in err
    assert tree(v2_file.parent) == before


# --- the delivery commands never evaluate readiness -------------------------------------------


def test_diagnostics_and_stage_are_left_as_they_were(
    run: Run, write_package: Callable[..., Path], v2_package: WorkPackage
) -> None:
    pkg = replace(v2_package, stage="planning", approvals=())
    path = write_package(pkg)
    assert run(*closeout_args(path))[0] == 0
    saved = load_package(path).package
    assert saved.stage == "planning"
    assert saved.approvals == ()
    code, out, _ = run("validate", path, "--stage", "implementation")
    assert code == 3  # the missing approval is still reported, exactly as before
    assert "approval.missing" in out


def test_delivery_commands_use_no_readiness_context_or_network(
    run: Run, v2_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_a: Any, **_k: Any) -> None:
        raise AssertionError("must not be called")

    for name in ("resolve_context", "build_host_mapping", "digest_file"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli.readiness, "evaluate_all", forbidden)
    monkeypatch.setattr(cli.readiness, "evaluate", forbidden)
    assert run(*closeout_args(v2_file))[0] == 0
    assert run(*withdraw_args(v2_file))[0] == 0
    assert run("upgrade", v2_file, "--to-version", "2")[0] == 0


# --- persistence and output failures (AC16) ----------------------------------------------------


class BrokenStdout:
    def write(self, _text: str) -> int:
        raise BrokenPipeError("closed")

    def flush(self) -> None:
        raise BrokenPipeError("closed")

    def fileno(self) -> int:
        raise OSError("no descriptor")


def test_a_failure_before_the_replacement_keeps_the_original_and_cleans_up(
    run: Run, v2_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v2_file.chmod(0o640)
    before = tree(v2_file.parent)

    def fail(self: Path, target: Any) -> None:  # noqa: ARG001
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", fail)
    code, out, err = run(*closeout_args(v2_file))
    assert (code, out) == (1, "")
    assert "store.unwritable" in err
    monkeypatch.undo()
    assert tree(v2_file.parent) == before  # no temp file, no lock, same bytes and mode


def test_a_successful_write_keeps_the_file_mode_and_leaves_no_stray_files(
    run: Run, v2_file: Path
) -> None:
    v2_file.chmod(0o640)
    assert run(*closeout_args(v2_file))[0] == 0
    assert stat.S_IMODE(v2_file.stat().st_mode) == 0o640
    assert [p.name for p in v2_file.parent.iterdir()] == ["work-package.json"]


def test_an_output_failure_after_a_saved_change_is_reported_and_safely_replayable(
    run: Run,
    v2_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = closeout_args(v2_file)
    monkeypatch.setattr(sys, "stdout", BrokenStdout())
    code = main([str(a) for a in args])
    err = capsys.readouterr().err
    monkeypatch.undo()
    assert code == 1
    assert "AFTER the change was saved" in err
    assert "nothing was rolled back" in err
    assert "nothing was written" not in err.replace(
        "nothing was written to the package", ""
    )
    saved = load_package(v2_file).package
    assert len(saved.delivery) == 1  # not rolled back
    # Running the same command replays it: still exactly one record.
    code, out, _ = run(*args)
    assert code == 0
    assert out.startswith("replayed acceptance C1")
    assert len(load_package(v2_file).package.delivery) == 1


def test_a_missing_standard_output_is_an_output_failure_not_a_crash(
    v2_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A closed descriptor 1 makes ``sys.stdout`` ``None``."""
    args = closeout_args(v2_file)
    monkeypatch.setattr(sys, "stdout", None)
    code = main([str(a) for a in args])
    err = capsys.readouterr().err
    monkeypatch.undo()
    assert code == 1
    assert "AFTER the change was saved" in err
    assert len(load_package(v2_file).package.delivery) == 1


def test_a_really_closed_standard_output_in_a_child_process(v2_file: Path) -> None:
    command = closeout_args(v2_file, op="C7")
    done = subprocess.run(
        [
            "/bin/sh",
            "-c",
            'exec 1>&-; exec "$@"',
            "sh",
            sys.executable,
            "-m",
            "omnitient",
            *map(str, command),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 1
    assert "AFTER the change was saved" in done.stderr
    assert "Traceback" not in done.stderr
    assert [d.operation_id for d in load_package(v2_file).package.delivery] == ["C7"]
    replay = subprocess.run(
        [sys.executable, "-m", "omnitient", *map(str, command)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert replay.returncode == 0
    assert replay.stdout.startswith("replayed acceptance C7")
    assert len(load_package(v2_file).package.delivery) == 1


def test_an_output_failure_on_a_replay_says_nothing_was_written(
    run: Run,
    v2_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(*closeout_args(v2_file))
    monkeypatch.setattr(sys, "stdout", BrokenStdout())
    code = main([str(a) for a in closeout_args(v2_file)])
    err = capsys.readouterr().err
    assert code == 1
    assert "AFTER" not in err
    assert "nothing was written to the package" in err


# --- concurrency (AC15) -----------------------------------------------------------------------


def test_a_package_changed_after_loading_is_refused_without_a_lost_record(
    run: Run, v2_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = cli.update_package

    def competing(loaded: Any, mutate: Any, **kw: Any) -> Any:
        calls.append("update")
        # Another writer appends its own acceptance after this command loaded the file.
        other = load_package(v2_file)
        theirs = ops.add_evidence(
            other.package,
            kind="commit",
            ref="abcdef9",
            recorded_by="other",
            at=__import__("datetime").datetime(
                2026, 1, 1, tzinfo=__import__("datetime").UTC
            ),
        )
        v2_file.write_bytes(dumps(theirs))
        return real(loaded, mutate, **kw)

    monkeypatch.setattr(cli, "update_package", competing)
    code, out, err = run(*closeout_args(v2_file))
    monkeypatch.undo()
    assert (code, out) == (1, "")
    assert "store.conflict" in err
    assert calls == ["update"]  # no automatic retry
    saved = load_package(v2_file).package
    assert saved.delivery == ()  # this command's record was not written
    assert saved.evidence[-1].recorded_by == "other"  # the competing change is intact
    assert [p.name for p in v2_file.parent.iterdir()] == ["work-package.json"]


def test_competing_acceptances_cannot_both_win(
    run: Run, v2_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = cli.update_package

    def after_other_closeout(loaded: Any, mutate: Any, **kw: Any) -> Any:
        monkeypatch.setattr(cli, "update_package", real)
        assert run(*closeout_args(v2_file, op="Other", evidence=("E2",)))[0] == 0
        return real(loaded, mutate, **kw)

    monkeypatch.setattr(cli, "update_package", after_other_closeout)
    code, _, err = run(*closeout_args(v2_file, op="C1"))
    assert code == 1
    assert "store.conflict" in err
    assert [d.operation_id for d in load_package(v2_file).package.delivery] == ["Other"]


def test_a_lock_file_refuses_and_is_left_alone(run: Run, v2_file: Path) -> None:
    lock = v2_file.with_name("work-package.json.lock")
    lock.write_text("someone else\n")
    before = v2_file.read_bytes()
    code, _, err = run(*closeout_args(v2_file))
    assert code == 1
    assert "store.locked" in err
    assert v2_file.read_bytes() == before
    assert lock.read_text() == "someone else\n"


# --- upgrade (AC03 to AC05) ---------------------------------------------------------------------


@pytest.fixture
def v1_file(write_package: Callable[..., Path], approved: WorkPackage) -> Path:
    return write_package(approved, "v1pkg")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_preview_reports_the_change_and_writes_nothing(
    run: Run, v1_file: Path, approved: WorkPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a preview must not resolve context or evaluate readiness")

    for name in ("resolve_context", "build_host_mapping"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli.readiness, "evaluate_all", forbidden)
    before = tree(v1_file.parent)
    code, out, _ = run("upgrade", v1_file, "--to-version", "2")
    assert code == 0
    upgraded = ops.upgrade_to_v2(approved)
    assert f"package: {approved.package_id}" in out
    assert "manifest v1 -> v2" in out
    assert content_digest(approved) in out
    assert content_digest(upgraded) in out
    assert "covered-sha256-v1" in out
    assert "covered-sha256-v2" in out
    assert "1 current now, none current after the upgrade" in out
    assert f"resulting file size: {len(dumps(upgraded))} bytes" in out
    assert f"file sha256: {file_hash(v1_file)}" in out
    assert tree(v1_file.parent) == before
    # Deterministic: a second preview is identical.
    assert run("upgrade", v1_file, "--to-version", "2")[1] == out


def test_apply_changes_only_the_format(
    run: Run,
    v1_file: Path,
    approved: WorkPackage,
    write_package: Callable[..., Path],
    package: WorkPackage,
) -> None:
    other = write_package(package, "other")
    other_before = tree(other.parent)
    expected = file_hash(v1_file)
    code, out, err = run(
        "upgrade",
        v1_file,
        "--to-version",
        "2",
        "--apply",
        "--expected-file-sha256",
        expected,
    )
    assert code == 0
    assert out.startswith("saved upgrade:")
    assert "no approval, no repin and no stage change" in err
    saved = load_package(v1_file).package
    assert saved == ops.upgrade_to_v2(approved)
    assert saved.approvals == approved.approvals
    assert saved.context == approved.context
    assert saved.stage == approved.stage
    assert saved.delivery == ()
    assert tree(other.parent) == other_before  # nothing else was touched
    # The retained approval is stale for the new revision.
    code, out, _ = run("show", v1_file, "--json")
    assert json.loads(out)["approvals"][0]["current"] is False


def test_apply_with_the_old_hash_after_success_refuses_with_guidance(
    run: Run, v1_file: Path
) -> None:
    old = file_hash(v1_file)
    args = [
        "upgrade",
        v1_file,
        "--to-version",
        "2",
        "--apply",
        "--expected-file-sha256",
    ]
    assert run(*args, old)[0] == 0
    upgraded = v1_file.read_bytes()
    code, out, err = run(*args, old)
    assert (code, out) == (1, "")
    assert "upgrade.hash_mismatch" in err
    assert "already manifest v2" in err
    assert "omnitient show" in err
    assert v1_file.read_bytes() == upgraded  # no second migration, no receipt
    # The matching current hash is a truthful no-op.
    before = tree(v1_file.parent)
    code, out, _ = run(*args, file_hash(v1_file))
    assert code == 0
    assert out.startswith("unchanged:")
    assert tree(v1_file.parent) == before
    # A v2 preview says no upgrade is needed.
    code, out, _ = run("upgrade", v1_file, "--to-version", "2")
    assert code == 0
    assert out.startswith("no upgrade needed")
    assert tree(v1_file.parent) == before


@pytest.mark.parametrize(
    "extra",
    [
        ["--apply"],
        ["--expected-file-sha256", "0" * 64],
        ["--apply", "--expected-file-sha256", "ABC"],
        ["--apply", "--expected-file-sha256", "0" * 63],
    ],
)
def test_apply_needs_a_valid_guard(run: Run, v1_file: Path, extra: list[str]) -> None:
    before = tree(v1_file.parent)
    code, out, err = run("upgrade", v1_file, "--to-version", "2", *extra)
    assert (code, out) == (1, "")
    assert "upgrade." in err
    assert tree(v1_file.parent) == before


@pytest.mark.parametrize("version", ["1", "3", "0"])
def test_only_version_2_exists_and_there_is_no_downgrade(
    run: Run, v1_file: Path, version: str
) -> None:
    code, _, err = run("upgrade", v1_file, "--to-version", version)
    assert code == 1
    assert "upgrade.unsupported_version" in err


def test_a_wrong_hash_or_drift_since_the_preview_refuses(
    run: Run, v1_file: Path
) -> None:
    code, out, _ = run("upgrade", v1_file, "--to-version", "2")
    guard = file_hash(v1_file)
    assert code == 0
    # Evidence-only drift changes the file, not the covered revision, and still refuses.
    assert (
        run(
            "evidence",
            "add",
            v1_file,
            "--kind",
            "commit",
            "--ref",
            "abcdef1",
            "--by",
            "me",
        )[0]
        == 0
    )
    before = tree(v1_file.parent)
    args = [
        "upgrade",
        v1_file,
        "--to-version",
        "2",
        "--apply",
        "--expected-file-sha256",
    ]
    code, out, err = run(*args, guard)
    assert (code, out) == (1, "")
    assert "upgrade.hash_mismatch" in err
    assert tree(v1_file.parent) == before
    code, _, err = run(*args, "f" * 64)
    assert code == 1
    assert tree(v1_file.parent) == before


def test_an_oversized_result_refuses_in_preview_and_apply(
    run: Run, write_package: Callable[..., Path], package: WorkPackage
) -> None:
    from omnitient.limits import MAX_PACKAGE_BYTES
    from test_delivery import padded

    near = padded(package, MAX_PACKAGE_BYTES - 5)  # a valid v1 file; v2 adds a field
    path = write_package(near, "big")
    assert len(path.read_bytes()) == MAX_PACKAGE_BYTES - 5
    before = tree(path.parent)
    for extra in ([], ["--apply", "--expected-file-sha256", file_hash(path)]):
        code, out, err = run("upgrade", path, "--to-version", "2", *extra)
        assert (code, out) == (1, "")
        assert "schema.too_large" in err
    assert tree(path.parent) == before


def test_an_invalid_package_is_not_upgraded(run: Run, tmp_path: Path) -> None:
    path = tmp_path / "work-package.json"
    path.write_text('{"schema": "omnitient/work-package", "schema_version": 1}')
    before = path.read_bytes()
    code, _, err = run("upgrade", path, "--to-version", "2")
    assert code == 1
    assert "schema." in err
    assert path.read_bytes() == before


def test_a_real_v1_package_shape_upgrades_and_then_accepts_end_to_end(
    run: Run, v1_file: Path
) -> None:
    """The whole lifecycle on a synthetic package: upgrade, accept, withdraw, again."""
    guard = file_hash(v1_file)
    assert (
        run(
            "upgrade",
            v1_file,
            "--to-version",
            "2",
            "--apply",
            "--expected-file-sha256",
            guard,
        )[0]
        == 0
    )
    assert (
        run(
            "evidence",
            "add",
            v1_file,
            "--kind",
            "commit",
            "--ref",
            "abcdef1",
            "--by",
            "me",
        )[0]
        == 0
    )
    assert run(*closeout_args(v1_file, op="C1", evidence=("E1",)))[0] == 0
    assert run("show", v1_file, "--json")[0] == 0
    assert run(*withdraw_args(v1_file))[0] == 0
    assert run(*closeout_args(v1_file, op="C2", evidence=("E1",)))[0] == 0
    assert run(*closeout_args(v1_file, op="C1", evidence=("E1",)))[0] == 0  # replay
    pkg = load_package(v1_file).package
    assert [d.operation_id for d in pkg.delivery] == ["C1", "W1", "C2"]
    assert parse_package(v1_file.read_bytes()).package == pkg


# --- new and schema --------------------------------------------------------------------------------


def test_new_creates_a_v2_package_with_empty_history(run: Run, tmp_path: Path) -> None:
    target = tmp_path / "p" / "work-package.json"
    code, _, _ = run(
        "new", target, "--project", "demo", "--initiative", "init",
        "--work-package", "wp", "--title", "T",
    )  # fmt: skip
    assert code == 0
    data = json.loads(target.read_text())
    assert data["schema_version"] == 2
    assert data["delivery"] == []


def test_schema_selection(run: Run) -> None:
    from importlib import resources

    base = resources.files("omnitient") / "data"
    code, out, _ = run("schema")
    assert code == 0
    assert out == (base / "work-package.schema.json").read_text("utf-8")
    code, out, _ = run("schema", "--schema-version", "1")
    assert out == (base / "work-package.schema.json").read_text("utf-8")
    code, out, _ = run("schema", "--schema-version", "2")
    assert out == (base / "work-package.v2.schema.json").read_text("utf-8")
    assert json.loads(out)["properties"]["schema_version"] == {"const": 2}
    with pytest.raises(SystemExit) as info:
        main(["schema", "--schema-version", "3"])
    assert info.value.code == 2


# --- delivering the report: write counts, operation-aware guidance, best-effort notes (AC16) --------


class Stream:
    """A text stream whose write result and failures the test chooses."""

    def __init__(
        self,
        result: Callable[[str], Any] = len,
        *,
        fail_write: bool = False,
        fail_flush: bool = False,
    ) -> None:
        self.result = result
        self.fail_write = fail_write
        self.fail_flush = fail_flush
        self.attempts = 0
        self.text = ""

    def write(self, text: str) -> Any:
        self.attempts += 1
        if self.fail_write:
            raise BrokenPipeError("closed")
        self.text += text
        return self.result(text)

    def flush(self) -> None:
        if self.fail_flush:
            raise BrokenPipeError("closed")

    def fileno(self) -> int:
        raise OSError("no descriptor")


WRITE_RESULTS: dict[str, Callable[[str], Any]] = {
    "short": lambda t: len(t) - 1,
    "zero": lambda _t: 0,
    "none": lambda _t: None,
    "more than offered": lambda t: len(t) + 1,
    "bytes counted for a text stream": lambda t: len(t.encode("utf-8")),
}

MULTIBYTE_ACTOR = "Åsa ☃ \U0001f600"


def delivery_args(path: Path, **kw: str) -> list[object]:
    return closeout_args(path, by=MULTIBYTE_ACTOR, **kw)


@pytest.mark.parametrize("name", sorted(WRITE_RESULTS))
def test_an_incomplete_write_count_is_a_saved_change_output_failure_without_a_retry(
    v2_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run: Run,
    name: str,
) -> None:
    args = delivery_args(v2_file)
    stream = Stream(WRITE_RESULTS[name])
    monkeypatch.setattr(sys, "stdout", stream)
    code = main([str(a) for a in args])
    err = capsys.readouterr().err
    monkeypatch.undo()
    # The count is only meaningful if the text really has more bytes than characters.
    assert len(stream.text.encode()) > len(stream.text)
    assert code == 1
    assert "AFTER the change was saved" in err
    assert "nothing was rolled back" in err
    assert stream.attempts == 1  # no automatic retry, nothing appended again
    assert [d.operation_id for d in load_package(v2_file).package.delivery] == ["C1"]
    code, out, _ = run(*args)  # the documented recovery: replay, not a new record
    assert (code, out.startswith("replayed acceptance C1")) == (0, True)
    assert len(load_package(v2_file).package.delivery) == 1


def test_a_text_stream_is_complete_when_it_counts_every_character(
    v2_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multibyte text: characters are the unit of a text stream, not bytes."""
    stream = Stream(len)
    monkeypatch.setattr(sys, "stdout", stream)
    code = main([str(a) for a in delivery_args(v2_file)])
    monkeypatch.undo()
    assert code == 0
    assert MULTIBYTE_ACTOR in stream.text
    assert len(stream.text.encode()) > len(stream.text)
    assert stream.attempts == 1
    assert len(load_package(v2_file).package.delivery) == 1


def test_a_flush_failure_after_a_complete_write_is_still_an_output_failure(
    v2_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdout", Stream(fail_flush=True))
    code = main([str(a) for a in closeout_args(v2_file)])
    err = capsys.readouterr().err
    monkeypatch.undo()
    assert code == 1
    assert "AFTER the change was saved" in err
    assert len(load_package(v2_file).package.delivery) == 1


def test_a_short_write_on_a_replay_says_the_record_was_saved_earlier(
    run: Run,
    v2_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(*closeout_args(v2_file))[0] == 0
    before = tree(v2_file.parent)
    monkeypatch.setattr(sys, "stdout", Stream(lambda _t: 0))
    code = main([str(a) for a in closeout_args(v2_file)])
    err = capsys.readouterr().err
    monkeypatch.undo()
    assert code == 1
    assert "nothing was written to the package by this run" in err
    assert "saved by an earlier run" in err
    assert "AFTER" not in err
    assert tree(v2_file.parent) == before


def apply_args(path: Path, expected: str | None = None) -> list[object]:
    return [
        "upgrade",
        path,
        "--to-version",
        "2",
        "--apply",
        "--expected-file-sha256",
        expected or file_hash(path),
    ]


@pytest.mark.parametrize(
    "stdout",
    [BrokenStdout, lambda: Stream(lambda _t: 0), lambda: None],
    ids=["raises", "short count", "closed"],
)
def test_an_output_failure_after_a_saved_upgrade_gives_upgrade_guidance(
    run: Run,
    v1_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdout: Callable[[], Any],
) -> None:
    args = apply_args(v1_file)
    old_hash = file_hash(v1_file)
    monkeypatch.setattr(sys, "stdout", stdout())
    code = main([str(a) for a in args])
    err = capsys.readouterr().err
    monkeypatch.undo()
    assert code == 1
    assert "AFTER the change was saved" in err
    assert "upgraded to manifest v2" in err
    assert "nothing was rolled back" in err
    assert "omnitient show" in err
    # No promise of a replay that does not exist, and no operation id or receipt.
    assert "same operation id" not in err
    assert "replay it safely" not in err
    assert "no operation id" in err
    assert "refused by design" in err
    saved = load_package(v1_file).package
    assert saved.schema_version == 2
    assert saved == ops.upgrade_to_v2(saved_back_to_v1(saved))
    # What the guidance says is what happens: the same command is refused, and the
    # preview reports that nothing is left to do.
    after = tree(v1_file.parent)
    code, out, err = run(*apply_args(v1_file, old_hash))
    assert (code, out) == (1, "")
    assert "upgrade.hash_mismatch" in err
    assert tree(v1_file.parent) == after
    code, out, _ = run("upgrade", v1_file, "--to-version", "2")
    assert (code, out.startswith("no upgrade needed")) == (0, True)


def saved_back_to_v1(pkg: WorkPackage) -> WorkPackage:
    return replace(pkg, schema_version=1)


def test_a_preview_or_a_no_op_with_failing_output_says_nothing_was_written(
    run: Run,
    v1_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing(args: list[object]) -> str:
        before = tree(v1_file.parent)
        monkeypatch.setattr(sys, "stdout", Stream(lambda _t: 0))
        code = main([str(a) for a in args])
        err = capsys.readouterr().err
        monkeypatch.undo()
        assert code == 1
        assert tree(v1_file.parent) == before  # no mutation of any kind
        return err

    preview = failing(["upgrade", v1_file, "--to-version", "2"])
    assert run(*apply_args(v1_file))[0] == 0  # now a v2 package
    no_op_preview = failing(["upgrade", v1_file, "--to-version", "2"])
    unchanged = failing(apply_args(v1_file))
    for err in (preview, no_op_preview, unchanged):
        assert "nothing was written to the package" in err
        assert "AFTER" not in err
        assert "operation id" not in err
        assert "rolled back" not in err


class BrokenStderr:
    def __init__(self) -> None:
        self.attempts = 0

    def write(self, _text: str) -> int:
        self.attempts += 1
        raise BrokenPipeError("closed")

    def flush(self) -> None:
        raise BrokenPipeError("closed")

    def fileno(self) -> int:
        raise OSError("no descriptor")


STDERRS: dict[str, Callable[[], Any]] = {
    "raises": BrokenStderr,
    "closed": lambda: None,
    "short count": lambda: Stream(lambda _t: 0),
}


def stderr_command(
    run: Run, command: str, v2_file: Path, v1_file: Path
) -> tuple[Path, list[object]]:
    """The file a command changes and its arguments, with any prerequisite done."""
    if command == "closeout":
        return v2_file, closeout_args(v2_file)
    if command == "withdraw":
        assert run(*closeout_args(v2_file, op="C0"))[0] == 0  # something to withdraw
        return v2_file, withdraw_args(v2_file, target="C0", op="W1")
    return v1_file, apply_args(v1_file)


@pytest.mark.parametrize("stderr", sorted(STDERRS))
@pytest.mark.parametrize("command", ["closeout", "withdraw", "upgrade"])
def test_optional_notes_on_a_broken_stderr_cannot_prevent_the_receipt(
    run: Run,
    v2_file: Path,
    v1_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    stderr: str,
) -> None:
    target, args = stderr_command(run, command, v2_file, v1_file)
    before = target.read_bytes()
    monkeypatch.setattr(sys, "stderr", STDERRS[stderr]())
    code = main([str(a) for a in args])
    out = capsys.readouterr().out
    monkeypatch.undo()
    assert code == 0
    first_words = {"closeout": "saved acceptance", "withdraw": "saved withdrawal"}
    assert out.startswith(first_words.get(command, "saved upgrade"))
    saved = load_package(target).package
    assert target.read_bytes() != before  # saved, and only once
    if command == "upgrade":
        assert saved.schema_version == 2
    else:
        assert len(saved.delivery) == (1 if command == "closeout" else 2)


def test_a_working_stderr_still_gets_its_notes_and_a_failing_stdout_its_message(
    v2_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdout", BrokenStdout())
    code = main([str(a) for a in closeout_args(v2_file)])
    err = capsys.readouterr().err
    monkeypatch.undo()
    assert code == 1
    assert "note: delivery records are workflow provenance" in err
    assert "AFTER the change was saved" in err


@pytest.mark.parametrize(
    "streams",
    [
        (BrokenStdout, BrokenStderr),
        (lambda: None, lambda: None),
        (lambda: Stream(lambda _t: 0), BrokenStderr),
    ],
    ids=["both raise", "both closed", "short stdout, raising stderr"],
)
def test_with_both_streams_unusable_the_saved_state_stays_and_nothing_is_claimed(
    v2_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    streams: tuple[Callable[[], Any], Callable[[], Any]],
) -> None:
    out_stream, err_stream = streams[0](), streams[1]()
    monkeypatch.setattr(sys, "stdout", out_stream)
    monkeypatch.setattr(sys, "stderr", err_stream)
    code = main([str(a) for a in closeout_args(v2_file)])  # must not raise
    monkeypatch.undo()
    captured = capsys.readouterr()
    assert code == 1  # the receipt was not delivered, and nothing says it was
    assert (captured.out, captured.err) == ("", "")
    if isinstance(out_stream, Stream):
        assert out_stream.attempts == 1  # tried once, never retried
    assert [d.operation_id for d in load_package(v2_file).package.delivery] == ["C1"]


def run_with_closed(
    descriptors: str, command: list[object]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/sh",
            "-c",
            f'exec {descriptors}; exec "$@"',
            "sh",
            sys.executable,
            "-m",
            "omnitient",
            *map(str, command),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_really_closed_standard_error_does_not_prevent_the_receipt(
    v2_file: Path,
) -> None:
    done = run_with_closed("2>&-", closeout_args(v2_file, op="C7"))
    assert done.returncode == 0
    assert done.stdout.startswith("saved acceptance C7")
    assert "Traceback" not in done.stdout + done.stderr
    assert [d.operation_id for d in load_package(v2_file).package.delivery] == ["C7"]


def test_really_closed_standard_output_and_error_are_a_clean_failure(
    v2_file: Path,
) -> None:
    done = run_with_closed("1>&- 2>&-", closeout_args(v2_file, op="C8"))
    assert done.returncode == 1
    assert (done.stdout, done.stderr) == ("", "")
    assert [d.operation_id for d in load_package(v2_file).package.delivery] == ["C8"]
