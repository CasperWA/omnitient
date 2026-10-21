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
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnitient.errors import OmnitientError, refusal
from omnitient.limits import MAX_BUNDLE_OUTPUTS, MAX_OUTPUT_BYTES, MAX_PACKAGE_BYTES
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


def _create_exclusive(path: Path, data: bytes, mode: int | None = None) -> _FileId:
    """Create ``path`` (it must not exist), write ``data`` and return its identity.

    Without ``mode`` the file gets 0o666 minus the umask. With ``mode`` it gets
    exactly those permission bits, set before any data is written.
    A failure after the file exists removes it again: it is ours.
    """
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o666 if mode is None else 0o600,
    )
    with os.fdopen(fd, "wb") as handle:
        info = os.fstat(fd)
        ident = (info.st_dev, info.st_ino)
        try:
            if mode is not None:
                os.fchmod(fd, mode)
            handle.write(data)
            handle.flush()
            os.fsync(fd)
        except BaseException:
            _discard(path, ident)
            raise
    return ident


def _kept_mode(path: Path) -> int | None:
    """The permission bits a replacement of ``path`` keeps, or ``None`` if new.

    Only the read, write and execute bits of a regular file are kept; never
    the set-user-ID, set-group-ID or sticky bits, and never the owner.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return stat.S_IMODE(info.st_mode) & 0o777


def _discard(path: Path, ident: _FileId, token: bytes | None = None) -> None:
    """Remove ``path`` only while it is still the file this operation created.

    A deleted file's device and inode can be given to a new file, so where
    the creator wrote a unique ``token`` as the content, that must match too.
    """
    try:
        info = os.lstat(path)
        if (info.st_dev, info.st_ino) != ident:
            return
        if token is not None and read_regular(path, len(token)) != token:
            return
        path.unlink()
    except (OSError, NotRegularFileError):
        pass


def _write_temp(path: Path, data: bytes) -> tuple[Path, _FileId]:
    """Write ``data`` to a new, uniquely named temporary file next to ``path``.

    The file gets the permission bits ``path`` has now (:func:`_kept_mode`),
    so replacing ``path`` with it does not widen or narrow access.
    """
    mode = _kept_mode(path)
    for _ in range(_TEMP_ATTEMPTS):
        temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
        try:
            return temp, _create_exclusive(temp, data, mode)
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
    token = f"{os.getpid()} {secrets.token_hex(16)}\n".encode("ascii")
    try:
        lock_ident = _create_exclusive(lock, token)
    except FileExistsError as exc:
        raise refusal(
            "store.locked",
            f"{lock} exists: another update is running, or one crashed; "
            "remove the lock file by hand if no update is running (after a "
            "crash, first check whether the package already has the change)",
        ) from exc
    except OSError as exc:
        raise refusal(
            "store.unwritable", f"cannot lock {path}: {exc.strerror}"
        ) from exc

    temp: tuple[Path, _FileId] | None = None
    try:
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
        _discard(lock, lock_ident, token)  # only our own lock, never one taken since
    return new


def publish_bundle(
    directory: Path,
    files: Mapping[str, bytes],
    manifest: tuple[str, bytes],
    *,
    overwrite: bool,
) -> None:
    """Publish derived files as one bundle whose completion manifest comes last.

    The manifest lists the SHA-256 of every file, so a reader that uses the
    bytes :func:`read_bundle` returns never gets a mixture of two writes or a
    bundle that was interrupted. Verifying and then reopening the files by
    name is not covered: a later write can replace them in between.

    Without ``overwrite`` every file is created exclusively and nothing existing
    is touched; losing a race removes only the files this call created.
    With ``overwrite`` the old manifest is removed first, so it can never vouch
    for new data, then each file is replaced atomically and the manifest is
    created last. Replaced files and the new manifest keep the permission bits
    of the files they replace. A failure part-way leaves the bundle without a
    manifest, and the files already replaced stay replaced.
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
    manifest_mode = _kept_mode(directory / manifest_name)
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
        _create_exclusive(directory / manifest_name, manifest_data, manifest_mode)
    except FileExistsError as exc:
        raise refusal(
            "output.conflict",
            f"another writer published {manifest_name} while this one was "
            "replacing the bundle; nothing of theirs was removed. The files may be "
            "a mixture, so check the bundle and re-run the command.",
        ) from exc


