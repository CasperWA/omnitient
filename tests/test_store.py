"""Persistence: no clobbering, no silent replacement of another update."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from omnitient import ops
from omnitient.errors import OmnitientError
from omnitient.limits import MAX_PACKAGE_BYTES
from omnitient.model import WorkPackage
from omnitient.serialize import dumps
from omnitient.store import (
    create_package,
    load_package,
    package_path,
    update_package,
    write_output,
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


def test_write_output_never_clobbers_unless_asked(tmp_path: Path) -> None:
    target = tmp_path / "out.md"
    write_output(target, b"one", overwrite=False)
    with pytest.raises(OmnitientError) as info:
        write_output(target, b"two", overwrite=False)
    assert code_of(info) == "output.exists"
    assert target.read_bytes() == b"one"
    write_output(target, b"three", overwrite=True)
    assert target.read_bytes() == b"three"
    assert sorted(f.name for f in tmp_path.iterdir()) == ["out.md"]


def test_write_output_does_not_follow_a_symlink_when_not_forced(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    link = tmp_path / "out.md"
    link.symlink_to(victim)
    with pytest.raises(OmnitientError):
        write_output(link, b"overwritten", overwrite=False)
    assert victim.read_text(encoding="utf-8") == "keep"
