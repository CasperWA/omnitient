# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Golden values for manifest v1, recorded before manifest v2 existed.

Every constant below was produced by the code as it was before O3 changed the
digest and serialization paths. A change to any of them means the digest or the
stored form of an existing v1 package changed, which O3 forbids.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from omnitient.schema import parse_package
from omnitient.serialize import canonical_bytes, content_digest, covered_dict, dumps

REPO_ROOT = Path(__file__).resolve().parent.parent

# A stored v1 manifest that uses every section, in the stored (canonical) form.
V1_STORED = """\
{
  "schema": "omnitient/work-package",
  "schema_version": 1,
  "project": "demo",
  "initiative": "init",
  "work_package": "wp1",
  "title": "Golden v1 package - unicode: åøæ \U0001f600",
  "stage": "implementation",
  "objective": "Build the thing.\\nSecond line.",
  "scope": [
    "Implement A"
  ],
  "exclusions": [
    "No C"
  ],
  "acceptance": [
    "A works"
  ],
  "dependencies": [
    {
      "package": "demo/init/wp0",
      "note": "after wp0"
    }
  ],
  "roots": [
    {
      "name": "repo",
      "description": "The demo repository"
    }
  ],
  "context": [
    {
      "id": "spec",
      "root": "repo",
      "path": "docs/spec.md",
      "sha256": "1111111111111111111111111111111111111111111111111111111111111111",
      "required": true,
      "mode": "embed",
      "purpose": "The specification",
      "git": {
        "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "repository": "demo-repo",
        "ref": "main"
      }
    }
  ],
  "decisions": [
    {
      "id": "D1",
      "question": "Which format?",
      "rationale": "Because.",
      "status": "resolved",
      "answer": "JSON",
      "source": "user:alice",
      "needed_by": "implementation"
    }
  ],
  "recommendations": [
    {
      "id": "R1",
      "stage": "implementation",
      "model": "some-model",
      "limits": {
        "max_turns": 50,
        "max_cost_usd": 5.0
      }
    }
  ],
  "evidence": [
    {
      "id": "E1",
      "kind": "commit",
      "ref": "abcdef1",
      "summary": "first",
      "recorded_by": "me",
      "recorded_at": "2026-01-02T03:04:05Z",
      "engine": {
        "run_id": "run-1",
        "attempt_id": "a1"
      }
    },
    {
      "id": "E2",
      "kind": "report",
      "ref": "repo:docs/spec.md",
      "recorded_by": "me",
      "recorded_at": "2026-01-02T03:04:06Z"
    }
  ],
  "approvals": [
    {
      "id": "A1",
      "stage": "implementation",
      "approved_by": "user:alice",
      "approved_at": "2026-01-02T03:04:05Z",
      "content_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "note": "ok"
    }
  ]
}
"""

# The revision of V1_STORED and the SHA-256 of its stored bytes.
V1_DIGEST = "d1921938f38cacc766be3f21215475ce770c3678399f79ebb6d28eac283db572"
V1_FILE_SHA256 = "bd4c55682739a1fa268bb1e7f80a614ba78609c81348570629cd4408a0906ce3"

# Real packages in this repository that O3 must leave byte-identical and keep at
# the same revision: (path, content digest, SHA-256 of the file's bytes).
REAL_PACKAGES = [
    (
        "examples/o2-git-pinned-context/work-package.json",
        "c669932f470a5eeb86ece52903c8be7cd653354a0b16394749db4db4e2dc1489",
        "9b607cd9abc2cd7552d57f3a8df5129f8eb23876fbd40928c4823e96b7fa3a2c",
    ),
    (
        "work/o2-overview/work-package.json",
        "5ccc8c4cf0fe7b67641c7922b6665c77810cd3ccfbcbdd13e01e4721122be0b8",
        "7c2511846fc20784bb74061324c8ef556addfa1e98db7a2e79b71501e13927e7",
    ),
]


def test_the_golden_v1_manifest_digest_is_unchanged() -> None:
    parsed = parse_package(V1_STORED.encode("utf-8"))
    assert parsed.package is not None, parsed.diagnostics
    assert content_digest(parsed.package) == V1_DIGEST


def test_the_covered_projection_of_v1_hashes_schema_version_1() -> None:
    parsed = parse_package(V1_STORED.encode("utf-8"))
    assert parsed.package is not None
    covered = covered_dict(parsed.package)
    assert covered["schema_version"] == 1
    assert list(covered) == [
        "schema_version",
        "project",
        "initiative",
        "work_package",
        "title",
        "objective",
        "scope",
        "exclusions",
        "acceptance",
        "dependencies",
        "roots",
        "context",
        "decisions",
    ]
    assert hashlib.sha256(canonical_bytes(covered)).hexdigest() == V1_DIGEST


def test_the_golden_v1_manifest_round_trips_to_the_same_bytes() -> None:
    raw = V1_STORED.encode("utf-8")
    parsed = parse_package(raw)
    assert parsed.package is not None
    assert dumps(parsed.package) == raw
    assert hashlib.sha256(dumps(parsed.package)).hexdigest() == V1_FILE_SHA256


@pytest.mark.parametrize(("path", "digest", "file_sha"), REAL_PACKAGES)
def test_real_v1_packages_keep_their_digest_and_bytes(
    path: str, digest: str, file_sha: str
) -> None:
    file = REPO_ROOT / path
    if not file.is_file():
        pytest.skip("not part of an installed distribution")
    raw = file.read_bytes()
    parsed = parse_package(raw)
    assert parsed.package is not None, parsed.diagnostics
    assert content_digest(parsed.package) == digest
    assert hashlib.sha256(raw).hexdigest() == file_sha
    assert dumps(parsed.package) == raw
