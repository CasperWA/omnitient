"""Host mapping and safe, bounded resolution of explicitly listed context files."""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from omnitient import rules
from omnitient.errors import refusal
from omnitient.limits import (
    MAX_EMBED_FILE_BYTES,
    MAX_EMBED_TOTAL_BYTES,
    MAX_REFERENCE_FILE_BYTES,
)
from omnitient.model import ContextRef, WorkPackage

ContextStatus = Literal[
    "ok",
    "missing",
    "digest_mismatch",
    "root_unmapped",
    "unsafe_path",
    "too_large",
    "not_text",
    "unreadable",
]

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class HostMapping:
    """Logical root name to resolved absolute directory on this host."""

    roots: Mapping[str, Path] = field(default_factory=dict)

    def unknown_for(self, pkg: WorkPackage) -> list[str]:
        declared = {r.name for r in pkg.roots}
        return sorted(set(self.roots) - declared)


@dataclass(frozen=True)
class ResolvedContext:
    ref: ContextRef
    status: ContextStatus
    detail: str = ""
    actual_sha256: str | None = None
    size: int | None = None
    text: str | None = None


def build_host_mapping(
    pairs: Iterable[str] = (), roots_file: Path | None = None
) -> HostMapping:
    """Build a mapping from ``NAME=DIR`` pairs and/or a roots file.

    Raises on malformed input, relative or missing directories, and on a name
    that is given more than once (an ambiguous mapping).
    """
    entries: list[tuple[str, str]] = []
    for pair in pairs:
        name, sep, directory = pair.partition("=")
        if not sep or not name or not directory:
            raise refusal("roots.invalid", f"expected NAME=DIRECTORY, got {pair!r}")
        entries.append((name, directory))
    if roots_file is not None:
        entries.extend(_read_roots_file(roots_file))

    resolved: dict[str, Path] = {}
    for name, directory in entries:
        if not rules.SLUG.match(name):
            raise refusal("roots.invalid", f"root name {name!r} is not a valid slug")
        if name in resolved:
            raise refusal("roots.duplicate", f"root {name!r} is mapped more than once")
        path = Path(directory)
        if not path.is_absolute():
            raise refusal(
                "roots.invalid", f"root {name!r}: {directory!r} is not absolute"
            )
        try:
            real = path.resolve(strict=True)
        except OSError as exc:
            raise refusal(
                "roots.invalid",
                f"root {name!r}: cannot resolve {directory!r}: {exc.strerror}",
            ) from exc
        if not real.is_dir():
            raise refusal(
                "roots.invalid", f"root {name!r}: {directory!r} is not a directory"
            )
        resolved[name] = real
    return HostMapping(resolved)


def _read_roots_file(path: Path) -> list[tuple[str, str]]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(65_537)
    except OSError as exc:
        raise refusal(
            "roots.invalid", f"cannot read roots file {path}: {exc.strerror}"
        ) from exc
    if len(raw) > 65_536:
        raise refusal("roots.invalid", f"roots file {path} is larger than 65536 bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise refusal(
            "roots.invalid", f"roots file {path} is not valid JSON: {exc}"
        ) from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"roots"}
        or not isinstance(data["roots"], dict)
        or not all(isinstance(v, str) for v in data["roots"].values())
    ):
        raise refusal(
            "roots.invalid",
            f'roots file {path} must be {{"roots": {{"NAME": "DIRECTORY"}}}}',
        )
    return list(data["roots"].items())


def resolve_context(pkg: WorkPackage, mapping: HostMapping) -> list[ResolvedContext]:
    """Resolve every reference in package order; never raises for file problems."""
    results: list[ResolvedContext] = []
    embedded_total = 0
    for ref in pkg.context:
        budget = MAX_EMBED_TOTAL_BYTES - embedded_total
        result = _resolve_one(ref, mapping, budget)
        if (
            result.status == "ok"
            and result.text is not None
            and result.size is not None
        ):
            embedded_total += result.size
        results.append(result)
    return results


def _resolve_one(ref: ContextRef, mapping: HostMapping, budget: int) -> ResolvedContext:
    def fail(status: ContextStatus, detail: str) -> ResolvedContext:
        return ResolvedContext(ref, status, detail)

    root = mapping.roots.get(ref.root)
    if root is None:
        return fail(
            "root_unmapped", f"no host directory supplied for root '{ref.root}'"
        )
    problem = rules.relative_path_problem(ref.path) or rules.forbidden_reason(ref.path)
    if problem:
        return fail("unsafe_path", f"path {problem}")

    try:
        target = (root / ref.path).resolve(strict=True)
    except FileNotFoundError:
        return fail("missing", "file does not exist")
    except (OSError, RuntimeError) as exc:
        return fail("unreadable", f"cannot resolve path: {exc}")
    if not target.is_relative_to(root):
        return fail("unsafe_path", "resolves outside its root (symlink escape)")
    relative = target.relative_to(root).as_posix()
    if forbidden := rules.forbidden_reason(relative):
        return fail("unsafe_path", f"resolves to a file that {forbidden}")

    try:
        info = target.stat()
    except OSError as exc:
        return fail("unreadable", f"cannot stat file: {exc.strerror}")
    if not stat.S_ISREG(info.st_mode):
        return fail("missing", "not a regular file")

    embed = ref.mode == "embed"
    limit = MAX_EMBED_FILE_BYTES if embed else MAX_REFERENCE_FILE_BYTES
    if info.st_size > limit:
        return fail(
            "too_large", f"{info.st_size} bytes exceeds the {ref.mode} limit of {limit}"
        )
    if embed and info.st_size > budget:
        return fail(
            "too_large",
            f"{info.st_size} bytes exceeds the remaining embed budget of {budget} (total {MAX_EMBED_TOTAL_BYTES})",
        )

    digest = hashlib.sha256()
    chunks: list[bytes] = []
    size = 0
    try:
        with target.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                size += len(chunk)
                if size > limit:
                    return fail(
                        "too_large",
                        f"file grew beyond the {ref.mode} limit of {limit} while reading",
                    )
                digest.update(chunk)
                if embed:
                    chunks.append(chunk)
    except OSError as exc:
        return fail("unreadable", f"cannot read file: {exc.strerror}")

    actual = digest.hexdigest()
    if actual != ref.sha256:
        return ResolvedContext(
            ref,
            "digest_mismatch",
            f"expected {ref.sha256}, found {actual}",
            actual,
            size,
        )
    if not embed:
        return ResolvedContext(ref, "ok", "", actual, size)
    data = b"".join(chunks)
    if b"\x00" in data:
        return ResolvedContext(ref, "not_text", "contains NUL bytes", actual, size)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ResolvedContext(ref, "not_text", "is not valid UTF-8", actual, size)
    return ResolvedContext(ref, "ok", "", actual, size, text)


def digest_file(root: Path, relative_path: str) -> tuple[str, int]:
    """Hash one file under ``root`` with the same safety rules as resolution.

    Used by ``context add`` and ``context pin``; raises on any problem.
    """
    stub = ContextRef("probe", "probe", relative_path, "0" * 64, mode="reference")
    result = _resolve_one(stub, HostMapping({"probe": root}), 0)
    if result.actual_sha256 is None or result.size is None:
        raise refusal(f"context.{result.status}", f"{relative_path}: {result.detail}")
    return result.actual_sha256, result.size
