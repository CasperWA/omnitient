# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Read-only Git resolution for review preparation (docs/review-handoff-contract.md).

This is the only module that starts another program. It asks Git a handful of
read-only questions about one explicitly named local repository and returns
full object ids; it never fetches, never writes to the repository and never
runs a hook, a pager or a configured helper that these questions could reach.

The hardening is deliberate and limited. The Git program is looked up only in
absolute ``PATH`` entries outside the repository, and only a program whose own
location (the entry, the program and every directory above them) only the owner
and root can change is used. Global and system configuration are switched off,
replacement refs cannot change what an id means, optional locks and lazy
fetching are off, every transport is refused, by ``GIT_ALLOW_PROTOCOL`` as well
as ``protocol.allow``, so that a missing object is never fetched, even by a Git
too old to know ``GIT_NO_LAZY_FETCH`` and whatever the repository's own
``protocol.<name>.allow`` says, and the verbs are a fixed allowlist of
``version``, ``rev-parse``, ``symbolic-ref``, ``merge-base`` and
``for-each-ref``, plus ``status`` and ``check-ref-format`` for the commit mode of
``omnitient start`` (docs/start-contract.md). ``status`` runs with optional
locks off, so it never rewrites the index, and with hooks and the file system
monitor switched off. The repository's own ``.git/config`` still applies, so
this is not a defence against a repository a hostile party prepared. Review
preparation does not look at the working tree, so whether it is clean is not
observed there; only :func:`observe_checkout` asks.

Nothing in this module writes to a repository: the session, never this program,
creates a branch or a commit.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess  # nosec B404 - fixed argument vectors, never a shell
from pathlib import Path

from omnitient import start
from omnitient.errors import OmnitientError, refusal
from omnitient.review import PINNED_GIT_CONFIG, PINNED_GIT_ENV, Basis, Comparison

_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_TIMEOUT_SECONDS = 30
_MAX_REF_CHARS = 256
_REF_PREFIX = "refs/heads/"

# GIT_CONFIG_GLOBAL, which switches the user's configuration off, is Git 2.32.
# An older Git would silently apply it, so it is refused rather than used.
MIN_GIT_VERSION = (2, 32)
_VERSION = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")
_AMBIGUOUS = re.compile(
    r"^(?:warning: refname '.*' is ambiguous\.|"
    r"error: short object ID \S+ is ambiguous)$",
    re.MULTILINE,
)


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


def _inside(path: Path, top: Path) -> bool:
    return path == top or top in path.parents


def _trusted_chain(path: Path) -> bool:
    """Whether only the owner and root can change ``path`` or put another in its place.

    ``path`` is a resolved path. It and every directory above it must belong to
    root or to the current user and be writable by neither group nor others. A
    sticky directory (``/tmp``) is accepted although others can write to it,
    because they cannot rename or remove an entry they do not own, and every
    entry on the way is checked to be owned by root or the user. Access control
    lists and a process that runs as the owner are not considered.
    """
    getuid = getattr(os, "getuid", None)
    for item in (path, *path.parents):
        try:
            info = item.stat()
        except OSError:
            return False
        if getuid is not None and info.st_uid not in (0, getuid()):
            return False
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not (
            stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
        ):
            return False
    return True


def trusted_git(repo: Path) -> Path:
    """The Git program to run: the first acceptable ``git`` on the caller's ``PATH``.

    A ``PATH`` entry that is empty or relative (it would mean the current
    directory), that lies inside the repository, or whose program, directory or
    any directory above them others can change (:func:`_trusted_chain`), is
    skipped, so a program planted in the work tree or in a shared directory is
    never started. The program is used by its real path. Nothing is looked up
    anywhere else.
    """
    skipped: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not Path(entry).is_absolute():
            continue
        try:
            real = (Path(entry) / "git").resolve(strict=True)
            folder = Path(entry).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not real.is_file() or not os.access(real, os.X_OK):
            continue
        if _inside(real, repo) or _inside(folder, repo):
            skipped.append("inside the repository")
        elif not (_trusted_chain(real) and _trusted_chain(folder)):
            skipped.append("changeable by others")
        else:
            return real
    if skipped:
        raise refusal(
            "review.git_untrusted",
            "every git program on PATH is "
            + " or ".join(sorted(set(skipped)))
            + "; put a git that only you and root can change, in a directory that "
            "only you and root can change and whose parents only you and root can "
            "change, in an absolute PATH entry outside the repository",
        )
    raise refusal(
        "review.git_missing",
        "no git program was found in an absolute PATH directory (empty and "
        "relative entries are not searched)",
    )


