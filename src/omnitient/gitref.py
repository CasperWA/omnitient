# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Read-only Git resolution for review preparation (docs/review-handoff-contract.md).

This is the only module that starts another program. It asks Git a handful of
read-only questions about one explicitly named local repository and returns
full object ids; it never fetches, never writes to the repository and never
runs a hook, a pager or a configured helper that these questions could reach.

The hardening is deliberate and limited. Global and system configuration are
switched off, replacement refs cannot change what an id means, optional locks and
lazy fetching are off, and the verbs are a fixed allowlist of ``rev-parse``,
``symbolic-ref`` and ``merge-base``. The repository's own ``.git/config`` still
applies, so this is not a defence against a repository a hostile party prepared.
Nothing here looks at the working tree, so whether it is clean is not observed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argument vectors, never a shell
from pathlib import Path

from omnitient.errors import refusal
from omnitient.review import Basis, Comparison

_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_TIMEOUT_SECONDS = 30
_MAX_REF_CHARS = 256
_REF_PREFIX = "refs/heads/"


def validate_ref(label: str, ref: str) -> str:
    """``ref`` if Git could take it as one revision expression, else a refusal."""
    if not ref or ref != ref.strip():
        raise refusal(
            "review.ref_invalid", f"the {label} is empty or has surrounding space"
        )
    if len(ref) > _MAX_REF_CHARS:
        raise refusal(
            "review.ref_invalid",
            f"the {label} is longer than {_MAX_REF_CHARS} characters",
        )
    if ref.startswith("-"):
        raise refusal(
            "review.ref_invalid",
            f"the {label} begins with '-', which Git would read as an option",
        )
    if any(not (char == " " or char.isprintable()) for char in ref):
        raise refusal(
            "review.ref_invalid",
            f"the {label} holds a control or other unprintable character",
        )
    return ref


def _environment() -> dict[str, str]:
    env = {"LC_ALL": "C"}
    if "PATH" in os.environ:
        env["PATH"] = os.environ["PATH"]
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_ATTR_NOSYSTEM="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
    )
    return env


class _Git:
    def __init__(self, executable: str, repo: Path) -> None:
        self._base = [
            executable,
            "--no-optional-locks",
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-C",
            str(repo),
        ]
        self._env = _environment()

    def run(self, *args: str) -> tuple[int, str, str]:
        try:
            done = subprocess.run(  # nosec B603 - allowlisted verbs
                [*self._base, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
                env=self._env,
            )
        except subprocess.TimeoutExpired as exc:
            raise refusal(
                "review.git_failed",
                f"Git did not answer within {_TIMEOUT_SECONDS} seconds",
            ) from exc
        except OSError as exc:
            raise refusal("review.git_failed", f"cannot run Git: {exc}") from exc
        return done.returncode, done.stdout, done.stderr

    def commit_or_tree(self, spec: str, kind: str) -> str | None:
        """The full id ``spec`` names as a ``kind`` ("commit" or "tree"), if any."""
        code, out, _ = self.run(
            "rev-parse", "--verify", "--quiet", "--end-of-options", f"{spec}^{{{kind}}}"
        )
        answer = out.strip()
        return answer if code == 0 and _ID.fullmatch(answer) else None


def resolve(repo: Path, base: str, candidate: str, basis: Basis) -> Comparison:
    """Resolve the base and the candidate once, to full commit ids and trees.

    ``repo`` must be the top of a work tree. Nothing is fetched, so a ref that
    exists only on a remote must already be a local ref. Each ref is resolved
    exactly once here; every later use is of the ids this returns.
    """
    executable = shutil.which("git")
    if executable is None:
        raise refusal("review.git_missing", "the git program was not found on PATH")
    try:
        root = repo.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise refusal(
            "review.repo", f"cannot resolve the repository path: {exc}"
        ) from exc
    if not root.is_dir():
        raise refusal("review.repo", "the repository path is not a directory")
    git = _Git(executable, root)

    code, out, err = git.run("rev-parse", "--is-inside-work-tree")
    if code != 0:
        raise refusal(
            "review.repo",
            "the directory is not a Git work tree that Git will open "
            f"({err.strip() or 'no reason given'})",
        )
    if out.strip() != "true":
        raise refusal("review.repo", "the directory is not inside a Git work tree")
    code, out, _ = git.run("rev-parse", "--show-prefix")
    if code != 0 or out.strip():
        raise refusal(
            "review.repo",
            "the directory is inside a work tree but is not its top; name the "
            "top directory of the repository",
        )

    base_id = git.commit_or_tree(base, "commit")
    if base_id is None:
        raise refusal(
            "review.ref_unresolved",
            "the base does not name a commit in this repository (nothing is "
            "fetched; a remote branch must exist as a local ref)",
        )
    candidate_id = git.commit_or_tree(candidate, "commit")
    if candidate_id is None:
        raise refusal(
            "review.ref_unresolved",
            "the candidate does not name a commit in this repository (nothing is "
            "fetched; a remote branch must exist as a local ref)",
        )
    candidate_tree = git.commit_or_tree(candidate_id, "tree")
    if candidate_tree is None:
        raise refusal(
            "review.ref_unresolved", "the candidate commit has no readable tree"
        )

    if basis == "exact":
        basis_id = base_id
    else:
        code, out, err = git.run("merge-base", "--all", base_id, candidate_id)
        heads = out.split()
        if code == 1 and not heads:
            raise refusal(
                "review.no_merge_base",
                "the base and the candidate have no common ancestor; use "
                "--basis exact to compare them directly",
            )
        if code != 0 or not all(_ID.fullmatch(h) for h in heads):
            raise refusal(
                "review.git_failed",
                f"Git could not find a merge base ({err.strip() or 'no reason given'})",
            )
        if len(heads) > 1:
            raise refusal(
                "review.ambiguous_base",
                f"the history has {len(heads)} merge bases, so the comparison is "
                "ambiguous; name the base commit you mean and use --basis exact",
            )
        basis_id = heads[0]
    basis_tree = git.commit_or_tree(basis_id, "tree")
    if basis_tree is None:
        raise refusal("review.ref_unresolved", "the basis commit has no readable tree")
    if basis_id == candidate_id or basis_tree == candidate_tree:
        raise refusal(
            "review.empty_comparison",
            "the basis and the candidate have the same tree, so there is nothing "
            "to review",
        )

    code, out, _ = git.run("rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    head = out.strip() if code == 0 and _ID.fullmatch(out.strip()) else None
    code, out, _ = git.run("symbolic-ref", "--quiet", "HEAD")
    ref = out.strip()
    branch = ref.removeprefix(_REF_PREFIX) if code == 0 and ref else None
    return Comparison(
        repo=root,
        base_ref=base,
        candidate_ref=candidate,
        base=base_id,
        candidate=candidate_id,
        candidate_tree=candidate_tree,
        basis=basis,
        basis_commit=basis_id,
        basis_tree=basis_tree,
        head=head,
        branch=branch,
    )
