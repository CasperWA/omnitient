# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""Command-line interface. All file and terminal I/O lives here."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from omnitient import (
    __version__,
    delivery,
    ops,
    overview_render,
    readiness,
    render,
)
from omnitient.context import (
    HostMapping,
    ResolvedContext,
    build_host_mapping,
    digest_file,
    resolve_context,
)
from omnitient.errors import Diagnostic, OmnitientError, refusal
from omnitient.limits import MAX_PACKAGE_BYTES
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
                f"the bundle in {directory} verifies, but it is another writer's, "
                "which replaced this command's output after it was written. "
                "Re-run the command if you need this output.",
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


def _report(lines: Sequence[str], *, saved: bool) -> int:
    """Write the outcome to standard output, and say so if that fails.

    The change (if any) is already persisted when this runs, so a failure here
    must never claim that nothing changed, and nothing is rolled back or
    repeated: running the same command again replays it safely.
    """
    try:
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        _abandon_stdout()
        if saved:
            _err(
                "error[delivery.stdout]: standard output failed AFTER the change "
                "was saved; the package was changed and nothing was rolled back. "
                "Run the same command again (same operation id) to replay it "
                "safely and see the record; do not use a new operation id."
            )
        else:
            _err(
                "error[delivery.stdout]: standard output failed; nothing was "
                "written to the package"
            )
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
    _err(
        "note: delivery records are workflow provenance, not authentication, "
        "verification of any artifact or permission to run anything"
    )
    if saved:
        _err(
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
    return _report(
        _delivery_lines(planned, planned.package), saved=planned.outcome == "saved"
    )


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
    return _report(
        _delivery_lines(planned, planned.package), saved=planned.outcome == "saved"
    )


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
                saved=False,
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
                saved=False,
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
        return _report(lines, saved=False)
    if expected != loaded.file_sha256:
        raise _upgrade_refusal(
            "upgrade.hash_mismatch",
            f"the file hash is {loaded.file_sha256}, not the expected {expected}: "
            "the package is not the one that was previewed. Nothing was written. "
            "Inspect the package and preview again.",
        )
    update_package(loaded, ops.upgrade_to_v2)
    _err("note: the upgrade made no approval, no repin and no stage change")
    lines[0] = f"saved upgrade: {pkg.package_id}"
    return _report(lines, saved=True)


def _abandon_stdout() -> None:
    """Point a broken stdout at the null device so the interpreter's own flush at exit
    stays quiet; the command has already failed."""
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(fd, sys.stdout.fileno())
        finally:
            os.close(fd)
    except (OSError, ValueError):
        pass  # no real descriptor behind stdout (for example a captured stream)


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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except OmnitientError as exc:
        for diag in exc.diagnostics:
            _err(diag.format())
        return EXIT_REFUSED
