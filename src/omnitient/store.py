"""File persistence: bounded reads, exclusive creation and checked updates."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from omnitient.errors import OmnitientError, refusal
from omnitient.limits import MAX_PACKAGE_BYTES
from omnitient.model import WorkPackage
from omnitient.schema import parse_package
from omnitient.serialize import dumps, sha256_hex

PACKAGE_FILENAME = "work-package.json"


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
        if not stat.S_ISREG(path.stat().st_mode):
            raise refusal("store.unreadable", f"{path} is not a regular file")
        with path.open("rb") as handle:
            raw = handle.read(MAX_PACKAGE_BYTES + 1)
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
        with path.open("xb") as handle:
            handle.write(dumps(pkg))
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

    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with lock_handle:
            current = path.read_bytes()
            if sha256_hex(current) != loaded.file_sha256:
                raise refusal(
                    "store.conflict",
                    f"{path} changed since it was read; nothing was written. "
                    "Re-run the command.",
                )
            with temp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(path)
    except OSError as exc:
        raise refusal(
            "store.unwritable", f"cannot update {path}: {exc.strerror}"
        ) from exc
    finally:
        temp.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
    return new


def write_output(path: Path, data: bytes, *, overwrite: bool) -> None:
    """Write a derived output file; never replaces an existing file unless asked."""
    try:
        if overwrite:
            temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
            try:
                with temp.open("xb") as handle:
                    handle.write(data)
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
        else:
            try:
                with path.open("xb") as handle:
                    handle.write(data)
            except OSError as exc:
                if not isinstance(exc, FileExistsError):
                    path.unlink(missing_ok=True)
                raise
    except FileExistsError as exc:
        raise refusal(
            "output.exists", f"{path} already exists; use --force to replace it"
        ) from exc
    except OSError as exc:
        raise refusal(
            "output.unwritable", f"cannot write {path}: {exc.strerror}"
        ) from exc
