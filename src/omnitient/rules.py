# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Small, pure validation rules shared by the schema, context and ops layers."""

from __future__ import annotations

import fnmatch
import re
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit

from omnitient.limits import PATH_MAX, URL_MAX

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_REVISION = re.compile(r"^[0-9a-f]{7,64}$")
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,199}$")
ENGINE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
PACKAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}(/[a-z0-9][a-z0-9-]{0,62}){2}$")

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Bidirectional marks and overrides, spelled as code points so that this file
# itself contains no invisible characters.
_BIDI = frozenset(
    chr(code)
    for code in (0x200E, 0x200F, 0x061C, *range(0x202A, 0x202F), *range(0x2066, 0x206A))
)

FORBIDDEN_DIRECTORIES = frozenset({".git", ".ssh", ".aws", ".gnupg", "secrets"})
FORBIDDEN_NAMES = (
    ".env",
    ".env.*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "auth.json",
    "credentials*",
    "*.pem",
    "*.key",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
)


def text_problem(value: str, *, multiline: bool) -> str | None:
    """Return why ``value`` is unacceptable text, or ``None``."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "contains a lone surrogate"
    for char in value:
        if char in _BIDI:
            return "contains a bidirectional control character"
        category = unicodedata.category(char)
        if category in {"Zl", "Zp"}:
            return "contains a Unicode line or paragraph separator"
        if category == "Cc" and not (multiline and char in "\n\t"):
            return "contains a control character"
    return None


def is_timestamp(value: str) -> bool:
    if not TIMESTAMP.fullmatch(value):
        return False
    try:
        datetime.strptime(value, TIMESTAMP_FORMAT)
    except ValueError:
        return False
    return True


def url_problem(value: str) -> str | None:
    """Accept only credential-free ``https://`` URLs of bounded length."""
    if len(value) > URL_MAX:
        return f"is longer than {URL_MAX} characters"
    if text_problem(value, multiline=False) or any(c.isspace() for c in value):
        return "contains whitespace or control characters"
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        return "is not a well-formed URL"
    if parts.scheme != "https" or not parts.hostname:
        return "must be an https:// URL"
    if "@" in parts.netloc:
        return "must not embed credentials"
    return None


def repository_problem(value: str) -> str | None:
    """Reject a ``scheme://user:password@host`` style value or a malformed URL."""
    if "://" not in value:
        return None
    try:
        if urlsplit(value).password is not None:
            return "must not embed credentials"
    except ValueError:
        return "is not a well-formed URL"
    return None


def relative_path_problem(path: str) -> str | None:
    """Return why ``path`` is not an acceptable relative POSIX path, or ``None``."""
    if not path:
        return "is empty"
    if len(path) > PATH_MAX:
        return f"is longer than {PATH_MAX} characters"
    if text_problem(path, multiline=False):
        return "contains control characters"
    if "\\" in path:
        return "must use forward slashes"
    if path.startswith(("/", "~")):
        return "must be relative and must not start with '~'"
    for part in path.split("/"):
        if part in {"", ".", ".."}:
            return "must not contain empty, '.' or '..' components"
    return None


def forbidden_reason(path: str) -> str | None:
    """Return why ``path`` names a file that is never included, or ``None``."""
    parts = path.lower().split("/")
    for directory in parts[:-1]:
        if directory in FORBIDDEN_DIRECTORIES:
            return f"is inside a '{directory}' directory"
    name = parts[-1]
    if name in FORBIDDEN_DIRECTORIES:
        return f"is named '{name}'"
    for pattern in FORBIDDEN_NAMES:
        if fnmatch.fnmatchcase(name, pattern):
            return f"matches the excluded credential file pattern '{pattern}'"
    return None