def _environment(executable: Path) -> dict[str, str]:
    # Only the Git program's own directory: Git finds its helpers there and a
    # PATH entry of the caller cannot add one. Nothing else is inherited.
    return {**dict(PINNED_GIT_ENV), "PATH": str(executable.parent)}


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


class _Git:
    def __init__(self, executable: Path, repo: Path) -> None:
        self._base = [
            str(executable),
            "--no-optional-locks",
            "--no-replace-objects",
            *(arg for k, v in PINNED_GIT_CONFIG for arg in ("-c", f"{k}={v}")),
            "-C",
            str(repo),
        ]
        self._env = _environment(executable)

    def run(self, *args: str) -> tuple[int, str, str]:
        code, out, err = self.run_bytes(*args)
        return code, _text(out), _text(err)

    def run_bytes(self, *args: str) -> tuple[int, bytes, bytes]:
        """The exit status and the exact output: bytes, so that no path is altered."""
        try:
            done = subprocess.run(  # nosec B603 - allowlisted verbs
                [*self._base, *args],
                capture_output=True,
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
        return self.verify(spec, kind)[0]

    def verify(self, spec: str, kind: str) -> tuple[str | None, bool]:
        """The id ``spec`` names as a ``kind``, and whether Git found it ambiguous.

        Git warns about a name that is both a branch and a tag, or a ref and an
        abbreviated object id, and resolves it silently under ``--quiet``, so
        the warning is read instead.
        """
        code, out, err = self.run(
            "rev-parse", "--verify", "--end-of-options", f"{spec}^{{{kind}}}"
        )
        answer = out.strip()
        found = answer if code == 0 and _ID.fullmatch(answer) else None
        return found, _AMBIGUOUS.search(err) is not None

    def given(self, label: str, spec: str) -> str:
        """The commit a ref the operator named stands for, or a refusal."""
        found, ambiguous = self.verify(spec, "commit")
        if ambiguous:
            raise refusal(
                "review.ref_ambiguous",
                f"the {label} names more than one thing in this repository (for "
                "example a branch and a tag, or a ref and an abbreviated commit "
                "id), and Git would pick one silently; name it in full, such as "
                "refs/heads/NAME or refs/tags/NAME, or give the full commit id",
            )
        if found is None:
            raise refusal(
                "review.ref_unresolved",
                f"the {label} does not name a commit in this repository (nothing is "
                "fetched, so a remote branch must exist as a local ref, and a "
                "commit that is missing locally, for example in a partial or "
                "shallow clone, must be fetched by you first)",
            )
        return found

    def version(self) -> tuple[int, int, str]:
        code, out, _ = self.run("version")
        match = _VERSION.match(out.strip())
        if code != 0 or match is None:
            raise refusal("review.git_failed", "Git did not report its version")
        return int(match[1]), int(match[2]), out.strip()

    def directory(self, flag: str, root: Path) -> Path:
        # A path's leading and trailing spaces are part of it: only the one
        # newline that ends the line is removed, and the bytes are decoded as
        # the file system names them.
        code, out, err = self.run_bytes("rev-parse", flag)
        text = os.fsdecode(out.removesuffix(b"\n"))
        if code != 0 or not text:
            raise refusal(
                "review.git_failed",
                "Git did not name its directory "
                f"({_text(err).strip() or 'no reason given'})",
            )
        try:
            return (root / text).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise refusal(
                "review.git_failed", f"cannot resolve the Git directory: {exc}"
            ) from exc


def _identities(paths: tuple[Path, ...]) -> tuple[tuple[int, int], ...]:
    ids: list[tuple[int, int]] = []
    for path in paths:
        try:
            info = path.stat()
        except OSError as exc:
            raise refusal(
                "review.git_failed", f"cannot examine the repository: {exc.strerror}"
            ) from exc
        ids.append((info.st_dev, info.st_ino))
    return tuple(dict.fromkeys(ids))


def resolve(repo: Path, base: str, candidate: str, basis: Basis) -> Comparison:
    """Resolve the base and the candidate once, to full commit ids and trees.

    ``repo`` must be the top of a work tree. Nothing is fetched, so a ref that
    exists only on a remote must already be a local ref. Each ref is resolved
    exactly once here; every later use is of the ids this returns.
    """
    try:
        root = repo.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise refusal(
            "review.repo", f"cannot resolve the repository path: {exc}"
        ) from exc
    if not root.is_dir():
        raise refusal("review.repo", "the repository path is not a directory")
    executable = trusted_git(root)
    git = _Git(executable, root)
    major, minor, version = git.version()
    if (major, minor) < MIN_GIT_VERSION:
        raise refusal(
            "review.git_version",
            f"this Git is too old ({version}); version "
            f"{MIN_GIT_VERSION[0]}.{MIN_GIT_VERSION[1]} or later is needed, "
            "because an older one cannot be told to ignore the user's configuration",
        )

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
    if code != 0 or out != "\n":
        raise refusal(
            "review.repo",
            "the directory is inside a work tree but is not its top; name the "
            "top directory of the repository",
        )

    git_dir = git.directory("--absolute-git-dir", root)
    common_dir = git.directory("--git-common-dir", root)
    git_dirs = tuple(dict.fromkeys((git_dir, common_dir)))
    # Git still reads this deprecated file under --no-replace-objects, and it
    # can rewrite parents, so it could move a merge base.
    if any(os.path.lexists(d / "info" / "grafts") for d in git_dirs):
        raise refusal(
            "review.history_rewritten",
            "the repository has a graft file (info/grafts), which rewrites "
            "history in a way that cannot be switched off, so the comparison "
            "could differ from the commits' own; convert or remove it first "
            "(git replace --convert-graft-file)",
        )
    code, listed, err = git.run(
        "for-each-ref", "--count=1", "--format=%(refname)", "refs/replace/"
    )
    if code != 0:
        raise refusal(
            "review.git_failed",
            f"Git could not list replacement refs ({err.strip() or 'no reason given'})",
        )

    base_id = git.given("base", base)
    candidate_id = git.given("candidate", candidate)
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
    ref = out.removesuffix("\n")
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
        git_executable=executable,
        git_version=version,
        git_dirs=git_dirs,
        has_replacements=bool(listed.strip()),
        protected_ids=_identities((root, *git_dirs)),
    )


