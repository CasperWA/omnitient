# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Command-line interface. All file and terminal I/O lives here."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Literal, NoReturn, TextIO

from omnitient import (
    __version__,
    delivery,
    gitref,
    ops,
    overview_render,
    readiness,
    render,
    review,
    rules,
    start,
)
from omnitient.context import (
    HostMapping,
    ResolvedContext,
    build_host_mapping,
    digest_file,
    resolve_context,
)
from omnitient.errors import Diagnostic, OmnitientError, refusal
from omnitient.limits import MAX_OUTPUT_BYTES, MAX_PACKAGE_BYTES
from omnitient.model import (
    CONTEXT_MODES,
    EVIDENCE_KINDS,
    HANDOFF_STAGES,
    Acceptance,
    ContextRef,
    GitProvenance,
    HandoffStage,
    WorkPackage,
)
from omnitient.overview import collection_overview
from omnitient.overview_render import escape_text
from omnitient.readiness import Readiness
from omnitient.rules import SHA256
from omnitient.schema import parse_package
from omnitient.serialize import content_digest, dumps, sha256_hex
from omnitient.store import (
    MANIFEST_SCHEMA,
    MANIFEST_SCHEMA_VERSION,
    DestinationChangedError,
    IncompleteBundleError,
    LoadedPackage,
    create_package,
    create_private_directory,
    discard_in,
    load_package,
    package_path,
    publish_bundle,
    publish_bundle_in,
    read_bundle,
    read_bundle_in,
    remove_directory,
    update_package,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_NOT_READY = 3


def _now() -> datetime:
    return datetime.now(UTC)


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _json(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _mapping(args: argparse.Namespace, pkg: WorkPackage | None = None) -> HostMapping:
    mapping = build_host_mapping(args.root or (), args.roots_file)
    if pkg is not None:
        for name in mapping.unknown_for(pkg):
            _err(
                f"warning[roots.unknown]: root '{name}' is mapped but not declared by the package"
            )
    return mapping


@dataclass(frozen=True)
class Assessment:
    loaded: LoadedPackage
    mapping: HostMapping
    resolved: list[ResolvedContext]
    reports: dict[HandoffStage, Readiness]


def _assess(args: argparse.Namespace) -> Assessment:
    loaded = load_package(args.package)
    mapping = _mapping(args, loaded.package)
    resolved = resolve_context(loaded.package, mapping)
    return Assessment(
        loaded, mapping, resolved, readiness.evaluate_all(loaded.package, resolved)
    )


def _add_package_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "package",
        type=Path,
        metavar="PACKAGE",
        help="package file, or a directory holding work-package.json",
    )


def _readiness_lines(report: Readiness, indent: str = "  ") -> list[str]:
    state = "READY" if report.ready else f"BLOCKED ({len(report.blockers)} blocker(s))"
    lines = [f"{report.stage}: {state}"]
    lines += [f"{indent}{d.format()}" for d in (*report.blockers, *report.warnings)]
    return lines


def _truncate(text: str, width: int) -> str:
    """First line of ``text``, marked with an ellipsis when anything was cut."""
    lines = text.splitlines() or [""]
    first = lines[0]
    if len(first) > width:
        return first[: width - 1] + "…"
    return first + "…" if len(lines) > 1 else first


def cmd_new(args: argparse.Namespace) -> int:
    pkg = ops.new_package(args.project, args.initiative, args.work_package, args.title)
    result = parse_package(dumps(pkg))
    if result.package is None:
        raise OmnitientError(result.diagnostics)
    path = package_path(args.path)
    create_package(path, pkg)
    _out(f"created {path}: {pkg.package_id} (stage draft, no approvals)")
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    assessed = _assess(args)
    loaded, reports = assessed.loaded, assessed.reports
    digest = content_digest(loaded.package)
    if args.json:
        _out(
            _json(
                {
                    "schema_valid": True,
                    "package": loaded.package.package_id,
                    "content_digest": digest,
                    "readiness": {
                        stage: {
                            "ready": r.ready,
                            "blockers": [d.to_dict() for d in r.blockers],
                            "warnings": [d.to_dict() for d in r.warnings],
                        }
                        for stage, r in reports.items()
                    },
                }
            )
        )
    else:
        _out(f"schema: valid ({loaded.package.package_id}, revision {digest[:12]})")
        _out("readiness:")
        for report in reports.values():
            for line in _readiness_lines(report, "    "):
                _out(f"  {line}")
    if args.stage and not reports[args.stage].ready:
        return EXIT_NOT_READY
    return EXIT_OK


SHOW_SCHEMA = "omnitient/show"
SHOW_SCHEMA_VERSION = 1


def _show_delivery(pkg: WorkPackage) -> dict[str, object]:
    reserve = delivery.reserve_of(pkg)
    diag = delivery.reserve_deficit_diagnostic(pkg)
    return {
        "summary": delivery.summarize(pkg).to_dict(),
        "reserve": reserve.to_dict() if reserve else None,
        "diagnostics": [diag.to_dict()] if diag else [],
        "history": delivery.history_dict(pkg),
    }


def _delivery_show_lines(pkg: WorkPackage) -> list[str]:
    summary = delivery.summarize(pkg)
    lines = [f"delivery: {summary.describe()}"]
    statuses = {s.record.operation_id: s for s in delivery.acceptance_statuses(pkg)}
    for record in pkg.delivery:
        if isinstance(record, Acceptance):
            status = statuses[record.operation_id]
            linked = (
                f", withdrawn by {status.withdrawn_by}" if status.withdrawn_by else ""
            )
            lines.append(
                f"  {record.operation_id} acceptance of revision "
                f"{record.revision.digest[:12]} by {escape_text(record.actor)} at "
                f"{record.recorded_at} [{status.state}{linked}]"
            )
        else:
            lines.append(
                f"  {record.operation_id} withdrawal of {record.target_acceptance_id} "
                f"by {escape_text(record.actor)} at {record.recorded_at}"
            )
    diag = delivery.reserve_deficit_diagnostic(pkg)
    if diag:
        lines.append(f"  {diag.format()}")
    return lines


def cmd_show(args: argparse.Namespace) -> int:
    assessed = _assess(args)
    pkg, reports = assessed.loaded.package, assessed.reports
    digest = content_digest(pkg)
    states = {item.ref.id: item.status for item in assessed.resolved}
    if args.json:
        _out(
            _json(
                {
                    "schema": SHOW_SCHEMA,
                    "schema_version": SHOW_SCHEMA_VERSION,
                    "package": pkg.package_id,
                    "title": pkg.title,
                    "stage": pkg.stage,
                    "manifest_schema_version": pkg.schema_version,
                    "digest_scheme": pkg.digest_scheme,
                    "content_digest": digest,
                    "counts": {
                        "scope": len(pkg.scope),
                        "exclusions": len(pkg.exclusions),
                        "acceptance": len(pkg.acceptance),
                        "dependencies": len(pkg.dependencies),
                        "decisions": len(pkg.decisions),
                        "context": len(pkg.context),
                        "evidence": len(pkg.evidence),
                        "approvals": len(pkg.approvals),
                        "delivery": len(pkg.delivery),
                    },
                    "decisions": [
                        {
                            "id": d.id,
                            "status": d.status,
                            "needed_by": d.needed_by,
                            "question": d.question,
                        }
                        for d in pkg.decisions
                    ],
                    "context": [
                        {
                            "id": c.id,
                            "root": c.root,
                            "path": c.path,
                            "required": c.required,
                            "mode": c.mode,
                            "status": states[c.id],
                        }
                        for c in pkg.context
                    ],
                    "approvals": [
                        {
                            "id": a.id,
                            "stage": a.stage,
                            "approved_by": a.approved_by,
                            "approved_at": a.approved_at,
                            "current": a.content_digest == digest,
                        }
                        for a in pkg.approvals
                    ],
                    "evidence": [
                        {"id": e.id, "kind": e.kind, "ref": e.ref} for e in pkg.evidence
                    ],
                    "delivery": _show_delivery(pkg),
                    "readiness": {s: r.ready for s, r in reports.items()},
                }
            )
        )
        return EXIT_OK

    _out(f"{pkg.package_id}  stage={pkg.stage}  revision={digest[:12]}")
    _out(f"manifest: schema v{pkg.schema_version}, digest scheme {pkg.digest_scheme}")
    _out(f"title: {pkg.title}")
    _out(f"objective: {_truncate(pkg.objective, 100)}")
    _out(
        f"scope {len(pkg.scope)} | exclusions {len(pkg.exclusions)} | "
        f"acceptance {len(pkg.acceptance)} | dependencies {len(pkg.dependencies)}"
    )
    unresolved = sum(d.status == "unresolved" for d in pkg.decisions)
    _out(
        f"decisions: {len(pkg.decisions) - unresolved} resolved, {unresolved} unresolved"
    )
    for d in pkg.decisions:
        _out(
            f"  {d.id} {d.status} needed_by={d.needed_by}  {_truncate(d.question, 80)}"
        )
    _out(f"context: {len(pkg.context)} reference(s)")
    for c in pkg.context:
        _out(
            f"  {c.id} {c.root}:{c.path} "
            f"{'required' if c.required else 'optional'} {c.mode} [{states[c.id]}]"
        )
    _out(f"approvals: {len(pkg.approvals)}")
    for a in pkg.approvals:
        state = "current" if a.content_digest == digest else "stale"
        _out(f"  {a.id} {a.stage} by {a.approved_by} at {a.approved_at} [{state}]")
    _out(f"evidence: {len(pkg.evidence)}")
    for e in pkg.evidence:
        _out(f"  {e.id} {e.kind} {e.ref}")
    for line in _delivery_show_lines(pkg):
        _out(line)
    _out("readiness:")
    for report in reports.values():
        _out(f"  {_readiness_lines(report)[0]}")
    return EXIT_OK


def _handoff_manifest(
    stage: HandoffStage,
    outputs: Mapping[str, bytes],
    handoff: render.Handoff,
    assessed: Assessment | StartInputs,
    extra: Mapping[str, object] | None,
) -> bytes:
    """The completion manifest of o1-contract.md section 8.4, as bytes."""
    loaded = assessed.loaded
    meta = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": ops.timestamp(_now()),
        "omnitient_version": __version__,
        "package_file": str(loaded.path.resolve()),
        "package_file_sha256": loaded.file_sha256,
        "target_stage": stage,
        "status": handoff.status,
        "content_digest": handoff.digest,
        "host_roots": {
            name: str(path) for name, path in sorted(assessed.mapping.roots.items())
        },
        "outputs": {name: sha256_hex(data) for name, data in outputs.items()},
        **(extra or {}),
    }
    return (_json(meta) + "\n").encode("utf-8")


