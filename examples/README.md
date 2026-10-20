# Examples

## o2-git-pinned-context

A **draft** work package for a plausible next Omnitient slice: verifying pinned Git revisions of context files from a local repository.
It was written with Omnitient itself and is deliberately unfinished:

- it is unapproved (`approvals` is empty and `stage` is `draft`);
- four decisions are unresolved, one of them at the planning stage, so even its planning handoff is generated as BLOCKED;
- an implementation handoff is refused until the decisions are resolved and a person approves a revision.

Files:

- [work-package.json](o2-git-pinned-context/work-package.json): the package.
- [handoffs/handoff-planning.md](o2-git-pinned-context/handoffs/handoff-planning.md) and [handoffs/handoff-planning.json](o2-git-pinned-context/handoffs/handoff-planning.json): the generated planning handoff.
  They embed the three context files listed by the package and contain no host paths.

Try it from the repository root:

```bash
omnitient show examples/o2-git-pinned-context --root omnitient=$PWD
omnitient validate examples/o2-git-pinned-context --root omnitient=$PWD --stage implementation
omnitient render examples/o2-git-pinned-context --stage planning --out /tmp/o2-handoff --root omnitient=$PWD
```

The generated `*.meta.json` sidecar records host paths and the generation time, so it is not committed.

### Keeping the example fresh

The package pins SHA-256 digests of three repository files.
A test regenerates the handoff and compares it with the committed one, so it fails when a pinned file changes.
To refresh after an intended change:

```bash
omnitient context pin examples/o2-git-pinned-context contract context-module readiness-module --root omnitient=$PWD
omnitient render examples/o2-git-pinned-context --stage planning --out examples/o2-git-pinned-context/handoffs --root omnitient=$PWD --force
rm examples/o2-git-pinned-context/handoffs/*.meta.json
```

Pinning does not touch the supplied `git` revisions; update those by hand if you want them to name a newer commit.
`omnitient render` exits with status 3 for this package because it is blocked; that is expected.