_IN_PROGRESS = (
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase or am"),
    ("BISECT_LOG", "bisect"),
    ("sequencer", "sequencer"),
)


def _status_paths(out: bytes) -> tuple[list[str], list[str], list[str], list[str]]:
    """Staged, unstaged, untracked and unmerged paths from ``status --porcelain -z``."""
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    unmerged: list[str] = []
    for entry in out.split(b"\0"):
        if len(entry) < 4:
            continue
        x, y, path = chr(entry[0]), chr(entry[1]), os.fsdecode(entry[3:])
        if x == "?" and y == "?":
            untracked.append(path)
        elif "U" in (x, y) or (x, y) in {("A", "A"), ("D", "D")}:
            unmerged.append(path)
        else:
            if x not in " !":
                staged.append(path)
            if y not in " !":
                unstaged.append(path)
    return staged, unstaged, untracked, unmerged


def _branch_problem(git: _Git, root: Path, name: str) -> tuple[str | None, str | None]:
    """The commit an existing local branch of that name points at, and any problem."""
    if (
        not name
        or name != name.strip()
        or name.startswith(("-", "refs/"))
        or name == "HEAD"
        or len(name) > _MAX_REF_CHARS
        or any(not char.isprintable() for char in name)
    ):
        return (
            None,
            "the name is empty, starts with '-' or 'refs/', is HEAD, or holds a control character",
        )
    full = _REF_PREFIX + name
    code, _, _ = git.run("check-ref-format", full)
    if code != 0:
        return None, "Git does not accept it as a branch name"
    code, out, err = git.run(
        "for-each-ref",
        "--format=%(refname)%09%(objectname)%09%(worktreepath)",
        "refs/heads/",
    )
    if code != 0:
        return (
            None,
            f"Git could not list the local branches ({err.strip() or 'no reason given'})",
        )
    found: str | None = None
    for line in out.split("\n"):
        ref, _, rest = line.partition("\t")
        if not ref:
            continue
        if ref == full:
            found, _, worktree = rest.partition("\t")
            if not _ID.fullmatch(found):
                return None, "Git did not name the commit it points at"
            if worktree and Path(worktree) != root:
                return found, "it is checked out in another work tree"
        elif ref.startswith(full + "/") or full.startswith(ref + "/"):
            return (
                None,
                f"it collides with the existing branch '{ref.removeprefix(_REF_PREFIX)}'",
            )
    code, _, _ = git.run("rev-parse", "--verify", "--quiet", f"refs/tags/{name}")
    if code == 0:
        return (
            found,
            "a tag with the same name exists, so Git would read the name ambiguously",
        )
    return found, None