def _publish_handoff(
    directory: Path,
    stage: HandoffStage,
    outputs: dict[str, bytes],
    handoff: render.Handoff,
    assessed: Assessment | StartInputs,
    *,
    force: bool,
    extra: Mapping[str, object] | None = None,
    mode: int | None = None,
) -> list[Path]:
    """Publish a handoff bundle, read it back and return the paths written.

    The manifest comes last and the bundle is verified under the reader rule of
    docs/o1-contract.md section 8.4, so a caller that reports success has a
    bundle whose files match their recorded SHA-256.
    """
    manifest_name = f"handoff-{stage}.meta.json"
    manifest_data = _handoff_manifest(stage, outputs, handoff, assessed, extra)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise refusal(
            "output.unwritable",
            f"cannot create {directory}: {exc.strerror}",
        ) from exc
    publish_bundle(
        directory,
        outputs,
        (manifest_name, manifest_data),
        overwrite=force,
        mode=mode,
    )
    shown = directory
    try:
        bundle = read_bundle(directory, manifest_name)
    except IncompleteBundleError as exc:
        raise refusal(
            "output.conflict",
            f"the bundle in {shown} does not verify after writing, so "
            f"another writer probably interfered: {exc}. Re-run the command.",
        ) from exc
    if bundle.files != outputs or bundle.manifest != json.loads(manifest_data):
        raise refusal(
            "output.conflict",
            f"the bundle in {shown} verifies, but it is another writer's, "
            "which replaced this command's output after it was written. "
            "Re-run the command if you need this output.",
        )
    return [directory / name for name in (*outputs, manifest_name)]


def cmd_render(args: argparse.Namespace) -> int:
    stage: HandoffStage = args.stage
    if args.out == "-" and args.format == "both":
        raise refusal("render.format", "--out - needs --format md or --format json")
    assessed = _assess(args)
    loaded = assessed.loaded
    handoff = render.build_handoff(loaded.package, assessed.resolved, stage)
    report = handoff.readiness
    if not report.ready and stage != "planning" and not args.allow_blocked:
        _err(
            f"not ready for {stage}; nothing was written (use --allow-blocked for a BLOCKED document)"
        )
        for diag in report.blockers:
            _err(diag.format())
        return EXIT_NOT_READY

    outputs: dict[str, bytes] = {}
    if args.format in {"md", "both"}:
        outputs[f"handoff-{stage}.md"] = render.render_format(handoff, "md")
    if args.format in {"json", "both"}:
        outputs[f"handoff-{stage}.json"] = render.render_format(handoff, "json")

    if args.out == "-":
        sys.stdout.buffer.write(next(iter(outputs.values())))
        sys.stdout.flush()
    else:
        directory = Path(args.out)
        for path in _publish_handoff(
            directory, stage, outputs, handoff, assessed, force=args.force
        ):
            _out(f"wrote {path}")

    if not report.ready:
        _err(f"BLOCKED for {stage}: {len(report.blockers)} blocker(s)")
        for diag in report.blockers:
            _err(diag.format())
        return EXIT_NOT_READY
    return EXIT_OK


def _resolve_checkout(arg: Path | None) -> start.Checkout:
    """The actual target directory: ``--checkout``, else the working directory."""
    try:
        working = Path.cwd().resolve(strict=True)
        path = (arg if arg is not None else working).resolve(strict=True)
        info = path.stat()
    except (OSError, RuntimeError) as exc:
        raise refusal(
            "start.checkout",
            f"cannot resolve the target checkout: {exc}",
        ) from exc
    if not path.is_dir():
        raise refusal("start.checkout", "the target checkout is not a directory")
    forbidden = rules.forbidden_directory(path.parts)
    if forbidden:
        raise refusal(
            "start.checkout",
            f"the target checkout lies inside a directory named {forbidden!r}, "
            "which is never a work location",
        )
    return start.Checkout(
        path,
        info.st_dev,
        info.st_ino,
        os.path.lexists(path / ".git"),
        path == working,
    )


