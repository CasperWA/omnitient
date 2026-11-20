# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Commit mode of ``omnitient start`` (docs/start-contract.md section 4).

What these tests demonstrate, and what they do not:

* Every ``omnitient`` call below is the real command line entry point, run against
  a real Git repository created in a temporary directory.
* The "session" steps (branch creation, staging, hooks, the commit) are performed
  by the test through the same Git operations the preview lists, with a hook
  fixture that rewrites a file. That shows the operation set can be carried
  through a hook rewrite to one clean commit, and what the commands answer along
  the way. It does not show that a live model follows the skill.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from omnitient import start
from omnitient.cli import main
from omnitient.model import WorkPackage
from omnitient.start import COMMIT_TAG, PREVIEW_TAG

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)

Run = Callable[..., tuple[int, str, str]]
Write = Callable[..., Path]

BRANCH = "feature/demo"
TOKEN = re.compile(r"^preview token: (\S+)$", re.MULTILINE)
FULL = re.compile(r"[0-9a-f]{40}")


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Run:
    def invoke(*args: object) -> tuple[int, str, str]:
        code = main([str(a) for a in args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke


@pytest.fixture(autouse=True)
def git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system Git configuration reaches these repositories."""
    # Git exports GIT_INDEX_FILE and friends to a hook, and the pytest hook of a
    # commit would otherwise point these temporary repositories at that index.
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name, value in {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "author@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-02T03:04:05Z",
        "GIT_COMMITTER_DATE": "2026-01-02T03:04:05Z",
    }.items():
        monkeypatch.setenv(name, value)


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if check:
        assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done


def out_of(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.strip()


@pytest.fixture
def target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Git checkout with two commits on ``main``, entered as the working directory."""
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    git(root, "add", "--", "src/app.py")
    git(root, "commit", "-q", "-m", "first")
    (root / "src" / "app.py").write_text("print('two')\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "second")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "handoff-out"


class Commit:
    """Preview, recheck and start in commit mode, the way the skill does."""

    def __init__(
        self,
        run: Run,
        pkg_file: Path,
        repo: Path,
        out: Path,
        target: Path,
    ) -> None:
        self.run, self.pkg_file, self.repo, self.out, self.target = (
            run,
            pkg_file,
            repo,
            out,
            target,
        )
        self.base = "main"
        self.branch = BRANCH

    def _mode(self) -> list[object]:
        # The attached form, so a value that begins with '-' is data, not an option.
        return ["--mode", "commit", f"--base={self.base}", f"--branch={self.branch}"]

    def preview(self, *extra: object) -> tuple[int, str, str]:
        return self.run(
            "start", "preview", "--root", f"repo={self.repo}", "--out", self.out,
            *self._mode(), *extra, "--", self.pkg_file,
        )  # fmt: skip

    def token(self) -> str:
        code, stdout, err = self.preview()
        assert code == 0, err
        match = TOKEN.search(stdout)
        assert match, stdout
        return match.group(1)

    def handoff(self, token: str) -> tuple[int, str, str]:
        return self.run(
            "start", "handoff", "--root", f"repo={self.repo}", "--expect-preview",
            token, "--out", self.out, *self._mode(), "--", self.pkg_file,
        )  # fmt: skip

    def recheck(self, token: str, *extra: object) -> tuple[int, str, str]:
        return self.run(
            "start", "git-check", "--expect-preview", token, f"--base={self.base}",
            f"--branch={self.branch}", *extra,
        )  # fmt: skip


@pytest.fixture
def commit(
    run: Run,
    write_package: Write,
    approved: WorkPackage,
    repo: Path,
    out: Path,
    target: Path,
) -> Commit:
    return Commit(run, write_package(approved), repo, out, target)


def git_snapshot(root: Path) -> dict[str, str]:
    """Everything a Git write would change, taken without touching the index."""
    return {
        "refs": out_of(root, "for-each-ref", "--format=%(refname) %(objectname)"),
        "head": out_of(root, "symbolic-ref", "HEAD"),
        "index": (root / ".git" / "index").read_bytes().hex(),
        "status": git(root, "status", "--porcelain", "--untracked-files=all").stdout,
    }


class TestStartablePreview:
    def test_shows_where_the_commit_lands_from_what_base_and_what_is_authorized(
        self, commit: Commit, target: Path, out: Path
    ) -> None:
        head = out_of(target, "rev-parse", "HEAD")
        code, stdout, err = commit.preview()
        assert code == 0, err
        lines = stdout.splitlines()
        # where
        assert f"git work tree: {target.resolve()}" in lines
        assert f"target checkout: {target.resolve()}" in lines
        assert "observed branch: main" in lines
        assert f"observed HEAD: {head}" in lines
        assert "staged changes: none" in lines
        assert "unstaged changes: none" in lines
        assert "untracked files: none" in lines
        # from what base
        assert "git base ref: main" in lines
        assert any(
            line.startswith(
                f"git base commit: {head} (a Git commit, not the package revision"
            )
            for line in lines
        )
        assert f"feature branch: refs/heads/{BRANCH}" in lines
        assert "feature branch state: does not exist" in lines
        assert "proposed branch action: create" in lines
        # what is authorized
        assert (
            "operations a go-ahead authorizes, in this order, and only these:" in lines
        )
        ops = [line for line in lines if re.match(r"  \d\. ", line)]
        assert len(ops) == 4
        assert (
            f"create the local branch '{BRANCH}' at the git base commit {head}"
            in ops[0]
        )
        assert "by explicit path" in ops[1]
        assert "never a whole-tree add" in ops[1]
        assert "restage only the intended paths, repeating with hooks enabled" in ops[2]
        assert "exactly one local commit" in ops[3]
        assert "no --no-verify" in ops[3]
        not_authorized = next(
            line for line in lines if line.startswith("not authorized:")
        )
        for forbidden in (
            "push",
            "pull request",
            "rebase",
            "amend",
            "bypassing a hook",
        ):
            assert forbidden in not_authorized
        assert any(line.startswith("git commit mode: STARTABLE") for line in lines)
        assert (
            "no handoff was produced; it needs the owner's go-ahead for this scope"
            in lines
        )
        assert not out.exists()

    def test_the_package_revision_and_the_git_base_are_different_lines(
        self, commit: Commit
    ) -> None:
        _, stdout, _ = commit.preview()
        revision = re.search(r"^revision: ([0-9a-f]{64})$", stdout, re.MULTILINE)
        assert revision
        in_git = re.search(r"^git base commit: ([0-9a-f]{40}) ", stdout, re.MULTILINE)
        assert in_git
        assert revision[1] != in_git[1]
        assert (
            f"package revision: {revision[1]} (content digest of the work package; "
            "not a Git object)" in stdout
        )
        # the Git base is never presented as the revision
        assert in_git[1] not in revision[0]

    def test_it_writes_nothing_and_asks_git_only_to_read(
        self, commit: Commit, target: Path, tmp_path: Path
    ) -> None:
        before = git_snapshot(target)
        files = sorted(p for p in tmp_path.rglob("*") if ".git" not in p.parts)
        assert commit.preview()[0] == 0
        assert git_snapshot(target) == before
        assert out_of(target, "branch", "--list", BRANCH) == ""
        assert sorted(p for p in tmp_path.rglob("*") if ".git" not in p.parts) == files

    def test_the_token_is_a_commit_mode_token_with_the_three_git_facts(
        self, commit: Commit
    ) -> None:
        token = commit.token()
        facts = start.parse_token(token)
        assert token.startswith(f"{COMMIT_TAG}.")
        assert facts.commit_mode
        assert len(facts.values) == 11
        assert [*start.FACT_NAMES, *start.GIT_FACT_NAMES] == list(
            start.COMMIT_FACT_NAMES
        )

    def test_a_stale_local_main_can_be_the_head_when_the_base_is_an_exact_commit(
        self, commit: Commit, target: Path
    ) -> None:
        first = out_of(target, "rev-parse", "HEAD~1")
        commit.base = first
        code, stdout, _ = commit.preview()
        assert code == 0
        assert f"git base commit: {first} " in stdout
        assert "observed branch: main" in stdout
        assert "tracked files change when the branch is entered: yes" in stdout
        assert TOKEN.search(stdout)

    @pytest.mark.parametrize(
        ("setup", "action"),
        [("exists", "switch"), ("current", "stay")],
    )
    def test_an_existing_branch_at_the_base_is_selected_not_recreated(
        self, commit: Commit, target: Path, setup: str, action: str
    ) -> None:
        git(target, "branch", BRANCH)
        if setup == "current":
            git(target, "switch", "-q", BRANCH)
        _, stdout, _ = commit.preview()
        assert f"proposed branch action: {action}" in stdout
        assert (
            "feature branch state: exists and points at the git base commit" in stdout
        )
        assert TOKEN.search(stdout)


def escaped_ref(ref: str) -> str:
    return ref.replace("\n", "\\x0a")


class TestWithheld:
    """A start that is not usable prints no token, whatever the package says."""

    def reasons(self, stdout: str) -> list[str]:
        assert not TOKEN.search(stdout), stdout
        return [
            line.removeprefix("no usable handoff token: ")
            for line in stdout.splitlines()
            if line.startswith("no usable handoff token: ")
        ]

    @pytest.mark.parametrize(
        ("change", "needle"),
        [
            ("stage", "staged changes"),
            ("modify", "unstaged changes"),
            ("untracked", "untracked files"),
        ],
    )
    def test_a_dirty_checkout(
        self, commit: Commit, target: Path, change: str, needle: str
    ) -> None:
        if change == "untracked":
            (target / "note.txt").write_text("x\n", encoding="utf-8")
        else:
            (target / "src" / "app.py").write_text("print('dirty')\n", encoding="utf-8")
        if change == "stage":
            git(target, "add", "--", "src/app.py")
        code, stdout, _ = commit.preview()
        assert code == 0  # informational: shown, but nothing starts from it
        reasons = self.reasons(stdout)
        assert any(needle in r and "not clean" in r for r in reasons), reasons
        assert any(
            line.startswith("git commit mode: WITHHELD") for line in stdout.splitlines()
        )

    def test_a_dirty_checkout_names_the_paths_without_forging_lines(
        self, commit: Commit, target: Path
    ) -> None:
        (target / "evil\nno usable handoff token: fine").write_text(
            "x", encoding="utf-8"
        )
        code, stdout, _ = commit.preview()
        assert code == 0
        assert "evil\\x0a" in stdout
        assert not any(
            line.startswith("no usable handoff token: fine")
            for line in stdout.splitlines()
        )

    def test_a_merge_in_progress(self, commit: Commit, target: Path) -> None:
        git(target, "switch", "-q", "-c", "other", "HEAD~1")
        (target / "src" / "app.py").write_text("print('other')\n", encoding="utf-8")
        git(target, "commit", "-q", "-am", "other")
        git(target, "switch", "-q", "main")
        git(target, "merge", "other", check=False)
        assert (target / ".git" / "MERGE_HEAD").exists()
        _, stdout, _ = commit.preview()
        reasons = self.reasons(stdout)
        assert any("operation is in progress" in r and "merge" in r for r in reasons)

    def test_a_feature_branch_that_exists_elsewhere(
        self, commit: Commit, target: Path
    ) -> None:
        git(target, "branch", BRANCH, "HEAD~1")
        code, stdout, _ = commit.preview()
        assert code == 0
        reasons = self.reasons(stdout)
        assert any(
            "already exists" in r and "not at the git base" in r for r in reasons
        )
        assert "proposed branch action: none" in stdout

    @pytest.mark.parametrize(
        ("existing", "words"),
        [
            ("feature", "collides with the existing branch"),
            ("feature/demo/deeper", "collides"),
        ],
    )
    def test_a_branch_whose_name_cannot_be_created(
        self, commit: Commit, target: Path, existing: str, words: str
    ) -> None:
        git(target, "branch", existing)
        _, stdout, _ = commit.preview()
        assert any(words in r for r in self.reasons(stdout))

    def test_a_tag_of_the_same_name_is_ambiguous(
        self, commit: Commit, target: Path
    ) -> None:
        git(target, "tag", BRANCH)
        _, stdout, _ = commit.preview()
        assert any("tag with the same name" in r for r in self.reasons(stdout))

    def test_a_branch_checked_out_in_another_work_tree(
        self, commit: Commit, target: Path, tmp_path: Path
    ) -> None:
        git(target, "worktree", "add", "-q", "-b", BRANCH, str(tmp_path / "other-wt"))
        _, stdout, _ = commit.preview()
        assert any("another work tree" in r for r in self.reasons(stdout))

    @pytest.mark.parametrize(
        "name", ["", "-x", "refs/heads/x", "HEAD", "a..b", "a b", "x.lock"]
    )
    def test_a_branch_name_git_would_not_take(self, commit: Commit, name: str) -> None:
        commit.branch = name
        code, stdout, _ = commit.preview()
        assert code == 0
        assert any("unusable" in r for r in self.reasons(stdout)), stdout

    @pytest.mark.parametrize(
        "base", ["no-such-ref", "0" * 40, "-x", "main\nnew line", " main"]
    )
    def test_an_unresolved_base(self, commit: Commit, base: str) -> None:
        commit.base = base
        code, stdout, _ = commit.preview()
        assert code == 0
        assert any("git base is unresolved" in r for r in self.reasons(stdout)), stdout
        assert "git base commit: UNRESOLVED" in stdout
        assert "git base ref: " + escaped_ref(base) in stdout
        assert not any(line.startswith("new line") for line in stdout.splitlines())

    def test_a_target_that_is_not_a_git_checkout(
        self, commit: Commit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        code, stdout, _ = commit.preview()
        assert code == 0
        assert any("Git cannot be asked" in r for r in self.reasons(stdout))

    def test_a_directory_inside_a_checkout_is_not_the_work_tree_top(
        self, commit: Commit, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(target / "src")
        _, stdout, _ = commit.preview()
        assert any("not its top directory" in r for r in self.reasons(stdout))

    def test_files_that_would_be_replaced_under_a_mapped_root_that_is_the_checkout(
        self, commit: Commit, target: Path
    ) -> None:
        # The handoff embeds context read from the checkout's files at HEAD; entering
        # a branch at a different tree would replace them, so no token is printed.
        shutil.copytree(commit.repo / "docs", target / "docs")
        git(target, "add", "--", "docs")
        git(target, "commit", "-q", "-m", "docs")
        commit.base = out_of(target, "rev-parse", "HEAD")
        (target / "src" / "app.py").write_text("print('four')\n", encoding="utf-8")
        git(target, "commit", "-q", "-am", "four")
        commit.repo = target
        code, stdout, _ = commit.preview()
        assert code == 0
        assert "status: READY" in stdout
        assert any("about to be replaced" in r for r in self.reasons(stdout)), stdout
        # With HEAD at the base nothing is replaced, so the same mapping can start.
        git(target, "switch", "-q", "--detach", commit.base)
        assert TOKEN.search(commit.preview()[1])

    def test_a_blocked_package_still_shows_the_git_facts_and_no_token(
        self, run: Run, write_package: Write, package: WorkPackage, repo: Path,
        out: Path, target: Path,
    ) -> None:  # fmt: skip
        blocked = Commit(run, write_package(package), repo, out, target)  # no approval
        code, stdout, _ = blocked.preview()
        assert code == 3
        assert "observed HEAD: " in stdout
        assert not TOKEN.search(stdout)
        assert "withheld while the package is blocked" in stdout


class TestDriftAfterConsent:
    """What the owner confirmed is what must still be there; otherwise preview again."""

    def delivered_nothing(
        self, out: Path, target: Path, before: dict[str, str]
    ) -> None:
        assert not out.exists()
        assert git_snapshot(target) == before

    def test_an_untracked_file_after_the_preview(
        self, commit: Commit, target: Path, out: Path
    ) -> None:
        token = commit.token()
        (target / "late.txt").write_text("x\n", encoding="utf-8")
        before = git_snapshot(target)
        code, _, err = commit.handoff(token)
        assert code == 1
        assert "start.changed" in err
        assert "checkout git state" in err
        assert "Show the owner a new preview" in err
        self.delivered_nothing(out, target, before)
        # a new preview is now withheld, not silently accepted
        assert not TOKEN.search(commit.preview()[1])

    def test_the_base_ref_moving(self, commit: Commit, target: Path, out: Path) -> None:
        token = commit.token()
        (target / "src" / "app.py").write_text("print('three')\n", encoding="utf-8")
        git(target, "commit", "-q", "-am", "third")
        code, _, err = commit.handoff(token)
        assert code == 1
        assert "start.changed" in err
        assert "git base commit" in err
        assert "checkout git state" in err
        assert not out.exists()

    def test_the_branch_being_created_by_someone_else(
        self, commit: Commit, target: Path, out: Path
    ) -> None:
        token = commit.token()
        git(target, "branch", BRANCH)
        code, _, err = commit.handoff(token)
        assert code == 1
        assert "start.changed" in err
        assert "branch and operations" in err
        assert not out.exists()

    def test_the_head_or_branch_moving(
        self, commit: Commit, target: Path, out: Path
    ) -> None:
        token = commit.token()
        git(target, "switch", "-q", "--detach", "HEAD~1")
        code, _, err = commit.handoff(token)
        assert code == 1
        assert "checkout git state" in err
        assert not out.exists()

    def test_the_checkout_replaced_by_another_repository(
        self, commit: Commit, target: Path, out: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        token = commit.token()
        elsewhere = tmp_path / "elsewhere"
        shutil.copytree(target, elsewhere)
        shutil.rmtree(target)
        elsewhere.rename(target)  # same path, same content, another directory
        monkeypatch.chdir(target)
        code, _, err = commit.handoff(token)
        assert code == 1
        assert "target checkout identity" in err
        assert not out.exists()

    def test_a_fresh_preview_after_the_change_can_start(
        self, commit: Commit, target: Path, out: Path
    ) -> None:
        token = commit.token()
        (target / "src" / "app.py").write_text("print('three')\n", encoding="utf-8")
        git(target, "commit", "-q", "-am", "third")
        assert commit.handoff(token)[0] == 1
        code, stdout, err = commit.handoff(commit.token())
        assert code == 0, err
        assert "handoff delivered" in stdout
        assert out.is_dir()

    def test_the_package_changing_is_still_a_change(
        self, commit: Commit, out: Path
    ) -> None:
        token = commit.token()
        commit.pkg_file.write_bytes(commit.pkg_file.read_bytes() + b"\n")
        code, _, err = commit.handoff(token)
        assert code == 1
        assert "package file contents" in err
        assert not out.exists()

    def test_a_dirty_checkout_never_gets_a_handoff_even_with_a_forged_matching_token(
        self, commit: Commit, target: Path, out: Path
    ) -> None:
        # The fingerprint of the clean state cannot be reproduced for a dirty one.
        token = commit.token()
        (target / "src" / "app.py").write_text("print('x')\n", encoding="utf-8")
        assert commit.handoff(token)[0] == 1
        assert not out.exists()


class TestHandoffAndRecheck:
    def test_the_handoff_is_delivered_with_no_git_write_and_the_plan_stays_the_tail(
        self, commit: Commit, target: Path, out: Path
    ) -> None:
        token = commit.token()
        before = git_snapshot(target)
        code, stdout, err = commit.handoff(token)
        assert code == 0, err
        assert git_snapshot(target) == before, "the handoff step makes no Git write"
        assert (out / start.HANDOFF_FILE).is_file()
        lines = stdout.splitlines()
        confirmed = next(
            i for i, l in enumerate(lines) if l.startswith("git commit mode confirmed")
        )
        plan = next(i for i, l in enumerate(lines) if l.startswith("reading plan:"))
        assert confirmed < plan, "the reading plan is every line from its summary on"
        assert "no Git write has been made" in lines[confirmed]
        assert "omnitient start git-check" in stdout
        assert all(
            re.match(r"(reading plan:|reader probe:|  probe line |page |long line )", l)
            for l in lines[plan:]
        )

    def test_the_recheck_passes_while_nothing_changed_and_writes_nothing(
        self, commit: Commit, target: Path
    ) -> None:
        token = commit.token()
        assert commit.handoff(token)[0] == 0
        before = git_snapshot(target)
        code, stdout, err = commit.recheck(token)
        assert code == 0, err
        assert "git preconditions unchanged since the preview" in stdout
        assert git_snapshot(target) == before

    def test_the_recheck_stops_the_first_write_when_the_checkout_changed_meanwhile(
        self, commit: Commit, target: Path
    ) -> None:
        token = commit.token()
        assert commit.handoff(token)[0] == 0
        (target / "src" / "app.py").write_text("print('meanwhile')\n", encoding="utf-8")
        code, _, err = commit.recheck(token)
        assert code == 1
        assert "start.changed" in err
        assert "checkout git state" in err
        assert out_of(target, "branch", "--list", BRANCH) == ""

    def test_the_recheck_is_valid_only_until_the_first_write(
        self, commit: Commit, target: Path
    ) -> None:
        token = commit.token()
        assert commit.recheck(token)[0] == 0
        git(target, "switch", "-q", "-c", BRANCH, "main")
        assert commit.recheck(token)[0] == 1

    def test_the_recheck_refuses_the_wrong_kind_of_token_and_other_directories(
        self, commit: Commit, run: Run, target: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        code, _, err = run(
            "start", "git-check", "--expect-preview", f"{PREVIEW_TAG}." + ".".join(["0" * 64] * 8),
            "--base", "main", "--branch", BRANCH,
        )  # fmt: skip
        assert code == 2
        assert "not a commit-mode token" in err
        token = commit.token()
        monkeypatch.chdir(tmp_path)
        code, _, err = commit.recheck(token, "--checkout", target)
        assert code == 1
        assert "start.checkout_not_cwd" in err

    def test_every_git_question_is_a_read_only_allowlisted_verb(
        self, commit: Commit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        real = subprocess.run

        def spy(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            calls.append(list(argv))
            env = kwargs["env"]
            assert isinstance(env, dict)
            assert env["GIT_OPTIONAL_LOCKS"] == "0"
            assert env["GIT_CONFIG_GLOBAL"] == os.devnull
            assert "core.hooksPath=" + os.devnull in argv
            assert "core.fsmonitor=false" in argv
            return real(argv, **kwargs)  # type: ignore[call-overload, no-any-return]

        monkeypatch.setattr(subprocess, "run", spy)
        token = commit.token()
        assert commit.handoff(token)[0] == 0
        assert commit.recheck(token)[0] == 0
        verbs = {argv[argv.index("-C") + 2] for argv in calls}
        assert calls
        assert verbs <= {
            "version", "rev-parse", "symbolic-ref", "for-each-ref", "status",
            "check-ref-format",
        }  # fmt: skip
        assert all(
            argv[1:3] == ["--no-optional-locks", "--no-replace-objects"]
            for argv in calls
        )


class TestEditModeIsUnchanged:
    """Callers of the current mode see what they saw."""

    def test_the_default_preview_does_not_observe_git_and_keeps_its_token(
        self, run: Run, write_package: Write, approved: WorkPackage, repo: Path,
        out: Path, target: Path,
    ) -> None:  # fmt: skip
        pkg = write_package(approved)
        before = git_snapshot(target)
        code, stdout, _ = run(
            "start", "preview", "--root", f"repo={repo}", "--out", out, "--", pkg
        )
        assert code == 0
        assert (
            "checkout checks: path, directory identity and a .git entry only; "
            "the branch, HEAD and local modifications are not observed" in stdout
        )
        assert "git commit mode" not in stdout
        assert "observed HEAD" not in stdout
        assert "commits, pushes and publication" in stdout
        token = TOKEN.search(stdout)
        assert token
        assert token[1].startswith(f"{PREVIEW_TAG}.")
        assert len(token[1].split(".")) == 9
        assert git_snapshot(target) == before

    def test_the_default_handoff_still_works_on_a_dirty_checkout(
        self, run: Run, write_package: Write, approved: WorkPackage, repo: Path,
        out: Path, target: Path,
    ) -> None:  # fmt: skip
        (target / "dirty.txt").write_text("x\n", encoding="utf-8")
        pkg = write_package(approved)
        _, stdout, _ = run(
            "start", "preview", "--root", f"repo={repo}", "--out", out, "--", pkg
        )
        token = TOKEN.search(stdout)
        assert token
        code, stdout, err = run(
            "start", "handoff", "--root", f"repo={repo}", "--expect-preview", token[1],
            "--out", out, "--", pkg,
        )  # fmt: skip
        assert code == 0, err
        assert "handoff delivered" in stdout
        assert "git commit mode" not in stdout

    def test_the_mode_options_must_agree(
        self, run: Run, write_package: Write, approved: WorkPackage, out: Path
    ) -> None:
        pkg = write_package(approved)
        code, _, err = run(
            "start", "preview", "--out", out, "--base", "main", "--", pkg
        )
        assert code == 2
        assert "belong to --mode commit" in err
        code, _, err = run(
            "start", "preview", "--out", out, "--mode", "commit", "--", pkg
        )
        assert code == 2
        assert "--mode commit needs --base and --branch" in err
        code, _, err = run(
            "start",
            "preview",
            "--out",
            out,
            "--mode",
            "commit",
            "--base",
            "main",
            "--",
            pkg,
        )
        assert code == 2
        assert "needs --branch" in err

    def test_tokens_of_one_mode_cannot_start_the_other(
        self, commit: Commit, run: Run, write_package: Write, approved: WorkPackage,
        repo: Path, out: Path,
    ) -> None:  # fmt: skip
        pkg = write_package(approved)
        commit_token = commit.token()
        code, _, err = run(
            "start", "handoff", "--root", f"repo={repo}", "--expect-preview", commit_token,
            "--out", out, "--", pkg,
        )  # fmt: skip
        assert code == 2
        assert "commit-mode token; pass --mode commit" in err
        _, stdout, _ = run(
            "start", "preview", "--root", f"repo={repo}", "--out", out, "--", pkg
        )
        edit_token = TOKEN.search(stdout)
        assert edit_token
        code, _, err = run(
            "start", "handoff", "--root", f"repo={repo}", "--expect-preview", edit_token[1],
            "--out", out, "--mode", "commit", "--base", "main", "--branch", BRANCH, "--", pkg,
        )  # fmt: skip
        assert code == 2
        assert "not a commit-mode token" in err
        assert not out.exists()

    def test_the_two_token_shapes_parse_apart(self) -> None:
        eight = ".".join([PREVIEW_TAG, *["a" * 64] * 8])
        eleven = ".".join([COMMIT_TAG, *["a" * 64] * 11])
        assert not start.parse_token(eight).commit_mode
        assert start.parse_token(eleven).commit_mode
        for bad in (
            ".".join([PREVIEW_TAG, *["a" * 64] * 11]),
            ".".join([COMMIT_TAG, *["a" * 64] * 8]),
            ".".join(["omnitient-start-1", *["a" * 64] * 8]),
        ):
            with pytest.raises(ValueError, match="not a preview token"):
                start.parse_token(bad)
        assert start.parse_token(eight).changed_from(start.parse_token(eleven)) == [
            "mode"
        ]


HOOK = """#!/bin/sh
# A stand-in for a formatter hook: it rewrites staged files in the work tree and
# rejects the commit, as pre-commit does when a hook changed something.
status=0
for file in $(git diff --cached --name-only --diff-filter=AM); do
  if grep -q ' $' "$file"; then
    sed -i 's/ *$//' "$file"
    status=1
  fi
done
exit $status
"""


class TestCarryThroughHookRewrite:
    """The authorized operation set, driven through a hook rewrite to one commit.

    The test plays the session: it runs the Git operations the preview listed, in
    the order listed, and the real Git program runs a real hook. The model's own
    behaviour is not exercised.
    """

    def test_a_hook_rewrite_is_inspected_restaged_and_committed_once(
        self, commit: Commit, target: Path
    ) -> None:
        base = out_of(target, "rev-parse", "HEAD")
        hook = target / ".git" / "hooks" / "pre-commit"
        hook.write_text(HOOK, encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

        token = commit.token()
        assert commit.handoff(token)[0] == 0
        assert commit.recheck(token)[0] == 0

        # 1. the branch action the owner was shown, at the base the owner was shown
        git(target, "switch", "-q", "-c", BRANCH, base)
        assert out_of(target, "rev-parse", "HEAD") == base

        # 2. the work: in-scope edit with trailing space, and an unrelated bystander
        scope = "src/feature.py"
        (target / scope).write_text("x = 1   \ny = 2\n", encoding="utf-8")
        git(target, "add", "--", scope)  # 3. explicit path, never a whole-tree add
        staged = out_of(target, "diff", "--cached", "--name-only")
        assert staged == scope

        # 4. the hook rewrites and rejects: no commit exists yet
        first = git(target, "commit", "-q", "-m", "Add feature", check=False)
        assert first.returncode != 0
        assert out_of(target, "rev-parse", "HEAD") == base, "no commit was made"
        assert git(target, "status", "--porcelain").stdout.splitlines() == [
            "AM src/feature.py"
        ], "the hook's rewrite is unstaged and must be inspected"
        rewrite = out_of(target, "diff", "--", scope)
        assert "-x = 1   " in rewrite
        assert "+x = 1" in rewrite
        assert (target / scope).read_text(encoding="utf-8") == "x = 1\ny = 2\n"

        # 5. restage only the intended path, with hooks still enabled, and commit
        git(target, "add", "--", scope)
        second = git(target, "commit", "-q", "-m", "Add feature", check=False)
        assert second.returncode == 0, second.stderr

        # the outcome: one non-empty commit on the branch, parent = the base, clean
        head = out_of(target, "rev-parse", "HEAD")
        assert FULL.fullmatch(head)
        assert head != base
        assert out_of(target, "rev-list", "--count", f"{base}..{head}") == "1"
        assert out_of(target, "rev-parse", f"{head}^") == base
        assert out_of(target, "rev-parse", f"refs/heads/{BRANCH}") == head
        assert out_of(target, "diff", "--name-only", f"{base}", head) == scope
        assert out_of(target, "show", f"{head}:{scope}") == "x = 1\ny = 2"
        assert (
            git(target, "status", "--porcelain", "--untracked-files=all").stdout == ""
        )
        assert out_of(target, "rev-parse", "main") == base, "main was not moved"


ROOT = Path(__file__).parent.parent
SKILL_TEXT = (ROOT / ".claude" / "skills" / "omnitient-start" / "SKILL.md").read_text(
    encoding="utf-8"
)
CONTRACT_TEXT = (ROOT / "docs" / "start-contract.md").read_text(encoding="utf-8")


def flat(text: str) -> str:
    return " ".join(text.split())


class TestCommitModeSkill:
    """What a static check of the skill's text and the contract can establish.

    Nothing here shows that a live session follows the procedure.
    """

    body = SKILL_TEXT.split("---", 2)[2]

    def test_the_edit_mode_text_is_still_there_and_still_forbids_commits(self) -> None:
        text = flat(self.body)
        assert "The current mode is `--mode edit`, the default" in text
        assert (
            "In edit mode do not commit, push, open a pull request or start a review."
            in text
        )
        assert "In edit mode follow 6.1 and never run a Git write." in text
        assert text.index("Wait for the owner") < text.index("start handoff")

    def test_it_asks_for_the_owners_choices_and_never_makes_them(self) -> None:
        text = flat(self.body)
        for required in (
            "`--base` and `--branch` are both required and are the owner's choice",
            "never pick a base or a branch name, and never fall back to the edit mode",
            "'--base=main'",
            "'--branch=feature/name'",
            "so a value that begins with a dash stays data",
        ):
            assert required in text, required

    def test_consent_covers_exactly_the_displayed_operations(self) -> None:
        text = flat(self.body)
        for required in (
            "where the commit lands, from what base and what the owner would be authorizing",
            "the resolved checkout, the observed branch and full HEAD",
            "the staged, unstaged and untracked state",
            "the base ref and its full commit ID",
            "the exact feature branch, the proposed branch action",
            "the revision is a content digest of the package and is not a Git object",
            "the base commit is a Git commit and is not the revision",
            "authorizes the displayed Git operations in the displayed order and nothing else",
            "scoped staging, hook-driven repairs and exactly one local commit",
        ):
            assert required in text, required
        assert self.body.index("Wait for the owner") < self.body.index("### 6.2")

    def test_a_withheld_preview_is_never_repaired_by_the_skill(self) -> None:
        text = flat(self.body)
        for required in (
            "a dirty checkout",
            "a base that does not resolve to a local commit",
            "a feature branch that exists elsewhere or cannot be created",
            "Do not clean, stash, reset, commit, switch or otherwise change the checkout",
        ):
            assert required in text, required

    def test_the_recheck_comes_after_reading_and_before_the_first_git_write(
        self,
    ) -> None:
        text = flat(self.body)
        assert (
            self.body.index("### 5.3 Read")
            < self.body.index("### 6.2")
            < self.body.index("### 6.3")
        )
        for required in (
            "after the reading procedure of step 5 is complete and reported, and before any edit",
            "Immediately before the first Git write, run `omnitient start git-check`",
            "It only reads.",
            "make no Git write and no edit",
            "go back to step 2 to preview again",
            "naming the full base commit ID from the preview and never the base ref",
            "`git switch -c BRANCH BASE_COMMIT`",
            "Afterwards check that HEAD is the base commit",
        ):
            assert required in text, required

    def test_it_states_the_repair_cycle_and_when_to_stop(self) -> None:
        text = flat(self.body)
        for required in (
            "Stage by explicit path only",
            "Never stage the whole tree",
            "commit with hooks enabled and never with `--no-verify`",
            "that is part of the authorized work and needs no new consent",
            "rerun the affected checks on the resulting content, restage only the intended paths",
            "Make exactly one local commit.",
            "Do not amend it",
            "when a failure is unrelated to the in-scope paths, when a Git or file operation is denied",
            "the state you kept",
            "Do not reset, stash, clean, force or bypass anything",
            "parent count confirmed as one and equal to the base",
            "`git status --porcelain --untracked-files=all` prints nothing",
            "that this session did not push, open a pull request, record an approval or start a review",
        ):
            assert required in text, required

    def test_the_success_report_requires_the_provenance_fields(self) -> None:
        """A check of the skill's instructions, not of what a session reports."""
        step = flat(self.body.split("### 6.3")[1])
        report = step[step.index("After the commit, verify") :]
        for required in (
            "the resolved checkout path",
            "the feature branch",
            "the full base SHA, labeled as the base",
            "the full candidate commit SHA (`git rev-parse HEAD`)",
            "the verified sole parent SHA (`git rev-parse HEAD^`",
            "parent count confirmed as one and equal to the base",
            (
                "the full candidate tree SHA, obtained from the committed candidate "
                "(`git rev-parse HEAD^{tree}`) and not from the index"
            ),
            "the commit count (`git rev-list --count`",
            "the changed paths (`git diff --name-status`",
            "the final index and work tree status with untracked files included",
            "each as a full value and not an abbreviation",
        ):
            assert required in report, required
        for required in (
            "every check with its exact command, its outcome and its runner",
            "run by this session after the final change or as quoted from elsewhere",
            "a quoted result is never presented as one this session ran",
        ):
            assert required in report.replace(
                "Report every check", "every check"
            ), required

    def test_the_contract_requires_the_same_provenance_fields(self) -> None:
        text = flat(CONTRACT_TEXT)
        section = text[text.index("### 4.5") : text.index("### 4.6")]
        for required in (
            "the resolved checkout path",
            "the feature branch",
            "the full base SHA labeled as the base",
            "the full candidate commit SHA",
            "the verified sole parent SHA",
            "the full candidate tree SHA obtained from the committed candidate",
            "the commit count",
            "the changed paths",
            "the final index and work tree status with untracked files included",
            "exact command, outcome and runner",
            "a result the session quotes is marked as quoted and kept apart from one it ran",
        ):
            assert required in section, required

    def test_it_pre_approves_nothing_new(self) -> None:
        meta = SKILL_TEXT.split("---", 2)[1]
        assert "allowed-tools: Bash(omnitient start preview *)" in meta
        assert "git" not in meta.split("allowed-tools:")[1].split("\n")[0]
        assert not re.findall(r"```", self.body)

    def test_the_contract_states_the_same_binding_and_limits(self) -> None:
        text = flat(CONTRACT_TEXT)
        for required in (
            "where will my commit land, from what base, and what am I authorizing?",
            "Omnitient never performs a Git write in either mode.",
            "The package revision and the Git base are two lines with two meanings.",
            "A go-ahead in the commit mode authorizes exactly these, in this order",
            "Not authorized: a push, fetch, pull request or publication",
            "A routine hook rewrite is not such a case: it is step 3.",
            "`omnitient-start-3.` followed by the eight values",
            "Preview, `handoff` and `git-check` never write to the repository; the session does.",
            "`TestCarryThroughHookRewrite` is a fixture",
            "Not exercised in a live session, because that needs a model",
        ):
            assert required in text, required

    def test_the_operations_the_contract_lists_are_the_ones_the_preview_prints(
        self,
    ) -> None:
        obs = start.GitObservation(
            checkout=Path("/x"), base_ref="main", branch_name=BRANCH, base="a" * 40
        )
        plan = start.CommitPlan("create", (), False)
        printed = start.commit_operations(obs, plan)
        assert len(printed) == 4
        text = flat(CONTRACT_TEXT)
        for phrase in (
            "restaging only the intended paths, repeating with hooks enabled",
            "no `--no-verify`, no amend, no second commit",
            "by explicit path",
        ):
            assert phrase in text
        assert (
            "restage only the intended paths, repeating with hooks enabled"
            in printed[2]
        )
        assert "(no --no-verify, no --amend, no second commit)" in printed[3]