def observe_checkout(checkout: Path, base: str, branch: str) -> start.GitObservation:
    """Ask read-only questions about ``checkout``, ``base`` and ``branch`` once.

    ``checkout`` is a resolved directory. Every difficulty is returned in the
    observation instead of raised, because a commit-mode preview shows it as the
    reason the start is withheld.
    """
    seen = start.GitObservation(checkout=checkout, base_ref=base, branch_name=branch)
    try:
        return _observe(checkout, seen)
    except OmnitientError as exc:
        return start.GitObservation(
            checkout=checkout,
            base_ref=base,
            branch_name=branch,
            error="; ".join(d.message for d in exc.diagnostics),
        )


def _observe(root: Path, seen: start.GitObservation) -> start.GitObservation:
    git = _Git(trusted_git(root), root)
    major, minor, version = git.version()
    if (major, minor) < MIN_GIT_VERSION:
        raise refusal("start.git", f"this Git is too old ({version})")
    code, out, err = git.run("rev-parse", "--is-inside-work-tree")
    if code != 0 or out.strip() != "true":
        raise refusal(
            "start.git",
            "the target is not a Git work tree that Git will open "
            f"({err.strip() or 'no reason given'})",
        )
    code, out, _ = git.run("rev-parse", "--show-prefix")
    if code != 0 or out != "\n":
        raise refusal(
            "start.git",
            "the target is inside a work tree but is not its top directory",
        )
    git_dir = git.directory("--absolute-git-dir", root)
    in_progress = tuple(
        label for name, label in _IN_PROGRESS if os.path.lexists(git_dir / name)
    )
    code, out, _ = git.run("rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    head = out.strip() if code == 0 and _ID.fullmatch(out.strip()) else None
    code, out, _ = git.run("rev-parse", "--verify", "--quiet", "HEAD^{tree}")
    head_tree = out.strip() if code == 0 and _ID.fullmatch(out.strip()) else None
    code, out, _ = git.run("symbolic-ref", "--quiet", "HEAD")
    ref = out.removesuffix("\n")
    current = (
        ref.removeprefix(_REF_PREFIX)
        if code == 0 and ref.startswith(_REF_PREFIX)
        else None
    )

    code, raw, status_err = git.run_bytes(
        "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
        "--ignore-submodules=none",
    )  # fmt: skip
    if code != 0:
        raise refusal(
            "start.git",
            f"Git could not report the status "
            f"({_text(status_err).strip() or 'no reason given'})",
        )
    staged, unstaged, untracked, unmerged = _status_paths(raw)

    base_id: str | None = None
    base_tree: str | None = None
    base_problem: str | None = None
    try:
        validate_ref("base", seen.base_ref)
        base_id = git.given("base", seen.base_ref)
        base_tree = git.commit_or_tree(base_id, "tree")
        if base_tree is None:
            base_problem = "the base commit has no readable tree"
    except OmnitientError as exc:
        base_problem = "; ".join(d.message for d in exc.diagnostics)
    branch_id, branch_problem = _branch_problem(git, root, seen.branch_name)

    return start.GitObservation(
        checkout=root,
        base_ref=seen.base_ref,
        branch_name=seen.branch_name,
        branch=current,
        head=head,
        head_tree=head_tree,
        staged=tuple(staged),
        unstaged=tuple(unstaged),
        untracked=tuple(untracked),
        unmerged=tuple(unmerged),
        in_progress=in_progress,
        base=base_id,
        base_tree=base_tree,
        base_problem=base_problem,
        branch_problem=branch_problem,
        branch_id=branch_id,
    )
