# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""File persistence: bounded reads, exclusive creation, checked updates and bundles.

Cleanup removes only files this operation created, recognised by device and
inode, never by name alone. It is not a defence against a hostile process of
the same user (contract, section 10).
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from omnitient.errors import OmnitientError, refusal
from omnitient.limits import MAX_OUTPUT_BYTES, MAX_PACKAGE_BYTES
from omnitient.model import WorkPackage
from omnitient.safeio import NotRegularFileError, loads_strict, read_regular
from omnitient.schema import parse_package
from omnitient.serialize import dumps, sha256_hex

PACKAGE_FILENAME = "work-package.json"
MANIFEST_SCHEMA = "omnitient/handoff-manifest"
MANIFEST_SCHEMA_VERSION = 1

_FileId = tuple[int, int]
_TEMP_ATTEMPTS = 4
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _create_exclusive(path: Path, data: bytes) -> _FileId:
    """Create ``path`` (it must not exist), write ``data`` and return its identity.

    A failure after the file exists removes it again: it is ours.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o666)
    info = os.fstat(fd)
    ident = (info.st_dev, info.st_ino)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _discard(path, ident)
        raise
    return ident


def _discard(path: Path, ident: _FileId) -> None:
    """Remove ``path`` only while it is still the file this operation created."""
    try:
        info = os.lstat(path)
        if (info.st_dev, info.st_ino) == ident:
            path.unlink()
    except OSError:
        pass


def _write_temp(path: Path, data: bytes) -> tuple[Path, _FileId]:
    """Write ``data`` to a new, uniquely named temporary file next to ``path``."""
    for _ in range(_TEMP_ATTEMPTS):
        temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
        try:
            return temp, _create_exclusive(temp, data)
        except FileExistsError:
            continue  # someone else's file: leave it alone and pick another name
    raise OSError(errno.EAGAIN, "no unused temporary file name was found")


@dataclass(frozen=True)
class LoadedPackage:
    path: Path
    file_sha256: str
    package: WorkPackage


def package_path(arg: Path) -> Path:
    """A directory means ``DIR/work-package.json``."""
    return arg / PACKAGE_FILENAME if arg.is_dir() else arg


def load_package(arg: Path) -> LoadedPackage:
    """Read and validate a package; raises with all schema diagnostics."""
    path = package_path(arg)
    try:
        raw = read_regular(path, MAX_PACKAGE_BYTES)
    except NotRegularFileError as exc:
        raise refusal("store.unreadable", f"{path} is not a regular file") from exc
    except OSError as exc:
        raise refusal(
            "store.unreadable", f"cannot read {path}: {exc.strerror}"
        ) from exc
    result = parse_package(raw)
    if result.package is None:
        raise OmnitientError(result.diagnostics)
    return LoadedPackage(path, sha256_hex(raw), result.package)


def create_package(path: Path, pkg: WorkPackage) -> None:
    """Create a new package file; fails if the path already exists."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _create_exclusive(path, dumps(pkg))
    except FileExistsError as exc:
        raise refusal(
            "store.exists", f"{path} already exists; refusing to overwrite"
        ) from exc
    except OSError as exc:
        raise refusal(
            "store.unwritable", f"cannot create {path}: {exc.strerror}"
        ) from exc


def update_package(
    loaded: LoadedPackage, mutate: Callable[[WorkPackage], WorkPackage]
) -> WorkPackage:
    """Apply ``mutate`` to the package and replace the file safely.

    The new package is validated by a serialise-and-parse round trip before
    anything is written.
    The replacement happens only if the file still has the bytes that were
    read, while an exclusive lock file is held.
    """
    new = mutate(loaded.package)
    data = dumps(new)
    result = parse_package(data)
    if result.package is None:
        raise OmnitientError(result.diagnostics)

    path = loaded.path.resolve()  # a symlinked package is updated in place
    lock = path.with_name(path.name + ".lock")
    try:
        lock_handle = lock.open("x")
    except FileExistsError as exc:
        raise refusal(
            "store.locked",
            f"{lock} exists: another update is running, or one crashed; "
            "remove the lock file by hand if no update is running",
        ) from exc
    except OSError as exc:
        raise refusal(
            "store.unwritable", f"cannot lock {path}: {exc.strerror}"
        ) from exc

    temp: tuple[Path, _FileId] | None = None
    try:
        with lock_handle:
            try:
                current = read_regular(path, MAX_PACKAGE_BYTES)
            except NotRegularFileError as exc:
                raise refusal(
                    "store.conflict",
                    f"{path} changed since it was read: it is not a regular file "
                    "now; nothing was written. Re-run the command.",
                ) from exc
            if len(current) > MAX_PACKAGE_BYTES:
                raise refusal(
                    "store.conflict",
                    f"{path} changed since it was read: it is now larger than "
                    f"{MAX_PACKAGE_BYTES} bytes; nothing was written. "
                    "Re-run the command.",
                )
            if sha256_hex(current) != loaded.file_sha256:
                raise refusal(
                    "store.conflict",
                    f"{path} changed since it was read; nothing was written. "
                    "Re-run the command.",
                )
            temp = _write_temp(path, data)
            temp[0].replace(path)
            temp = None
    except OSError as exc:
        raise refusal(
            "store.unwritable", f"cannot update {path}: {exc.strerror}"
        ) from exc
    finally:
        if temp is not None:
            _discard(*temp)
        lock.unlink(missing_ok=True)
    return new


