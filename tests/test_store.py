"""Persistence: no clobbering, no silent replacement of another update."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import textwrap
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from conftest import sha
from omnitient import ops, store
from omnitient.errors import OmnitientError
from omnitient.limits import MAX_PACKAGE_BYTES
from omnitient.model import WorkPackage
from omnitient.serialize import dumps
from omnitient.store import (
    MANIFEST_SCHEMA,
    check_bundle,
    create_package,
    load_package,
    package_path,
    publish_bundle,
    update_package,
)


def code_of(info: pytest.ExceptionInfo[OmnitientError]) -> str:
    return info.value.diagnostics[0].code


def test_create_refuses_to_overwrite(tmp_path: Path, package: WorkPackage) -> None:
    path = tmp_path / "new" / "deep" / "work-package.json"
    create_package(path, package)
    assert path.read_bytes() == dumps(package)
    with pytest.raises(OmnitientError) as info:
        create_package(path, replace(package, title="Other"))
    assert code_of(info) == "store.exists"
    assert path.read_bytes() == dumps(package)


def test_directory_argument_means_the_package_file(
    tmp_path: Path, package: WorkPackage
) -> None:
    create_package(tmp_path / "work-package.json", package)
    assert package_path(tmp_path) == tmp_path / "work-package.json"
    assert load_package(tmp_path).package == package


def test_load_diagnoses_missing_oversized_and_invalid(tmp_path: Path) -> None:
    with pytest.raises(OmnitientError) as info:
        load_package(tmp_path / "absent.json")
    assert code_of(info) == "store.unreadable"
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (MAX_PACKAGE_BYTES + 5))
    with pytest.raises(OmnitientError) as info:
        load_package(big)
    assert code_of(info) == "schema.too_large"
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(OmnitientError) as info:
        load_package(bad)
    assert len(info.value.diagnostics) > 1  # all problems at once


def test_update_replaces_atomically_and_cleans_up(
    tmp_path: Path, package: WorkPackage
) -> None:
    path = tmp_path / "p.json"
    create_package(path, package)
    loaded = load_package(path)
    update_package(loaded, lambda p: replace(p, title="Updated"))
    assert load_package(path).package.title == "Updated"
    assert sorted(f.name for f in tmp_path.iterdir()) == ["p.json"]


def test_concurrent_edit_is_detected_not_overwritten(
    tmp_path: Path, package: WorkPackage
) -> None:
    path = tmp_path / "p.json"
    create_package(path, package)
    loaded = load_package(path)
    other = replace(package, title="Someone else's edit")
    path.write_bytes(dumps(other))
    with pytest.raises(OmnitientError) as info:
        update_package(loaded, lambda p: replace(p, objective="mine"))
    assert code_of(info) == "store.conflict"
    assert load_package(path).package.title == "Someone else's edit"
    assert not (tmp_path / "p.json.lock").exists()


def test_existing_lock_blocks_update_and_is_left_alone(
    tmp_path: Path, package: WorkPackage
) -> None:
    path = tmp_path / "p.json"
    create_package(path, package)
    lock = tmp_path / "p.json.lock"
    lock.write_text("held", encoding="utf-8")
    with pytest.raises(OmnitientError) as info:
        update_package(load_package(path), lambda p: replace(p, title="X"))
    assert code_of(info) == "store.locked"
    assert lock.read_text(encoding="utf-8") == "held"
    assert load_package(path).package == package


def test_invalid_result_is_never_written(tmp_path: Path, package: WorkPackage) -> None:
    path = tmp_path / "p.json"
    create_package(path, package)
    original = path.read_bytes()
    with pytest.raises(OmnitientError):
        update_package(load_package(path), lambda p: replace(p, project="Not A Slug"))
    assert path.read_bytes() == original
    assert sorted(f.name for f in tmp_path.iterdir()) == ["p.json"]


def test_failed_mutation_leaves_no_lock(tmp_path: Path, package: WorkPackage) -> None:
    path = tmp_path / "p.json"
    create_package(path, package)

    def boom(_pkg: WorkPackage) -> WorkPackage:
        raise ops.refusal("test.boom", "no")

    with pytest.raises(OmnitientError):
        update_package(load_package(path), boom)
    assert not (tmp_path / "p.json.lock").exists()


def manifest_for(files: dict[str, bytes], **extra: Any) -> tuple[str, bytes]:
    body = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": 1,
        "outputs": {name: sha(data) for name, data in files.items()},
        **extra,
    }
    return "h.meta.json", (json.dumps(body, indent=2) + "\n").encode("utf-8")


FILES = {"h.md": b"# one\n", "h.json": b'{"one": 1}\n'}


def publish(directory: Path, files: dict[str, bytes] | None = None, **kw: Any) -> None:
    data = FILES if files is None else files
    publish_bundle(directory, data, manifest_for(data), **{"overwrite": False, **kw})


def listing(directory: Path) -> list[str]:
    return sorted(f.name for f in directory.iterdir())


def test_bundle_never_clobbers_unless_asked(tmp_path: Path) -> None:
    publish(tmp_path)
    assert check_bundle(tmp_path, "h.meta.json") == []
    with pytest.raises(OmnitientError) as info:
        publish(tmp_path, {"h.md": b"two", "h.json": b"{}"})
    assert code_of(info) == "output.exists"
    assert (tmp_path / "h.md").read_bytes() == FILES["h.md"]
    newer = {"h.md": b"three", "h.json": b'{"three": 3}'}
    publish(tmp_path, newer, overwrite=True)
    assert (tmp_path / "h.md").read_bytes() == b"three"
    assert check_bundle(tmp_path, "h.meta.json") == []
    assert listing(tmp_path) == ["h.json", "h.md", "h.meta.json"]


def test_bundle_does_not_follow_a_symlink_when_not_forced(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "h.md").symlink_to(victim)
    with pytest.raises(OmnitientError):
        publish(out)
    assert victim.read_text(encoding="utf-8") == "keep"
    assert listing(out) == ["h.md"]


def test_bundle_symlink_is_replaced_not_followed_when_forced(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "h.md").symlink_to(victim)
    publish(out, overwrite=True)
    assert victim.read_text(encoding="utf-8") == "keep"
    assert not (out / "h.md").is_symlink()
    assert check_bundle(out, "h.meta.json") == []


@pytest.fixture
def syscalls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record file creations, replacements and removals by (event, file name).

    Temporary files (``.name.tmp-*``) are left out; they are an implementation
    detail, and the protocol is about the published names.
    """
    events: list[tuple[str, str]] = []
    real_open, real_replace, real_unlink = os.open, os.replace, os.unlink

    def note(event: str, path: Any) -> None:
        name = Path(os.fsdecode(path)).name
        if not name.startswith("."):
            events.append((event, name))

    def spy_open(path: Any, flags: int, *args: Any, **kw: Any) -> int:
        if flags & os.O_CREAT:
            note("create", path)
        return real_open(path, flags, *args, **kw)

    def spy_replace(src: Any, dst: Any, *args: Any, **kw: Any) -> None:
        note("replace", dst)
        real_replace(src, dst, *args, **kw)

    def spy_unlink(path: Any, *args: Any, **kw: Any) -> None:
        note("unlink", path)
        real_unlink(path, *args, **kw)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(os, "unlink", spy_unlink)
    return events


