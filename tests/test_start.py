# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""``omnitient start`` and the project skill that wraps it (docs/start-contract.md)."""

from __future__ import annotations

import fnmatch
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import NOTES_TEXT, SPEC_TEXT
from omnitient import cli, ops, render, start
from omnitient.cli import build_parser, main
from omnitient.context import build_host_mapping, resolve_context
from omnitient.model import Decision, WorkPackage
from omnitient.serialize import content_digest, dumps, sha256_hex
from omnitient.start import HANDOFF_FILE, Facts
from omnitient.store import load_package, read_bundle

Run = Callable[..., tuple[int, str, str]]
Write = Callable[..., Path]

ROOT = Path(__file__).parent.parent
SKILL = ROOT / ".claude" / "skills" / "omnitient-start"
TOKEN = re.compile(r"^preview token: (\S+)$", re.MULTILINE)


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Run:
    def invoke(*args: object) -> tuple[int, str, str]:
        code = main([str(a) for a in args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke


@pytest.fixture(autouse=True)
def target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The target checkout, which is also the working directory, as in a session.

    Every test starts there, as a session does.
    """
    directory = tmp_path / "target"
    directory.mkdir()
    monkeypatch.chdir(directory)
    return directory


@pytest.fixture
def out(tmp_path: Path) -> Path:
    """A handoff directory that does not exist yet and is outside the target."""
    return tmp_path / "handoff-out"


def approve(pkg: WorkPackage) -> WorkPackage:
    return ops.add_approval(
        pkg,
        stage="implementation",
        approved_by="user:alice",
        note=None,
        at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )


class Session:
    """Preview, then start, the way the skill does."""

    def __init__(self, run: Run, pkg_file: Path, repo: Path) -> None:
        self.run, self.pkg_file, self.repo = run, pkg_file, repo

    def preview(self, *extra: object) -> tuple[int, str, str]:
        return self.run(
            "start", "preview", "--root", f"repo={self.repo}", *extra, "--",
            self.pkg_file,
        )  # fmt: skip

    def token(self, *extra: object) -> str:
        code, out, err = self.preview(*extra)
        assert code == 0, err
        match = TOKEN.search(out)
        assert match, out
        return match.group(1)

    def handoff(self, token: str, out: Path, *extra: object) -> tuple[int, str, str]:
        return self.run(
            "start", "handoff", "--root", f"repo={self.repo}", "--expect-preview",
            token, "--out", out, *extra, "--", self.pkg_file,
        )  # fmt: skip


@pytest.fixture
def session(
    run: Run, write_package: Write, approved: WorkPackage, repo: Path
) -> Session:
    return Session(run, write_package(approved), repo)


def ready_document(approved: WorkPackage, repo: Path) -> bytes:
    mapping = build_host_mapping([f"repo={repo}"])
    handoff = render.build_handoff(
        approved, resolve_context(approved, mapping), "implementation"
    )
    return render.render_format(handoff, "md")


class TestPreview:
    def test_shows_what_the_owner_decides_on_and_no_handoff(
        self, session: Session, approved: WorkPackage, target: Path, tmp_path: Path
    ) -> None:
        before = sorted(tmp_path.rglob("*"))
        code, out, _ = session.preview()
        assert code == 0
        loaded = load_package(session.pkg_file)
        for expected in (
            "status: READY",
            "package: demo/init/wp1",
            "title: Demo package",
            f"revision: {content_digest(approved)}",
            "implementation approval: current (A1, implementation) by user:alice",
            f"package file sha256: {loaded.file_sha256}",
            f"    repo = {session.repo.resolve()}",
            f"target checkout: {target.resolve()}",
            "target checkout identity: device",
            "and so of the session: yes",
            "the branch, HEAD and local modifications are not observed",
            "checkout git entry: absent",
            "may differ from the target): none",
            "    - Implement A",
            "    - No C",
            "enforcement: none by omnitient",
            f"handoff renders: {len(ready_document(approved, session.repo))} bytes",
        ):
            assert expected in out
        assert TOKEN.search(out)
        assert "Build the thing." not in out
        assert "The spec says" not in out
        assert sorted(tmp_path.rglob("*")) == before

    def test_lists_which_inputs_are_embedded_or_referenced(
        self, run: Run, write_package: Write, package: WorkPackage, repo: Path,
    ) -> None:  # fmt: skip
        spec, notes = package.context
        gone = replace(notes, id="gone", path="docs/missing.md", mode="reference")
        pkg = approve(
            replace(package, context=(spec, replace(notes, mode="reference"), gone))
        )
        (repo / "docs" / "notes.md").write_text("drifted\n", encoding="utf-8")
        code, out, _ = Session(run, write_package(pkg), repo).preview()
        assert code == 0, out
        assert "    - spec repo:docs/spec.md embed required: embedded" in out
        assert "notes repo:docs/notes.md reference optional: not embedded" in out
        assert "gone repo:docs/missing.md reference optional: not embedded" in out
        assert "The spec says" not in out

    def test_a_reference_mode_input_is_listed_as_referenced(
        self, run: Run, write_package: Write, package: WorkPackage, repo: Path,
    ) -> None:  # fmt: skip
        spec = replace(package.context[0], mode="reference")
        pkg = approve(replace(package, context=(spec,)))
        code, out, _ = Session(run, write_package(pkg), repo).preview()
        assert code == 0
        assert "referenced (path and digest only)" in out

    def test_a_target_that_is_not_the_working_directory_is_shown_and_not_startable(
        self, session: Session, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        code, out, _ = session.preview("--checkout", other)
        assert code == 0
        assert "and so of the session: NO" in out
        assert "open a session there and preview again" in out

    def test_a_root_that_differs_from_the_target_is_information_not_a_refusal(
        self, session: Session
    ) -> None:
        code, out, _ = session.preview()
        assert code == 0
        assert "context roots may differ from the target): none" in out

    def test_a_render_that_cannot_succeed_is_not_previewed_as_ready(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(render, "MAX_OUTPUT_BYTES", 100)
        code, out, err = session.preview()
        assert code == 1
        assert "output.too_large" in err
        assert "status: READY" not in out


class TestBlocked:
    """A blocked package yields a refusal and nothing an agent could act on."""

    def assert_no_work(self, out: str, err: str) -> None:
        assert "preview token" not in out
        assert "proposed write scope: withheld" in out
        for text in (
            "Build the thing.",
            "Implement A",
            "Implement B",
            "No C",
            "The spec says",
            "declared inputs",
        ):
            assert text not in out
            assert text not in err

    def test_missing_approval(
        self, run: Run, write_package: Write, package: WorkPackage, repo: Path,
    ) -> None:  # fmt: skip
        code, out, err = Session(run, write_package(package), repo).preview()
        assert code == 3
        assert "status: BLOCKED" in out
        assert "blocker[approval.missing]" in out
        self.assert_no_work(out, err)

    def test_stale_approval(
        self, run: Run, write_package: Write, approved: WorkPackage, repo: Path,
    ) -> None:  # fmt: skip
        pkg_file = write_package(replace(approved, objective="A different goal."))
        code, out, err = Session(run, pkg_file, repo).preview()
        assert code == 3
        assert "blocker[approval.stale]" in out
        assert "implementation approval: stale" in out
        self.assert_no_work(out, err)

    def test_unresolved_root(
        self, run: Run, write_package: Write, approved: WorkPackage
    ) -> None:
        code, out, err = run("start", "preview", "--", write_package(approved))
        assert code == 3
        assert "blocker[context.root_unmapped]" in out
        self.assert_no_work(out, err)

    def test_context_drift_before_start_blocks_the_handoff_and_writes_nothing(
        self, session: Session, out: Path
    ) -> None:
        token = session.token()
        (session.repo / "docs" / "spec.md").write_text("changed\n", encoding="utf-8")
        code, stdout, err = session.handoff(token, out)
        assert code == 3
        assert "blocker[context.digest_mismatch]" in stdout
        self.assert_no_work(stdout, err)
        assert not out.exists()

    def test_unresolved_decision(
        self, run: Run, write_package: Write, approved: WorkPackage,
        unresolved: Decision, repo: Path,
    ) -> None:  # fmt: skip
        pkg = replace(approved, decisions=(*approved.decisions, unresolved))
        code, out, err = Session(run, write_package(pkg), repo).preview()
        assert code == 3
        assert "blocker[decision.unresolved]" in out
        self.assert_no_work(out, err)


class TestHandoff:
    def test_writes_the_renderers_ready_output_and_verifies_it(
        self, session: Session, approved: WorkPackage, out: Path
    ) -> None:
        code, stdout, err = session.handoff(session.token(), out)
        assert code == 0, err
        document = ready_document(approved, session.repo)
        assert (out / HANDOFF_FILE).read_bytes() == document
        bundle = read_bundle(out, "handoff-implementation.meta.json")
        assert bundle.files == {HANDOFF_FILE: document}
        assert f"handoff bytes: {len(document)}" in stdout
        assert f"handoff newline count: {document.count(b'\n')}" in stdout
        assert f"handoff sha256: {sha256_hex(document)}" in stdout
        assert "handoff file:" in stdout
        assert "begin ===" not in stdout
        # It is the document `render` produces.
        code, direct, _ = session.run(
            "render", session.pkg_file, "--stage", "implementation", "--out", "-",
            "--format", "md", "--root", f"repo={session.repo}",
        )  # fmt: skip
        assert code == 0
        assert direct.encode() == document

    def test_the_handoff_directory_must_be_outside_the_target(
        self, session: Session, target: Path
    ) -> None:
        token = session.token()
        for inside in (target / "handoff", target):
            code, stdout, err = session.handoff(token, inside)
            assert code == 1
            assert "error[start.out_inside_checkout]" in err
            assert stdout == ""
        assert list(target.iterdir()) == []

    def test_an_existing_bundle_is_never_replaced(
        self, session: Session, out: Path
    ) -> None:
        token = session.token()
        assert session.handoff(token, out)[0] == 0
        before = (out / HANDOFF_FILE).read_bytes()
        code, _, err = session.handoff(token, out)
        assert code == 1
        assert "output.exists" in err
        assert (out / HANDOFF_FILE).read_bytes() == before

    def test_a_target_that_is_not_the_working_directory_is_refused(
        self, session: Session, tmp_path: Path, out: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        token = session.token("--checkout", other)
        code, stdout, err = session.handoff(token, out, "--checkout", other)
        assert code == 1
        assert "error[start.checkout_not_cwd]" in err
        assert stdout == ""
        assert not out.exists()

    def test_a_bundle_that_does_not_verify_is_not_reported_as_delivered(
        self, session: Session, out: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = session.token()
        real = cli.read_bundle

        def tampered(directory: Path, name: str) -> object:
            (directory / HANDOFF_FILE).write_bytes(b"truncated")
            return real(directory, name)

        monkeypatch.setattr(cli, "read_bundle", tampered)
        code, stdout, err = session.handoff(token, out)
        assert code == 1
        assert "output.conflict" in err
        assert "handoff delivered" not in stdout

    def test_a_render_over_the_bound_is_refused_before_anything_is_written(
        self, session: Session, out: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = session.token()
        monkeypatch.setattr(render, "MAX_OUTPUT_BYTES", 100)
        code, stdout, err = session.handoff(token, out)
        assert code == 1
        assert "output.too_large" in err
        assert "handoff delivered" not in stdout
        assert not out.exists()


class TestChangeSincePreview:
    """What was confirmed must still be what is found, or nothing starts."""

    def assert_refused(
        self, result: tuple[int, str, str], what: set[str], out: Path
    ) -> None:
        code, stdout, err = result
        assert code == 1
        assert "error[start.changed]" in err
        assert {name for name in start.FACT_NAMES if name in err} == what
        assert stdout == ""
        assert not out.exists()

    def test_nothing_changed_starts(self, session: Session, out: Path) -> None:
        assert session.handoff(session.token(), out)[0] == 0

    def test_a_changed_package_file_that_the_revision_does_not_cover(
        self, session: Session, out: Path
    ) -> None:
        token = session.token()
        code, _, _ = session.run(
            "evidence", "add", session.pkg_file, "--kind", "commit", "--ref",
            "abcdef1", "--by", "user:alice",
        )  # fmt: skip
        assert code == 0
        self.assert_refused(session.handoff(token, out), {"package file contents"}, out)

    def test_a_changed_revision(
        self, session: Session, approved: WorkPackage, write_package: Write, out: Path
    ) -> None:
        token = session.token()
        write_package(replace(approved, objective="Something else."))
        self.assert_refused(
            session.handoff(token, out), {"revision", "package file contents"}, out
        )

    def test_an_identical_copy_of_the_package_elsewhere(
        self, session: Session, tmp_path: Path, out: Path
    ) -> None:
        token = session.token()
        copy = tmp_path / "copy"
        copy.mkdir()
        shutil.copy(session.pkg_file, copy / "work-package.json")
        session.pkg_file = copy / "work-package.json"
        self.assert_refused(session.handoff(token, out), {"package file location"}, out)

    def test_an_alternate_root_mapping_is_caught_before_any_context_is_read(
        self,
        session: Session,
        tmp_path: Path,
        out: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = session.token()
        twin = tmp_path / "twin"
        shutil.copytree(session.repo, twin)  # identical contents, other mapping

        def forbidden(*_a: object, **_k: object) -> None:
            raise AssertionError("context was read before the expectations matched")

        monkeypatch.setattr(cli, "resolve_context", forbidden)
        session.repo = twin
        self.assert_refused(session.handoff(token, out), {"root mappings"}, out)

    def test_an_extra_root_mapping(
        self, session: Session, tmp_path: Path, out: Path
    ) -> None:
        token = session.token()
        extra = tmp_path / "extra"
        extra.mkdir()
        result = session.handoff(token, out, "--root", f"more={extra}")
        self.assert_refused(result, {"root mappings"}, out)

    def test_a_different_checkout(
        self,
        session: Session,
        tmp_path: Path,
        out: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = session.token()
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(other)
        self.assert_refused(
            session.handoff(token, out),
            {"target checkout location", "target checkout identity"},
            out,
        )

    def test_a_directory_replaced_at_the_same_path(
        self,
        session: Session,
        target: Path,
        tmp_path: Path,
        out: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = session.token()
        monkeypatch.chdir(tmp_path)
        # The old directory stays, so the new one cannot reuse its inode number.
        target.rename(tmp_path / "moved")
        target.mkdir()
        monkeypatch.chdir(target)
        self.assert_refused(
            session.handoff(token, out), {"target checkout identity"}, out
        )

    def test_a_previewed_name_that_became_a_link_to_another_checkout(
        self,
        session: Session,
        target: Path,
        tmp_path: Path,
        out: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = session.token("--checkout", target)
        monkeypatch.chdir(tmp_path)
        target.rename(tmp_path / "moved")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        target.symlink_to(elsewhere, target_is_directory=True)
        monkeypatch.chdir(target)
        # Re-resolving the expected path would now name the same directory as
        # the actual one and accept this; the confirmed location is compared.
        result = session.handoff(token, out, "--checkout", target)
        self.assert_refused(
            result, {"target checkout location", "target checkout identity"}, out
        )

    def test_a_token_carries_fingerprints_and_no_paths(self, session: Session) -> None:
        token = session.token()
        facts = start.parse_token(token)
        assert len(facts.values) == len(start.FACT_NAMES)
        assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in facts.values)
        assert str(session.pkg_file.parent) not in token
        assert facts.changed_from(facts) == []

    def test_facts_name_exactly_what_differs(self) -> None:
        base = Facts(tuple(str(i) * 64 for i in range(6)))
        other = Facts(("a" * 64, *base.values[1:5], "b" * 64))
        assert other.changed_from(base) == [start.FACT_NAMES[0], start.FACT_NAMES[5]]
        assert start.parse_token(base.token) == base


class TestCheckout:
    def test_missing_checkout_is_refused_and_its_name_is_escaped(
        self, run: Run, write_package: Write, approved: WorkPackage, tmp_path: Path
    ) -> None:
        hostile = tmp_path / "gone\nerror[start.checkout] forged\x1b[31m"
        code, out, err = run(
            "start", "preview", "--checkout", hostile, "--", write_package(approved)
        )
        assert code == 1
        assert out == ""
        assert "\x1b" not in err
        errors = [ln for ln in err.splitlines() if ln.startswith("error[")]
        assert len(errors) == 1

    def test_file_as_checkout_is_refused(
        self, run: Run, write_package: Write, approved: WorkPackage
    ) -> None:
        pkg_file = write_package(approved)
        code, _, err = run("start", "preview", "--checkout", pkg_file, "--", pkg_file)
        assert code == 1
        assert "not a directory" in err

    def test_git_internals_are_not_a_checkout(
        self, run: Run, write_package: Write, approved: WorkPackage, target: Path
    ) -> None:
        inner = target / ".git" / "hooks"
        inner.mkdir(parents=True)
        code, out, err = run(
            "start", "preview", "--checkout", inner, "--", write_package(approved)
        )
        assert code == 1
        assert "'.git'" in err
        assert out == ""

    def test_git_entry_is_reported_but_is_not_isolation(
        self, run: Run, write_package: Write, approved: WorkPackage, repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        (repo / ".git").mkdir()
        monkeypatch.chdir(repo)
        code, out, _ = Session(run, write_package(approved), repo).preview()
        assert code == 0
        assert "checkout git entry: present" in out
        assert "is the mapped root 'repo'" in out
        assert "a Git directory alone is no isolation" in out

    def test_package_file_inside_the_target_is_said_so(self, session: Session) -> None:
        code, out, _ = session.preview("--checkout", session.pkg_file.parent)
        assert code == 0
        assert "package file inside target checkout: yes" in out

    def test_relations_to_mapped_roots(self, tmp_path: Path) -> None:
        outer, inner = tmp_path / "outer", tmp_path / "outer" / "inner"
        stray = tmp_path / "stray"
        roots = {"outer": outer, "inner": inner}
        assert start.root_relations(outer, roots) == [
            "contains the mapped root 'inner'",
            "is the mapped root 'outer'",
        ]
        assert start.root_relations(inner, roots) == [
            "is the mapped root 'inner'",
            "lies inside the mapped root 'outer'",
        ]
        assert start.root_relations(stray, roots) == ["none"]
        assert start.root_relations(stray, {}) == ["none"]


class TestDisplayIsEscaped:
    """Host paths are data: they cannot forge a line of the preview."""

    FORGED = "forged: yes"

    def test_paths_with_newlines_and_escapes_cannot_forge_lines(
        self,
        run: Run,
        approved: WorkPackage,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hostile = f"x\n{self.FORGED}\x1b[2J"
        checkout = tmp_path / f"checkout {hostile}"
        checkout.mkdir()
        monkeypatch.chdir(checkout)
        repo = tmp_path / f"repo {hostile}"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "spec.md").write_text(SPEC_TEXT, encoding="utf-8")
        (repo / "docs" / "notes.md").write_text(NOTES_TEXT, encoding="utf-8")
        pkg_dir = tmp_path / f"pkg {hostile}"
        pkg_dir.mkdir()
        (pkg_dir / "work-package.json").write_bytes(dumps(approved))
        code, out, err = run(
            "start", "preview", "--root", f"repo={repo}", "--", pkg_dir
        )
        assert code == 0, err
        lines = out.splitlines()
        assert not [ln for ln in lines if ln.startswith("forged")]
        assert self.FORGED in out  # present, but only inside an escaped line
        assert "\x1b" not in out
        assert "\\x0a" in out
        assert "\\x1b" in out


class TestUsage:
    def test_handoff_needs_a_token_and_a_directory(
        self, write_package: Write, approved: WorkPackage, out: Path
    ) -> None:
        pkg_file = str(write_package(approved))
        with pytest.raises(SystemExit) as raised:
            main(["start", "handoff", "--out", str(out), "--", pkg_file])
        assert raised.value.code == 2
        with pytest.raises(SystemExit) as raised:
            main(["start", "handoff", "--expect-preview", "x", "--", pkg_file])
        assert raised.value.code == 2

    @pytest.mark.parametrize(
        "bad", ["abc", "", "omnitient-start-1", "omnitient-start-2." + "a." * 5 + "a"]
    )
    def test_malformed_tokens_are_a_usage_error(
        self, session: Session, out: Path, bad: str
    ) -> None:
        code, stdout, err = session.handoff(bad, out)
        assert code == 2
        assert stdout == ""
        assert "preview token" in err
        assert not out.exists()

    def test_the_preview_takes_no_handoff_options(self, session: Session) -> None:
        with pytest.raises(SystemExit) as raised:
            session.preview("--out", "x")
        assert raised.value.code == 2


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
        pkg = approve(
            replace(
                package,
                title="Title $(touch PWNED7)",
                objective=hostile,
                scope=(hostile,),
                exclusions=(hostile,),
            )
        )
        (directory / "work-package.json").write_bytes(dumps(pkg))
        checkout = work / "checkout dir; touch PWNED8"
        checkout.mkdir()
        handoff_dir = work / "out dir; touch PWNED9"
        mapped = f"repo={repo}"

        preview = self.omnitient(
            checkout, "start", "preview", "--root", mapped, "--", str(directory)
        )
        assert preview.returncode == 0, preview.stderr
        assert "Title $(touch PWNED7)" in preview.stdout
        assert f"    - {hostile}" in preview.stdout
        token = TOKEN.search(preview.stdout)
        assert token

        started = self.omnitient(
            checkout, "start", "handoff", "--root", mapped,
            "--expect-preview", token.group(1), "--out", str(handoff_dir),
            "--", str(directory),
        )  # fmt: skip
        assert started.returncode == 0, started.stderr
        assert hostile in (handoff_dir / HANDOFF_FILE).read_text(encoding="utf-8")

        assert sorted(p.name for p in work.iterdir()) == sorted(
            [self.NAME, checkout.name, handoff_dir.name]
        )
        assert not list(tmp_path.rglob("PWNED*"))

    def test_a_path_that_looks_like_an_option_needs_the_separator(
        self, tmp_path: Path, approved: WorkPackage, repo: Path
    ) -> None:
        directory = tmp_path / "-pkg"
        directory.mkdir()
        (directory / "work-package.json").write_bytes(dumps(approved))
        root = f"repo={repo}"
        refused = self.omnitient(tmp_path, "start", "preview", "--root", root, "-pkg")
        assert refused.returncode == 2
        accepted = self.omnitient(
            tmp_path, "start", "preview", "--root", root, "--", "-pkg"
        )
        assert accepted.returncode == 0, accepted.stderr


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    head, _, body = text.removeprefix("---\n").partition("\n---\n")
    pairs = (line.split(": ", 1) for line in head.splitlines())
    return dict(pairs), body


class TestSkill:
    """What a static check can establish about the skill's text.

    Nothing here shows how a live session behaves: that the owner is asked,
    that a permission prompt appears, or what happens to long output.
    """

    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    def test_it_is_a_project_skill_only_the_owner_can_invoke(self) -> None:
        meta, _ = frontmatter(self.text)
        assert SKILL.parent.name == "skills"
        assert SKILL.parent.parent.name == ".claude"
        assert meta["name"] == SKILL.name == "omnitient-start"
        assert meta["disable-model-invocation"] == "true"
        assert meta["description"]

    def test_only_the_read_only_preview_is_pre_approved(self) -> None:
        meta, _ = frontmatter(self.text)
        pattern = meta["allowed-tools"]
        assert pattern == "Bash(omnitient start preview *)"
        glob = pattern.removeprefix("Bash(").removesuffix(")")
        assert fnmatch.fnmatchcase("omnitient start preview -- 'pkg'", glob)
        for step in (
            "omnitient start handoff --out 'x' -- 'pkg'",
            "omnitient start --out x handoff",
            "omnitient render 'pkg' --stage implementation --out x",
            "omnitient approve 'pkg' --stage implementation --by x",
        ):
            assert not fnmatch.fnmatchcase(step, glob)

    def test_arguments_are_prose_never_shell_injection(self) -> None:
        _, body = frontmatter(self.text)
        assert body.count("$ARGUMENTS") == 1
        assert not re.findall(r"```.*?```", body, flags=re.DOTALL)
        assert not re.search(r"!`|^```!", body, flags=re.MULTILINE)

    def test_every_flag_it_names_exists_or_is_one_it_forbids(self) -> None:
        top = next(
            a for a in build_parser()._actions if hasattr(a, "choices") and a.choices
        )
        steps = next(
            a
            for a in top.choices["start"]._actions
            if hasattr(a, "choices") and a.choices
        )
        known = set(steps.choices["preview"]._option_string_actions) | set(
            steps.choices["handoff"]._option_string_actions
        )
        named = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", self.text))
        assert named - known == {"--allow-blocked"}
        assert {"--expect-preview", "--out", "--checkout", "--root"} <= named

    def test_it_states_the_boundaries_the_brief_requires(self) -> None:
        _, body = frontmatter(self.text)
        flat = " ".join(body.split())
        for required in (
            "working directory",
            "preview again",
            "start.changed",
            "newline count",
            "last line number",
            "not observed",
            "outside the target checkout",
            "on anyone's behalf",
        ):
            assert required in flat, required

    def test_consent_precedes_the_handoff_step_and_a_denial_stops(self) -> None:
        _, body = frontmatter(self.text)
        assert body.index("Wait for the owner") < body.index("start handoff")
        flat = " ".join(body.split())
        assert "declines" in flat
        assert "denied" in flat
        assert "do not retry" in flat


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
class TestIgnoreBoundary:
    """Only the shipped skill is un-ignored, even if a global rule hides .claude/.

    These ask Git about paths only; no file is opened or read.
    """

    def ignored(self, tmp_path: Path, path: str) -> bool:
        excludes = tmp_path / "global-excludes"
        excludes.write_text(".claude/\n", encoding="utf-8")
        config = tmp_path / "global-gitconfig"
        config.write_text(f"[core]\n\texcludesFile = {excludes}\n", encoding="utf-8")
        env = {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}
        result = subprocess.run(
            [
                str(shutil.which("git")),
                "-C",
                str(ROOT),
                "check-ignore",
                "-q",
                "--no-index",
                path,
            ],
            env=env,
            capture_output=True,
            check=False,
            timeout=60,
        )
        assert result.returncode in {0, 1}, result.stderr
        return result.returncode == 0

    def test_the_shipped_skill_is_tracked(self, tmp_path: Path) -> None:
        assert not self.ignored(tmp_path, ".claude/skills/omnitient-start/SKILL.md")

    @pytest.mark.parametrize(
        "path",
        [
            ".claude/settings.json",
            ".claude/settings.local.json",
            ".claude/test.local.json",
            ".claude/worktrees/agent-1/file",
            ".claude/agent-memory/notes.md",
            ".claude/scheduled_tasks.lock",
            ".claude/skills/other-skill/SKILL.md",
            ".claude/skills/omnitient-start-old/SKILL.md",
        ],
    )
    def test_everything_else_under_claude_stays_ignored(
        self, tmp_path: Path, path: str
    ) -> None:
        assert self.ignored(tmp_path, path)
