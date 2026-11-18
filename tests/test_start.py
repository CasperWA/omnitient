# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""``omnitient start`` and the project skill that wraps it (docs/start-contract.md)."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnitient import ops, render
from omnitient.cli import build_parser, main
from omnitient.context import build_host_mapping, resolve_context
from omnitient.model import Decision, WorkPackage
from omnitient.serialize import content_digest, dumps
from omnitient.start import HANDOFF_BEGIN, HANDOFF_END, root_relations
from omnitient.store import load_package

Run = Callable[..., tuple[int, str, str]]
Write = Callable[..., Path]

SKILL = Path(__file__).parent.parent / ".claude" / "skills" / "omnitient-start"


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Run:
    def invoke(*args: object) -> tuple[int, str, str]:
        code = main([str(a) for a in args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke


@pytest.fixture
def target(tmp_path: Path) -> Path:
    directory = tmp_path / "target"
    directory.mkdir()
    return directory


def expectations(pkg_file: Path, target: Path) -> list[str]:
    loaded = load_package(pkg_file)
    return [
        "--handoff",
        "--expect-revision",
        content_digest(loaded.package),
        "--expect-file-sha256",
        loaded.file_sha256,
        "--expect-checkout",
        str(target),
    ]


def test_preview_shows_what_the_owner_decides_on_and_no_handoff(
    run: Run, write_package: Write, approved: WorkPackage, repo: Path, target: Path
) -> None:
    pkg_file = write_package(approved)
    code, out, _ = run(
        "start", pkg_file, "--root", f"repo={repo}", "--checkout", target
    )
    assert code == 0
    loaded = load_package(pkg_file)
    for expected in (
        "package: demo/init/wp1",
        "title: Demo package",
        f"revision: {content_digest(approved)}",
        "implementation approval: current (A1, implementation) by user:alice",
        f"package file sha256: {loaded.file_sha256}",
        f"target checkout: {target.resolve()}",
        "checkout git marker: absent",
        "checkout relation to mapped roots: matches no mapped root",
        "    - Implement A",
        "    - Implement B",
        "    - No C",
        "enforcement: none by omnitient",
    ):
        assert expected in out
    assert HANDOFF_BEGIN not in out
    assert "Build the thing." not in out


def test_handoff_is_exactly_the_renderers_ready_output(
    run: Run, write_package: Write, approved: WorkPackage, repo: Path, target: Path
) -> None:
    pkg_file = write_package(approved)
    roots = ["--root", f"repo={repo}"]
    code, out, _ = run(
        "start", pkg_file, *roots, "--checkout", target, *expectations(pkg_file, target)
    )
    assert code == 0
    mapping = build_host_mapping([f"repo={repo}"])
    handoff = render.build_handoff(
        approved, resolve_context(approved, mapping), "implementation"
    )
    document = render.render_format(handoff, "md").decode()
    assert out.endswith(f"{HANDOFF_BEGIN}\n{document}{HANDOFF_END}\n")
    code, direct, _ = run(
        "render", pkg_file, "--stage", "implementation", "--out", "-", "--format", "md",
        "--root", f"repo={repo}",
    )  # fmt: skip
    assert code == 0
    assert direct == document
    assert "READY" in document


def test_preview_defaults_to_the_working_directory(
    run: Run,
    write_package: Write,
    approved: WorkPackage,
    repo: Path,
    target: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg_file = write_package(approved)
    monkeypatch.chdir(target)
    code, out, _ = run("start", pkg_file, "--root", f"repo={repo}")
    assert code == 0
    assert f"target checkout: {target.resolve()}" in out


class TestBlocked:
    """A blocked package yields a refusal and nothing an agent could act on."""

    def assert_no_work(self, out: str, err: str) -> None:
        assert HANDOFF_BEGIN not in out
        assert "begin ===" not in out
        for text in (
            "Build the thing.",
            "Implement A",
            "Implement B",
            "No C",
            "The spec says",
            "proposed write scope: withheld",
        ):
            if text.startswith("proposed"):
                assert text in out
            else:
                assert text not in out
                assert text not in err

    @pytest.mark.parametrize("handoff", [False, True])
    def test_missing_approval(
        self,
        run: Run,
        write_package: Write,
        package: WorkPackage,
        repo: Path,
        target: Path,
        handoff: bool,
    ) -> None:
        pkg_file = write_package(package)
        extra = expectations(pkg_file, target) if handoff else []
        code, out, err = run(
            "start", pkg_file, "--root", f"repo={repo}", "--checkout", target, *extra
        )
        assert code == 3
        assert "status: BLOCKED" in out
        assert "blocker[approval.missing]" in out
        self.assert_no_work(out, err)

    def test_stale_approval(
        self,
        run: Run,
        write_package: Write,
        approved: WorkPackage,
        repo: Path,
        target: Path,
    ) -> None:
        pkg_file = write_package(replace(approved, objective="A different goal."))
        code, out, err = run(
            "start", pkg_file, "--root", f"repo={repo}", "--checkout", target
        )
        assert code == 3
        assert "blocker[approval.stale]" in out
        assert "implementation approval: stale" in out
        self.assert_no_work(out, err)

    @pytest.mark.parametrize("handoff", [False, True])
    def test_unresolved_root(
        self,
        run: Run,
        write_package: Write,
        approved: WorkPackage,
        target: Path,
        handoff: bool,
    ) -> None:
        pkg_file = write_package(approved)
        extra = expectations(pkg_file, target) if handoff else []
        code, out, err = run("start", pkg_file, "--checkout", target, *extra)
        assert code == 3
        assert "blocker[context.root_unmapped]" in out
        self.assert_no_work(out, err)

    def test_context_drift(
        self,
        run: Run,
        write_package: Write,
        approved: WorkPackage,
        repo: Path,
        target: Path,
    ) -> None:
        (repo / "docs" / "spec.md").write_text("changed\n", encoding="utf-8")
        pkg_file = write_package(approved)
        code, out, err = run(
            "start", pkg_file, "--root", f"repo={repo}", "--checkout", target
        )
        assert code == 3
        assert "blocker[context.digest_mismatch]" in out
        self.assert_no_work(out, err)

    def test_unresolved_decision(
        self,
        run: Run,
        write_package: Write,
        approved: WorkPackage,
        unresolved: Decision,
        repo: Path,
        target: Path,
    ) -> None:
        pkg = replace(approved, decisions=(*approved.decisions, unresolved))
        pkg_file = write_package(pkg)
        code, out, err = run(
            "start", pkg_file, "--root", f"repo={repo}", "--checkout", target
        )
        assert code == 3
        assert "blocker[decision.unresolved]" in out
        self.assert_no_work(out, err)


class TestChangeSincePreview:
    """What was shown must still be what is found, or nothing starts."""

    def start(
        self, run: Run, pkg_file: Path, repo: Path, checkout: Path, *extra: str
    ) -> tuple[int, str, str]:
        return run(
            "start", pkg_file, "--root", f"repo={repo}", "--checkout", checkout, *extra
        )

    def test_wrong_checkout_is_surfaced_and_nothing_starts(
        self,
        run: Run,
        write_package: Write,
        approved: WorkPackage,
        repo: Path,
        target: Path,
        tmp_path: Path,
    ) -> None:
        pkg_file = write_package(approved)
        other = tmp_path / "other checkout"
        other.mkdir()
        shown = expectations(pkg_file, target)
        code, out, err = self.start(run, pkg_file, repo, other, *shown)
        assert code == 1
        assert "error[start.changed]" in err
        assert f"was {target.resolve()} and is now {other.resolve()}" in err
        assert out == ""

    def test_changed_package_file_is_surfaced(
        self,
        run: Run,
        write_package: Write,
        approved: WorkPackage,
        repo: Path,
        target: Path,
    ) -> None:
        pkg_file = write_package(approved)
        shown = expectations(pkg_file, target)
        # Evidence is outside the revision, so only the file hash can tell.
        code, _, _ = run(
            "evidence", "add", pkg_file, "--kind", "commit", "--ref", "abcdef1",
            "--by", "user:alice",
        )  # fmt: skip
        assert code == 0
        code, out, err = self.start(run, pkg_file, repo, target, *shown)
        assert code == 1
        assert "package file sha256 was" in err
        assert "revision was" not in err
        assert out == ""

    def test_changed_revision_is_surfaced(
        self,
        run: Run,
        write_package: Write,
        approved: WorkPackage,
        repo: Path,
        target: Path,
    ) -> None:
        pkg_file = write_package(approved)
        shown = expectations(pkg_file, target)
        write_package(replace(approved, objective="Something else."))
        code, out, err = self.start(run, pkg_file, repo, target, *shown)
        assert code == 1
        assert "revision was" in err
        assert out == ""

    def test_unchanged_start_succeeds(
        self,
        run: Run,
        write_package: Write,
        approved: WorkPackage,
        repo: Path,
        target: Path,
    ) -> None:
        pkg_file = write_package(approved)
        shown = expectations(pkg_file, target)
        code, out, _ = self.start(run, pkg_file, repo, target, *shown)
        assert code == 0
        assert HANDOFF_BEGIN in out


class TestCheckout:
    def test_missing_checkout_is_refused(
        self, run: Run, write_package: Write, approved: WorkPackage, tmp_path: Path
    ) -> None:
        code, out, err = run(
            "start", write_package(approved), "--checkout", tmp_path / "absent"
        )
        assert code == 1
        assert "error[start.checkout]" in err
        assert out == ""

    def test_file_as_checkout_is_refused(
        self, run: Run, write_package: Write, approved: WorkPackage
    ) -> None:
        pkg_file = write_package(approved)
        code, _, err = run("start", pkg_file, "--checkout", pkg_file)
        assert code == 1
        assert "not a directory" in err

    def test_git_internals_are_not_a_checkout(
        self, run: Run, write_package: Write, approved: WorkPackage, target: Path
    ) -> None:
        inner = target / ".git" / "hooks"
        inner.mkdir(parents=True)
        code, out, err = run("start", write_package(approved), "--checkout", inner)
        assert code == 1
        assert "'.git'" in err
        assert out == ""

    def test_git_marker_is_reported_but_is_not_isolation(
        self, run: Run, write_package: Write, approved: WorkPackage, repo: Path
    ) -> None:
        (repo / ".git").mkdir()
        code, out, _ = run(
            "start", write_package(approved), "--root", f"repo={repo}",
            "--checkout", repo,
        )  # fmt: skip
        assert code == 0
        assert "checkout git marker: present" in out
        assert "checkout relation to mapped roots: is the mapped root 'repo'" in out
        assert "a Git directory alone is no isolation" in out

    def test_package_file_inside_the_target_is_said_so(
        self, run: Run, write_package: Write, approved: WorkPackage, repo: Path
    ) -> None:
        pkg_file = write_package(approved)
        code, out, _ = run(
            "start", pkg_file, "--root", f"repo={repo}", "--checkout", pkg_file.parent
        )
        assert code == 0
        assert "package file inside target checkout: yes" in out

    def test_relations_to_mapped_roots(self, tmp_path: Path) -> None:
        outer, inner = tmp_path / "outer", tmp_path / "outer" / "inner"
        stray = tmp_path / "stray"
        roots = {"outer": outer, "inner": inner}
        assert root_relations(outer, roots) == [
            "contains the mapped root 'inner'",
            "is the mapped root 'outer'",
        ]
        assert root_relations(inner, roots) == [
            "is the mapped root 'inner'",
            "lies inside the mapped root 'outer'",
        ]
        assert root_relations(stray, roots) == ["matches no mapped root"]
        assert root_relations(stray, {}) == ["matches no mapped root"]


class TestUsage:
    def test_handoff_needs_all_expectations(
        self, run: Run, write_package: Write, approved: WorkPackage
    ) -> None:
        code, out, err = run("start", write_package(approved), "--handoff")
        assert code == 2
        assert out == ""
        assert "--expect-revision" in err

    def test_expectations_without_handoff_are_refused(
        self, run: Run, write_package: Write, approved: WorkPackage
    ) -> None:
        code, out, _ = run(
            "start", write_package(approved), "--expect-revision", "a" * 64
        )
        assert code == 2
        assert out == ""

    @pytest.mark.parametrize("bad", ["abc", "A" * 64, ("a" * 64) + "\n", ""])
    def test_malformed_digests_are_refused(
        self,
        run: Run,
        write_package: Write,
        approved: WorkPackage,
        target: Path,
        bad: str,
    ) -> None:
        pkg_file = write_package(approved)
        shown = expectations(pkg_file, target)
        shown[shown.index("--expect-revision") + 1] = bad
        code, out, _ = run("start", pkg_file, *shown)
        assert code == 2
        assert out == ""


class TestArgumentsAreData:
    """Spaces and shell metacharacters reach Omnitient as plain text.

    These run the real console entry point in a child process, through the same
    quoting the skill prescribes, and check that nothing in a path or in package
    text is ever run.
    """

    NAME = "my pkg; touch PWNED1 $(touch PWNED2) `touch PWNED3` 'q' \"dq\" *"

    def omnitient(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        command = " ".join(
            [shlex.quote(sys.executable), "-m", "omnitient", *map(shlex.quote, args)]
        )
        return subprocess.run(
            ["/bin/bash", "-c", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_hostile_paths_and_package_text_are_never_executed(
        self, tmp_path: Path, package: WorkPackage, repo: Path
    ) -> None:
        work = tmp_path / "work"
        directory = work / self.NAME
        directory.mkdir(parents=True)
        hostile = "echo $(touch PWNED4) `touch PWNED5`; touch PWNED6"
        pkg = ops.add_approval(
            replace(
                package,
                title="Title $(touch PWNED7)",
                objective=hostile,
                scope=(hostile,),
                exclusions=(hostile,),
            ),
            stage="implementation",
            approved_by="user:alice",
            note=None,
            at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
        (directory / "work-package.json").write_bytes(dumps(pkg))
        checkout = work / self.NAME / "checkout dir; touch PWNED8"
        checkout.mkdir()
        mapped = f"repo={repo}"

        preview = self.omnitient(
            work, "start", "--root", mapped, "--checkout", str(checkout),
            "--", self.NAME,
        )  # fmt: skip
        assert preview.returncode == 0, preview.stderr
        assert f"target checkout: {checkout.resolve()}" in preview.stdout
        assert "Title $(touch PWNED7)" in preview.stdout
        assert f"    - {hostile}" in preview.stdout

        loaded = load_package(directory)
        start = self.omnitient(
            work, "start", "--root", mapped, "--checkout", str(checkout),
            "--handoff",
            "--expect-revision", content_digest(loaded.package),
            "--expect-file-sha256", loaded.file_sha256,
            "--expect-checkout", str(checkout),
            "--", self.NAME,
        )  # fmt: skip
        assert start.returncode == 0, start.stderr
        assert HANDOFF_BEGIN in start.stdout
        assert hostile in start.stdout

        assert sorted(p.name for p in work.iterdir()) == [self.NAME]
        assert not list(tmp_path.glob("PWNED*"))
        assert not list(work.glob("PWNED*"))

    def test_a_path_that_looks_like_an_option_needs_the_separator(
        self, tmp_path: Path, approved: WorkPackage, repo: Path
    ) -> None:
        directory = tmp_path / "-pkg"
        directory.mkdir()
        (directory / "work-package.json").write_bytes(dumps(approved))
        root = f"repo={repo}"
        refused = self.omnitient(tmp_path, "start", "--root", root, "-pkg")
        assert refused.returncode == 2
        accepted = self.omnitient(tmp_path, "start", "--root", root, "--", "-pkg")
        assert accepted.returncode == 0, accepted.stderr


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    head, _, body = text.removeprefix("---\n").partition("\n---\n")
    pairs = (line.split(": ", 1) for line in head.splitlines())
    return dict(pairs), body


class TestSkill:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    def test_it_is_a_project_skill_only_the_owner_can_invoke(self) -> None:
        meta, _ = frontmatter(self.text)
        assert SKILL.parent.name == "skills"
        assert SKILL.parent.parent.name == ".claude"
        assert meta["name"] == SKILL.name == "omnitient-start"
        assert meta["disable-model-invocation"] == "true"
        assert meta["description"]

    def test_the_only_pre_approved_tool_is_the_read_only_start_command(self) -> None:
        meta, _ = frontmatter(self.text)
        assert meta["allowed-tools"] == "Bash(omnitient start *)"

    def test_arguments_are_prose_never_shell_injection(self) -> None:
        _, body = frontmatter(self.text)
        assert body.count("$ARGUMENTS") == 1
        fenced = re.findall(r"```.*?```", body, flags=re.DOTALL)
        assert not fenced
        # Dynamic context injection would run package arguments as shell text.
        assert not re.search(r"!`|^```!", body, flags=re.MULTILINE)

    def test_every_flag_it_names_exists_or_is_one_it_forbids(self) -> None:
        subparsers = next(
            a for a in build_parser()._actions if hasattr(a, "choices") and a.choices
        )
        start_flags = set(subparsers.choices["start"]._option_string_actions)
        named = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", self.text))
        assert named - start_flags == {"--allow-blocked"}
        assert {"--handoff", "--expect-revision", "--expect-file-sha256"} <= named

    def test_it_keeps_the_owner_at_the_transition_to_edits(self) -> None:
        _, body = frontmatter(self.text)
        assert "Wait for the owner" in body
        assert "before they say so" in body
        assert "start.changed" in body
        assert "approve" in body
        assert "on anyone's behalf" in body