def test_publication_order_puts_the_manifest_last(
    tmp_path: Path, syscalls: list[tuple[str, str]]
) -> None:
    publish(tmp_path)
    assert syscalls == [
        ("create", "h.md"),
        ("create", "h.json"),
        ("create", "h.meta.json"),
    ]
    syscalls.clear()
    publish(tmp_path, overwrite=True)
    # the old completion marker goes first, so it never blesses half-new data
    assert syscalls == [
        ("unlink", "h.meta.json"),
        ("replace", "h.md"),
        ("replace", "h.json"),
        ("create", "h.meta.json"),
    ]


def compete_on_creating(
    monkeypatch: pytest.MonkeyPatch, name: str, content: bytes
) -> Callable[[], bool]:
    """Another writer publishes ``name`` just before this process creates it."""
    real_open = os.open
    fired: list[bool] = []

    def spy(path: Any, flags: int, *args: Any, **kw: Any) -> int:
        target = Path(os.fsdecode(path))
        if flags & os.O_CREAT and target.name == name and not fired:
            fired.append(True)
            target.write_bytes(content)
        return real_open(path, flags, *args, **kw)

    monkeypatch.setattr(os, "open", spy)
    return lambda: bool(fired)


def test_losing_a_race_for_a_data_file_removes_only_files_it_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "unrelated.txt").write_text("mine", encoding="utf-8")
    fired = compete_on_creating(monkeypatch, "h.json", b"theirs")
    with pytest.raises(OmnitientError) as info:
        publish(tmp_path)
    assert fired()
    assert code_of(info) == "output.exists"
    assert (tmp_path / "h.json").read_bytes() == b"theirs"
    assert listing(tmp_path) == ["h.json", "unrelated.txt"]  # our h.md is gone
    assert (tmp_path / "unrelated.txt").read_text(encoding="utf-8") == "mine"


