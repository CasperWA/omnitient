# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Command-line interface. All file and terminal I/O lives here."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from omnitient import __version__, ops, readiness, render
from omnitient.context import (
    HostMapping,
    ResolvedContext,
    build_host_mapping,
    digest_file,
    resolve_context,
)
from omnitient.errors import OmnitientError, refusal
from omnitient.model import (
    CONTEXT_MODES,
    EVIDENCE_KINDS,
    HANDOFF_STAGES,
    ContextRef,
    GitProvenance,
    HandoffStage,
    WorkPackage,
)
from omnitient.readiness import Readiness
from omnitient.schema import parse_package
from omnitient.serialize import content_digest, dumps, sha256_hex
from omnitient.store import (
    MANIFEST_SCHEMA,
    MANIFEST_SCHEMA_VERSION,
    IncompleteBundleError,
    LoadedPackage,
    create_package,
    load_package,
    package_path,
    publish_bundle,
    read_bundle,
    update_package,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_NOT_READY = 3


def _now() -> datetime:
    return datetime.now(UTC)


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _json(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


# --- shared helpers -----------------------------------------------------------


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


# --- commands -----------------------------------------------------------------


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


def cmd_show(args: argparse.Namespace) -> int:
    assessed = _assess(args)
    pkg, reports = assessed.loaded.package, assessed.reports
    digest = content_digest(pkg)
    states = {item.ref.id: item.status for item in assessed.resolved}
    if args.json:
        _out(
            _json(
                {
                    "package": pkg.package_id,
                    "title": pkg.title,
                    "stage": pkg.stage,
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
                    "readiness": {s: r.ready for s, r in reports.items()},
                }
            )
        )
        return EXIT_OK

    _out(f"{pkg.package_id}  stage={pkg.stage}  revision={digest[:12]}")
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
    _out("readiness:")
    for report in reports.values():
        _out(f"  {_readiness_lines(report)[0]}")
    return EXIT_OK


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

    rendered = render.render(handoff)
    outputs: dict[str, bytes] = {}
    if args.format in {"md", "both"}:
        outputs[f"handoff-{stage}.md"] = rendered.markdown
    if args.format in {"json", "both"}:
        outputs[f"handoff-{stage}.json"] = rendered.json

    if args.out == "-":
        sys.stdout.buffer.write(next(iter(outputs.values())))
        sys.stdout.flush()
    else:
        directory = Path(args.out)
        manifest_name = f"handoff-{stage}.meta.json"
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
        }
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise refusal(
                "output.unwritable", f"cannot create {directory}: {exc.strerror}"
            ) from exc
        manifest_data = (_json(meta) + "\n").encode("utf-8")
        publish_bundle(
            directory, outputs, (manifest_name, manifest_data), overwrite=args.force
        )
        try:
            bundle = read_bundle(directory, manifest_name)
        except IncompleteBundleError as exc:
            raise refusal(
                "output.conflict",
                f"the bundle in {directory} does not verify after writing, so "
                f"another writer probably interfered: {exc}. Re-run the command.",
            ) from exc
        if bundle.files != outputs or bundle.manifest != json.loads(manifest_data):
            raise refusal(
                "output.conflict",
                f"the bundle in {directory} verifies, but another writer replaced "
                "it after this one wrote it. Re-run the command if you need this "
                "command's output.",
            )
        for name in (*outputs, manifest_name):
            _out(f"wrote {directory / name}")

    if not report.ready:
        _err(f"BLOCKED for {stage}: {len(report.blockers)} blocker(s)")
        for diag in report.blockers:
            _err(diag.format())
        return EXIT_NOT_READY
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


def cmd_schema(_args: argparse.Namespace) -> int:
    text = (
        resources.files("omnitient") / "data" / "work-package.schema.json"
    ).read_text(encoding="utf-8")
    sys.stdout.write(text)
    return EXIT_OK


# --- parser -------------------------------------------------------------------


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

    p = sub.add_parser("schema", help="print the packaged JSON Schema")
    p.set_defaults(func=cmd_schema)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except OmnitientError as exc:
        for diag in exc.diagnostics:
            _err(diag.format())
        return EXIT_REFUSED