@dataclass(frozen=True)
class StartInputs:
    """Everything a start reads before context: package, mapping, target, directory."""

    loaded: LoadedPackage
    mapping: HostMapping
    checkout: start.Checkout
    destination: start.Destination | None
    digest: str


def _destination(
    arg: Path | None, checkout: start.Checkout
) -> start.Destination | None:
    """The proposed handoff directory, resolved, and why it cannot be used."""
    if arg is None:
        return None
    try:
        path = arg.resolve()
        exists = os.path.lexists(path)
        parent_ok = path.parent.is_dir()
    except (OSError, RuntimeError) as exc:
        raise refusal(
            "start.out", f"cannot resolve the handoff directory: {exc}"
        ) from exc
    problem = None
    if path == checkout.path or path.is_relative_to(checkout.path):
        problem = (
            "is inside the target checkout, so it would be part of the write scope"
        )
    elif exists:
        problem = "already exists, and an existing file or directory is never reused"
    elif not parent_ok:
        problem = "has no existing parent directory, and none is created"
    return start.Destination(path, problem)


def _start_inputs(args: argparse.Namespace) -> StartInputs:
    loaded = load_package(args.package)
    mapping = _mapping(args, loaded.package)
    checkout = _resolve_checkout(args.checkout)
    return StartInputs(
        loaded,
        mapping,
        checkout,
        _destination(args.out, checkout),
        content_digest(loaded.package),
    )


def _facts(
    inputs: StartInputs, destination: start.Destination, document: bytes | None
) -> start.Facts:
    return start.gather_facts(
        revision=inputs.digest,
        file_sha256=inputs.loaded.file_sha256,
        package_file=inputs.loaded.path.resolve(),
        roots=inputs.mapping.roots,
        checkout=inputs.checkout,
        destination=destination.path,
        document=document,
    )


def _start_preview_lines(
    inputs: StartInputs,
    resolved: list[ResolvedContext],
    handoff: render.Handoff,
    document: bytes | None,
) -> list[str]:
    plan = start.reading_plan(document) if document is not None else None
    withheld = (
        start.withheld_reasons(inputs.checkout, inputs.destination, plan)
        if plan is not None
        else []
    )
    facts = (
        _facts(inputs, inputs.destination, document)
        if inputs.destination is not None and document is not None
        else None
    )
    return start.preview_lines(
        inputs.loaded.package,
        inputs.digest,
        handoff.readiness,
        package_file=inputs.loaded.path.resolve(),
        file_sha256=inputs.loaded.file_sha256,
        checkout=inputs.checkout,
        roots=inputs.mapping.roots,
        resolved=resolved,
        destination=inputs.destination,
        document=document,
        plan=plan,
        token=facts.token if facts is not None else None,
        withheld=withheld,
    )


def cmd_start_preview(args: argparse.Namespace) -> int:
    inputs = _start_inputs(args)
    resolved = resolve_context(inputs.loaded.package, inputs.mapping)
    handoff = render.build_handoff(inputs.loaded.package, resolved, start.START_STAGE)
    # Rendering is part of readiness for the owner's purposes: a handoff that
    # cannot be rendered (for example over the output bound) must not preview READY.
    document = render.render_format(handoff, "md") if handoff.readiness.ready else None
    for line in _start_preview_lines(inputs, resolved, handoff, document):
        _out(line)
    if not handoff.readiness.ready:
        return EXIT_NOT_READY
    _out("no handoff was produced; it needs the owner's go-ahead for this scope")
    return EXIT_OK


# Advice of the shared bundle writer that this command cannot follow: it has no
# --force, and the directory it names is never reused.
_START_ADVICE = re.compile(
    r"[;,]?\s*(?:\(?remove the listed files or )?(?:use --force[^.)]*|[Rr]e-run the command)\)?\.?"
)


def _publish_for_start(
    out: Path,
    stage: HandoffStage,
    outputs: dict[str, bytes],
    handoff: render.Handoff,
    inputs: StartInputs,
) -> list[Path]:
    """Create ``out`` (it must be new), publish into it and leave nothing half-claimed."""
    try:
        out.mkdir()
    except FileExistsError as exc:
        raise refusal(
            "start.out_exists",
            "the handoff directory already exists; an existing file or directory "
            "is never reused. Preview again naming a new directory with --out.",
        ) from exc
    except OSError as exc:
        raise refusal(
            "start.out_unwritable",
            f"cannot create the handoff directory: {exc.strerror}. Preview again "
            "naming another new directory with --out.",
        ) from exc
    try:
        return _publish_handoff(out, stage, outputs, handoff, inputs, force=False)
    except OmnitientError as exc:
        with contextlib.suppress(OSError):
            out.rmdir()  # only if empty: nothing that was written is removed
        raise OmnitientError(
            [
                Diagnostic(
                    d.code,
                    d.severity,
                    _START_ADVICE.sub("", d.message).rstrip(" .;,")
                    + ". Nothing is reused or replaced: preview again naming a new "
                    "directory with --out.",
                    d.location,
                )
                for d in exc.diagnostics
            ]
        ) from exc


def cmd_start_handoff(args: argparse.Namespace) -> int:
    try:
        shown = start.parse_token(args.expect_preview)
    except ValueError as exc:
        _err(f"omnitient start handoff: error: --expect-preview: {exc}")
        return EXIT_USAGE

    inputs = _start_inputs(args)
    if not inputs.checkout.is_working_directory:
        raise refusal(
            "start.checkout_not_cwd",
            "the target checkout is not this command's working directory, so "
            "implementation cannot start from here; run the preview and the start "
            "from the target directory",
        )
    # Cheap comparisons come before any context file is read.
    destination = inputs.destination
    if destination is None:  # the parser requires --out; this is a safeguard
        raise refusal("start.out", "no handoff directory was given with --out")
    changed = _facts(inputs, destination, None).changed_from(shown, early_only=True)
    if changed:
        raise _changed(changed)
    if destination.problem:
        code = (
            "start.out_inside_checkout"
            if "inside" in destination.problem
            else "start.out_exists"
        )
        raise refusal(
            code,
            f"the handoff directory {destination.problem}. "
            "Preview again naming a new directory with --out.",
        )

    resolved = resolve_context(inputs.loaded.package, inputs.mapping)
    handoff = render.build_handoff(inputs.loaded.package, resolved, start.START_STAGE)
    if not handoff.readiness.ready:
        for line in _start_preview_lines(inputs, resolved, handoff, None):
            _out(line)
        return EXIT_NOT_READY
    document = render.render_format(handoff, "md")
    # What the owner confirmed includes the document itself: an optional input
    # that appeared or vanished keeps the package READY but changes the handoff.
    changed = _facts(inputs, destination, document).changed_from(shown)
    if changed:
        raise _changed(changed)
    plan = start.reading_plan(document)
    if not plan.establishable:
        raise refusal(
            "start.unreadable",
            "the handoff cannot be read under the bounded reading procedure: "
            + start.plan_summary(plan),
        )
    written = _publish_for_start(
        destination.path,
        start.START_STAGE,
        {start.HANDOFF_FILE: document, start.PROBE_FILE: start.probe_document()},
        handoff,
        inputs,
    )
    _out("omnitient start: handoff delivered")
    _out(f"handoff file: {escape_text(str(written[0]))}")
    _out(f"reader probe file: {escape_text(str(written[1]))}")
    _out(f"manifest: {escape_text(str(written[2]))}")
    _out(f"handoff bytes: {len(document)}")
    _out(f"handoff sha256: {sha256_hex(document)}")
    _out(
        "stored bytes verified against the manifest; what a reader's tool "
        "presents is not verified by this command"
    )
    for line in start.plan_lines(plan):
        _out(line)
    return EXIT_OK


