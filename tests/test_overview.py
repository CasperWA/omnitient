# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""The collection overview: discovery, assessment, JSON and text output, exits.

Expected values are written out by hand; parity with the O1 commands is checked
separately in ``test_overview_agrees_with_validate``.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from conftest import NOTES_TEXT, SPEC_TEXT, sha
from omnitient import ops, overview_render
from omnitient.cli import main
from omnitient.errors import Diagnostic
from omnitient.limits import MAX_OUTPUT_BYTES
from omnitient.model import ContextRef, Decision, Root, WorkPackage
from omnitient.overview import Overview
from omnitient.serialize import content_digest, dumps

Run = Callable[..., tuple[int, str, str]]
Doc = dict[str, Any]

NOT_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores permission bits; fault injection covers it"
)


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Run:
    def invoke(*args: object) -> tuple[int, str, str]:
        code = main([str(a) for a in args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke


@pytest.fixture
def coll(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def put(collection: Path, name: str, pkg: WorkPackage) -> Path:
    directory = collection / name
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "work-package.json"
    manifest.write_bytes(dumps(pkg))
    return manifest


def variant(pkg: WorkPackage, slug: str, **changes: Any) -> WorkPackage:
    return replace(pkg, work_package=slug, **changes)


def overview(run: Run, *args: object, expect: int | None = None) -> tuple[int, Doc]:
    code, out, err = run("overview", *args, "--json")
    assert err == "", "normal JSON diagnostics are never duplicated to stderr"
    assert out.endswith("}\n")
    if expect is not None:
        assert code == expect, out
    return code, json.loads(out)


def entry_by_path(doc: Doc) -> dict[str, Doc]:
    return {e["path"]: e for e in doc["entries"]}


def codes(diagnostics: list[Doc]) -> list[str]:
    return [d["code"] for d in diagnostics]


def refusal_envelope(code: str, location: str, message: str) -> Doc:
    return {
        "schema": "omnitient/overview",
        "schema_version": 1,
        "discovery_complete": False,
        "assessment_complete": False,
        "results_status": "omitted",
        "counts": None,
        "diagnostics": [
            {
                "code": code,
                "severity": "error",
                "message": message,
                "location": location,
            }
        ],
        "entries": [],
    }


# --- 1, 2: discovery ---------------------------------------------------------


def test_empty_collection_has_the_exact_envelope(run: Run, coll: Path) -> None:
    code, out, err = run("overview", coll, "--json")
    assert (code, err) == (0, "")
    assert out == (
        "{\n"
        '  "schema": "omnitient/overview",\n'
        '  "schema_version": 1,\n'
        '  "discovery_complete": true,\n'
        '  "assessment_complete": true,\n'
        '  "results_status": "complete",\n'
        '  "counts": {\n'
        '    "collection_entries": 0,\n'
        '    "ignored": 0,\n'
        '    "candidates": 0,\n'
        '    "valid": 0,\n'
        '    "invalid": 0,\n'
        '    "excluded": 0,\n'
        '    "unassessed": 0\n'
        "  },\n"
        '  "diagnostics": [],\n'
        '  "entries": []\n'
        "}\n"
    )


def test_only_immediate_children_with_the_exact_manifest_name_are_candidates(
    run: Run, coll: Path, package: WorkPackage
) -> None:
    put(coll, "real", package)
    (coll / "work-package.json").write_bytes(dumps(variant(package, "at-root")))
    (coll / "notes.txt").write_text("x")
    (coll / ".hidden").write_text("x")
    (coll / "docs").mkdir()
    (coll / ".git").mkdir()
    nested = coll / "real" / "nested"
    nested.mkdir()
    (nested / "work-package.json").write_bytes(dumps(variant(package, "nested")))
    other = coll / "other"
    other.mkdir()
    for name in ("Work-Package.json", "work-package.json.bak", "work-package.json~"):
        (other / name).write_bytes(dumps(variant(package, "wrong-name")))

    _, doc = overview(run, coll, expect=0)
    assert doc["counts"] == {
        "collection_entries": 7,
        "ignored": 6,
        "candidates": 1,
        "valid": 1,
        "invalid": 0,
        "excluded": 0,
        "unassessed": 0,
    }
    assert [e["path"] for e in doc["entries"]] == ["real/work-package.json"]
    assert doc["entries"][0]["package"]["id"] == "demo/init/wp1"


def test_generated_handoffs_and_fixtures_below_a_package_are_not_candidates(
    run: Run, coll: Path, package: WorkPackage
) -> None:
    put(coll, "o2", package)
    handoffs = coll / "o2" / "handoffs"
    handoffs.mkdir()
    (handoffs / "handoff-planning.json").write_text("{}")
    (handoffs / "handoff-planning.meta.json").write_text("{}")
    fixtures = coll / "o2" / "fixtures" / "bad"
    fixtures.mkdir(parents=True)
    (fixtures / "work-package.json").write_text("not json")

    _, doc = overview(run, coll, expect=0)
    assert [e["path"] for e in doc["entries"]] == ["o2/work-package.json"]
    assert doc["counts"]["collection_entries"] == 1


def test_listing_is_sorted_by_bytes_and_ignores_creation_and_enumeration_order(
    run: Run,
    coll: Path,
    package: WorkPackage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["b-pkg", "B-pkg", "a-pkg", "_pkg", "z-pkg", "0-pkg"]
    for i, name in enumerate(reversed(names)):
        put(coll, name, variant(package, f"wp-{i}"))
    _, forward = overview(run, coll, expect=0)
    expected = sorted(names, key=os.fsencode)
    assert [e["path"].split("/")[0] for e in forward["entries"]] == expected
    assert expected[:2] == ["0-pkg", "B-pkg"]  # bytes, not locale or case folding

    real_scandir = os.scandir

    def reversed_scandir(fd: Any) -> Any:
        with real_scandir(fd) as it:
            items = list(it)
        items.reverse()

        class Reversed:
            def __enter__(self) -> Iterator[Any]:
                return iter(items)

            def __exit__(self, *exc: object) -> None:
                return None

        return Reversed()

    monkeypatch.setattr(os, "scandir", reversed_scandir)
    code, out, _ = run("overview", coll, "--json")
    assert code == 0
    assert json.loads(out) == forward


# --- 3, 4: links, containment, non-regular files, unreadable paths -----------


def test_a_link_at_the_collection_root_is_refused(
    run: Run, coll: Path, tmp_path: Path, package: WorkPackage
) -> None:
    put(coll, "p", package)
    link = tmp_path / "alias"
    link.symlink_to(coll, target_is_directory=True)
    _, doc = overview(run, link, expect=1)
    assert doc == refusal_envelope(
        "overview.symlink",
        "collection",
        "the collection is a symbolic link, which is not followed; "
        "name the real directory",
    )
    # A trailing slash does not turn the link into a directory.
    assert overview(run, f"{link}/", expect=1)[1] == doc


def test_links_in_ancestor_components_resolve_normally(
    run: Run, tmp_path: Path, coll: Path, package: WorkPackage
) -> None:
    put(coll, "p", package)
    ancestor = tmp_path / "ancestor"
    ancestor.symlink_to(tmp_path, target_is_directory=True)
    _, doc = overview(run, ancestor / "work", expect=0)
    assert [e["path"] for e in doc["entries"]] == ["p/work-package.json"]


def test_links_at_child_and_manifest_level_are_excluded_and_not_followed(
    run: Run,
    coll: Path,
    tmp_path: Path,
    package: WorkPackage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    put(outside, "pkg", variant(package, "outside"))
    put(coll, "good", package)
    (coll / "dirlink").symlink_to(outside / "pkg", target_is_directory=True)
    (coll / "filelink.txt").symlink_to(outside / "pkg" / "work-package.json")
    (coll / "dangling").symlink_to(tmp_path / "does-not-exist")
    holder = coll / "manifestlink"
    holder.mkdir()
    (holder / "work-package.json").symlink_to(outside / "pkg" / "work-package.json")

    opened: list[str] = []
    real_open = os.open

    def spy(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fsdecode(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    _, doc = overview(run, coll, expect=1)
    monkeypatch.undo()

    assert doc["discovery_complete"] is False
    assert doc["assessment_complete"] is False
    assert doc["results_status"] == "partial"
    assert doc["counts"] == {
        "collection_entries": 5,
        "ignored": 0,
        "candidates": 5,
        "valid": 1,
        "invalid": 0,
        "excluded": 4,
        "unassessed": 0,
    }
    by_path = entry_by_path(doc)
    assert by_path["good/work-package.json"]["status"] == "valid"
    for name, location in (
        ("dangling", "directory"),
        ("dirlink", "directory"),
        ("filelink.txt", "directory"),
        ("manifestlink", "manifest"),
    ):
        entry = by_path[f"{name}/work-package.json"]
        assert entry["status"] == "excluded"
        assert (entry["package"], entry["readiness"]) == (None, None)
        assert entry["diagnostics"] == [
            {
                "code": "overview.symlink",
                "severity": "error",
                "message": "a symbolic link is not followed, so a package "
                "behind it is not examined",
                "location": location,
            }
        ]
    assert "demo/init/outside" not in json.dumps(doc)
    # Nothing was opened through a link: only the collection, real children and
    # the one good manifest.
    assert "outside" not in " ".join(opened)


def test_a_manifest_that_is_not_a_regular_file_is_excluded_without_being_read(
    run: Run, coll: Path, package: WorkPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(coll, "good", package)
    fifo = coll / "fifo"
    fifo.mkdir()
    os.mkfifo(fifo / "work-package.json")
    folder = coll / "folder"
    (folder / "work-package.json").mkdir(parents=True)

    opened: list[str] = []
    real_open = os.open

    def spy(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fsdecode(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    _, doc = overview(run, coll, expect=1)
    monkeypatch.undo()

    by_path = entry_by_path(doc)
    for name in ("fifo", "folder"):
        entry = by_path[f"{name}/work-package.json"]
        assert entry["status"] == "excluded"
        assert [d["code"] for d in entry["diagnostics"]] == ["overview.not_regular"]
    # A manifest that is not a regular file hides no package: coverage is known.
    assert doc["discovery_complete"] is True
    assert doc["assessment_complete"] is False
    assert doc["results_status"] == "complete"
    assert opened.count("work-package.json") == 1  # only the good one


@NOT_ROOT
def test_unreadable_child_directory_and_manifest_with_permission_bits(
    run: Run, coll: Path, package: WorkPackage
) -> None:
    put(coll, "a-good", package)
    locked = coll / "b-locked"
    put(coll, "b-locked", variant(package, "locked"))
    unreadable = put(coll, "c-unreadable", variant(package, "unreadable"))
    put(coll, "d-good", variant(package, "other"))
    locked.chmod(0)
    unreadable.chmod(0)
    try:
        _, doc = overview(run, coll, expect=1)
    finally:
        locked.chmod(stat.S_IRWXU)
        unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)

    by_path = entry_by_path(doc)
    assert by_path["b-locked/work-package.json"]["status"] == "unassessed"
    assert [
        d["code"] for d in by_path["b-locked/work-package.json"]["diagnostics"]
    ] == ["overview.discovery_unreadable"]
    assert by_path["c-unreadable/work-package.json"]["status"] == "invalid"
    assert [
        d["code"] for d in by_path["c-unreadable/work-package.json"]["diagnostics"]
    ] == ["overview.manifest_unreadable"]
    assert by_path["a-good/work-package.json"]["status"] == "valid"
    assert by_path["d-good/work-package.json"]["status"] == "valid"
    assert doc["discovery_complete"] is False
    assert doc["results_status"] == "partial"
    assert "Permission" not in json.dumps(doc)


def fault_open(
    monkeypatch: pytest.MonkeyPatch, name: str, error: int, *, on_dir: bool
) -> None:
    """Make ``os.open`` of ``name`` fail, as a file system with other rules would."""
    real_open = os.open

    def faulty(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        is_dir = bool(flags & os.O_DIRECTORY)
        if os.fsdecode(path) == name and is_dir == on_dir:
            raise OSError(error, os.strerror(error), os.fsdecode(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", faulty)


def test_unreadable_paths_by_fault_injection_even_where_modes_are_ignored(
    run: Run, coll: Path, package: WorkPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(coll, "a-good", package)
    put(coll, "b-locked", variant(package, "locked"))
    put(coll, "c-manifest", variant(package, "manifest"))
    real_open = os.open

    def faulty(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        target = os.fsdecode(path)
        if target == "b-locked" and flags & os.O_DIRECTORY:
            raise PermissionError(errno.EACCES, "Permission denied", target)
        # The manifest of c-manifest: found by lstat, then refused when opened.
        if (
            target == "work-package.json"
            and "dir_fd" in kwargs
            and Path(f"/proc/self/fd/{kwargs['dir_fd']}").readlink().name
            == "c-manifest"
        ):
            raise OSError(errno.EIO, "Input/output error", target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", faulty)
    _, doc = overview(run, coll, expect=1)
    by_path = entry_by_path(doc)
    assert by_path["b-locked/work-package.json"]["status"] == "unassessed"
    assert by_path["c-manifest/work-package.json"]["status"] == "invalid"
    assert by_path["a-good/work-package.json"]["status"] == "valid"
    text = json.dumps(doc)
    assert "Permission denied" not in text
    assert "Input/output" not in text


def test_a_manifest_swapped_for_a_fifo_after_inventory_is_never_blocked_on(
    run: Run, coll: Path, package: WorkPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = put(coll, "p", package)
    from omnitient import collection

    real_probe = collection.probe_child

    def swap_then_probe(root_fd: int, name: str, *, read: bool) -> Any:
        if read:
            manifest.unlink()
            os.mkfifo(manifest)
        return real_probe(root_fd, name, read=read)

    monkeypatch.setattr("omnitient.overview.probe_child", swap_then_probe)
    _, doc = overview(run, coll, expect=1)
    assert [e["status"] for e in doc["entries"]] == ["excluded"]
    assert codes(doc["entries"][0]["diagnostics"]) == ["overview.not_regular"]


@pytest.mark.parametrize("swap", ["directory", "manifest"])
def test_a_link_swapped_in_after_inventory_is_not_followed(
    run: Run,
    coll: Path,
    tmp_path: Path,
    package: WorkPackage,
    monkeypatch: pytest.MonkeyPatch,
    swap: str,
) -> None:
    outside = tmp_path / "outside"
    put(outside, "pkg", variant(package, "outside"))
    manifest = put(coll, "p", package)
    from omnitient import collection

    real_probe = collection.probe_child

    def swap_then_probe(root_fd: int, name: str, *, read: bool) -> Any:
        if read and swap == "directory":
            manifest.unlink()
            manifest.parent.rmdir()
            (coll / "p").symlink_to(outside / "pkg", target_is_directory=True)
        elif read:
            manifest.unlink()
            manifest.symlink_to(outside / "pkg" / "work-package.json")
        return real_probe(root_fd, name, read=read)

    monkeypatch.setattr("omnitient.overview.probe_child", swap_then_probe)
    _, doc = overview(run, coll, expect=1)
    entry = doc["entries"][0]
    assert entry["status"] == "excluded"
    assert codes(entry["diagnostics"]) == ["overview.symlink"]
    assert doc["discovery_complete"] is False
    assert "outside" not in json.dumps(doc)


def test_entry_that_cannot_be_examined_is_unassessed_and_peers_continue(
    run: Run, coll: Path, package: WorkPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(coll, "a", package)
    put(coll, "b", variant(package, "b"))
    real_lstat = os.lstat

    def faulty(path: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fsdecode(path) == "a" and "dir_fd" in kwargs:
            raise OSError(errno.EIO, "Input/output error")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", faulty)
    _, doc = overview(run, coll, expect=1)
    assert [(e["path"], e["status"]) for e in doc["entries"]] == [
        ("a/work-package.json", "unassessed"),
        ("b/work-package.json", "valid"),
    ]
    assert doc["counts"]["unassessed"] == 1
    assert doc["results_status"] == "partial"


def test_manifest_that_vanishes_or_fails_midway_is_reported_not_guessed(
    run: Run, coll: Path, package: WorkPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = put(coll, "a-gone", package)
    put(coll, "b-search", variant(package, "search"))
    put(coll, "c-read", variant(package, "read"))
    from omnitient import collection

    real_probe = collection.probe_child
    real_lstat = os.lstat
    real_fstat = os.fstat

    def vanish(root_fd: int, name: str, *, read: bool) -> Any:
        if read and name == "a-gone":
            gone.unlink()
        return real_probe(root_fd, name, read=read)

    def faulty_lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if (
            os.fsdecode(path) == "work-package.json"
            and Path(f"/proc/self/fd/{kwargs.get('dir_fd', -1)}").exists()
            and Path(f"/proc/self/fd/{kwargs['dir_fd']}").readlink().name == "b-search"
        ):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_lstat(path, *args, **kwargs)

    def faulty_fstat(fd: int) -> Any:
        if Path(f"/proc/self/fd/{fd}").readlink().name == "work-package.json" and (
            Path(f"/proc/self/fd/{fd}").readlink().parent.name == "c-read"
        ):
            raise OSError(errno.EIO, "Input/output error")
        return real_fstat(fd)

    monkeypatch.setattr("omnitient.overview.probe_child", vanish)
    monkeypatch.setattr(os, "lstat", faulty_lstat)
    monkeypatch.setattr(os, "fstat", faulty_fstat)
    _, doc = overview(run, coll, expect=1)
    by_path = entry_by_path(doc)
    assert by_path["a-gone/work-package.json"]["status"] == "unassessed"
    assert codes(by_path["a-gone/work-package.json"]["diagnostics"]) == [
        "overview.discovery_unreadable"
    ]
    assert by_path["b-search/work-package.json"]["status"] == "unassessed"
    assert by_path["c-read/work-package.json"]["status"] == "invalid"
    assert codes(by_path["c-read/work-package.json"]["diagnostics"]) == [
        "overview.manifest_unreadable"
    ]
    assert "Permission" not in json.dumps(doc)
    assert "Input/output" not in json.dumps(doc)


def test_reading_a_manifest_refuses_links_and_fifos_that_appear_after_the_check(
    tmp_path: Path,
) -> None:
    from omnitient import collection

    holder = tmp_path / "holder"
    holder.mkdir()
    target = tmp_path / "target.json"
    target.write_text("{}")
    (holder / "work-package.json").symlink_to(target)
    fd = os.open(holder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        link = collection._read_manifest(fd)
        (holder / "work-package.json").unlink()
        os.mkfifo(holder / "work-package.json")
        fifo = collection._read_manifest(fd)  # must return, not block
    finally:
        os.close(fd)
    assert (link.kind, link.diagnostic and link.diagnostic.code) == (
        "excluded",
        "overview.symlink",
    )
    assert (fifo.kind, fifo.diagnostic and fifo.diagnostic.code) == (
        "excluded",
        "overview.not_regular",
    )
    assert link.raw is None
    assert fifo.raw is None


def test_a_collection_path_that_cannot_be_used_at_all_is_a_refusal() -> None:
    from omnitient.overview import collection_overview

    result = collection_overview(Path("a\0b"), (), None)
    assert result.results_status == "omitted"
    assert result.diagnostics[0].code == "overview.collection_unreadable"


def test_invalid_collections_get_a_bounded_refusal(run: Run, tmp_path: Path) -> None:
    missing = overview(run, tmp_path / "nope", expect=1)[1]
    assert missing == refusal_envelope(
        "overview.collection_invalid", "collection", "the collection does not exist"
    )
    plain = tmp_path / "file"
    plain.write_text("x")
    assert overview(run, plain, expect=1)[1] == refusal_envelope(
        "overview.collection_invalid", "collection", "the collection is not a directory"
    )
    below_file = overview(run, plain / "child", expect=1)[1]
    assert below_file["diagnostics"][0]["code"] == "overview.collection_invalid"
    for doc in (missing, below_file):
        assert str(tmp_path) not in json.dumps(doc)


@NOT_ROOT
def test_unreadable_collection_directory(run: Run, coll: Path) -> None:
    coll.chmod(0)
    try:
        _, doc = overview(run, coll, expect=1)
    finally:
        coll.chmod(stat.S_IRWXU)
    assert doc == refusal_envelope(
        "overview.collection_unreadable", "collection", "the collection cannot be read"
    )


def test_unreadable_collection_by_fault_injection(
    run: Run, coll: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open

    def faulty(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & os.O_DIRECTORY and "dir_fd" not in kwargs:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", faulty)
    _, doc = overview(run, coll, expect=1)
    assert doc["diagnostics"][0]["code"] == "overview.collection_unreadable"
    assert "Permission" not in json.dumps(doc)


# --- 5: mixed results and duplicates ----------------------------------------


def test_mixed_valid_malformed_and_unreadable_candidates_in_one_report(
    run: Run, coll: Path, package: WorkPackage, repo: Path
) -> None:
    put(coll, "a-ok", package)
    bad = coll / "b-bad"
    bad.mkdir()
    (bad / "work-package.json").write_text('{"schema": "omnitient/work-package"}')
    syntax = coll / "c-syntax"
    syntax.mkdir()
    (syntax / "work-package.json").write_bytes(b"\xff\xfe")
    big = coll / "d-big"
    big.mkdir()
    (big / "work-package.json").write_bytes(b" " * 262_145)

    _, doc = overview(run, coll, "--root", f"repo={repo}", expect=1)
    by_path = entry_by_path(doc)
    assert doc["discovery_complete"] is True
    assert doc["assessment_complete"] is False
    assert doc["results_status"] == "complete"
    assert doc["counts"]["valid"] == 1
    assert doc["counts"]["invalid"] == 3
    assert doc["counts"]["candidates"] == 4
    for name in ("b-bad", "c-syntax", "d-big"):
        entry = by_path[f"{name}/work-package.json"]
        assert entry["status"] == "invalid"
        assert (entry["package"], entry["readiness"]) == (None, None)
        assert entry["diagnostics"]
    assert "schema.missing_field" in codes(
        by_path["b-bad/work-package.json"]["diagnostics"]
    )
    assert codes(by_path["c-syntax/work-package.json"]["diagnostics"]) == [
        "schema.syntax"
    ]
    assert codes(by_path["d-big/work-package.json"]["diagnostics"]) == [
        "schema.too_large"
    ]
    assert by_path["a-ok/work-package.json"]["status"] == "valid"


def test_duplicate_ids_keep_every_path_and_assessment_and_fail_the_command(
    run: Run, coll: Path, package: WorkPackage, repo: Path
) -> None:
    put(coll, "first", package)
    put(coll, "second", replace(package, title="Second copy"))
    put(coll, "third", variant(package, "unique"))
    _, doc = overview(run, coll, "--root", f"repo={repo}", expect=1)
    assert doc["discovery_complete"] is True
    assert doc["assessment_complete"] is True
    assert doc["results_status"] == "complete"
    assert doc["diagnostics"] == [
        {
            "code": "overview.duplicate_id",
            "severity": "error",
            "message": "package id demo/init/wp1 is declared by 2 manifests: "
            "first/work-package.json, second/work-package.json",
            "location": "collection",
        }
    ]
    by_path = entry_by_path(doc)
    for name, title in (("first", "Demo package"), ("second", "Second copy")):
        entry = by_path[f"{name}/work-package.json"]
        assert entry["status"] == "valid"
        assert entry["package"]["title"] == title
        assert entry["readiness"] is not None
        assert codes(entry["diagnostics"]) == ["overview.duplicate_id"]
        assert entry["diagnostics"][0]["location"] == "package.id"
        # Duplicate-ID problems never become O1 readiness blockers.
        for stage in entry["readiness"].values():
            assert "overview.duplicate_id" not in codes(stage["blockers"])
    assert by_path["third/work-package.json"]["diagnostics"] == []


# --- 6, 7: declared stage vs readiness; context and decisions ----------------


def test_declared_done_with_a_stale_approval_is_not_ready(
    run: Run, coll: Path, approved: WorkPackage, repo: Path
) -> None:
    changed = replace(approved, stage="done", objective="A different objective.")
    put(coll, "p", changed)
    _, doc = overview(run, coll, "--root", f"repo={repo}", expect=0)
    entry = doc["entries"][0]
    assert entry["package"]["declared_stage"] == "done"
    assert entry["package"]["content_digest"] == content_digest(changed)
    impl = entry["readiness"]["implementation"]
    assert impl["ready"] is False
    assert [(d["code"], d["severity"]) for d in impl["blockers"]] == [
        ("approval.stale", "blocker")
    ]
    assert impl["actions"] == [
        {
            "code": "seek_approval",
            "diagnostics": [{"code": "approval.stale", "location": None}],
        }
    ]
    review = entry["readiness"]["review"]
    assert [d["code"] for d in review["blockers"]] == ["approval.missing"]
    planning = entry["readiness"]["planning"]
    assert planning["ready"] is True
    assert planning["actions"] == [{"code": "handoff_ready", "diagnostics": []}]
    assert list(entry["readiness"]) == ["planning", "implementation", "review"]


def test_context_and_decisions_map_to_advice_in_the_fixed_category_order(
    run: Run, coll: Path, package: WorkPackage, repo: Path
) -> None:
    spec = package.context[0]
    pkg = replace(
        package,
        roots=(*package.roots, Root("elsewhere", "Not mapped")),
        context=(
            replace(spec, sha256=sha("other")),  # required, drifted
            replace(package.context[1], id="gone", path="docs/gone.md"),  # optional
            ContextRef("remote", "elsewhere", "a.md", sha("a"), True),  # unmapped
            ContextRef("remote2", "elsewhere", "b.md", sha("b"), False),
        ),
        decisions=(
            Decision("D1", "Which format?", "unresolved", "me", "implementation"),
            Decision("D2", "Later?", "unresolved", "me", "review"),
        ),
        objective="TODO: write it",
    )
    put(coll, "p", pkg)
    _, doc = overview(run, coll, "--root", f"repo={repo}", expect=0)
    readiness = doc["entries"][0]["readiness"]

    planning = readiness["planning"]
    assert [(d["code"], d["location"]) for d in planning["blockers"]] == [
        ("context.digest_mismatch", "spec"),
        ("context.root_unmapped", "remote"),
        ("content.incomplete", None),
    ]
    assert [
        (d["code"], d["severity"], d["location"]) for d in planning["warnings"]
    ] == [
        ("decision.open", "warning", "D1"),
        ("decision.open", "warning", "D2"),
        ("context.missing", "warning", "gone"),
        ("context.root_unmapped", "warning", "remote2"),
    ]
    assert planning["ready"] is False
    assert [a["code"] for a in planning["actions"]] == [
        "inspect_package",
        "resolve_decision",
        "review_context",
        "supply_context",
    ]
    assert planning["actions"][3]["diagnostics"] == [
        {"code": "context.root_unmapped", "location": "remote"},
        {"code": "context.missing", "location": "gone"},
        {"code": "context.root_unmapped", "location": "remote2"},
    ]
    impl = readiness["implementation"]
    assert [d["code"] for d in impl["blockers"]][:1] == ["decision.unresolved"]
    assert [a["code"] for a in impl["actions"]] == [
        "inspect_package",
        "resolve_decision",
        "review_context",
        "supply_context",
        "seek_approval",
    ]
    # A handoff_ready action exists only when a stage has no blockers.
    assert all(
        a["code"] != "handoff_ready" for s in readiness.values() for a in s["actions"]
    )


def test_identical_action_references_are_listed_once_but_diagnostics_are_not_merged(
    run: Run, coll: Path, package: WorkPackage
) -> None:
    pkg = replace(package, objective="todo", scope=(), acceptance=())
    put(coll, "p", pkg)
    _, doc = overview(run, coll, expect=0)
    impl = doc["entries"][0]["readiness"]["implementation"]
    assert codes(impl["blockers"]).count("content.incomplete") == 2
    inspect = next(a for a in impl["actions"] if a["code"] == "inspect_package")
    assert inspect["diagnostics"] == [{"code": "content.incomplete", "location": None}]


def test_warnings_alone_keep_a_stage_ready_and_the_exit_zero(
    run: Run, coll: Path, package: WorkPackage, repo: Path
) -> None:
    pkg = replace(
        package,
        decisions=(Decision("D9", "Later?", "unresolved", "me", "review"),),
        context=(package.context[0], replace(package.context[1], sha256=sha("x"))),
    )
    put(coll, "p", pkg)
    _, doc = overview(run, coll, "--root", f"repo={repo}", expect=0)
    planning = doc["entries"][0]["readiness"]["planning"]
    assert planning["ready"] is True
    assert [d["severity"] for d in planning["warnings"]] == ["warning", "warning"]
    assert [a["code"] for a in planning["actions"]] == [
        "resolve_decision",
        "review_context",
        "handoff_ready",
    ]


def test_other_context_problems_map_to_inspect_diagnostic(
    run: Run, coll: Path, package: WorkPackage, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    (repo / "docs" / "escape.md").symlink_to(outside)
    (repo / "docs" / "binary.md").write_bytes(b"\x00\x01")
    pkg = replace(
        package,
        context=(
            ContextRef("escape", "repo", "docs/escape.md", sha("secret")),
            ContextRef("binary", "repo", "docs/binary.md", sha(b"\x00\x01")),
        ),
    )
    put(coll, "p", pkg)
    _, doc = overview(run, coll, "--root", f"repo={repo}", expect=0)
    planning = doc["entries"][0]["readiness"]["planning"]
    assert [(d["code"], d["location"]) for d in planning["blockers"]] == [
        ("context.unsafe_path", "escape"),
        ("context.not_text", "binary"),
    ]
    assert planning["actions"] == [
        {
            "code": "inspect_diagnostic",
            "diagnostics": [
                {"code": "context.unsafe_path", "location": "escape"},
                {"code": "context.not_text", "location": "binary"},
            ],
        }
    ]
    assert str(tmp_path) not in json.dumps(doc)


def test_unreadable_context_message_is_normalized_and_keeps_code_and_location(
    run: Run,
    coll: Path,
    package: WorkPackage,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put(coll, "p", package)

    def failing(_path: Path) -> Any:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr("omnitient.context.open_regular", failing)
    _, doc = overview(run, coll, "--root", f"repo={repo}", expect=0)
    blocker = doc["entries"][0]["readiness"]["planning"]["blockers"][0]
    assert blocker == {
        "code": "context.unreadable",
        "severity": "blocker",
        "message": "repo:docs/spec.md: the path or file could not be read",
        "location": "spec",
    }
    assert "Input/output" not in json.dumps(doc)


def pkg_id(collection: Path, name: str) -> str:
    return str(
        json.loads((collection / name / "work-package.json").read_text())[
            "work_package"
        ]
    )


def test_overview_agrees_with_validate(
    run: Run, coll: Path, package: WorkPackage, repo: Path, approved: WorkPackage
) -> None:
    spec = package.context[0]
    cases = {
        "ready": approved,
        "drifted": replace(package, context=(replace(spec, sha256=sha("x")),)),
        "blocked": replace(
            package,
            decisions=(Decision("D1", "Open?", "unresolved", "me", "planning"),),
            context=(),
        ),
        "unmapped": replace(
            package,
            roots=(Root("repo", ""), Root("other", "")),
            context=(ContextRef("o", "other", "x.md", sha("x")),),
        ),
    }
    for index, (name, pkg) in enumerate(cases.items()):
        # The work-package slug is approval-covered, so the approved case keeps
        # the fixture's own identity; the others get distinct synthetic ones.
        put(coll, name, pkg if name == "ready" else variant(pkg, f"case-{index}"))
    assert len({pkg_id(coll, name) for name in cases}) == len(cases)
    _, doc = overview(run, coll, "--root", f"repo={repo}", expect=0)

    # Independent expectation: the approved case is ready for implementation, with
    # a current approval, under both commands. It holds no review approval, so
    # review readiness is not asserted ready.
    code, out, _ = run("validate", coll / "ready", "--json", "--root", f"repo={repo}")
    assert code == 0
    o1_implementation = json.loads(out)["readiness"]["implementation"]
    o2_implementation = entry_by_path(doc)["ready/work-package.json"]["readiness"][
        "implementation"
    ]
    for report in (o1_implementation, o2_implementation):
        assert report["ready"] is True
        assert report["blockers"] == []
        blocker_codes = [d["code"] for d in report["blockers"]]
        assert "approval.missing" not in blocker_codes
        assert "approval.stale" not in blocker_codes
    assert [a["code"] for a in o2_implementation["actions"]] == ["handoff_ready"]
    assert entry_by_path(doc)["ready/work-package.json"]["package"]["id"] == (
        "demo/init/wp1"
    )

    for name in cases:
        code, out, _ = run("validate", coll / name, "--json", "--root", f"repo={repo}")
        assert code == 0
        o1 = json.loads(out)
        entry = entry_by_path(doc)[f"{name}/work-package.json"]
        assert entry["package"]["id"] == o1["package"]
        assert entry["package"]["content_digest"] == o1["content_digest"]
        for stage, expected in o1["readiness"].items():
            got = entry["readiness"][stage]
            assert got["ready"] == expected["ready"]
            for kind in ("blockers", "warnings"):
                assert [
                    {**d, "location": d.get("location")} for d in expected[kind]
                ] == got[kind]


# --- 8: host mapping ----------------------------------------------------------


def test_invalid_mapping_aborts_before_discovery_and_is_host_neutral(
    run: Run, coll: Path, tmp_path: Path, package: WorkPackage
) -> None:
    put(coll, "p", package)
    missing = tmp_path / "no-such-dir"
    _, relative = overview(run, coll, "--root", "repo=relative/dir", expect=1)
    assert relative == refusal_envelope(
        "roots.invalid",
        "roots",
        "the host mapping is invalid; check --root and --roots-file",
    )
    _, gone = overview(run, coll, "--root", f"repo={missing}", expect=1)
    assert gone == relative
    loop = tmp_path / "loop"
    loop.symlink_to(loop)  # resolving it raises RuntimeError on Python 3.12
    assert overview(run, coll, "--root", f"repo={loop}", expect=1)[1] == relative
    for bad in ("nodirectory", "=x", "Bad_Name=/tmp"):
        assert overview(run, coll, "--root", bad, expect=1)[1] == relative
    _, duplicate = overview(
        run, coll, "--root", f"a={tmp_path}", "--root", f"a={tmp_path}", expect=1
    )
    assert duplicate["diagnostics"] == [
        {
            "code": "roots.duplicate",
            "severity": "error",
            "message": "a root name is mapped more than once; check --root and --roots-file",
            "location": "roots",
        }
    ]
    assert str(tmp_path) not in json.dumps([relative, gone, duplicate])


def test_roots_file_validation_is_o1_validation(
    run: Run, coll: Path, tmp_path: Path, repo: Path
) -> None:
    good = tmp_path / "roots.json"
    good.write_text(json.dumps({"roots": {"repo": str(repo)}}))
    assert (
        overview(run, coll, "--roots-file", good, expect=0)[1]["counts"]["candidates"]
        == 0
    )
    for text in (
        "{",
        '{"roots": {"repo": 1}}',
        '{"roots": {"a": "/tmp", "a": "/tmp"}}',
        '{"roots": {"repo": "/tmp\\u0000x"}}',
        '{"other": {}}',
    ):
        bad = tmp_path / "bad.json"
        bad.write_text(text)
        _, doc = overview(run, coll, "--roots-file", bad, expect=1)
        assert doc["results_status"] == "omitted"
        assert doc["diagnostics"][0]["code"] == "roots.invalid"
        assert str(tmp_path) not in json.dumps(doc)
    _, mixed = overview(
        run, coll, "--roots-file", good, "--root", f"repo={repo}", expect=1
    )
    assert mixed["diagnostics"][0]["code"] == "roots.duplicate"
    _, absent = overview(run, coll, "--roots-file", tmp_path / "absent.json", expect=1)
    assert absent["diagnostics"][0]["code"] == "roots.invalid"


def test_preflight_order_is_mapping_before_collection(run: Run, tmp_path: Path) -> None:
    _, doc = overview(run, tmp_path / "missing", "--root", "repo=relative", expect=1)
    assert doc["diagnostics"][0]["code"] == "roots.invalid"
    _, doc = overview(run, tmp_path / "missing", expect=1)
    assert doc["diagnostics"][0]["code"] == "overview.collection_invalid"
    # argparse comes before everything else and keeps its own behaviour.
    with pytest.raises(SystemExit) as exc:
        main(["overview"])
    assert exc.value.code == 2


def test_unknown_mapped_roots_are_reported_once_for_the_whole_collection(
    run: Run, coll: Path, package: WorkPackage, repo: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    spare = tmp_path / "spare"
    spare.mkdir()
    put(coll, "a", package)  # declares only "repo"
    put(coll, "b", variant(package, "b", roots=(Root("other", ""),), context=()))
    _, doc = overview(
        run,
        coll,
        "--root",
        f"repo={repo}",
        "--root",
        f"other={other}",
        "--root",
        f"spare={spare}",
        expect=0,
    )
    assert doc["diagnostics"] == [
        {
            "code": "roots.unknown",
            "severity": "warning",
            "message": "root 'spare' is mapped but not declared by any package "
            "in the collection",
            "location": "roots",
        }
    ]


def test_unused_root_warnings_are_suppressed_when_usage_cannot_be_established(
    run: Run, coll: Path, package: WorkPackage, repo: Path
) -> None:
    put(coll, "a", package)
    broken = coll / "broken"
    broken.mkdir()
    (broken / "work-package.json").write_text("{")
    # "repo" might be declared by the broken manifest.
    unused = repo.parent / "unused"
    unused.mkdir()
    _, doc = overview(
        run, coll, "--root", f"repo={repo}", "--root", f"unused={unused}", expect=1
    )
    assert doc["diagnostics"] == []
    # A collection that is fully understood does report it, even an empty one.
    empty = coll.parent / "empty"
    empty.mkdir()
    _, doc = overview(run, empty, "--root", f"unused={unused}", expect=0)
    assert codes(doc["diagnostics"]) == ["roots.unknown"]


# --- 9: envelope shape and completeness --------------------------------------


def test_valid_blocked_packages_exit_zero_and_never_three(
    run: Run, coll: Path, package: WorkPackage
) -> None:
    put(coll, "p", replace(package, objective="TODO"))
    code, doc = overview(run, coll)
    assert code == 0
    assert doc["assessment_complete"] is True
    assert doc["entries"][0]["readiness"]["implementation"]["ready"] is False


def test_json_envelope_key_order_types_and_nullability(
    run: Run, coll: Path, package: WorkPackage
) -> None:
    put(coll, "ok", package)
    bad = coll / "bad"
    bad.mkdir()
    (bad / "work-package.json").write_text("[")
    _, out, _ = run("overview", coll, "--json")
    doc = json.loads(out)
    assert list(doc) == [
        "schema",
        "schema_version",
        "discovery_complete",
        "assessment_complete",
        "results_status",
        "counts",
        "diagnostics",
        "entries",
    ]
    assert list(doc["counts"]) == [
        "collection_entries",
        "ignored",
        "candidates",
        "valid",
        "invalid",
        "excluded",
        "unassessed",
    ]
    assert all(type(v) is int for v in doc["counts"].values())
    assert type(doc["discovery_complete"]) is bool
    assert type(doc["assessment_complete"]) is bool
    valid, invalid = doc["entries"][1], doc["entries"][0]
    assert list(valid) == ["path", "status", "package", "diagnostics", "readiness"]
    assert list(valid["package"]) == ["id", "title", "declared_stage", "content_digest"]
    assert len(valid["package"]["content_digest"]) == 64
    assert list(valid["readiness"]["planning"]) == [
        "ready",
        "blockers",
        "warnings",
        "actions",
    ]
    assert invalid["package"] is None
    assert invalid["readiness"] is None
    assert list(invalid["diagnostics"][0]) == [
        "code",
        "severity",
        "message",
        "location",
    ]
    for stage in valid["readiness"].values():
        for diag in (*stage["blockers"], *stage["warnings"]):
            assert "location" in diag
        for action in stage["actions"]:
            assert list(action) == ["code", "diagnostics"]
    # The output is exactly what the contract says: ASCII, indented, one final LF.
    assert out == json.dumps(doc, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    out.encode("ascii")


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("empty", (True, True, "complete", 0)),
        ("blocked", (True, True, "complete", 0)),
        ("malformed", (True, False, "complete", 1)),
        ("not_regular", (True, False, "complete", 1)),
        ("duplicate", (True, True, "complete", 1)),
        ("symlink", (False, False, "partial", 1)),
    ],
)
def test_completeness_combinations_and_exit_status(
    run: Run,
    coll: Path,
    package: WorkPackage,
    setup: str,
    expected: tuple[bool, bool, str, int],
) -> None:
    if setup == "blocked":
        put(coll, "p", replace(package, objective="TODO"))
    elif setup == "malformed":
        (coll / "p").mkdir()
        (coll / "p" / "work-package.json").write_text("nope")
    elif setup == "not_regular":
        (coll / "p" / "work-package.json").mkdir(parents=True)
    elif setup == "duplicate":
        put(coll, "p", package)
        put(coll, "q", package)
    elif setup == "symlink":
        (coll / "p").symlink_to(coll, target_is_directory=True)
    code, doc = overview(run, coll)
    assert (
        doc["discovery_complete"],
        doc["assessment_complete"],
        doc["results_status"],
        code,
    ) == expected


# --- 10: terminal text, authored strings, odd file names ---------------------


def test_escape_text_covers_controls_bidi_surrogates_and_the_backslash() -> None:
    escape = overview_render.escape_text
    assert escape("a\x1b[31m\x00\x7f\x85\r\n\t") == (
        "a\\x1b[31m\\x00\\x7f\\x85\\x0d\\x0a\\x09"
    )
    assert escape("\u202e\u2066\u200f\u061c\u2028\u00a0") == (
        "\\u202e\\u2066\\u200f\\u061c\\u2028\\xa0"
    )
    assert escape("\udcff\U000f0000\U0010ffff") == "\\udcff\\U000f0000\\U0010ffff"
    assert escape("back\\slash") == "back\\\\slash"
    assert escape("Støtte æøå ✓") == "Støtte æøå ✓"  # printable text is kept
    assert overview_render.shorten("abcdef", 4) == "abc…"
    assert overview_render.shorten("abcd", 4) == "abcd"


def test_text_output_escapes_invisible_characters_and_marks_shortened_text(
    run: Run, coll: Path, package: WorkPackage
) -> None:
    # Bidi marks and controls are schema errors in a title, but other invisible
    # format characters are valid, and a terminal would hide or reorder around them.
    title = "Tit\u200bl\u00ade \ufeffæøå back\\slash " + "y" * 150
    put(coll, "p", replace(package, title=title))
    code, out, err = run("overview", coll)
    assert (code, err) == (0, "")
    for char in "\u200b\u00ad\ufeff\x1b\r":
        assert char not in out
    for line in out.splitlines():
        assert all(c.isprintable() for c in line)
    for escaped in ("\\u200b", "\\xad", "\\ufeff"):
        assert escaped in out
    assert "back\\\\slash" in out
    assert "æøå" in out
    assert "…" in out  # the shortened title is visibly marked
    assert "y" * 150 not in out
    _, doc = overview(run, coll)
    assert doc["entries"][0]["package"]["title"] == title  # JSON is exact


def test_text_diagnostic_messages_escape_authored_text(
    run: Run, coll: Path, package: WorkPackage
) -> None:
    question = "Which\u200b format\u2060? \\x1b[2J" + "q" * 300
    pkg = replace(
        package,
        decisions=(Decision("D1", question, "unresolved", "me", "planning"),),
    )
    put(coll, "p", pkg)
    _, out, _ = run("overview", coll)
    assert "\u200b" not in out
    assert "\u2060" not in out
    assert "\\u200b" in out
    assert "\\u2060" in out
    assert "\\\\x1b[2J" in out  # a literal backslash is doubled, never ambiguous
    assert "…" in out
    _, doc = overview(run, coll)
    message = doc["entries"][0]["readiness"]["planning"]["blockers"][0]["message"]
    assert question in message


def test_unusual_file_names_in_json_and_text(
    run: Run, coll: Path, package: WorkPackage
) -> None:
    raw_names = [
        b"caf\xc3\xa9",
        b"bad\xff\xfe",
        b"line\nbreak",
        b"esc\x1b[31m",
        b"\xe2\x80\xae",
    ]
    for i, raw in enumerate(raw_names):
        directory = Path(os.fsdecode(os.fsencode(coll) + b"/" + raw))
        directory.mkdir()
        (directory / "work-package.json").write_bytes(
            dumps(variant(package, f"odd-{i}"))
        )
    code, out, err = run("overview", coll, "--json")
    assert (code, err) == (0, "")
    out.encode("ascii")
    doc = json.loads(out)
    paths = [e["path"] for e in doc["entries"]]
    assert paths == [
        os.fsdecode(raw) + "/work-package.json" for raw in sorted(raw_names, key=bytes)
    ]
    assert any("\udcff" in p for p in paths)  # surrogate escape survives, no crash
    code, text, err = run("overview", coll)
    assert (code, err) == (0, "")
    assert "\x1b" not in text
    assert "\\udcff" in text
    assert "line\\x0abreak" in text
    assert all(c.isprintable() or c in " \n" for c in text)


def test_output_never_contains_host_paths_or_context_content(
    run: Run, coll: Path, tmp_path: Path, package: WorkPackage, repo: Path
) -> None:
    put(coll, "p", package)
    broken = coll / "broken"
    broken.mkdir()
    (broken / "work-package.json").write_text("{")
    for flags in (["--json"], []):
        _, out, err = run("overview", coll, "--root", f"repo={repo}", *flags)
        assert str(tmp_path) not in out + err
        assert SPEC_TEXT not in out
        assert NOTES_TEXT not in out
        assert "checkout-a" not in out


# --- 12, 13: bounds -----------------------------------------------------------


def test_one_hundred_candidates_are_accepted_and_a_hundred_and_one_refused(
    run: Run, coll: Path, package: WorkPackage
) -> None:
    for i in range(100):
        put(coll, f"p{i:03d}", variant(package, f"wp-{i}", context=(), roots=()))
    _, doc = overview(run, coll, expect=0)
    assert doc["counts"]["candidates"] == 100
    assert len(doc["entries"]) == 100
    put(coll, "p100", variant(package, "wp-100", context=(), roots=()))
    _, doc = overview(run, coll, expect=1)
    assert doc == refusal_envelope(
        "overview.package_limit",
        "collection",
        "the collection has more than 100 candidate package locations",
    )


def test_a_thousand_entries_are_accepted_and_a_thousand_and_one_refused(
    run: Run, coll: Path
) -> None:
    for i in range(1000):
        (coll / f"f{i:04d}").write_text("")
    _, doc = overview(run, coll, expect=0)
    assert doc["counts"] == {
        "collection_entries": 1000,
        "ignored": 1000,
        "candidates": 0,
        "valid": 0,
        "invalid": 0,
        "excluded": 0,
        "unassessed": 0,
    }
    (coll / ".hidden-one-more").write_text("")
    _, doc = overview(run, coll, expect=1)
    assert doc == refusal_envelope(
        "overview.entry_limit",
        "collection",
        "the collection has more than 1000 immediate entries",
    )


def test_entry_limit_stops_listing_instead_of_reading_everything(
    run: Run, coll: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(1001):
        (coll / f"f{i:04d}").write_text("")
    seen = 0
    real_scandir = os.scandir

    def counting(fd: Any) -> Any:
        nonlocal seen
        with real_scandir(fd) as it:
            items = list(it)

        class Counting:
            def __enter__(self) -> Iterator[Any]:
                return self.gen()

            def __exit__(self, *exc: object) -> None:
                return None

            def gen(self) -> Iterator[Any]:
                nonlocal seen
                for item in items:
                    seen += 1
                    yield item

        return Counting()

    monkeypatch.setattr(os, "scandir", counting)
    overview(run, coll, expect=1)
    assert seen == 1001


def synthetic(message: str) -> Overview:
    return Overview(
        True,
        True,
        "complete",
        None,
        (Diagnostic("x.y", "warning", message, "collection"),),
        (),
    )


def serialized_size(message: str) -> int:
    return len(overview_render.dumps_json(synthetic(message)))


def message_for_size(target: int, filler: str = "a") -> str:
    base = serialized_size("")
    per_char = len(overview_render.dumps_json(synthetic(filler))) - base
    assert (target - base) % per_char == 0
    return filler * ((target - base) // per_char)


@pytest.mark.parametrize("filler", ["a", "\x01", "é", "\U0001f600"])
def test_json_at_the_output_limit_is_delivered_and_one_byte_more_is_not(
    filler: str,
) -> None:
    # Escaping counts: one control character is six bytes, one astral two escapes.
    step = len(overview_render.dumps_json(synthetic(filler))) - serialized_size("")
    target = MAX_OUTPUT_BYTES - ((MAX_OUTPUT_BYTES - serialized_size("")) % step)
    message = message_for_size(target, filler)
    at_limit = overview_render.render_json(synthetic(message))
    assert len(at_limit.data) == target <= MAX_OUTPUT_BYTES
    assert at_limit.data.endswith(b"}\n")
    assert at_limit.overview.results_status == "complete"
    # One more character pushes it past the bound, whatever it costs when escaped.
    assert target + step > MAX_OUTPUT_BYTES
    over = overview_render.render_json(synthetic(message + filler))
    assert over.overview.results_status == "omitted"
    assert over.overview.discovery_complete
    assert over.overview.assessment_complete


def test_json_size_boundary_is_inclusive_of_the_final_line_feed() -> None:
    message = message_for_size(MAX_OUTPUT_BYTES)
    data = overview_render.dumps_json(synthetic(message))
    assert len(data) == MAX_OUTPUT_BYTES
    assert data.endswith(b"\n")
    assert overview_render.render_json(synthetic(message)).overview.results_status == (
        "complete"
    )
    over = overview_render.render_json(synthetic(message + "a"))
    assert over.overview.results_status == "omitted"
    assert len(over.data) < 1000


def test_over_limit_output_is_a_bounded_refusal_that_keeps_the_flags(
    run: Run, coll: Path, package: WorkPackage, repo: Path
) -> None:
    context = tuple(
        ContextRef(f"c{i}", "repo", f"docs/none-{i}-{'x' * 200}.md", sha(str(i)), False)
        for i in range(64)
    )
    for i in range(40):
        put(coll, f"p{i:02d}", variant(package, f"wp-{i}", context=context))
    code, out, err = run("overview", coll, "--root", f"repo={repo}", "--json")
    assert (code, err) == (1, "")
    assert len(out) < 2000
    assert json.loads(out) == {
        "schema": "omnitient/overview",
        "schema_version": 1,
        "discovery_complete": True,
        "assessment_complete": True,
        "results_status": "omitted",
        "counts": None,
        "diagnostics": [
            {
                "code": "overview.output_limit",
                "severity": "error",
                "message": "the result is larger than 1048576 bytes, "
                "so no entries are shown",
                "location": "output",
            }
        ],
        "entries": [],
    }
    # Text output is bounded the same way and reports on stderr.
    code, out, err = run("overview", coll, "--root", f"repo={repo}")
    assert code == 1
    assert out == ""
    assert "overview.output_limit" in err


# --- 14: failures during enumeration ----------------------------------------


def test_failure_partway_through_the_listing_is_a_whole_refusal(
    run: Run, coll: Path, package: WorkPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(5):
        put(coll, f"p{i}", variant(package, f"wp-{i}"))
    real_scandir = os.scandir

    def failing(fd: Any) -> Any:
        with real_scandir(fd) as it:
            items = list(it)[:3]

        class Failing:
            def __enter__(self) -> Iterator[Any]:
                return self.gen()

            def __exit__(self, *exc: object) -> None:
                return None

            def gen(self) -> Iterator[Any]:
                yield from items
                raise OSError(errno.EIO, "Input/output error")

        return Failing()

    monkeypatch.setattr(os, "scandir", failing)
    _, doc = overview(run, coll, expect=1)
    assert doc == refusal_envelope(
        "overview.collection_unreadable",
        "collection",
        "listing the collection failed before it finished",
    )
    code, out, err = run("overview", coll)
    assert (code, out) == (1, "")
    assert "overview.collection_unreadable" in err
    assert "Input/output" not in err


def test_broken_stdout_reports_a_nonzero_exit_without_retrying(
    coll: Path, package: WorkPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(coll, "p", package)
    writes: list[bytes] = []

    class Broken:
        def write(self, data: bytes) -> int:
            writes.append(data)
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")

        def flush(self) -> None:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    class Stdout:
        buffer = Broken()

        def fileno(self) -> int:
            raise OSError("no descriptor")

    monkeypatch.setattr(sys, "stdout", Stdout())
    assert main(["overview", str(coll), "--json"]) == 1
    assert len(writes) == 1  # one attempt, not retried or split


def test_programming_errors_are_not_reported_as_partial_results(
    coll: Path, package: WorkPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(coll, "p", package)

    def boom(*_args: object) -> Any:
        raise AssertionError("bug")

    monkeypatch.setattr("omnitient.overview.parse_package", boom)
    with pytest.raises(AssertionError, match="bug"):
        main(["overview", str(coll), "--json"])


# --- 15: no mutation, network or subprocess ------------------------------------


def tree_state(root: Path) -> dict[str, tuple[Any, ...]]:
    state: dict[str, tuple[Any, ...]] = {}
    for path in sorted([root, *root.rglob("*")]):
        info = path.lstat()
        content = (
            path.read_bytes()
            if stat.S_ISREG(info.st_mode)
            else path.readlink() if stat.S_ISLNK(info.st_mode) else None
        )
        state[str(path.relative_to(root.parent))] = (
            stat.S_IFMT(info.st_mode),
            stat.S_IMODE(info.st_mode),
            info.st_size,
            info.st_mtime_ns,
            content,
        )
    return state


def rich_collection(tmp_path: Path, package: WorkPackage) -> Path:
    base = tmp_path / "tree"
    coll = base / "work"
    coll.mkdir(parents=True)
    put(coll, "ok", package)
    put(coll, "dup", package)
    (coll / "bad").mkdir()
    (coll / "bad" / "work-package.json").write_text("{")
    (coll / "alias").symlink_to(coll / "ok", target_is_directory=True)
    (coll / "fifo").mkdir()
    os.mkfifo(coll / "fifo" / "work-package.json")
    (coll / "note.txt").write_text("hi")
    return base


def test_overview_makes_no_writes_spawns_nothing_and_opens_no_sockets(
    run: Run,
    tmp_path: Path,
    package: WorkPackage,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins
    import shutil
    import socket
    import subprocess

    base = rich_collection(tmp_path, package)
    before = tree_state(base)
    repo_before = tree_state(repo)
    writing = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    real_open = os.open

    def guarded_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert not flags & writing, f"opened {path!r} for writing"
        return real_open(path, flags, *args, **kwargs)

    def forbid(name: str) -> Callable[..., Any]:
        def refuse(*_a: object, **_k: object) -> Any:
            raise AssertionError(f"overview must not call {name}")

        return refuse

    monkeypatch.setattr(os, "open", guarded_open)
    real_builtin_open = builtins.open

    def guarded_builtin(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        assert not set(mode) & set("wax+"), f"builtin open({file!r}, {mode!r})"
        return real_builtin_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_builtin)
    for name in (
        "mkdir",
        "makedirs",
        "rename",
        "replace",
        "unlink",
        "remove",
        "rmdir",
        "chmod",
        "chown",
        "utime",
        "symlink",
        "link",
        "truncate",
        "mkfifo",
        "system",
        "popen",
        "fork",
        "execv",
        "execve",
        "posix_spawn",
        "spawnv",
    ):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbid(f"os.{name}"))
    monkeypatch.setattr(subprocess, "Popen", forbid("subprocess.Popen"))
    monkeypatch.setattr(shutil, "rmtree", forbid("shutil.rmtree"))
    monkeypatch.setattr(socket, "socket", forbid("socket.socket"))
    for flags in (["--json"], []):
        code, out, _ = run("overview", base / "work", "--root", f"repo={repo}", *flags)
        assert code == 1
        assert out
    monkeypatch.undo()

    assert tree_state(base) == before
    assert tree_state(repo) == repo_before


def test_the_overview_modules_import_nothing_that_could_spawn_or_connect() -> None:
    import ast

    source = Path(__file__).resolve().parent.parent / "src" / "omnitient"
    forbidden = {
        "subprocess",
        "socket",
        "urllib",
        "http",
        "ssl",
        "shutil",
        "tempfile",
        "importlib",
        "runpy",
        "multiprocessing",
        "asyncio",
        "ctypes",
        "pickle",
    }
    for name in ("collection.py", "overview.py", "overview_render.py"):
        tree = ast.parse((source / name).read_text(encoding="utf-8"))
        imported = {
            part
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for part in (
                [a.name.split(".")[0] for a in node.names]
                if isinstance(node, ast.Import)
                else [(node.module or "").split(".")[0]]
            )
        }
        assert not imported & forbidden, (name, imported & forbidden)
        calls = {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert not calls & {
            "system",
            "popen",
            "exec",
            "eval",
            "write_text",
            "write_bytes",
        }


def test_a_child_process_leaves_the_tree_byte_identical_with_bytecode_disabled(
    tmp_path: Path, package: WorkPackage, repo: Path
) -> None:
    import subprocess

    base = rich_collection(tmp_path, package)
    (base / "work" / "ok").chmod(0o555)
    before = tree_state(base)
    src = Path(__file__).resolve().parent.parent / "src"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(src),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "omnitient",
                "overview",
                str(base / "work"),
                "--root",
                f"repo={repo}",
                "--json",
            ],
            capture_output=True,
            env=env,
            timeout=60,
            check=False,
        )
    finally:
        (base / "work" / "ok").chmod(0o755)
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["schema"] == "omnitient/overview"
    after = tree_state(base)
    ok_key = "tree/work/ok"
    assert after.pop(ok_key)[1] == 0o755
    assert before.pop(ok_key)[1] == 0o555
    assert after == before
    assert not list(src.rglob("*.pyc")) or all(
        "__pycache__" in str(p) for p in src.rglob("*.pyc")
    )


def test_manifest_code_is_never_imported_or_executed(
    run: Run, coll: Path, package: WorkPackage, tmp_path: Path
) -> None:
    marker = tmp_path / "executed"
    pkg_dir = coll / "p"
    put(coll, "p", package)
    (pkg_dir / "evil.py").write_text(f"open({str(marker)!r}, 'w').write('x')")
    (pkg_dir / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').write('x')")
    raw = json.loads((pkg_dir / "work-package.json").read_text())
    raw["title"] = "__import__('evil')"
    raw["objective"] = "${open('x','w')}"
    (pkg_dir / "work-package.json").write_text(json.dumps(raw))
    overview(run, coll, expect=0)
    assert not marker.exists()


# --- 16: command availability ---------------------------------------------------


def test_the_overview_command_is_listed_and_o1_commands_are_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    help_text = capsys.readouterr().out
    for command in (
        "new",
        "validate",
        "show",
        "render",
        "approve",
        "evidence",
        "context",
        "schema",
        "overview",
    ):
        assert command in help_text


def test_text_report_is_deterministic_and_states_what_it_does_not_verify(
    run: Run, coll: Path, approved: WorkPackage, repo: Path
) -> None:
    put(coll, "p", replace(approved, stage="done"))
    first = run("overview", coll, "--root", f"repo={repo}")
    second = run("overview", coll, "--root", f"repo={repo}")
    assert first == second
    code, out, err = first
    assert (code, err) == (0, "")
    assert "declared stage: done" in out
    assert "not verified" in out
    assert "implementation: READY" in out
    assert "advice: handoff_ready" in out
    assert "execute" not in out.lower()


def test_creating_packages_with_ops_scaffold_is_reported_as_blocked_draft(
    run: Run, coll: Path
) -> None:
    pkg = ops.new_package("demo", "init", "scaffold", "Scaffold")
    put(coll, "s", pkg)
    _, doc = overview(run, coll, expect=0)
    entry = doc["entries"][0]
    assert entry["package"]["declared_stage"] == "draft"
    assert entry["readiness"]["planning"]["ready"] is False
