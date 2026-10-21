"""Context resolution: digests, missing files, host mapping and path safety."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from conftest import sha
from omnitient import context as ctx
from omnitient.context import (
    HostMapping,
    build_host_mapping,
    digest_file,
    resolve_context,
)
from omnitient.errors import OmnitientError
from omnitient.model import ContextRef, WorkPackage


def one(pkg: WorkPackage, mapping: HostMapping, index: int = 0) -> ctx.ResolvedContext:
    return resolve_context(pkg, mapping)[index]


def with_ref(pkg: WorkPackage, **changes: object) -> WorkPackage:
    return replace(pkg, context=(replace(pkg.context[0], **changes),))  # type: ignore[arg-type]


def test_matching_files_resolve_and_embed(
    package: WorkPackage, mapping: HostMapping
) -> None:
    first, second = resolve_context(package, mapping)
    assert first.status == "ok"
    assert first.text is not None
    assert first.text.startswith("# Spec")
    assert second.status == "ok"


def test_digest_drift_is_diagnosed_and_content_withheld(
    package: WorkPackage, mapping: HostMapping, repo: Path
) -> None:
    (repo / "docs" / "spec.md").write_text("changed\n", encoding="utf-8")
    result = one(package, mapping)
    assert result.status == "digest_mismatch"
    assert result.actual_sha256 == sha("changed\n")
    assert package.context[0].sha256 in result.detail
    assert result.text is None


def test_missing_file(package: WorkPackage, mapping: HostMapping, repo: Path) -> None:
    (repo / "docs" / "spec.md").unlink()
    assert one(package, mapping).status == "missing"


def test_directory_is_not_a_regular_file(
    package: WorkPackage, mapping: HostMapping, repo: Path
) -> None:
    (repo / "docs" / "dir").mkdir()
    assert one(with_ref(package, path="docs/dir"), mapping).status == "missing"


def test_fifo_is_not_read(
    package: WorkPackage, mapping: HostMapping, repo: Path
) -> None:
    os.mkfifo(repo / "docs" / "pipe")
    assert one(with_ref(package, path="docs/pipe"), mapping).status == "missing"


def test_unmapped_root(package: WorkPackage) -> None:
    assert one(package, HostMapping()).status == "root_unmapped"


def test_symlink_escape_is_unsafe(
    package: WorkPackage, mapping: HostMapping, repo: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_text("outside", encoding="utf-8")
    (repo / "docs" / "link.md").symlink_to(secret)
    result = one(with_ref(package, path="docs/link.md", sha256=sha("outside")), mapping)
    assert result.status == "unsafe_path"
    assert result.text is None


def test_symlinked_directory_escape_is_unsafe(
    package: WorkPackage, mapping: HostMapping, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "a.md").write_text("x", encoding="utf-8")
    (repo / "docs" / "ext").symlink_to(outside, target_is_directory=True)
    assert one(with_ref(package, path="docs/ext/a.md"), mapping).status == "unsafe_path"


def test_symlink_inside_root_is_allowed(
    package: WorkPackage, mapping: HostMapping, repo: Path
) -> None:
    (repo / "docs" / "alias.md").symlink_to(repo / "docs" / "spec.md")
    assert one(with_ref(package, path="docs/alias.md"), mapping).status == "ok"


def test_symlink_to_forbidden_file_is_refused(
    package: WorkPackage, mapping: HostMapping, repo: Path
) -> None:
    (repo / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (repo / "docs" / "innocent.md").symlink_to(repo / ".env")
    result = one(
        with_ref(package, path="docs/innocent.md", sha256=sha("TOKEN=secret")), mapping
    )
    assert result.status == "unsafe_path"
    assert result.text is None


def test_embed_file_over_limit(
    package: WorkPackage,
    mapping: HostMapping,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ctx, "MAX_EMBED_FILE_BYTES", 10)
    assert one(package, mapping).status == "too_large"


def test_total_embed_budget(
    package: WorkPackage, mapping: HostMapping, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_size = len(b"# Spec\n\nThe spec says: build the thing.\n")
    monkeypatch.setattr(ctx, "MAX_EMBED_TOTAL_BYTES", first_size + 3)
    first, second = resolve_context(package, mapping)
    assert first.status == "ok"
    assert second.status == "too_large"
    assert "budget" in second.detail


def test_reference_mode_hashes_without_embedding(
    package: WorkPackage, mapping: HostMapping, repo: Path
) -> None:
    blob = bytes(range(256))
    (repo / "docs" / "data.bin").write_bytes(blob)
    result = one(
        with_ref(package, path="docs/data.bin", mode="reference", sha256=sha(blob)),
        mapping,
    )
    assert result.status == "ok"
    assert result.text is None
    assert result.size == 256


@pytest.mark.parametrize("blob", [b"nul\x00byte", b"\xff\xfe bad utf8"])
def test_embedding_binary_is_refused(
    package: WorkPackage, mapping: HostMapping, repo: Path, blob: bytes
) -> None:
    (repo / "docs" / "blob").write_bytes(blob)
    result = one(with_ref(package, path="docs/blob", sha256=sha(blob)), mapping)
    assert result.status == "not_text"


def test_digest_file(repo: Path) -> None:
    digest, size = digest_file(repo.resolve(), "docs/notes.md")
    assert digest == sha("Optional notes.\n")
    assert size == len("Optional notes.\n")
    with pytest.raises(OmnitientError):
        digest_file(repo.resolve(), "docs/absent.md")
    with pytest.raises(OmnitientError):
        digest_file(repo.resolve(), "../checkout-a/docs/notes.md")


def test_only_listed_files_are_read(
    package: WorkPackage,
    mapping: HostMapping,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo / "docs" / "unlisted.md").write_text("never read", encoding="utf-8")
    opened: list[str] = []
    real_open = os.open

    def spy(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(Path(os.fsdecode(path)).name)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    resolve_context(package, mapping)
    assert sorted(opened) == ["notes.md", "spec.md"]


class TestHostMapping:
    def test_pairs_and_unknown_names(
        self, repo: Path, package: WorkPackage, tmp_path: Path
    ) -> None:
        extra = tmp_path / "extra"
        extra.mkdir()
        mapping = build_host_mapping([f"repo={repo}", f"extra={extra}"])
        assert mapping.unknown_for(package) == ["extra"]

    @pytest.mark.parametrize("pair", ["noequals", "=dir", "name="])
    def test_malformed_pair(self, pair: str) -> None:
        with pytest.raises(OmnitientError):
            build_host_mapping([pair])

    def test_relative_missing_and_file_directories(self, tmp_path: Path) -> None:
        (tmp_path / "f").write_text("x", encoding="utf-8")
        for bad in ("relative/dir", str(tmp_path / "nope"), str(tmp_path / "f")):
            with pytest.raises(OmnitientError):
                build_host_mapping([f"r={bad}"])

    def test_duplicate_name_is_ambiguous(self, repo: Path, tmp_path: Path) -> None:
        roots_file = tmp_path / "roots.json"
        roots_file.write_text(
            json.dumps({"roots": {"repo": str(repo)}}), encoding="utf-8"
        )
        with pytest.raises(OmnitientError) as info:
            build_host_mapping([f"repo={repo}"], roots_file)
        assert info.value.diagnostics[0].code == "roots.duplicate"

    def test_roots_file(self, repo: Path, tmp_path: Path) -> None:
        roots_file = tmp_path / "roots.json"
        roots_file.write_text(
            json.dumps({"roots": {"repo": str(repo)}}), encoding="utf-8"
        )
        assert build_host_mapping([], roots_file).roots["repo"] == repo.resolve()

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            "[]",
            '{"roots": []}',
            '{"roots": {"a": 1}}',
            '{"roots": {}, "x": 1}',
        ],
    )
    def test_bad_roots_file(self, tmp_path: Path, content: str) -> None:
        roots_file = tmp_path / "roots.json"
        roots_file.write_text(content, encoding="utf-8")
        with pytest.raises(OmnitientError):
            build_host_mapping([], roots_file)

    def test_duplicate_keys_in_a_roots_file_are_refused(
        self, repo: Path, tmp_path: Path
    ) -> None:
        roots_file = tmp_path / "roots.json"
        roots_file.write_text(
            f'{{"roots": {{"repo": "/elsewhere", "repo": "{repo}"}}}}', encoding="utf-8"
        )
        with pytest.raises(OmnitientError) as info:
            build_host_mapping([], roots_file)
        diag = info.value.diagnostics[0]
        assert diag.code == "roots.invalid"
        assert "duplicate key" in diag.message

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("[" * 60_000, id="array"),
            pytest.param('{"roots": {"a": ' + "[" * 60_000, id="array-in-roots"),
        ],
    )
    def test_deeply_nested_roots_file_is_a_diagnostic_not_a_crash(
        self, tmp_path: Path, content: str
    ) -> None:
        roots_file = tmp_path / "roots.json"
        roots_file.write_text(content, encoding="utf-8")
        with pytest.raises(OmnitientError) as info:
            build_host_mapping([], roots_file)
        assert info.value.diagnostics[0].code == "roots.invalid"

    def test_roots_file_that_is_not_a_regular_file_is_refused_without_blocking(
        self, tmp_path: Path, write_package: Callable[..., Path], package: WorkPackage
    ) -> None:
        # A FIFO with no writer blocks an ordinary open() forever, so the CLI runs
        # in a child process that is killed (and reaped) by the timeout.
        fifo = tmp_path / "roots.fifo"
        os.mkfifo(fifo)
        try:
            done = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "omnitient",
                    "validate",
                    str(write_package(package)),
                    "--roots-file",
                    str(fifo),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                cwd=tmp_path,
            )
        except subprocess.TimeoutExpired:
            pytest.fail("opening a FIFO as a roots file blocked")
        assert done.returncode == 1
        assert "roots.invalid" in done.stderr
        assert "not a regular file" in done.stderr
        assert "Traceback" not in done.stderr

    def test_mapping_never_changes_context_identity(self, package: WorkPackage) -> None:
        assert all(isinstance(c, ContextRef) for c in package.context)


def test_symlink_loop_is_reported_not_raised(
    package: WorkPackage, mapping: HostMapping, repo: Path
) -> None:
    (repo / "docs" / "loop.md").symlink_to(repo / "docs" / "loop.md")
    found = one(with_ref(package, path="docs/loop.md"), mapping)
    assert found.status == "unreadable"
    assert str(repo) not in found.detail  # handoffs carry no host paths
    assert "loop" in found.detail or "symbolic links" in found.detail


def test_digest_file_applies_the_mode_rules(repo: Path) -> None:
    (repo / "docs" / "blob.bin").write_bytes(b"\xff\x00binary")
    with pytest.raises(OmnitientError) as info:
        digest_file(repo.resolve(), "docs/blob.bin", "embed")
    assert info.value.diagnostics[0].code == "context.not_text"
    digest, size = digest_file(repo.resolve(), "docs/blob.bin", "reference")
    assert (digest, size) == (sha(b"\xff\x00binary"), 8)