def _changed(names: list[str]) -> OmnitientError:
    return refusal(
        "start.changed",
        "changed since the preview, so nothing was started: "
        + ", ".join(names)
        + ". Show the owner a new preview.",
    )


_FileId = tuple[int, int]
_ANCESTOR_LIMIT = 4096


def _file_id(path: Path) -> _FileId:
    info = path.stat()
    return info.st_dev, info.st_ino


def _chain_ids(fd: int) -> list[_FileId]:
    """The identity of the directory ``fd`` holds and of each directory above it.

    The walk goes through ``..`` from the open directory, so it describes what
    the descriptor is and not what any path to it names now.
    """
    ids: list[_FileId] = []
    current = os.dup(fd)
    try:
        for _ in range(_ANCESTOR_LIMIT):
            info = os.fstat(current)
            ids.append((info.st_dev, info.st_ino))
            above = os.open(
                "..", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=current
            )
            os.close(current)
            current = above
            top = os.fstat(above)
            if (top.st_dev, top.st_ino) == ids[-1]:
                break
        else:
            raise OSError("the directory has too many parents")
    finally:
        os.close(current)
    return ids


@dataclass
class _Destination:
    """The selected new directory: its path, its name, and its parent held open.

    Everything that is created or removed afterwards is done relative to the
    open parent, so it concerns the directory that was selected and examined,
    not whatever the path comes to name later.
    """

    path: Path
    name: str
    parent_fd: int
    parent_id: _FileId

    def close(self) -> None:
        os.close(self.parent_fd)


def _outside_repository(message: str) -> OmnitientError:
    return refusal(
        "review.out_inside_repo",
        message + "; name a directory outside them with --out",
    )


def _review_destination(arg: Path, comparison: review.Comparison) -> _Destination:
    """The new directory for the regular track's file, resolved and checked.

    The parent is opened, and its identity and that of every directory above it
    are compared with the repository's, so that the check is of the directory
    that will be written into.
    """
    try:
        path = arg.resolve()
        exists = os.path.lexists(path)
        parent_ok = path.parent.is_dir()
    except (OSError, RuntimeError) as exc:
        raise refusal(
            "review.out", f"cannot resolve the output directory: {exc}"
        ) from exc
    if _in_repository(path, comparison):
        raise _outside_repository(
            "the output directory is inside the repository or its Git directory "
            "(which can lie outside the work tree, for a linked work tree or a "
            "separate Git directory), where it would change the repository"
        )
    if exists:
        raise refusal(
            "review.out_exists",
            "the output directory already exists, and an existing file or "
            "directory is never reused; name a new directory with --out",
        )
    if not parent_ok or not path.name:
        raise refusal(
            "review.out_parent",
            "the output directory has no existing parent directory, and none is "
            "created; name a new directory in an existing one with --out",
        )
    if not review.path_is_literal(path / review.REQUEST_FILE):
        raise refusal(
            "review.out_not_literal",
            "the path of the request file holds characters that cannot be given "
            "to a reader as they are, so no usable instruction could name it; "
            "use a directory whose absolute path has only letters, digits and "
            "._+=@:,/- after the leading slash",
        )
    try:
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise refusal(
            "review.out", f"cannot open the output directory's parent: {exc.strerror}"
        ) from exc
    try:
        ids = _chain_ids(parent_fd)
        info = os.fstat(parent_fd)
    except OSError as exc:
        os.close(parent_fd)
        raise refusal(
            "review.out", f"cannot examine the output directory's parent: {exc}"
        ) from exc
    if set(ids) & set(comparison.protected_ids):
        os.close(parent_fd)
        raise _outside_repository(
            "the output directory's parent is, or lies inside, the repository or "
            "its Git directory"
        )
    return _Destination(path, path.name, parent_fd, (info.st_dev, info.st_ino))


def _in_repository(path: Path, comparison: review.Comparison) -> bool:
    return any(
        path == top or path.is_relative_to(top)
        for top in (comparison.repo, *comparison.git_dirs)
    )


def _destination_changed(detail: str) -> OmnitientError:
    return refusal(
        "review.out_changed",
        f"the output directory changed while it was being prepared ({detail}), so "
        "no instruction names it. Nothing was reused; check the directory you "
        "named, then prepare again with a new directory",
    )


def _publish_for_review(
    destination: _Destination,
    comparison: review.Comparison,
    outputs: dict[str, bytes],
    handoff: render.Handoff,
    assessed: Assessment,
    extra: Mapping[str, object],
) -> list[Path]:
    """Create the new directory and publish into it; leave nothing half-claimed.

    The directory, every file in it and every removal are made relative to the
    parent that was opened and checked, so they concern that directory even if
    the path is changed meanwhile. The directory has the bits 0o700 and the
    files 0o600 whatever the umask: they hold package content and a host path,
    and the parent may be shared. A name that is replaced by someone with the
    command's own rights between a check and a removal is not covered.
    """
    manifest_name = f"handoff-{review.REVIEW_STAGE}.meta.json"
    manifest_data = _handoff_manifest(
        review.REVIEW_STAGE, outputs, handoff, assessed, extra
    )
    try:
        dir_fd, made = create_private_directory(destination.parent_fd, destination.name)
    except FileExistsError as exc:
        raise refusal(
            "review.out_exists",
            "the output directory already exists, and an existing file or "
            "directory is never reused; name a new directory with --out",
        ) from exc
    except DestinationChangedError as exc:
        raise _destination_changed("it is not the directory that was created") from exc
    except OSError as exc:
        raise refusal(
            "review.out_unwritable",
            f"cannot create the output directory: {exc.strerror}",
        ) from exc
    created: list[tuple[str, _FileId]] = []
    try:
        try:
            if set(_chain_ids(dir_fd)) & set(comparison.protected_ids):
                raise _destination_changed("it is inside the repository")
            created = publish_bundle_in(
                dir_fd, outputs, (manifest_name, manifest_data), mode=0o600
            )
            _check_read_back(dir_fd, manifest_name, outputs, manifest_data, destination)
            if _file_id(destination.path) != made or (
                _file_id(destination.path.parent) != destination.parent_id
            ):
                raise _destination_changed(
                    "the path no longer names the directory written"
                )
        except OSError as exc:
            raise _destination_changed(str(exc)) from exc
    except BaseException:
        discard_in(dir_fd, created, {**outputs, manifest_name: manifest_data})
        remove_directory(destination.parent_fd, destination.name, made)
        raise
    finally:
        os.close(dir_fd)
    return [destination.path / name for name in (*outputs, manifest_name)]


