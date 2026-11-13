# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Shallow, read-only discovery of the packages in one explicit directory.

Only the immediate child directories of the collection are looked at, and in
each only the entry named exactly ``work-package.json``.
Discovery does not follow a symbolic link at the collection root, at a child
entry or at a manifest. Below the opened collection directory it is
descriptor-relative, uses ``O_NOFOLLOW`` on the relevant opens, refuses a link
substituted at those components and verifies that a manifest is a regular file
before reading it (docs/o2-overview-contract.md, section 3).
There is no atomic snapshot and no general protection against a hostile
concurrent writer to the file system; renames, hard links and mounts are not
defended against.
Nothing here writes, spawns a process or uses the network, and diagnostics
never carry host paths or operating-system error text.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from omnitient.errors import Diagnostic
from omnitient.limits import (
    MAX_COLLECTION_CANDIDATES,
    MAX_COLLECTION_ENTRIES,
    MAX_PACKAGE_BYTES,
)
from omnitient.store import PACKAGE_FILENAME

# ``ignored`` is never a candidate; ``invalid`` is only produced when reading.
Kind = Literal["ignored", "package", "invalid", "excluded", "unassessed"]

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class CollectionRefused(Exception):
    """The collection cannot be inventoried; nothing is reported for it."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.format())
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class Probe:
    """What one immediate entry turned out to be."""

    kind: Kind
    diagnostic: Diagnostic | None = None
    raw: bytes | None = None


@dataclass(frozen=True)
class Candidate:
    name: str  # as the file system reports it, possibly with surrogate escapes
    probe: Probe

    @property
    def path(self) -> str:
        return f"{self.name}/{PACKAGE_FILENAME}"


@dataclass(frozen=True)
class Inventory:
    entries: int
    ignored: int
    candidates: tuple[Candidate, ...]  # sorted by file system bytes


def _refuse(code: str, message: str) -> CollectionRefused:
    return CollectionRefused(Diagnostic(code, "error", message, "collection"))


@contextlib.contextmanager
def open_collection(path: Path) -> Iterator[int]:
    """Open the collection directory and yield its descriptor.

    Symbolic links in ancestor components resolve normally; a link at the final
    component is refused.
    """
    try:
        fd = os.open(path, _DIR_FLAGS)
    except (OSError, ValueError):
        raise _classify_collection_failure(path) from None
    try:
        yield fd
    finally:
        os.close(fd)


def _classify_collection_failure(path: Path) -> CollectionRefused:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return _refuse("overview.collection_invalid", "the collection does not exist")
    except NotADirectoryError:
        return _refuse(
            "overview.collection_invalid",
            "a parent of the collection is not a directory",
        )
    except (OSError, ValueError):
        return _refuse(
            "overview.collection_unreadable", "the collection cannot be read"
        )
    if stat.S_ISLNK(info.st_mode):
        return _refuse(
            "overview.symlink",
            "the collection is a symbolic link, which is not followed; "
            "name the real directory",
        )
    if not stat.S_ISDIR(info.st_mode):
        return _refuse(
            "overview.collection_invalid", "the collection is not a directory"
        )
    return _refuse("overview.collection_unreadable", "the collection cannot be read")


def _symlink_diagnostic(where: str) -> Diagnostic:
    return Diagnostic(
        "overview.symlink",
        "error",
        "a symbolic link is not followed, so a package behind it is not examined",
        where,
    )


def _unreadable_diagnostic(message: str) -> Diagnostic:
    return Diagnostic("overview.discovery_unreadable", "error", message, "directory")


def probe_child(root_fd: int, name: str, *, read: bool) -> Probe:
    """Classify the child directory ``name`` below ``root_fd``.

    With ``read`` the manifest bytes are read too (at most one byte more than the
    package limit); without it the manifest is only looked at with ``lstat``,
    which keeps the inventory pass cheap and unable to block.
    """
    try:
        child = os.open(name, _DIR_FLAGS, dir_fd=root_fd)
    except OSError as exc:
        # Linux reports a link refused by O_NOFOLLOW as ELOOP, or as ENOTDIR when
        # O_DIRECTORY is set too; ask lstat which of the two it really was.
        if exc.errno in {errno.ELOOP, errno.ENOTDIR} and _is_link(root_fd, name):
            return Probe("excluded", _symlink_diagnostic("directory"))
        return Probe(
            "unassessed", _unreadable_diagnostic("the directory cannot be opened")
        )
    except ValueError:
        return Probe(
            "unassessed", _unreadable_diagnostic("the directory cannot be opened")
        )
    try:
        return _probe_manifest(child, read=read)
    finally:
        os.close(child)


def _is_link(root_fd: int, name: str) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=root_fd).st_mode)
    except (OSError, ValueError):
        return False


def _probe_manifest(child: int, *, read: bool) -> Probe:
    try:
        info = os.lstat(PACKAGE_FILENAME, dir_fd=child)
    except FileNotFoundError:
        # Not a package location. When reading, the entry was one a moment ago.
        if read:
            return Probe(
                "unassessed", _unreadable_diagnostic("the manifest disappeared")
            )
        return Probe("ignored")
    except OSError:
        return Probe(
            "unassessed", _unreadable_diagnostic("the directory cannot be searched")
        )
    if stat.S_ISLNK(info.st_mode):
        return Probe("excluded", _symlink_diagnostic("manifest"))
    if not stat.S_ISREG(info.st_mode):
        return Probe("excluded", _not_regular())
    if not read:
        return Probe("package")
    return _read_manifest(child)


def _not_regular() -> Diagnostic:
    return Diagnostic(
        "overview.not_regular",
        "error",
        "the manifest is not a regular file, so it is not read",
        "manifest",
    )


def _read_manifest(child: int) -> Probe:
    try:
        fd = os.open(PACKAGE_FILENAME, _FILE_FLAGS, dir_fd=child)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return Probe("excluded", _symlink_diagnostic("manifest"))
        return Probe("invalid", _manifest_unreadable())
    try:
        # The type is checked on the open descriptor, so a FIFO swapped in after
        # the lstat above is never read (and O_NONBLOCK keeps the open from blocking).
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return Probe("excluded", _not_regular())
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_PACKAGE_BYTES + 1)
    except OSError:
        return Probe("invalid", _manifest_unreadable())
    finally:
        os.close(fd)
    return Probe("package", None, raw)


def _manifest_unreadable() -> Diagnostic:
    return Diagnostic(
        "overview.manifest_unreadable",
        "error",
        "the manifest cannot be read",
        "manifest",
    )


def _list_names(root_fd: int) -> list[str]:
    """Names of all immediate entries, or a refusal; never an arbitrary subset."""
    names: list[str] = []
    try:
        with os.scandir(root_fd) as it:
            for entry in it:
                names.append(entry.name)
                if len(names) > MAX_COLLECTION_ENTRIES:
                    raise _refuse(
                        "overview.entry_limit",
                        f"the collection has more than {MAX_COLLECTION_ENTRIES} "
                        "immediate entries",
                    )
    except OSError:
        raise _refuse(
            "overview.collection_unreadable",
            "listing the collection failed before it finished",
        ) from None
    return names


def take_inventory(root_fd: int) -> Inventory:
    """List and classify every immediate entry, within the fixed bounds.

    Raises :class:`CollectionRefused` when the listing fails, or when the number
    of entries or of candidate locations exceeds its bound.
    """
    names = sorted(_list_names(root_fd), key=os.fsencode)
    candidates: list[Candidate] = []
    ignored = 0
    for name in names:
        probe = _classify_entry(root_fd, name)
        if probe.kind == "ignored":
            ignored += 1
            continue
        candidates.append(Candidate(name, probe))
        if len(candidates) > MAX_COLLECTION_CANDIDATES:
            raise _refuse(
                "overview.package_limit",
                f"the collection has more than {MAX_COLLECTION_CANDIDATES} "
                "candidate package locations",
            )
    return Inventory(len(names), ignored, tuple(candidates))


def _classify_entry(root_fd: int, name: str) -> Probe:
    try:
        info = os.lstat(name, dir_fd=root_fd)
    except (OSError, ValueError):
        return Probe(
            "unassessed", _unreadable_diagnostic("the entry cannot be examined")
        )
    if stat.S_ISLNK(info.st_mode):
        return Probe("excluded", _symlink_diagnostic("directory"))
    if not stat.S_ISDIR(info.st_mode):
        return Probe("ignored")
    return probe_child(root_fd, name, read=False)
