# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""``omnitient start`` and the project skill that wraps it (docs/start-contract.md)."""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Collection
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import NOTES_TEXT, SPEC_TEXT, sha
from omnitient import cli, ops, render, start
from omnitient.cli import build_parser, main
from omnitient.context import build_host_mapping, resolve_context
from omnitient.errors import refusal
from omnitient.model import Decision, WorkPackage
from omnitient.serialize import content_digest, dumps, sha256_hex
from omnitient.start import HANDOFF_FILE, PROBE_FILE, Facts
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

    def __init__(self, run: Run, pkg_file: Path, repo: Path, out: Path) -> None:
        self.run, self.pkg_file, self.repo, self.out = run, pkg_file, repo, out

    def preview(self, *extra: object, out: Path | None = None) -> tuple[int, str, str]:
        return self.run(
            "start", "preview", "--root", f"repo={self.repo}", "--out",
            out or self.out, *extra, "--", self.pkg_file,
        )  # fmt: skip

    def token(self, *extra: object) -> str:
        code, stdout, err = self.preview(*extra)
        assert code == 0, err
        match = TOKEN.search(stdout)
        assert match, stdout
        return match.group(1)

    def handoff(
        self, token: str, out: Path | None = None, *extra: object
    ) -> tuple[int, str, str]:
        return self.run(
            "start", "handoff", "--root", f"repo={self.repo}", "--expect-preview",
            token, "--out", out or self.out, *extra, "--", self.pkg_file,
        )  # fmt: skip


@pytest.fixture
def session(
    run: Run, write_package: Write, approved: WorkPackage, repo: Path, out: Path
) -> Session:
    return Session(run, write_package(approved), repo, out)


def ready_document(approved: WorkPackage, repo: Path) -> bytes:
    mapping = build_host_mapping([f"repo={repo}"])
    handoff = render.build_handoff(
        approved, resolve_context(approved, mapping), "implementation"
    )
    return render.render_format(handoff, "md")


def with_context_text(
    package: WorkPackage, repo: Path, text: str, write_package: Write
) -> Path:
    """An approved package whose required spec file holds ``text``."""
    (repo / "docs" / "spec.md").write_text(text, encoding="utf-8")
    spec = replace(package.context[0], sha256=sha(text))
    return write_package(approve(replace(package, context=(spec, package.context[1]))))