def _check_read_back(
    dir_fd: int,
    manifest_name: str,
    outputs: Mapping[str, bytes],
    manifest_data: bytes,
    destination: _Destination,
) -> None:
    """The bundle just written, read back under the reader rule, is this command's."""
    try:
        bundle = read_bundle_in(dir_fd, manifest_name)
    except IncompleteBundleError as exc:
        raise refusal(
            "output.conflict",
            f"the bundle in {destination.path} does not verify after writing, so "
            f"another writer probably interfered: {exc}. Re-run the command.",
        ) from exc
    if bundle.files != outputs or bundle.manifest != json.loads(manifest_data):
        raise refusal(
            "output.conflict",
            f"the bundle in {destination.path} verifies, but it is another writer's, "
            "which replaced this command's output after it was written. "
            "Re-run the command if you need this output.",
        )


def cmd_review_prepare(args: argparse.Namespace) -> int:
    track: review.Track = args.track
    if track == "regular" and args.out is None:
        _err("omnitient review prepare: error: --track regular needs --out DIR")
        return EXIT_USAGE
    if track == "codex" and args.out is not None:
        _err(
            "omnitient review prepare: error: --track codex writes nothing, so "
            "it takes no --out"
        )
        return EXIT_USAGE

    assessed = _assess(args)
    pkg = assessed.loaded.package
    digest = content_digest(pkg)
    comparison = gitref.resolve(
        args.repo,
        gitref.validate_ref("base", args.base),
        gitref.validate_ref("candidate", args.candidate),
        args.basis,
    )
    report = assessed.reports[review.REVIEW_STAGE]

    def show(
        withheld: Sequence[str],
        request_file: Path | None = None,
        request_sha256: str | None = None,
    ) -> None:
        for line in review.preparation_lines(
            pkg,
            digest,
            report,
            comparison,
            track,
            request_file=request_file,
            request_sha256=request_sha256,
            withheld=withheld,
        ):
            _out(escape_text(line))

    if not report.ready:
        show(["the package is not ready for review; see the blockers"])
        return EXIT_NOT_READY

    view = review.independent_view(pkg)
    handoff = render.build_handoff(view, assessed.resolved, review.REVIEW_STAGE)

    if track == "codex":
        reasons = []
        mismatch = review.checkout_mismatch(comparison)
        if mismatch:
            reasons.append(
                f"{mismatch}. The review skill reviews the checked-out "
                "commit and refuses any other; check out the candidate "
                "commit in a clean work tree and prepare again"
            )
        if comparison.has_replacements:
            reasons.append(
                "the repository has replacement refs (refs/replace/), which the "
                "review skill's controller would apply when it reads the change, "
                "so it could review something other than the pinned commits, and "
                "the skill cannot be told to ignore them; remove them, or use the "
                "regular track, which reads the change with them disabled"
            )
        if reasons:
            show(reasons)
            return EXIT_NOT_READY
        instruction = review.codex_instruction(pkg, digest, comparison)
        show([])
        _out("launch instruction (one line, for the owner to invoke):")
        _out(instruction)
        return EXIT_OK

    destination = _review_destination(args.out, comparison)
    try:
        return _prepare_regular(
            destination, comparison, view, handoff, assessed, digest, show
        )
    finally:
        destination.close()


def _prepare_regular(
    destination: _Destination,
    comparison: review.Comparison,
    view: WorkPackage,
    handoff: render.Handoff,
    assessed: Assessment,
    digest: str,
    show: Callable[..., None],
) -> int:
    markdown = render.render_format(handoff, "md").decode("utf-8")
    document = review.regular_document(markdown, view, digest, comparison).encode(
        "utf-8"
    )
    if len(document) > MAX_OUTPUT_BYTES:
        raise refusal(
            "output.too_large",
            f"the review request would be {len(document)} bytes; the limit is "
            f"{MAX_OUTPUT_BYTES}. Nothing was truncated; mark large optional "
            "context as mode 'reference'.",
        )
    extra = {
        "review": {
            "track": "regular",
            "repository": str(comparison.repo),
            "base_commit": comparison.base,
            "candidate_commit": comparison.candidate,
            "candidate_tree": comparison.candidate_tree,
            "basis": comparison.basis,
            "basis_commit": comparison.basis_commit,
            "basis_tree": comparison.basis_tree,
            "git_executable": str(comparison.git_executable),
            "git_version": comparison.git_version,
        }
    }
    written = _publish_for_review(
        destination,
        comparison,
        {review.REQUEST_FILE: document},
        handoff,
        assessed,
        extra,
    )
    show([], written[0], sha256_hex(document))
    _out("launch instruction (for a fresh read-only review session):")
    _out(review.regular_instruction(written[0], comparison))
    return EXIT_OK


def cmd_approve(args: argparse.Namespace) -> int:
    loaded = load_package(args.package)
    resolved: list[ResolvedContext] | None = None
    if args.root or args.roots_file:
        resolved = resolve_context(loaded.package, _mapping(args, loaded.package))
    at = _now()
    new = update_package(
        loaded,
        lambda pkg: ops.add_approval(
            pkg,
            stage=args.stage,
            approved_by=args.by,
            note=args.note,
            at=at,
            resolved=resolved,
        ),
    )
    approval = new.approvals[-1]
    _out(
        f"recorded {approval.id}: {approval.stage} approved by {approval.approved_by} "
        f"for revision {approval.content_digest[:12]} (package stage now {new.stage})"
    )
    if resolved is None:
        _err("note: context was not verified (no --root or --roots-file given)")
    _err("note: approval records are workflow provenance, not authentication")
    return EXIT_OK


def cmd_evidence_add(args: argparse.Namespace) -> int:
    loaded = load_package(args.package)
    at = _now()
    new = update_package(
        loaded,
        lambda pkg: ops.add_evidence(
            pkg,
            kind=args.kind,
            ref=args.ref,
            recorded_by=args.by,
            at=at,
            summary=args.summary,
            run_id=args.run_id,
            attempt_id=args.attempt_id,
        ),
    )
    _out(f"recorded {new.evidence[-1].id}: {args.kind} {args.ref}")
    return EXIT_OK


def _split_spec(spec: str) -> tuple[str, str]:
    root, sep, path = spec.partition(":")
    if not sep or not root or not path:
        raise refusal("context.invalid", f"expected ROOT:RELATIVE/PATH, got {spec!r}")
    return root, path


def cmd_context_add(args: argparse.Namespace) -> int:
    loaded = load_package(args.package)
    root, path = _split_spec(args.spec)
    mapping = _mapping(args)
    if root not in mapping.roots:
        raise refusal(
            "context.root_unmapped", f"supply --root {root}=DIRECTORY to hash the file"
        )
    digest, size = digest_file(mapping.roots[root], path, args.mode)
    git = (
        GitProvenance(args.git_revision, args.git_repository, args.git_ref)
        if args.git_revision
        else None
    )
    if (args.git_repository or args.git_ref) and not git:
        raise refusal(
            "context.invalid", "--git-repository and --git-ref need --git-revision"
        )
    ident = args.id or ops.next_id("C", {c.id for c in loaded.package.context})
    ref = ContextRef(
        ident,
        root,
        path,
        digest,
        required=not args.optional,
        mode=args.mode,
        purpose=args.purpose or "",
        git=git,
    )
    update_package(loaded, lambda pkg: ops.add_context(pkg, ref))
    _out(f"added context {ident}: {root}:{path} sha256={digest} ({size} bytes)")
    return EXIT_OK