def publish_bundle(
    directory: Path,
    files: Mapping[str, bytes],
    manifest: tuple[str, bytes],
    *,
    overwrite: bool,
) -> None:
    """Publish derived files as one bundle whose completion manifest comes last.

    The manifest lists the SHA-256 of every file, so a reader that checks it
    (:func:`check_bundle`) never accepts a mixture of two writes or a bundle
    that was interrupted.

    Without ``overwrite`` every file is created exclusively and nothing existing
    is touched; losing a race removes only the files this call created.
    With ``overwrite`` the old manifest is removed first, so it can never vouch
    for new data, then each file is replaced atomically and the manifest is
    created last. A failure part-way leaves the bundle without a manifest, and
    the files already replaced stay replaced.
    """
    manifest_name, manifest_data = manifest
    items = [*files.items(), (manifest_name, manifest_data)]
    try:
        if overwrite:
            _publish_replacing(directory, list(files.items()), manifest)
        else:
            _publish_new(directory, items, manifest_name)
    except FileExistsError as exc:
        raise refusal(
            "output.exists",
            f"{exc.filename or directory} already exists, so another writer is "
            "publishing here; nothing existing was touched. Re-run the command.",
        ) from exc
    except OSError as exc:
        raise refusal(
            "output.unwritable", f"cannot write into {directory}: {exc.strerror}"
        ) from exc


def _publish_new(
    directory: Path, items: list[tuple[str, bytes]], manifest_name: str
) -> None:
    existing = [
        name
        for name, _ in items
        if (directory / name).exists() or (directory / name).is_symlink()
    ]
    if existing:
        hint = (
            ""
            if manifest_name in existing
            else f" ({manifest_name} is missing, so an earlier write was probably "
            "interrupted; remove the listed files or use --force)"
        )
        raise refusal(
            "output.exists",
            f"{', '.join(existing)} already exist in {directory}; "
            f"use --force to replace them{hint}",
        )
    created: list[tuple[Path, _FileId]] = []
    try:
        for name, data in items:
            path = directory / name
            created.append((path, _create_exclusive(path, data)))
    except BaseException:
        for path, ident in reversed(created):
            _discard(path, ident)
        raise


def _publish_replacing(
    directory: Path, files: list[tuple[str, bytes]], manifest: tuple[str, bytes]
) -> None:
    manifest_name, manifest_data = manifest
    with contextlib.suppress(FileNotFoundError):
        (directory / manifest_name).unlink()
    for name, data in files:
        path = directory / name
        temp, ident = _write_temp(path, data)
        try:
            temp.replace(path)
        except BaseException:
            _discard(temp, ident)
            raise
    try:
        _create_exclusive(directory / manifest_name, manifest_data)
    except FileExistsError as exc:
        raise refusal(
            "output.conflict",
            f"another writer published {manifest_name} while this one was "
            "replacing the bundle; nothing of theirs was removed. The files may be "
            "a mixture, so check the bundle and re-run the command.",
        ) from exc


def check_bundle(directory: Path, manifest_name: str) -> list[str]:
    """The reader rule: why the bundle is not complete, or ``[]`` when it is.

    Complete means the manifest exists, is valid JSON of the manifest schema,
    and every file it lists is a regular file of at most ``MAX_OUTPUT_BYTES``
    bytes in ``directory`` whose SHA-256 matches. Unlisted files are ignored.
    """
    try:
        raw = read_regular(directory / manifest_name, MAX_OUTPUT_BYTES)
    except FileNotFoundError:
        return [
            (
                f"the completion manifest {manifest_name} does not exist: an "
                "earlier write was interrupted or is still running"
            )
        ]
    except (OSError, NotRegularFileError) as exc:
        return [f"the completion manifest {manifest_name} cannot be read: {exc}"]
    try:
        if len(raw) > MAX_OUTPUT_BYTES:
            raise ValueError(f"larger than {MAX_OUTPUT_BYTES} bytes")
        data = loads_strict(raw.decode("utf-8"))
    except ValueError as exc:
        return [f"the completion manifest {manifest_name} is not valid JSON: {exc}"]
    outputs = data.get("outputs") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("schema") != MANIFEST_SCHEMA
        or data.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or not isinstance(outputs, dict)
        or not outputs
    ):
        return [
            (
                f"{manifest_name} does not have schema {MANIFEST_SCHEMA} version "
                f"{MANIFEST_SCHEMA_VERSION} with a non-empty 'outputs' object"
            )
        ]
    problems: list[str] = []
    for name, expected in outputs.items():
        if name in {"", ".", ".."} or name != Path(name).name:
            problems.append(f"{name!r} is not a plain file name")
            continue
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            problems.append(f"{name}: the manifest has no valid SHA-256 for it")
            continue
        try:
            actual = read_regular(directory / name, MAX_OUTPUT_BYTES)
        except FileNotFoundError:
            problems.append(f"{name} is missing")
            continue
        except (OSError, NotRegularFileError):
            problems.append(f"{name} cannot be read as a regular file")
            continue
        if len(actual) > MAX_OUTPUT_BYTES or sha256_hex(actual) != expected:
            problems.append(f"{name} differs from the manifest")
    return problems