class TestPreview:
    def test_shows_what_the_owner_decides_on_and_no_handoff(
        self, session: Session, approved: WorkPackage, target: Path, tmp_path: Path
    ) -> None:
        before = sorted(tmp_path.rglob("*"))
        code, stdout, _ = session.preview()
        assert code == 0
        loaded = load_package(session.pkg_file)
        document = ready_document(approved, session.repo)
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
            "target checkout is the working directory of this command: yes",
            "the branch, HEAD and local modifications are not observed",
            "checkout git entry: absent",
            "may differ from the target): none",
            f"handoff directory: {session.out} (new, outside the target)",
            "    - Implement A",
            "    - No C",
            "enforcement: none by omnitient",
            f"handoff renders: {len(document)} bytes, sha256 {sha256_hex(document)}",
            "reading plan: ",
        ):
            assert expected in stdout
        assert TOKEN.search(stdout)
        assert "no usable handoff token" not in stdout
        assert "and so of the session" not in stdout
        assert "Build the thing." not in stdout
        assert "The spec says" not in stdout
        assert sorted(tmp_path.rglob("*")) == before
        assert not session.out.exists()

    def test_lists_which_inputs_are_embedded_or_referenced(
        self, run: Run, write_package: Write, package: WorkPackage, repo: Path,
        out: Path,
    ) -> None:  # fmt: skip
        spec, notes = package.context
        gone = replace(notes, id="gone", path="docs/missing.md", mode="reference")
        pkg = approve(
            replace(package, context=(spec, replace(notes, mode="reference"), gone))
        )
        (repo / "docs" / "notes.md").write_text("drifted\n", encoding="utf-8")
        code, stdout, _ = Session(run, write_package(pkg), repo, out).preview()
        assert code == 0, stdout
        assert "    - spec repo:docs/spec.md embed required: embedded" in stdout
        assert "notes repo:docs/notes.md reference optional: not embedded" in stdout
        assert "gone repo:docs/missing.md reference optional: not embedded" in stdout
        assert "The spec says" not in stdout

    def test_a_reference_mode_input_is_listed_as_referenced(
        self, run: Run, write_package: Write, package: WorkPackage, repo: Path,
        out: Path,
    ) -> None:  # fmt: skip
        spec = replace(package.context[0], mode="reference")
        pkg = approve(replace(package, context=(spec,)))
        code, stdout, _ = Session(run, write_package(pkg), repo, out).preview()
        assert code == 0
        assert "referenced (path and digest only)" in stdout

    def test_a_root_that_differs_from_the_target_is_information_not_a_refusal(
        self, session: Session
    ) -> None:
        code, stdout, _ = session.preview()
        assert code == 0
        assert "context roots may differ from the target): none" in stdout
        assert TOKEN.search(stdout)

    def test_a_render_that_cannot_succeed_is_not_previewed_as_ready(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(render, "MAX_OUTPUT_BYTES", 100)
        code, stdout, err = session.preview()
        assert code == 1
        assert "output.too_large" in err
        assert "status: READY" not in stdout


class TestNoUsableToken:
    """An informational preview may show readiness but never a usable token."""

    def reasons(self, stdout: str) -> list[str]:
        return re.findall(r"^no usable handoff token: (.*)$", stdout, re.MULTILINE)

    def test_a_target_that_is_not_the_working_directory(
        self, session: Session, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        code, stdout, _ = session.preview("--checkout", other)
        assert code == 0
        assert "status: READY" in stdout
        assert "target checkout is the working directory of this command: NO" in stdout
        assert "not the working directory of this command" in self.reasons(stdout)[0]
        assert not TOKEN.search(stdout)
        assert "preview token" not in stdout

    def test_a_missing_handoff_directory_proposal(
        self, run: Run, write_package: Write, approved: WorkPackage, repo: Path
    ) -> None:
        code, stdout, _ = run(
            "start", "preview", "--root", f"repo={repo}", "--", write_package(approved)
        )
        assert code == 0
        assert "handoff directory: none proposed" in stdout
        assert "no handoff directory was proposed" in self.reasons(stdout)[0]
        assert not TOKEN.search(stdout)

    @pytest.mark.parametrize(
        ("make", "text"),
        [
            ("inside", "is inside the target checkout"),
            ("target", "is inside the target checkout"),
            ("exists", "already exists"),
            ("file", "already exists"),
            ("orphan", "has no existing parent directory"),
        ],
    )
    def test_an_unusable_handoff_directory(
        self, session: Session, target: Path, tmp_path: Path, make: str, text: str
    ) -> None:
        proposed = {
            "inside": target / "handoff",
            "target": target,
            "exists": tmp_path / "taken",
            "file": tmp_path / "afile",
            "orphan": tmp_path / "no" / "parent" / "dir",
        }[make]
        if make == "exists":
            proposed.mkdir()
        if make == "file":
            proposed.write_text("x", encoding="utf-8")
        code, stdout, _ = session.preview(out=proposed)
        assert code == 0
        assert f"NOT USABLE, it {text}" in stdout
        assert text in self.reasons(stdout)[0]
        assert not TOKEN.search(stdout)

    def test_a_handoff_that_cannot_be_read_under_the_procedure(
        self,
        run: Run,
        write_package: Write,
        package: WorkPackage,
        repo: Path,
        out: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        text = "\n".join(["x" * 2000, "ok"]) + "\n"
        pkg_file = with_context_text(package, repo, text, write_package)
        monkeypatch.setattr(start, "MAX_LONG_LINES", 0)
        code, stdout, _ = Session(run, pkg_file, repo, out).preview()
        assert code == 0
        assert "reading plan: cannot be established" in stdout
        assert "bounded reading procedure" in self.reasons(stdout)[0]
        assert not TOKEN.search(stdout)


class TestBlocked:
    """A blocked package yields a refusal and nothing an agent could act on."""

    def assert_no_work(self, stdout: str, err: str) -> None:
        assert "preview token" not in stdout
        assert "proposed write scope: withheld" in stdout
        for text in (
            "Build the thing.",
            "Implement A",
            "Implement B",
            "No C",
            "The spec says",
            "declared inputs",
            "handoff renders",
        ):
            assert text not in stdout
            assert text not in err

    def test_missing_approval(
        self, run: Run, write_package: Write, package: WorkPackage, repo: Path,
        out: Path,
    ) -> None:  # fmt: skip
        code, stdout, err = Session(run, write_package(package), repo, out).preview()
        assert code == 3
        assert "status: BLOCKED" in stdout
        assert "blocker[approval.missing]" in stdout
        self.assert_no_work(stdout, err)

    def test_stale_approval(
        self, run: Run, write_package: Write, approved: WorkPackage, repo: Path,
        out: Path,
    ) -> None:  # fmt: skip
        pkg_file = write_package(replace(approved, objective="A different goal."))
        code, stdout, err = Session(run, pkg_file, repo, out).preview()
        assert code == 3
        assert "blocker[approval.stale]" in stdout
        assert "implementation approval: stale" in stdout
        self.assert_no_work(stdout, err)

    def test_unresolved_root(
        self, run: Run, write_package: Write, approved: WorkPackage, out: Path
    ) -> None:
        code, stdout, err = run(
            "start", "preview", "--out", out, "--", write_package(approved)
        )
        assert code == 3
        assert "blocker[context.root_unmapped]" in stdout
        self.assert_no_work(stdout, err)

    def test_context_drift_before_start_blocks_the_handoff_and_writes_nothing(
        self, session: Session
    ) -> None:
        token = session.token()
        (session.repo / "docs" / "spec.md").write_text("changed\n", encoding="utf-8")
        code, stdout, err = session.handoff(token)
        assert code == 3
        assert "blocker[context.digest_mismatch]" in stdout
        self.assert_no_work(stdout, err)
        assert not session.out.exists()

    def test_unresolved_decision(
        self, run: Run, write_package: Write, approved: WorkPackage,
        unresolved: Decision, repo: Path, out: Path,
    ) -> None:  # fmt: skip
        pkg = replace(approved, decisions=(*approved.decisions, unresolved))
        code, stdout, err = Session(run, write_package(pkg), repo, out).preview()
        assert code == 3
        assert "blocker[decision.unresolved]" in stdout
        self.assert_no_work(stdout, err)


class TestHandoff:
    def test_writes_the_renderers_ready_output_and_verifies_it(
        self, session: Session, approved: WorkPackage
    ) -> None:
        code, stdout, err = session.handoff(session.token())
        assert code == 0, err
        document = ready_document(approved, session.repo)
        out = session.out
        assert (out / HANDOFF_FILE).read_bytes() == document
        assert (out / PROBE_FILE).read_bytes() == start.probe_document()
        bundle = read_bundle(out, "handoff-implementation.meta.json")
        assert bundle.files == {
            HANDOFF_FILE: document,
            PROBE_FILE: start.probe_document(),
        }
        assert f"handoff bytes: {len(document)}" in stdout
        assert f"handoff sha256: {sha256_hex(document)}" in stdout
        assert "stored bytes verified against the manifest" in stdout
        assert "is not verified by this command" in stdout
        assert "reading plan: " in stdout
        assert "handoff file:" in stdout
        assert "reader probe file:" in stdout
        # It is the document `render` produces.
        code, direct, _ = session.run(
            "render", session.pkg_file, "--stage", "implementation", "--out", "-",
            "--format", "md", "--root", f"repo={session.repo}",
        )  # fmt: skip
        assert code == 0
        assert direct.encode() == document

    def test_an_existing_directory_is_never_reused(self, session: Session) -> None:
        token = session.token()
        session.out.mkdir()
        (session.out / "mine.txt").write_text("keep", encoding="utf-8")
        code, stdout, err = session.handoff(token)
        assert code == 1
        assert "error[start.out_exists]" in err
        assert "already exists" in err
        assert stdout == ""
        assert [p.name for p in session.out.iterdir()] == ["mine.txt"]
        assert (session.out / "mine.txt").read_text(encoding="utf-8") == "keep"

    def test_an_empty_existing_directory_is_not_reused_either(
        self, session: Session
    ) -> None:
        token = session.token()
        session.out.mkdir()
        code, _, err = session.handoff(token)
        assert code == 1
        assert "start.out_exists" in err
        assert list(session.out.iterdir()) == []

    def test_a_delivered_directory_cannot_be_started_into_again(
        self, session: Session
    ) -> None:
        token = session.token()
        assert session.handoff(token)[0] == 0
        before = (session.out / HANDOFF_FILE).read_bytes()
        code, _, err = session.handoff(token)
        assert code == 1
        assert "start.out_exists" in err
        assert "--force" not in err
        assert (session.out / HANDOFF_FILE).read_bytes() == before

    def test_the_write_step_itself_refuses_a_directory_that_exists(
        self, tmp_path: Path
    ) -> None:
        taken = tmp_path / "taken"
        taken.mkdir()
        with pytest.raises(cli.OmnitientError) as raised:
            cli._publish_for_start(taken, "implementation", {}, None, None)  # type: ignore[arg-type]
        assert raised.value.diagnostics[0].code == "start.out_exists"
        assert list(taken.iterdir()) == []

    def test_the_destination_inside_the_target_is_refused_by_the_write_step(
        self, session: Session, target: Path
    ) -> None:
        inside = target / "handoff"
        forged = forge_destination(session.token(), inside)
        code, stdout, err = session.handoff(forged, inside)
        assert code == 1
        assert "error[start.out_inside_checkout]" in err
        assert stdout == ""
        assert list(target.iterdir()) == []

    def test_a_target_that_is_not_the_working_directory_is_refused(
        self, session: Session, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        code, stdout, err = session.handoff(session.token(), None, "--checkout", other)
        assert code == 1
        assert "error[start.checkout_not_cwd]" in err
        assert stdout == ""
        assert not session.out.exists()

    def test_a_bundle_that_does_not_verify_is_not_reported_as_delivered(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = session.token()
        real = cli.read_bundle

        def tampered(directory: Path, name: str) -> object:
            (directory / HANDOFF_FILE).write_bytes(b"truncated")
            return real(directory, name)

        monkeypatch.setattr(cli, "read_bundle", tampered)
        code, stdout, err = session.handoff(token)
        assert code == 1
        assert "output.conflict" in err
        assert "handoff delivered" not in stdout
        self.assert_start_advice(err)

    def assert_start_advice(self, err: str) -> None:
        assert "--force" not in err
        assert "Re-run the command" not in err
        assert "re-run the command" not in err
        assert "preview again naming a new directory with --out" in err

    @pytest.mark.parametrize(
        "message",
        [
            "an earlier write was probably interrupted; remove the listed files or use --force)",
            "already exist (use --force to replace them); nothing existing was touched",
            "nothing was written. Re-run the command.",
            "the bundle changed, so check the bundle and re-run the command.",
        ],
    )
    def test_publication_advice_is_one_this_command_supports(
        self, session: Session, monkeypatch: pytest.MonkeyPatch, message: str
    ) -> None:
        token = session.token()

        def failing(*_a: object, **_k: object) -> None:
            raise refusal("output.exists", message)

        monkeypatch.setattr(cli, "publish_bundle", failing)
        code, stdout, err = session.handoff(token)
        assert code == 1
        assert stdout == ""
        assert "error[output.exists]" in err
        self.assert_start_advice(err)
        assert not session.out.exists()  # it was created empty, so it is removed

    def test_a_render_over_the_bound_is_refused_before_anything_is_written(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = session.token()
        monkeypatch.setattr(render, "MAX_OUTPUT_BYTES", 100)
        code, stdout, err = session.handoff(token)
        assert code == 1
        assert "output.too_large" in err
        assert "handoff delivered" not in stdout
        assert not session.out.exists()

    def test_a_handoff_that_cannot_be_read_is_refused_before_anything_is_written(
        self,
        run: Run,
        write_package: Write,
        package: WorkPackage,
        repo: Path,
        out: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pkg_file = with_context_text(package, repo, "x" * 2000 + "\n", write_package)
        session = Session(run, pkg_file, repo, out)
        token = session.token()
        monkeypatch.setattr(start, "MAX_LONG_LINES", 0)
        code, stdout, err = session.handoff(token)
        assert code == 1
        assert "error[start.unreadable]" in err
        assert stdout == ""
        assert not out.exists()


def forge_destination(token: str, destination: Path) -> str:
    """A well-formed token for another destination, as a hostile caller could build."""
    facts = start.parse_token(token)
    values = list(facts.values)
    values[6] = sha256_hex(os.fsencode(destination.resolve()))
    return Facts(tuple(values)).token


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

    def test_nothing_changed_starts(self, session: Session) -> None:
        assert session.handoff(session.token())[0] == 0

    def test_a_changed_package_file_that_the_revision_does_not_cover(
        self, session: Session
    ) -> None:
        token = session.token()
        code, _, _ = session.run(
            "evidence", "add", session.pkg_file, "--kind", "commit", "--ref",
            "abcdef1", "--by", "user:alice",
        )  # fmt: skip
        assert code == 0
        self.assert_refused(
            session.handoff(token), {"package file contents"}, session.out
        )

    def test_a_changed_revision(
        self, session: Session, approved: WorkPackage, write_package: Write
    ) -> None:
        token = session.token()
        write_package(replace(approved, objective="Something else."))
        self.assert_refused(
            session.handoff(token), {"revision", "package file contents"}, session.out
        )

    def test_an_identical_copy_of_the_package_elsewhere(
        self, session: Session, tmp_path: Path
    ) -> None:
        token = session.token()
        copy = tmp_path / "copy"
        copy.mkdir()
        shutil.copy(session.pkg_file, copy / "work-package.json")
        session.pkg_file = copy / "work-package.json"
        self.assert_refused(
            session.handoff(token), {"package file location"}, session.out
        )

    def test_an_alternate_root_mapping_is_caught_before_any_context_is_read(
        self, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = session.token()
        twin = tmp_path / "twin"
        shutil.copytree(session.repo, twin)  # identical contents, other mapping

        def forbidden(*_a: object, **_k: object) -> None:
            raise AssertionError("context was read before the expectations matched")

        monkeypatch.setattr(cli, "resolve_context", forbidden)
        session.repo = twin
        self.assert_refused(session.handoff(token), {"root mappings"}, session.out)

    def test_an_extra_root_mapping(self, session: Session, tmp_path: Path) -> None:
        token = session.token()
        extra = tmp_path / "extra"
        extra.mkdir()
        result = session.handoff(token, None, "--root", f"more={extra}")
        self.assert_refused(result, {"root mappings"}, session.out)

    def test_a_different_handoff_directory_is_caught_before_context_is_read(
        self, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = session.token()
        elsewhere = tmp_path / "elsewhere-out"

        def forbidden(*_a: object, **_k: object) -> None:
            raise AssertionError("context was read before the expectations matched")

        monkeypatch.setattr(cli, "resolve_context", forbidden)
        self.assert_refused(
            session.handoff(token, elsewhere), {"handoff directory"}, elsewhere
        )
        assert not session.out.exists()

    def test_a_different_checkout(
        self, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = session.token()
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(other)
        self.assert_refused(
            session.handoff(token),
            {"target checkout location", "target checkout identity"},
            session.out,
        )

    def test_a_directory_replaced_at_the_same_path(
        self,
        session: Session,
        target: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = session.token()
        monkeypatch.chdir(tmp_path)
        # The old directory stays, so the new one cannot reuse its inode number.
        target.rename(tmp_path / "moved")
        target.mkdir()
        monkeypatch.chdir(target)
        self.assert_refused(
            session.handoff(token), {"target checkout identity"}, session.out
        )

    def test_a_previewed_name_that_became_a_link_to_another_checkout(
        self,
        session: Session,
        target: Path,
        tmp_path: Path,
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
        result = session.handoff(token, None, "--checkout", target)
        self.assert_refused(
            result,
            {"target checkout location", "target checkout identity"},
            session.out,
        )

    def test_an_optional_input_that_vanishes_keeps_it_ready_but_changes_the_handoff(
        self, session: Session
    ) -> None:
        token = session.token()
        (session.repo / "docs" / "notes.md").unlink()
        code, stdout, err = session.preview()
        assert code == 0, err  # still READY: the input is only optional
        assert "status: READY" in stdout
        assert (
            "notes repo:docs/notes.md embed optional: not embedded (missing)" in stdout
        )
        self.assert_refused(session.handoff(token), {"rendered handoff"}, session.out)

    def test_an_optional_input_that_appears_changes_the_handoff(
        self, session: Session
    ) -> None:
        notes = session.repo / "docs" / "notes.md"
        saved = notes.read_bytes()
        notes.unlink()
        token = session.token()
        notes.write_bytes(saved)
        self.assert_refused(session.handoff(token), {"rendered handoff"}, session.out)

    def test_an_optional_input_whose_contents_changed(self, session: Session) -> None:
        token = session.token()
        (session.repo / "docs" / "notes.md").write_text("other\n", encoding="utf-8")
        self.assert_refused(session.handoff(token), {"rendered handoff"}, session.out)

    def test_nothing_is_created_when_the_rendered_handoff_differs(
        self, session: Session, tmp_path: Path
    ) -> None:
        token = session.token()
        (session.repo / "docs" / "notes.md").unlink()
        before = sorted(tmp_path.rglob("*"))
        session.handoff(token)
        assert sorted(tmp_path.rglob("*")) == before

    def test_a_token_carries_fingerprints_and_no_paths(self, session: Session) -> None:
        token = session.token()
        facts = start.parse_token(token)
        assert len(facts.values) == len(start.FACT_NAMES) == 8
        assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in facts.values)
        assert str(session.pkg_file.parent) not in token
        assert str(session.out) not in token
        assert facts.changed_from(facts) == []

    def test_facts_name_exactly_what_differs(self) -> None:
        base = Facts(tuple(str(i) * 64 for i in range(8)))
        other = Facts(("a" * 64, *base.values[1:7], "b" * 64))
        assert other.changed_from(base) == [start.FACT_NAMES[0], start.FACT_NAMES[7]]
        assert other.changed_from(base, early_only=True) == [start.FACT_NAMES[0]]
        assert start.parse_token(base.token) == base


class TestCheckout:
    def test_file_as_checkout_is_refused(self, session: Session) -> None:
        code, _, err = session.preview("--checkout", session.pkg_file)
        assert code == 1
        assert "not a directory" in err

    def test_git_internals_are_not_a_checkout(
        self, session: Session, target: Path
    ) -> None:
        inner = target / ".git" / "hooks"
        inner.mkdir(parents=True)
        code, stdout, err = session.preview("--checkout", inner)
        assert code == 1
        assert "'.git'" in err
        assert stdout == ""

    def test_git_entry_is_reported_but_is_not_isolation(
        self, run: Run, write_package: Write, approved: WorkPackage, repo: Path,
        out: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        (repo / ".git").mkdir()
        monkeypatch.chdir(repo)
        code, stdout, _ = Session(run, write_package(approved), repo, out).preview()
        assert code == 0
        assert "checkout git entry: present" in stdout
        assert "is the mapped root 'repo'" in stdout
        assert "a Git directory alone is no isolation" in stdout

    def test_package_file_inside_the_target_is_said_so(self, session: Session) -> None:
        code, stdout, _ = session.preview("--checkout", session.pkg_file.parent)
        assert code == 0
        assert "package file inside target checkout: yes" in stdout

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
    """Host paths are data: they cannot forge a line of any output of `start`."""

    FORGED = "forged: yes"
    HOSTILE = f"x\n{FORGED}\x1b[2J"

    def error_lines(self, err: str) -> list[str]:
        return [ln for ln in err.splitlines() if ln.startswith(("error[", "warning["))]

    def assert_safe(self, text: str) -> None:
        assert not [ln for ln in text.splitlines() if ln.startswith("forged")]
        assert "\x1b" not in text

    def test_paths_with_newlines_and_escapes_cannot_forge_preview_lines(
        self,
        run: Run,
        approved: WorkPackage,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        checkout = tmp_path / f"checkout {self.HOSTILE}"
        checkout.mkdir()
        monkeypatch.chdir(checkout)
        repo = tmp_path / f"repo {self.HOSTILE}"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "spec.md").write_text(SPEC_TEXT, encoding="utf-8")
        (repo / "docs" / "notes.md").write_text(NOTES_TEXT, encoding="utf-8")
        pkg_dir = tmp_path / f"pkg {self.HOSTILE}"
        pkg_dir.mkdir()
        (pkg_dir / "work-package.json").write_bytes(dumps(approved))
        out = tmp_path / f"out {self.HOSTILE}"
        code, stdout, err = run(
            "start", "preview", "--root", f"repo={repo}", "--out", out, "--", pkg_dir
        )
        assert code == 0, err
        self.assert_safe(stdout)
        assert self.FORGED in stdout  # present, but only inside an escaped line
        assert "\\x0a" in stdout
        assert "\\x1b" in stdout
        assert "handoff directory:" in stdout

    def test_a_missing_package_cannot_forge_lines(
        self, run: Run, tmp_path: Path
    ) -> None:
        code, stdout, err = run("start", "preview", "--", tmp_path / self.HOSTILE)
        assert code == 1
        assert stdout == ""
        self.assert_safe(err)
        assert len(self.error_lines(err)) == 1
        assert "\\x0a" in err

    def test_a_bad_roots_file_cannot_forge_lines(
        self, run: Run, write_package: Write, approved: WorkPackage, tmp_path: Path
    ) -> None:
        code, stdout, err = run(
            "start", "preview", "--roots-file", tmp_path / self.HOSTILE, "--",
            write_package(approved),
        )  # fmt: skip
        assert code == 1
        assert stdout == ""
        self.assert_safe(err)
        assert len(self.error_lines(err)) == 1
        assert "roots.invalid" in err

    def test_a_bad_checkout_cannot_forge_lines(
        self, session: Session, tmp_path: Path
    ) -> None:
        code, stdout, err = session.preview("--checkout", tmp_path / self.HOSTILE)
        assert code == 1
        assert stdout == ""
        self.assert_safe(err)
        assert len(self.error_lines(err)) == 1

    def test_a_publication_failure_cannot_forge_lines(
        self, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hostile_out = tmp_path / f"out {self.HOSTILE}"
        code, stdout, err = session.preview(out=hostile_out)
        assert code == 0
        match = TOKEN.search(stdout)
        assert match

        def failing(*_a: object, **_k: object) -> None:
            raise refusal("output.conflict", f"cannot publish in {hostile_out}")

        monkeypatch.setattr(cli, "publish_bundle", failing)
        code, stdout, err = session.handoff(match.group(1), hostile_out)
        assert code == 1
        self.assert_safe(err)
        assert len(self.error_lines(err)) == 1
        assert "output.conflict" in err

    def test_usage_errors_cannot_forge_lines(self) -> None:
        for argv in (
            ["start", "preview", f"--bad{self.HOSTILE}".replace(" ", "")],
            ["start", f"bad {self.HOSTILE}"],
        ):
            with pytest.raises(SystemExit) as raised:
                main(argv)
            assert raised.value.code == 2

    def test_usage_error_text_is_escaped(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            main(["start", "preview", "--bad\x1b[2Jx", "--", "p"])
        captured = capsys.readouterr().err
        assert "\x1b" not in captured
        assert "\\x1b" in captured

    def test_other_commands_keep_their_output(self, run: Run, tmp_path: Path) -> None:
        code, _, err = run("validate", tmp_path / "no\x07such")
        assert code == 1
        assert "\x07" in err  # unchanged: only `start` escapes


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
        "bad",
        [
            "abc",
            "",
            "omnitient-start-2",
            "omnitient-start-1." + "a." * 7 + "a",
            "omnitient-start-2." + "a." * 7 + "a",
        ],
    )
    def test_malformed_tokens_are_a_usage_error(
        self, session: Session, bad: str
    ) -> None:
        code, stdout, err = session.handoff(bad)
        assert code == 2
        assert stdout == ""
        assert "preview token" in err
        assert not session.out.exists()

    def test_the_preview_takes_no_token(self, session: Session) -> None:
        with pytest.raises(SystemExit) as raised:
            session.preview("--expect-preview", "x")
        assert raised.value.code == 2


class TestReadingPlan:
    def test_pages_anchors_and_probe(self) -> None:
        text = "".join(f"line {i} of the document\n" for i in range(1, 451))
        plan = start.reading_plan(text.encode())
        assert plan.lines == 450
        assert [(p.first, p.last) for p in plan.pages] == [
            (1, 200),
            (201, 400),
            (401, 450),
        ]
        assert plan.pages[1].head == "line 201 of the "
        assert plan.pages[1].tail == " 400 of the document"[-16:]
        assert plan.long_lines == ()
        assert (
            plan.probe_lines == 1
        )  # the longest ordinary line fits the first probe line
        assert plan.establishable

    def test_an_unterminated_last_line_counts(self) -> None:
        plan = start.reading_plan(b"a\nb")
        assert plan.lines == 2
        assert plan.pages[0].tail == "b"

    def test_long_lines_get_a_tail_and_ordinary_ones_the_probe(self) -> None:
        text = "short\n" + "y" * 900 + "\n" + "z" * 3000 + "ENDS-HERE-12345\n"
        plan = start.reading_plan(text.encode())
        assert [(n.number, n.length, n.tail) for n in plan.long_lines] == [
            (3, 3015, "z" * 1 + "ENDS-HERE-12345")
        ]
        assert plan.probe_lines == 5  # 900 characters need the 1024-character line

    def test_the_bound_on_long_lines(self) -> None:
        many = ("x" * 2000 + "\n") * (start.MAX_LONG_LINES + 1)
        plan = start.reading_plan(many.encode())
        assert not plan.establishable
        assert "cannot be established" in start.plan_summary(plan)

    def test_probe_lines_have_exact_lengths_and_markers(self) -> None:
        lines = start.probe_document().decode().splitlines()
        assert [len(x) for x in lines] == list(start.PROBE_LENGTHS)
        for n, line in zip(start.PROBE_LENGTHS, lines, strict=True):
            assert line.endswith(start.probe_marker(n))
            assert line.startswith(f"PROBE-{n}-")

    def test_plan_lines_are_stable_and_delimited(self) -> None:
        plan = start.reading_plan(("ab\n" * 3).encode())
        assert start.plan_lines(plan)[:3] == [
            "reading plan: 3 lines in 1 pages of at most 200, 0 line(s) over 1024 characters",
            "reader probe: probe file lines 1 to 1 must each be shown whole",
            "  probe line 1: 64 characters, ends |-END-64|",
        ]
        assert "page 1: lines 1-3, first line starts |ab|, last line ends |ab|" in (
            start.plan_lines(plan)
        )


class Reader:
    """Stands in for the tool a session reads files with.

    It can behave: faithfully, or by dropping lines from what it shows, cutting
    lines at a length, or ending its output early. Each is what a real reader
    might do for reasons this project cannot observe.
    """

    def __init__(
        self,
        *,
        cap: int | None = None,
        drop: Collection[int] = (),
        keep_first: int | None = None,
        blank: Collection[int] = (),
    ) -> None:
        self.cap, self.drop, self.keep_first, self.blank = (
            cap,
            set(drop),
            keep_first,
            set(blank),
        )

    def read(self, path: Path, offset: int, limit: int) -> list[tuple[int, str]]:
        lines = path.read_text(encoding="utf-8").split("\n")
        if lines[-1] == "":
            lines.pop()
        shown = [
            (n, lines[n - 1])
            for n in range(offset, min(offset + limit - 1, len(lines)) + 1)
            if n not in self.drop
        ]
        if self.keep_first is not None:
            shown = shown[: self.keep_first]
        if self.cap is not None:
            shown = [(n, text[: self.cap]) for n, text in shown]
        return [(n, "" if n in self.blank else text) for n, text in shown]


PAGE = re.compile(
    r"^page (\d+): lines (\d+)-(\d+), first line starts \|(.*)\|, last line ends \|(.*)\|$",
    re.MULTILINE,
)
PROBE = re.compile(r"^  probe line (\d+): \d+ characters, ends \|(.*)\|$", re.MULTILINE)
LONG = re.compile(r"^long line (\d+): \d+ characters, ends \|(.*)\|$", re.MULTILINE)


def follow_procedure(
    plan_text: str,
    handoff: Path,
    probe: Path,
    reader: Reader,
    *,
    skip_pages: Collection[int] = (),
) -> list[str]:
    """The reading procedure of the skill (step 5), applied mechanically.

    ``skip_pages`` stands for an agent that did not read those pages.
    """
    problems: list[str] = []
    shown_probe = dict(reader.read(probe, 1, 100))
    for number, marker in PROBE.findall(plan_text):
        text = shown_probe.get(int(number))
        if text is None or not text.endswith(marker):
            problems.append(f"probe line {number} is not shown whole")
    seen: dict[int, str] = {}
    read_pages: set[int] = set()
    for number, first, last, head, tail in PAGE.findall(plan_text):
        if int(number) in skip_pages:
            continue
        start_line, end_line = int(first), int(last)
        shown = reader.read(handoff, start_line, end_line - start_line + 1)
        read_pages.add(int(number))
        if [n for n, _ in shown] != list(range(start_line, end_line + 1)):
            problems.append(
                f"page {number}: the line numbers shown are not {start_line}-{end_line}"
            )
            continue
        if not shown[0][1].startswith(head):
            problems.append(f"page {number}: the first line does not start as planned")
        if not shown[-1][1].endswith(tail):
            problems.append(f"page {number}: the last line does not end as planned")
        seen.update(shown)
    for number, _first, _last, _head, _tail in PAGE.findall(plan_text):
        if int(number) not in read_pages:
            problems.append(f"page {number} was not read")
    for number, tail in LONG.findall(plan_text):
        text = seen.get(int(number))
        if text is None or not text.endswith(tail):
            problems.append(f"long line {number} does not end as planned")
    return problems


def old_rule(plan_text: str, handoff: Path, reader: Reader) -> bool:
    """The round-one check: the last line number reached equals the line count.

    It looks at the last page only, so it cannot tell which others were read.
    """
    total = int(re.search(r"reading plan: (\d+) lines", plan_text).group(1))  # type: ignore[union-attr]
    pages = [(int(a), int(b)) for _, a, b, _, _ in PAGE.findall(plan_text)]
    first, last = pages[-1]
    shown = reader.read(handoff, first, last - first + 1)
    return bool(shown) and shown[-1][0] == total


class TestReadingProcedure:
    """Missing middle content cannot satisfy the procedure; the old rule let it.

    The documents are real handoffs written by `start handoff`; only the reader
    is simulated.
    """

    @pytest.fixture
    def delivered(
        self,
        run: Run,
        write_package: Write,
        package: WorkPackage,
        repo: Path,
        out: Path,
    ) -> tuple[str, Path, Path]:
        lines = [f"spec line {i} " + "word " * 8 for i in range(1, 450)]
        lines[99] = "A" * 899 + "Z"  # ordinary: under the probe's longest line
        lines[299] = "B" * 3000 + "-TAIL-OF-LONG-LINE"
        pkg_file = with_context_text(
            package, repo, "\n".join(lines) + "\n", write_package
        )
        session = Session(run, pkg_file, repo, out)
        code, stdout, err = session.handoff(session.token())
        assert code == 0, err
        return stdout, out / HANDOFF_FILE, out / PROBE_FILE

    def test_the_plan_has_what_the_procedure_needs(
        self, delivered: tuple[str, Path, Path]
    ) -> None:
        plan_text, _handoff, _ = delivered
        assert len(PAGE.findall(plan_text)) >= 3
        assert len(LONG.findall(plan_text)) == 1
        assert len(PROBE.findall(plan_text)) == 5
        assert "stored bytes verified" in plan_text

    def test_a_faithful_reader_satisfies_it(
        self, delivered: tuple[str, Path, Path]
    ) -> None:
        plan_text, handoff, probe = delivered
        assert follow_procedure(plan_text, handoff, probe, Reader()) == []

    def test_a_skipped_range_in_the_middle_of_a_page_is_detected(
        self, delivered: tuple[str, Path, Path]
    ) -> None:
        plan_text, handoff, probe = delivered
        reader = Reader(drop=range(250, 261))
        problems = follow_procedure(plan_text, handoff, probe, reader)
        assert any("line numbers shown" in p for p in problems)
        # The round-one rule reached the right last line number and passed.
        assert old_rule(plan_text, handoff, reader)

    def test_a_page_that_ends_early_is_detected(
        self, delivered: tuple[str, Path, Path]
    ) -> None:
        plan_text, handoff, probe = delivered
        problems = follow_procedure(plan_text, handoff, probe, Reader(keep_first=150))
        assert any("line numbers shown" in p for p in problems)

    def test_a_page_never_read_is_detected(
        self, delivered: tuple[str, Path, Path]
    ) -> None:
        plan_text, handoff, probe = delivered
        problems = follow_procedure(plan_text, handoff, probe, Reader(), skip_pages={2})
        assert problems == ["page 2 was not read"]
        # Reading only the first and the last page satisfied the round-one rule.
        assert old_rule(plan_text, handoff, Reader())

    def test_a_long_line_cut_inside_an_otherwise_complete_looking_page(
        self, delivered: tuple[str, Path, Path]
    ) -> None:
        plan_text, handoff, probe = delivered
        reader = Reader(cap=2000)
        problems = follow_procedure(plan_text, handoff, probe, reader)
        long_line = LONG.findall(plan_text)[0][0]
        assert problems == [f"long line {long_line} does not end as planned"]
        # Every line number looks right: only the cut tail gives it away.
        assert old_rule(plan_text, handoff, reader)

    @pytest.mark.parametrize("cap", [60, 400, 800, 1000])
    def test_a_reader_that_cuts_ordinary_lines_fails_the_probe(
        self, delivered: tuple[str, Path, Path], cap: int
    ) -> None:
        plan_text, handoff, probe = delivered
        problems = follow_procedure(plan_text, handoff, probe, Reader(cap=cap))
        assert any(p.startswith("probe line") for p in problems)

    def test_what_the_procedure_cannot_see(
        self, delivered: tuple[str, Path, Path]
    ) -> None:
        """A reader that blanks text but keeps numbers, starts and ends passes.

        Only the text at anchors is compared, so this part still rests on the
        reader presenting what is on disk.
        """
        plan_text, handoff, probe = delivered
        reader = Reader(blank=range(30, 40))
        assert follow_procedure(plan_text, handoff, probe, reader) == []
        shown = dict(reader.read(handoff, 30, 5))
        assert set(shown.values()) == {""}


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
            checkout, "start", "preview", "--root", mapped, "--out", str(handoff_dir),
            "--", str(directory),
        )  # fmt: skip
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
    that a permission prompt appears, or what a reader's tool presents.
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
            "not observed",
            "outside the target checkout",
            "on anyone's behalf",
            "no usable handoff token",
            "never reuse a directory that exists",
            "rendered handoff",
        ):
            assert required in flat, required

    def test_it_states_the_reading_procedure_and_when_to_stop(self) -> None:
        _, body = frontmatter(self.text)
        flat = " ".join(body.split())
        for required in (
            "reading plan",
            "probe",
            "plan order",
            "none missing and none repeated",
            "start with",
            "end with",
            "long line",
            "stop before any edit",
            "exactly what could not be read",
            "not evidence of what this session received",
        ):
            assert required in flat, required
        # The round-one rule and the stored-file digests are not proof of reading.
        assert "last line number equals" not in flat
        assert (
            "do not treat the final line number, the byte count or the SHA-256 as proof"
            in flat
        )

    def test_consent_precedes_the_handoff_step_and_a_denial_stops(self) -> None:
        _, body = frontmatter(self.text)
        assert body.index("Wait for the owner") < body.index("start handoff")
        flat = " ".join(body.split())
        assert "declines" in flat
        assert "denied" in flat
        assert "do not retry" in flat

    def test_the_directory_is_chosen_before_consent_and_reused_unchanged(self) -> None:
        _, body = frontmatter(self.text)
        assert body.index("Choose the handoff directory") < body.index(
            "Wait for the owner"
        )
        assert "with the same directory" in " ".join(body.split())


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