def cmd_context_pin(args: argparse.Namespace) -> int:
    loaded = load_package(args.package)
    mapping = _mapping(args)
    by_id = {c.id: c for c in loaded.package.context}
    digests: dict[str, str] = {}
    for ident in args.ids:
        if ident not in by_id:
            raise refusal("context.unknown", f"no context reference with id {ident}")
        ref = by_id[ident]
        if ref.root not in mapping.roots:
            raise refusal(
                "context.root_unmapped", f"supply --root {ref.root}=DIRECTORY"
            )
        digests[ident], _ = digest_file(mapping.roots[ref.root], ref.path, ref.mode)
    update_package(loaded, lambda pkg: ops.pin_context(pkg, digests))
    for ident, digest in digests.items():
        changed = "unchanged" if by_id[ident].sha256 == digest else "updated"
        _out(f"{ident}: {changed} sha256={digest}")
    _err("note: pinning changes the package revision; earlier approvals are now stale")
    if any(by_id[ident].git for ident in digests):
        _err(
            "note: supplied git provenance is not updated by pin; edit it by hand if needed"
        )
    return EXIT_OK


Outcome = Literal["saved", "upgraded", "replayed", "none"]
"""What a command did to the package, for the report of an output failure.

``saved``: an acceptance or withdrawal was persisted and a same-operation-id
rerun replays it. ``upgraded``: an upgrade was persisted; it has no operation id
and no replay. ``replayed``: the operation was saved by an earlier run, and this
run wrote nothing. ``none``: a preview or a no-op; nothing was written.
"""


def _note(text: str) -> bool:
    """Write an optional line to standard error. Never raises.

    The caller has usually persisted a change already, so a missing or broken
    standard error must not stop it from reporting that on standard output.
    Returns whether the line was delivered.
    """
    try:
        stream = sys.stderr
        if stream is None:  # descriptor 2 was closed before the command started
            return False
        written = stream.write(text + "\n")
        if written != len(text) + 1:  # a text stream counts characters
            raise OSError("standard error accepted a partial write")
        stream.flush()
    except (OSError, ValueError):
        _abandon_stream(sys.stderr)
        return False
    return True


_STDOUT_FAILURE: dict[Outcome, str] = {
    "saved": (
        "error[delivery.stdout]: standard output failed AFTER the change "
        "was saved; the package was changed and nothing was rolled back. "
        "Run the same command again (same operation id) to replay it "
        "safely and see the record; do not use a new operation id."
    ),
    "upgraded": (
        "error[delivery.stdout]: standard output failed AFTER the change "
        "was saved; the package was upgraded to manifest v2 and nothing was "
        "rolled back. No receipt was printed. An upgrade has no operation id "
        "and no replay: running the same --apply command again is refused "
        "by design, because its expected file hash is the old one. Inspect "
        "the package with 'omnitient show PACKAGE', or run "
        "'omnitient upgrade PACKAGE --to-version 2' (no --apply) to see that "
        "no upgrade is needed and the current file hash."
    ),
    "replayed": (
        "error[delivery.stdout]: standard output failed; nothing was written "
        "to the package by this run (the record was saved by an earlier run). "
        "Run the same command again to see it."
    ),
    "none": (
        "error[delivery.stdout]: standard output failed; nothing was "
        "written to the package"
    ),
}


def _report(lines: Sequence[str], *, outcome: Outcome) -> int:
    """Write the outcome to standard output, and say so if that fails.

    Only a complete write counts as delivery: ``sys.stdout`` is a text stream,
    whose ``write`` returns the number of characters accepted, so anything but
    the document's length (a short count, zero, ``None``) is a failure, as is an
    exception from ``write`` or ``flush``. Nothing is retried, because part of
    the document may already have reached the reader.
    The change (if any) is already persisted when this runs, so a failure here
    must never claim that nothing changed, and nothing is rolled back or
    repeated. Which advice is true depends on the operation (see ``Outcome``).
    The failure message is best-effort: if standard error is unusable too, the
    exit status is all that is left.
    """
    text = "\n".join(lines) + "\n"
    try:
        stream = sys.stdout
        if stream is None:  # the descriptor was closed before the command started
            raise OSError("standard output is closed")
        written = stream.write(text)
        if written != len(text):
            raise OSError(
                f"standard output accepted {written!r} of {len(text)} characters"
            )
        stream.flush()
    except (OSError, ValueError):
        _abandon_stdout()
        _note(_STDOUT_FAILURE[outcome])
        return EXIT_REFUSED
    return EXIT_OK


def _status_text(status: delivery.AcceptanceStatus, pkg: WorkPackage) -> str:
    if status.state == "withdrawn":
        return f"withdrawn by {status.withdrawn_by}"
    if status.state == "current":
        return "active, associated with the present revision"
    return (
        f"active, associated with a prior revision "
        f"(present revision {content_digest(pkg)[:12]})"
    )


def _delivery_lines(planned: delivery.Planned, pkg: WorkPackage) -> list[str]:
    record = planned.record
    verb = "saved" if planned.outcome == "saved" else "replayed"
    tail = "" if planned.outcome == "saved" else " (nothing was written)"
    if isinstance(record, Acceptance):
        ids = ", ".join(e.id for e in record.evidence)
        return [
            (
                f"{verb} acceptance {record.operation_id}: {record.package_id} "
                f"revision {record.revision.digest[:12]} "
                f"({record.revision.scheme}), evidence {ids}; recorded "
                f"{record.recorded_at} by {escape_text(record.actor)}{tail}"
            ),
            f"status: {_status_text(planned.status, pkg)}",
        ]
    return [
        (
            f"{verb} withdrawal {record.operation_id} of acceptance "
            f"{record.target_acceptance_id}; recorded {record.recorded_at} by "
            f"{escape_text(record.actor)}{tail}"
        ),
        f"acceptance {record.target_acceptance_id}: {_status_text(planned.status, pkg)}",
    ]


def _delivery_notes(saved: bool) -> None:
    _note(
        "note: delivery records are workflow provenance, not authentication, "
        "verification of any artifact or permission to run anything"
    )
    if saved:
        _note(
            "note: stage, approvals, context pins and readiness were not "
            "evaluated or changed"
        )


def cmd_closeout(args: argparse.Namespace) -> int:
    loaded = load_package(args.package)
    request = delivery.acceptance_request(
        operation_id=args.operation_id,
        actor=args.by,
        revision=args.revision,
        evidence=args.evidence,
        reason=args.reason,
    )
    planned = delivery.plan_acceptance(loaded.package, request, _now())
    if planned.outcome == "saved":
        update_package(loaded, lambda _pkg: planned.package)
    _delivery_notes(planned.outcome == "saved")
    return _report(_delivery_lines(planned, planned.package), outcome=planned.outcome)


def cmd_withdraw(args: argparse.Namespace) -> int:
    loaded = load_package(args.package)
    request = delivery.withdrawal_request(
        operation_id=args.operation_id,
        actor=args.by,
        target=args.closeout_id,
        reason=args.reason,
    )
    planned = delivery.plan_withdrawal(loaded.package, request, _now())
    if planned.outcome == "saved":
        update_package(loaded, lambda _pkg: planned.package, reserve_recovery=True)
    _delivery_notes(planned.outcome == "saved")
    return _report(_delivery_lines(planned, planned.package), outcome=planned.outcome)


def _upgrade_refusal(code: str, message: str) -> OmnitientError:
    return OmnitientError(Diagnostic(code, "error", message))