class IncompleteBundleError(Exception):
    """The bundle does not satisfy the reader rule; ``problems`` says why."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class VerifiedBundle:
    """What :func:`read_bundle` read and verified.

    ``manifest`` is the parsed manifest and ``files`` maps each file it lists
    to the bytes whose SHA-256 was checked against it.
    """

    manifest: Mapping[str, Any]
    files: Mapping[str, bytes]


def read_bundle(directory: Path, manifest_name: str) -> VerifiedBundle:
    """Apply the reader rule and return the bytes it verified.

    Complete means the manifest exists, is valid JSON of the manifest schema,
    lists 1 to ``MAX_BUNDLE_OUTPUTS`` plain file names, and every file it lists
    is a regular file of at most ``MAX_OUTPUT_BYTES`` bytes in ``directory``
    whose SHA-256 matches. Unlisted files are ignored.

    The manifest and each listed file are read once, and the bytes returned
    are the bytes that were hashed. So everything returned matches one
    manifest, even if another writer replaces files during or after the call:
    a file replaced before it is read no longer matches, and the call fails
    instead of mixing two writes. Opening the files again by name afterwards
    is not covered.

    Raises :class:`IncompleteBundleError` listing every problem found.
    """
    try:
        raw = read_regular(directory / manifest_name, MAX_OUTPUT_BYTES)
    except FileNotFoundError as exc:
        raise IncompleteBundleError(
            [
                (
                    f"the completion manifest {manifest_name} does not exist: an "
                    "earlier write was interrupted or is still running"
                )
            ]
        ) from exc
    except (OSError, NotRegularFileError) as exc:
        raise IncompleteBundleError(
            [f"the completion manifest {manifest_name} cannot be read: {exc}"]
        ) from exc
    try:
        if len(raw) > MAX_OUTPUT_BYTES:
            raise ValueError(f"larger than {MAX_OUTPUT_BYTES} bytes")
        data = loads_strict(raw.decode("utf-8"))
    except ValueError as exc:
        raise IncompleteBundleError(
            [f"the completion manifest {manifest_name} is not valid JSON: {exc}"]
        ) from exc
    outputs = data.get("outputs") if isinstance(data, dict) else None
    version = data.get("schema_version") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("schema") != MANIFEST_SCHEMA
        or type(version) is not int  # not a bool, although True == 1
        or version != MANIFEST_SCHEMA_VERSION
        or not isinstance(outputs, dict)
        or not 0 < len(outputs) <= MAX_BUNDLE_OUTPUTS
    ):
        raise IncompleteBundleError(
            [
                (
                    f"{manifest_name} does not have schema {MANIFEST_SCHEMA} version "
                    f"{MANIFEST_SCHEMA_VERSION} with an 'outputs' object listing 1 to "
                    f"{MAX_BUNDLE_OUTPUTS} files"
                )
            ]
        )
    problems: list[str] = []
    files: dict[str, bytes] = {}
    for name, expected in outputs.items():
        if name in {"", ".", ".."} or "\0" in name or name != Path(name).name:
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
            continue
        files[name] = actual
    if problems:
        raise IncompleteBundleError(problems)
    return VerifiedBundle(data, files)


def check_bundle(directory: Path, manifest_name: str) -> list[str]:
    """The reader rule without the data: why the bundle is not complete, or ``[]``.

    A ``[]`` result says only that the files verified while they were read.
    To use a bundle's contents, take them from :func:`read_bundle`.
    """
    try:
        read_bundle(directory, manifest_name)
    except IncompleteBundleError as exc:
        return exc.problems
    return []
