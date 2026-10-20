# GitHub publication and review handoff

Nothing in this repository has been pushed.
The repository on GitHub is created by its owner, not by tooling.
This document records what is proposed for that step so it can be agreed first.

## Needed from the repository owner

1. The repository URL, once it exists.
2. Authorization for exactly these pushes, to be confirmed explicitly:
   - `main`, as the initial base (one commit: ignore rules, README stub and tool baseline);
   - `feature/o1-work-package-cli`, for the O1 pull request.
   No force pushes. No bypassing of signing or branch rules.
3. Decisions deliberately left open:
   - the license and copyright holder, which this repository does not declare;
   - the package registry, if any, and the release process;
   - the CI runner infrastructure.
4. Whether the O1 commits, authored and committed by a bot identity and unsigned in this session, should be re-signed before publication.

## Minimal CI proposal

One workflow, triggered on pull requests and on pushes to `main`, with read-only repository permissions and no secrets.

1. Check out the repository.
2. Set up Python 3.12 and install `.[dev]`.
3. Run `pre-commit run --all-files`.
4. Run `pre-commit run --all-files --hook-stage manual` for the installed-wheel test.
5. Optionally repeat steps 2 to 4 for Python 3.14, the other tested version.

The `runs-on` value is left to the owner.
Workflows are not added here because the runner choice is the owner's.

Not proposed: deployment, scheduled or write-capable jobs, package publication, and any automation that pushes or comments.

## Minimal repository templates

- A pull request template with three prompts: what changed, how it was checked (`pre-commit` and tests), and whether the contract in `docs/o1-contract.md` changed.
- No issue templates and no CODEOWNERS file until there is more than one contributor.
- Suggested branch protection for `main` once CI exists: require the CI check and a pull request, and disallow force pushes.

## Independent review handoff

Scope for reviewers: the diff `main...feature/o1-work-package-cli`, all of it new code, tests and documentation.
Review tracks are launched by the owner, not by this work.

Where to start:

1. [docs/o1-contract.md](o1-contract.md) is the specification; check the code against it.
2. `src/omnitient/context.py` and `src/omnitient/rules.py`: path safety, symlink handling, forbidden files, size bounds.
3. `src/omnitient/render.py` with `tests/test_render.py`: can embedded context or package text impersonate a control section?
4. `src/omnitient/store.py`: no-clobber creation, lock file, conflict detection.
5. `src/omnitient/readiness.py` and `src/omnitient/ops.py`: do the approval and staleness rules match the contract?

Questions worth asking:

- Is the content digest (contract section 4.2) covering the right fields?
- Are the interpretations in contract section 12 acceptable, especially "applicable revision" and decisions edited by hand?
- Is anything refused too strictly (for example `.env.example`) or too loosely?
- Does any command write when it is documented as read-only?

To reproduce the checks:

```bash
pip install -e ".[dev]"
pre-commit run --all-files
pre-commit run --all-files --hook-stage manual
```