def test_losing_a_race_for_the_manifest_removes_the_data_it_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fired = compete_on_creating(monkeypatch, "h.meta.json", b"theirs")
    with pytest.raises(OmnitientError) as info:
        publish(tmp_path)
    assert fired()
    assert code_of(info) == "output.exists"
    assert listing(tmp_path) == ["h.meta.json"]
    assert (tmp_path / "h.meta.json").read_bytes() == b"theirs"


def test_forced_replacement_that_is_interrupted_has_no_completion_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish(tmp_path)
    (tmp_path / "unrelated.txt").write_text("mine", encoding="utf-8")
    real_replace = os.replace

    def interrupted(src: Any, dst: Any, *args: Any, **kw: Any) -> None:
        if Path(os.fsdecode(dst)).name == "h.json":
            raise OSError(28, "No space left on device")
        real_replace(src, dst, *args, **kw)

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(OmnitientError) as info:
        publish(tmp_path, {"h.md": b"new", "h.json": b"{}"}, overwrite=True)
    assert code_of(info) == "output.unwritable"
    # new data file, old data file, no marker, no stray temporary files
    assert listing(tmp_path) == ["h.json", "h.md", "unrelated.txt"]
    assert (tmp_path / "h.md").read_bytes() == b"new"
    assert (tmp_path / "h.json").read_bytes() == FILES["h.json"]
    assert check_bundle(tmp_path, "h.meta.json") == [
        (
            "the completion manifest h.meta.json does not exist: an earlier "
            "write was interrupted or is still running"
        )
    ]
    assert (tmp_path / "unrelated.txt").read_text(encoding="utf-8") == "mine"


def test_forced_replacement_that_loses_the_manifest_race_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish(tmp_path)
    fired = compete_on_creating(monkeypatch, "h.meta.json", b"theirs")
    with pytest.raises(OmnitientError) as info:
        publish(tmp_path, {"h.md": b"new", "h.json": b"{}"}, overwrite=True)
    assert fired()
    assert code_of(info) == "output.conflict"
    assert listing(tmp_path) == ["h.json", "h.md", "h.meta.json"]
    assert (tmp_path / "h.meta.json").read_bytes() == b"theirs"
    assert check_bundle(tmp_path, "h.meta.json") != []  # a mixed bundle is detectable


def test_a_stray_data_file_without_a_manifest_is_reported_as_interrupted(
    tmp_path: Path,
) -> None:
    (tmp_path / "h.md").write_bytes(b"partial")
    with pytest.raises(OmnitientError) as info:
        publish(tmp_path)
    message = info.value.diagnostics[0].message
    assert code_of(info) == "output.exists"
    assert "interrupted" in message
    assert "--force" in message
    publish(tmp_path, overwrite=True)
    assert check_bundle(tmp_path, "h.meta.json") == []


def test_check_bundle_applies_the_reader_rules(tmp_path: Path) -> None:
    def problems() -> list[str]:
        return check_bundle(tmp_path, "h.meta.json")

    assert "does not exist" in problems()[0]
    publish(tmp_path)
    assert problems() == []
    (tmp_path / "extra.md").write_bytes(b"unlisted files are not part of the bundle")
    assert problems() == []
    (tmp_path / "h.json").write_bytes(b'{"two": 2}\n')
    assert any("h.json" in line and "differs" in line for line in problems())
    (tmp_path / "h.json").unlink()
    assert any("h.json" in line and "missing" in line for line in problems())
    (tmp_path / "h.json").write_bytes(FILES["h.json"])
    (tmp_path / "h.meta.json").write_text("{", encoding="utf-8")
    assert any("not valid JSON" in line for line in problems())
    (tmp_path / "h.meta.json").write_text('{"outputs": {}}', encoding="utf-8")
    assert any("schema" in line for line in problems())


def test_check_bundle_treats_unreadable_parts_as_problems_without_blocking(
    tmp_path: Path,
) -> None:
    publish(tmp_path)
    (tmp_path / "h.json").unlink()
    os.mkfifo(tmp_path / "h.json")  # a FIFO must be refused, not opened for reading
    assert any(
        "h.json" in p and "regular file" in p
        for p in check_bundle(tmp_path, "h.meta.json")
    )
    manifest = tmp_path / "h.meta.json"
    body = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": 1,
        "outputs": {"h.md": "not-a-digest"},
    }
    manifest.write_text(json.dumps(body), encoding="utf-8")
    assert any("no valid SHA-256" in p for p in check_bundle(tmp_path, "h.meta.json"))
    manifest.unlink()
    manifest.mkdir()
    assert any("cannot be read" in p for p in check_bundle(tmp_path, "h.meta.json"))


