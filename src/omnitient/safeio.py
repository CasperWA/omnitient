"""Bounded reads of untrusted files and strict JSON parsing.

Used for every file the tool reads that a person or another process controls:
packages, roots files and (through ``context``) context files.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, BinaryIO


class NotRegularFileError(Exception):
    """The path names something other than a regular file (a FIFO, a device...)."""


def open_regular(path: Path) -> BinaryIO:
    """Open ``path`` for reading, refusing anything but a regular file.

    The type is checked on the open descriptor, so it cannot change between
    the check and the read, and ``O_NONBLOCK`` keeps opening a FIFO that has
    no writer from blocking. Symlinks are followed.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFileError(str(path))
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def read_regular(path: Path, limit: int) -> bytes:
    """Read a regular file; the result is longer than ``limit`` only if the file is."""
    with open_regular(path) as handle:
        return handle.read(limit + 1)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def loads_strict(text: str) -> Any:
    """``json.loads`` that refuses duplicate keys, ``NaN``/``Infinity`` and deep nesting.

    Every failure is a ``ValueError``, so callers need one handler.
    """
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except RecursionError as exc:
        raise ValueError("nesting is too deep") from exc