def cmd_upgrade(args: argparse.Namespace) -> int:
    if args.to_version != 2:
        raise _upgrade_refusal(
            "upgrade.unsupported_version",
            f"cannot upgrade to version {args.to_version}: only --to-version 2 "
            "exists, and there is no downgrade",
        )
    expected: str | None = args.expected_file_sha256
    if expected is not None and not args.apply:
        raise _upgrade_refusal(
            "upgrade.usage", "--expected-file-sha256 is only used with --apply"
        )
    if args.apply and expected is None:
        raise _upgrade_refusal(
            "upgrade.hash_required",
            "--apply needs --expected-file-sha256 with the file hash the preview "
            "reported",
        )
    if expected is not None and not SHA256.fullmatch(expected):
        raise _upgrade_refusal(
            "upgrade.hash_invalid",
            "--expected-file-sha256 must be 64 lowercase hex characters",
        )
    loaded = load_package(args.package)  # manifest only: no readiness, context or Git
    pkg = loaded.package
    if pkg.schema_version >= 2:
        if not args.apply:
            return _report(
                [
                    (
                        f"no upgrade needed: {pkg.package_id} is already "
                        f"manifest v{pkg.schema_version}"
                    ),
                    f"file sha256: {loaded.file_sha256}",
                ],
                outcome="none",
            )
        if expected == loaded.file_sha256:
            return _report(
                [
                    (
                        f"unchanged: {pkg.package_id} is already manifest "
                        f"v{pkg.schema_version}; nothing was written"
                    ),
                    f"file sha256: {loaded.file_sha256}",
                ],
                outcome="none",
            )
        raise _upgrade_refusal(
            "upgrade.hash_mismatch",
            f"the package is already manifest v{pkg.schema_version} and its file hash "
            f"is {loaded.file_sha256}, not the expected {expected}. An earlier "
            "upgrade or another change may have happened; nothing was written. "
            "Inspect it with 'omnitient show' before doing anything else.",
        )

    new = ops.upgrade_to_v2(pkg)
    data = dumps(new)
    checked = parse_package(data)  # oversized or invalid results refuse here
    if checked.package is None:
        raise OmnitientError(checked.diagnostics)
    delivery.check_reserve(pkg, checked.package, code="upgrade.reserve")
    old_digest, new_digest = content_digest(pkg), content_digest(new)
    current = sum(a.content_digest == old_digest for a in pkg.approvals)
    lines = [
        f"package: {pkg.package_id}",
        f"format: manifest v{pkg.schema_version} -> v2",
        f"revision: {old_digest} ({pkg.digest_scheme})",
        f"      -> {new_digest} ({new.digest_scheme})",
        (
            f"approvals: {len(pkg.approvals)} kept unchanged; {current} current "
            "now, none current after the upgrade (every approval becomes stale)"
        ),
        f"resulting file size: {len(data)} bytes (limit {MAX_PACKAGE_BYTES})",
        f"file sha256: {loaded.file_sha256}",
    ]
    if not args.apply:
        lines.append(
            "preview only; nothing was written. To apply, keep the pre-upgrade file "
            "in Git history first, then run the same command with --apply "
            f"--expected-file-sha256 {loaded.file_sha256}"
        )
        return _report(lines, outcome="none")
    if expected != loaded.file_sha256:
        raise _upgrade_refusal(
            "upgrade.hash_mismatch",
            f"the file hash is {loaded.file_sha256}, not the expected {expected}: "
            "the package is not the one that was previewed. Nothing was written. "
            "Inspect the package and preview again.",
        )
    update_package(loaded, ops.upgrade_to_v2)
    _note("note: the upgrade made no approval, no repin and no stage change")
    lines[0] = f"saved upgrade: {pkg.package_id}"
    return _report(lines, outcome="upgraded")


def _abandon_stream(stream: TextIO | None) -> None:
    """Point a broken standard stream at the null device so the interpreter's own
    flush at exit stays quiet; the command has already failed."""
    try:
        if stream is None:
            return
        fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(fd, stream.fileno())
        finally:
            os.close(fd)
    except (OSError, ValueError):
        pass  # no real descriptor behind the stream (for example a captured stream)


def _abandon_stdout() -> None:
    _abandon_stream(sys.stdout)


def cmd_overview(args: argparse.Namespace) -> int:
    result = collection_overview(args.collection, args.root or (), args.roots_file)
    render_fn = (
        overview_render.render_json if args.json else overview_render.render_text
    )
    rendered = render_fn(result)
    delivered = rendered.overview
    if not args.json and delivered.results_status == "omitted":
        # Text mode reports a refusal like the other commands do; JSON always
        # answers with one envelope on stdout.
        for diag in delivered.diagnostics:
            _err(diag.format())
        return EXIT_REFUSED
    data = rendered.data
    try:
        # An unbuffered or raw stream can accept fewer bytes than offered, or none
        # (None on a full non-blocking pipe), without raising. Only a count equal to
        # the document length is a delivery; anything else is a failure.
        written: int | None = sys.stdout.buffer.write(data)
        delivered_in_full = written == len(data)
        if delivered_in_full:
            sys.stdout.buffer.flush()
    except OSError:
        delivered_in_full = False
    if not delivered_in_full:
        # Whatever reached the reader may be a cut-off document; do not retry.
        _abandon_stdout()
        _err(
            "error[overview.stdout]: standard output failed; the result may be incomplete"
        )
        return EXIT_REFUSED
    return EXIT_OK if delivered.succeeded else EXIT_REFUSED


SCHEMA_RESOURCES = {
    1: "work-package.schema.json",
    2: "work-package.v2.schema.json",
}


def cmd_schema(args: argparse.Namespace) -> int:
    text = (
        resources.files("omnitient") / "data" / SCHEMA_RESOURCES[args.schema_version]
    ).read_text(encoding="utf-8")
    sys.stdout.write(text)
    return EXIT_OK


