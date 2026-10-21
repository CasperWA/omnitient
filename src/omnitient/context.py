"""Host mapping and safe, bounded resolution of explicitly listed context files."""

from __future__ import annotations

import hashlib
import os
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
from omnitient.model import ContextMode, ContextRef, WorkPackage
from omnitient.safeio import (
    NotRegularFileError,
    loads_strict,
    open_regular,
    read_regular,
)

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
_ROOTS_FILE_MAX = 65_536


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
        if not rules.SLUG.fullmatch(name):
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
        raw = read_regular(path, _ROOTS_FILE_MAX)
    except NotRegularFileError as exc:
        raise refusal(
            "roots.invalid", f"roots file {path} is not a regular file"
        ) from exc
    except OSError as exc:
        raise refusal(
            "roots.invalid", f"cannot read roots file {path}: {exc.strerror}"
        ) from exc
    if len(raw) > _ROOTS_FILE_MAX:
        raise refusal(
            "roots.invalid", f"roots file {path} is larger than {_ROOTS_FILE_MAX} bytes"
        )
    try:
        data = loads_strict(raw.decode("utf-8"))
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
        result = _resolve_one(ref, mapping, MAX_EMBED_TOTAL_BYTES - embedded_total)
        if result.text is not None and result.size is not None:
            embedded_total += result.size
        results.append(result)
    return results


@dataclass(frozen=True)
class _Inspection:
    """What reading one file yielded, before comparing with any expected digest."""

    status: ContextStatus
    detail: str = ""
    sha256: str | None = None
    size: int | None = None
    text: str | None = None


def _inspect(root: Path, relative: str, mode: ContextMode, budget: int) -> _Inspection:
    """Locate, bound-check and read one file under ``root``; embed mode decodes it."""
    problem = rules.relative_path_problem(relative) or rules.forbidden_reason(relative)
    if problem:
        return _Inspection("unsafe_path", f"path {problem}")
    try:
        target = (root / relative).resolve(strict=True)
    except FileNotFoundError:
        return _Inspection("missing", "file does not exist")
    except OSError as exc:
        return _Inspection("unreadable", f"cannot resolve path: {exc.strerror}")
    except RuntimeError:
        return _Inspection("unreadable", "cannot resolve path: symlink loop")
    if not target.is_relative_to(root):
        return _Inspection("unsafe_path", "resolves outside its root (symlink escape)")
    if forbidden := rules.forbidden_reason(target.relative_to(root).as_posix()):
        return _Inspection("unsafe_path", f"resolves to a file that {forbidden}")

    embed = mode == "embed"
    limit = min(MAX_EMBED_FILE_BYTES, budget) if embed else MAX_REFERENCE_FILE_BYTES
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    size = 0
    try:
        with open_regular(target) as handle:
            declared = os.fstat(handle.fileno()).st_size
            if declared > limit:
                what = (
                    f"the {mode} limit of {MAX_EMBED_FILE_BYTES} or the remaining "
                    f"total embed budget of {budget}"
                    if embed
                    else f"the {mode} limit of {limit}"
                )
                return _Inspection("too_large", f"{declared} bytes exceeds {what}")
            while chunk := handle.read(_CHUNK):
                size += len(chunk)
                if size > limit:
                    return _Inspection(
                        "too_large", f"file grew beyond {limit} bytes while reading"
                    )
                digest.update(chunk)
                if embed:
                    chunks.append(chunk)
    except NotRegularFileError:
        return _Inspection("missing", "not a regular file")
    except FileNotFoundError:
        return _Inspection("missing", "file does not exist")
    except OSError as exc:
        return _Inspection("unreadable", f"cannot read file: {exc.strerror}")

    actual = digest.hexdigest()
    if not embed:
        return _Inspection("ok", "", actual, size)
    data = b"".join(chunks)
    if b"\x00" in data:
        return _Inspection("not_text", "contains NUL bytes", actual, size)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _Inspection("not_text", "is not valid UTF-8", actual, size)
    return _Inspection("ok", "", actual, size, text)


def _resolve_one(ref: ContextRef, mapping: HostMapping, budget: int) -> ResolvedContext:
    root = mapping.roots.get(ref.root)
    if root is None:
        return ResolvedContext(
            ref, "root_unmapped", f"no host directory supplied for root '{ref.root}'"
        )
    found = _inspect(root, ref.path, ref.mode, budget)
    if found.status == "ok" and found.sha256 != ref.sha256:
        # The content is withheld: it is not what the package approved.
        return ResolvedContext(
            ref,
            "digest_mismatch",
            f"expected {ref.sha256}, found {found.sha256}",
            found.sha256,
            found.size,
        )
    return ResolvedContext(
        ref, found.status, found.detail, found.sha256, found.size, found.text
    )


def digest_file(
    root: Path, relative_path: str, mode: ContextMode = "embed"
) -> tuple[str, int]:
    """Hash one file under ``root`` under the same rules as resolution.

    Used by ``context add`` and ``context pin``, so a reference is never created
    or re-pinned for a file that could not be used in its mode.
    Raises on any problem.
    """
    found = _inspect(root, relative_path, mode, MAX_EMBED_TOTAL_BYTES)
    if found.status != "ok" or found.sha256 is None or found.size is None:
        raise refusal(f"context.{found.status}", f"{relative_path}: {found.detail}")
    return found.sha256, found.size