def test_a_failed_write_removes_the_file_it_just_created(
    tmp_path: Path, package: WorkPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full_disk(_fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", full_disk)
    with pytest.raises(OmnitientError) as info:
        create_package(tmp_path / "p.json", package)
    assert code_of(info) == "store.unwritable"
    assert listing(tmp_path) == []


def test_check_bundle_refuses_names_outside_the_directory(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_bytes(b"x")
    body = {"schema": MANIFEST_SCHEMA, "schema_version": 1, "outputs": {}}
    out = tmp_path / "out"
    out.mkdir()
    body["outputs"] = {"../secret.txt": sha(b"x")}
    (out / "h.meta.json").write_text(json.dumps(body), encoding="utf-8")
    assert any("not a plain file name" in p for p in check_bundle(out, "h.meta.json"))


def test_preexisting_file_at_the_old_temporary_name_survives_an_update(
    tmp_path: Path, package: WorkPackage
) -> None:
    path = tmp_path / "p.json"
    create_package(path, package)
    bystander = tmp_path / f".p.json.tmp-{os.getpid()}"
    bystander.write_text("not ours", encoding="utf-8")
    update_package(load_package(path), lambda p: replace(p, title="Updated"))
    assert bystander.read_text(encoding="utf-8") == "not ours"
    assert load_package(path).package.title == "Updated"


def test_exhausted_temporary_names_fail_without_deleting_the_colliding_file(
    tmp_path: Path, package: WorkPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets, "token_hex", lambda _n=None: "feedface")
    path = tmp_path / "p.json"
    create_package(path, package)
    original = path.read_bytes()
    squatter = next(
        (tmp_path / name for name in [f".p.json.tmp-{os.getpid()}-feedface"]),
    )
    squatter.write_text("not ours", encoding="utf-8")
    with pytest.raises(OmnitientError) as info:
        update_package(load_package(path), lambda p: replace(p, title="Updated"))
    assert code_of(info) == "store.unwritable"
    assert squatter.read_text(encoding="utf-8") == "not ours"
    assert path.read_bytes() == original
    assert not (tmp_path / "p.json.lock").exists()
    out = tmp_path / "out"
    out.mkdir()
    (out / ".h.md.tmp-feedface").write_text("not ours", encoding="utf-8")
    (out / f".h.md.tmp-{os.getpid()}-feedface").write_text("not ours", "utf-8")
    with pytest.raises(OmnitientError) as info:
        publish(out, overwrite=True)
    assert code_of(info) == "output.unwritable"
    assert (out / f".h.md.tmp-{os.getpid()}-feedface").read_text("utf-8") == "not ours"


def test_reread_during_update_is_bounded_like_the_first_read(
    tmp_path: Path, package: WorkPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "p.json"
    create_package(path, package)
    loaded = load_package(path)
    monkeypatch.setattr(store, "MAX_PACKAGE_BYTES", len(path.read_bytes()) + 10)

    def grow(pkg: WorkPackage) -> WorkPackage:
        path.write_bytes(b" " * 5000)
        return replace(pkg, title="Mine")

    with pytest.raises(OmnitientError) as info:
        update_package(loaded, grow)
    assert code_of(info) == "store.conflict"
    assert "larger than" in info.value.diagnostics[0].message
    assert not (tmp_path / "p.json.lock").exists()


def test_reread_during_update_does_not_block_on_a_fifo(
    tmp_path: Path, package: WorkPackage
) -> None:
    path = tmp_path / "p.json"
    create_package(path, package)
    script = textwrap.dedent("""
        import os, sys
        from dataclasses import replace
        from pathlib import Path
        from omnitient.errors import OmnitientError
        from omnitient.store import load_package, update_package

        path = Path(sys.argv[1])
        loaded = load_package(path)

        def swap_for_a_fifo(pkg):
            path.unlink()
            os.mkfifo(path)
            return replace(pkg, title="Mine")

        try:
            update_package(loaded, swap_for_a_fifo)
        except OmnitientError as exc:
            print(exc.diagnostics[0].code, exc.diagnostics[0].message)
        """)
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", script, str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the conflict check blocked on a FIFO")
    assert done.stdout.startswith("store.conflict")
    assert "not a regular file" in done.stdout


def test_load_refuses_a_fifo_instead_of_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.json"
    os.mkfifo(fifo)
    with pytest.raises(OmnitientError) as info:
        load_package(fifo)
    assert code_of(info) == "store.unreadable"


def test_updating_through_a_symlink_edits_the_target(
    tmp_path: Path, package: WorkPackage
) -> None:
    real = tmp_path / "real.json"
    create_package(real, package)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    update_package(load_package(link), lambda p: replace(p, title="Via link"))
    assert link.is_symlink()
    assert load_package(real).package.title == "Via link"