def _host_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--root",
        action="append",
        metavar="NAME=DIR",
        help="map a logical root to an absolute host directory (repeatable)",
    )
    parent.add_argument(
        "--roots-file",
        type=Path,
        metavar="FILE",
        help='JSON file {"roots": {"NAME": "DIR"}} with host mappings',
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnitient",
        description="Portable work packages and self-contained agent handoffs.",
    )
    parser.add_argument(
        "--version", action="version", version=f"omnitient {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    host = _host_parent()

    p = sub.add_parser("new", help="scaffold a draft, unapproved package")
    p.add_argument("path", type=Path, metavar="PATH", help="package file to create")
    p.add_argument("--project", required=True)
    p.add_argument("--initiative", required=True)
    p.add_argument("--work-package", required=True)
    p.add_argument("--title", required=True)
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("validate", parents=[host], help="schema validity and readiness")
    _add_package_argument(p)
    p.add_argument(
        "--stage", choices=HANDOFF_STAGES, help="exit 3 if not ready for this stage"
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("show", parents=[host], help="compact read-only inspection")
    _add_package_argument(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("render", parents=[host], help="generate a handoff")
    _add_package_argument(p)
    p.add_argument("--stage", required=True, choices=HANDOFF_STAGES)
    p.add_argument(
        "--out",
        required=True,
        metavar="DIR|-",
        help="output directory, or - for stdout",
    )
    p.add_argument("--format", choices=("md", "json", "both"), default="both")
    p.add_argument(
        "--allow-blocked",
        action="store_true",
        help="also write a BLOCKED implementation/review document",
    )
    p.add_argument("--force", action="store_true", help="replace existing output files")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("start", help="preview, then deliver, an implementation handoff")
    _escape_usage_errors(p)
    start_sub = p.add_subparsers(dest="start_command", required=True, metavar="STEP")
    target = argparse.ArgumentParser(add_help=False)
    _add_package_argument(target)
    target.add_argument(
        "--checkout",
        type=Path,
        metavar="DIR",
        help="target checkout (default: the current directory)",
    )
    p = start_sub.add_parser(
        "preview",
        parents=[host, target],
        help="read-only preview of package, revision, target and write scope",
    )
    p.add_argument(
        "--out",
        type=Path,
        metavar="DIR",
        help="proposed new directory outside the target for the handoff; "
        "without it the preview prints no token",
    )
    _escape_usage_errors(p)
    p.set_defaults(func=cmd_start_preview)
    p = start_sub.add_parser(
        "handoff",
        parents=[host, target],
        help="after the owner approved the preview, write the verified handoff",
    )
    p.add_argument(
        "--expect-preview",
        required=True,
        metavar="TOKEN",
        help="the preview token the owner confirmed",
    )
    p.add_argument(
        "--out",
        required=True,
        type=Path,
        metavar="DIR",
        help="the new directory the preview proposed, outside the target checkout",
    )
    _escape_usage_errors(p)
    p.set_defaults(func=cmd_start_handoff)

    p = sub.add_parser(
        "review", help="prepare a handoff for one independent review (launches none)"
    )
    _escape_usage_errors(p)
    review_sub = p.add_subparsers(dest="review_command", required=True, metavar="STEP")
    p = review_sub.add_parser(
        "prepare",
        parents=[host],
        help="resolve one local comparison and prepare the handoff for one track",
    )
    _add_package_argument(p)
    p.add_argument(
        "--repo",
        required=True,
        type=Path,
        metavar="DIR",
        help="the top directory of the local Git repository",
    )
    p.add_argument("--base", required=True, metavar="REF", help="the base revision")
    p.add_argument(
        "--candidate", required=True, metavar="REF", help="the revision to review"
    )
    p.add_argument(
        "--basis",
        choices=review.BASES,
        default="merge-base",
        help="compare the candidate with the merge base of base and candidate "
        "(default) or with the base commit itself",
    )
    p.add_argument("--track", required=True, choices=review.TRACKS)
    p.add_argument(
        "--out",
        type=Path,
        metavar="DIR",
        help="regular track only: a new directory outside the repository",
    )
    _escape_usage_errors(p)
    p.set_defaults(func=cmd_review_prepare)

    p = sub.add_parser(
        "approve", parents=[host], help="record approval of the present revision"
    )
    _add_package_argument(p)
    p.add_argument("--stage", required=True, choices=HANDOFF_STAGES)
    p.add_argument("--by", required=True, help="who approves (recorded as given)")
    p.add_argument("--note")
    p.set_defaults(func=cmd_approve)

    evidence = sub.add_parser("evidence", help="evidence references").add_subparsers(
        dest="evidence_command", required=True, metavar="ACTION"
    )
    p = evidence.add_parser("add", help="append an evidence reference")
    _add_package_argument(p)
    p.add_argument("--kind", required=True, choices=EVIDENCE_KINDS)
    p.add_argument("--ref", required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--summary")
    p.add_argument("--run-id")
    p.add_argument("--attempt-id")
    p.set_defaults(func=cmd_evidence_add)

    context = sub.add_parser("context", help="context references").add_subparsers(
        dest="context_command", required=True, metavar="ACTION"
    )
    p = context.add_parser("add", parents=[host], help="add a context file reference")
    _add_package_argument(p)
    p.add_argument("spec", metavar="ROOT:PATH")
    p.add_argument("--id")
    p.add_argument("--optional", action="store_true")
    p.add_argument("--mode", choices=CONTEXT_MODES, default="embed")
    p.add_argument("--purpose")
    p.add_argument("--git-revision")
    p.add_argument("--git-repository")
    p.add_argument("--git-ref")
    p.set_defaults(func=cmd_context_add)

    p = context.add_parser("pin", parents=[host], help="accept current file digests")
    _add_package_argument(p)
    p.add_argument("ids", nargs="+", metavar="ID")
    p.set_defaults(func=cmd_context_pin)

    p = sub.add_parser(
        "overview",
        parents=[host],
        help="read-only overview of the work packages in one directory",
    )
    p.add_argument(
        "collection",
        type=Path,
        metavar="COLLECTION",
        help="directory whose immediate subdirectories hold work-package.json",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_overview)

    p = sub.add_parser(
        "closeout",
        help="record the acceptance of the present revision (manifest v2)",
    )
    _add_package_argument(p)
    p.add_argument("--by", required=True, help="who accepts (recorded as given)")
    p.add_argument(
        "--revision", required=True, metavar="DIGEST", help="the present revision"
    )
    p.add_argument(
        "--evidence",
        required=True,
        nargs="+",
        metavar="ID",
        help="ids of existing evidence entries to snapshot (1 to 20)",
    )
    p.add_argument("--reason", required=True, help="the owner's rationale")
    p.add_argument(
        "--operation-id", required=True, metavar="ID", help="your id for this record"
    )
    p.set_defaults(func=cmd_closeout)

    p = sub.add_parser("withdraw", help="withdraw an earlier acceptance")
    _add_package_argument(p)
    p.add_argument("--closeout-id", required=True, metavar="ID")
    p.add_argument("--by", required=True, help="who withdraws (recorded as given)")
    p.add_argument("--reason", required=True)
    p.add_argument(
        "--operation-id", required=True, metavar="ID", help="your id for this record"
    )
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser(
        "upgrade", help="preview or apply the upgrade of one package to manifest v2"
    )
    _add_package_argument(p)
    p.add_argument("--to-version", required=True, type=int, metavar="2")
    p.add_argument("--apply", action="store_true", help="write the upgrade")
    p.add_argument(
        "--expected-file-sha256",
        metavar="HASH",
        help="the file hash the preview reported; required with --apply",
    )
    p.set_defaults(func=cmd_upgrade)

    p = sub.add_parser("schema", help="print a packaged JSON Schema")
    p.add_argument(
        "--schema-version",
        type=int,
        choices=sorted(SCHEMA_RESOURCES),
        default=1,
        help="manifest format to describe (default 1, the original resource)",
    )
    p.set_defaults(func=cmd_schema)
    return parser


def _escape_usage_errors(parser: argparse.ArgumentParser) -> None:
    """Make a usage error of this parser print its text as escapes.

    argparse echoes the offending argument, which is host-supplied.
    """
    original = parser.error

    def error(message: str) -> NoReturn:
        original(escape_text(message))
        raise AssertionError("argparse's error exits")  # pragma: no cover

    parser.error = error  # type: ignore[method-assign]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    words = sys.argv[1:] if argv is None else list(argv)
    if words[:1] in (["start"], ["review"]):
        _escape_usage_errors(parser)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except OmnitientError as exc:
        # Every diagnostic of `start` and `review` can quote a host path or a ref, which is data.
        # Other commands keep their long-standing output.
        escaped = args.command in {"start", "review"}
        for diag in exc.diagnostics:
            _err(escape_text(diag.format()) if escaped else diag.format())
        return EXIT_REFUSED
