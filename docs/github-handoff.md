<!--
SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
SPDX-FileCopyrightText: 2026 SINTEF Ocean AS

SPDX-License-Identifier: MIT
-->

# GitHub repository and review handoff

The repository is `https://github.com/CasperWA/omnitient`, currently private.
Its owner pushed `main` (one commit: ignore rules, README stub and tool baseline) and `feature/o1-work-package-cli`.
The O1 work arrives through one draft pull request from the feature branch into `main`.
Nothing here changes the repository's visibility, merges, releases a package or launches a review.

## Decisions recorded

- **License:** the standard MIT License, with no noncommercial or no-resale restriction.
  Copyright holders are Casper Welzel Andersen and SINTEF Ocean AS, 2026, on first-party files.
  The owner confirmed ownership and authority for this attribution.
  See the README for how notices are carried and [REUSE.toml](../REUSE.toml) for the files that cannot hold one.
- **History:** the existing commits were authored by a bot identity and are unsigned; they are kept as they are.
  No re-signing, rebase or force push.
- **Commit trailers:** no tool or co-author trailers are added on the owner's behalf.
- **CI runners:** repository-attached self-hosted runners while the repository is private, GitHub-hosted runners when it is public; see [ci.md](ci.md).
- **Issue forms and labels:** the forms and labels below.

## Open items

- The package registry, if any, and the release process; nothing is published.
- Private vulnerability reporting.
  The issue chooser and the security form point to GitHub private vulnerability reporting.
  Only the repository owner can enable it (Settings, Code security), and it has not been verified as enabled, so the link does not work until then.
- Branch protection for `main`: require a pull request and the CI checks, and disallow force pushes, once the checks have run.
  Note that a skipped check counts as passing; see [ci.md](ci.md).
- No CODEOWNERS file until there is more than one contributor.

## CI

One workflow with two jobs, described in [ci.md](ci.md): static checks on Python 3.12, and the functional and installed-wheel tests on Python 3.12, 3.13, 3.14 and 3.15.
The `.github/workflows/ci.yml` file takes effect for pull requests when it is on the branch being tested; it triggers on pushes to `main` only after it is merged there.

## Issue forms and pull request template

Forms live in `.github/ISSUE_TEMPLATE/`; blank issues stay enabled.

| Form | Label applied by the form |
| --- | --- |
| Bug report | `bug` |
| Feature or contract change | `enhancement` |
| Documentation | `documentation` |
| Security hardening (non-sensitive) | `security` |

Each form has an optional priority dropdown with guidance.
Choosing a priority does not apply a label; a maintainer sets the `priority/*` label by hand.
No triage automation exists.

`.github/PULL_REQUEST_TEMPLATE.md` asks for one coherent, reviewable work package, which may relate to several issues, with its scope, verification, contract changes, risks and review status.
It does not pre-fill any issue-closing reference.

GitHub reads issue forms and the pull request template from the default branch.
Until this work is merged into `main`, they are not offered to new issues and pull requests.
This is a platform limitation and was not worked around by changing `main` directly.

## Labels

Labels that already existed were left unchanged: `bug`, `documentation`, `enhancement` and GitHub's other defaults.
These were added:

| Label | Meaning |
| --- | --- |
| `security` | Hardening or a safety-boundary concern, reported without exploit details. |
| `ci` | Workflows, runners and checks. |
| `decision-needed` | Waiting for a decision by the owner. |
| `priority/low` | Can wait; cosmetic or with an easy workaround. |
| `priority/medium` | Belongs in the next work package. |
| `priority/high` | Blocks other work, or a safety-boundary rule can be bypassed. |

`bug`, `enhancement`, `documentation`, `security` and `ci` say what kind of issue it is.
`decision-needed` says what it is waiting for.
`priority/*` says how soon.
Combine one of each kind where it helps.

## Independent review handoff

Scope for reviewers: the diff `main...feature/o1-work-package-cli`, all of it new code, tests and documentation.
Review tracks are launched by the owner, not by this work.

Where to start:

1. [docs/o1-contract.md](o1-contract.md) is the specification; check the code against it.
2. `src/omnitient/context.py` and `src/omnitient/rules.py`: path safety, symlink handling, forbidden files, size bounds.
3. `src/omnitient/render.py` with `tests/test_render.py`: can embedded context or package text impersonate or hide a control section under CommonMark? The tests parse with `markdown-it-py`.
4. `src/omnitient/store.py` and `src/omnitient/safeio.py`: no-clobber creation, lock file, conflict detection, bounded reads, and the handoff bundle protocol (contract section 8.4).
5. `src/omnitient/readiness.py` and `src/omnitient/ops.py`: do the approval and staleness rules match the contract?

6. The readiness delta, added after the O1 code: `LICENSE`, `REUSE.toml` and the notices; `.github/workflows/ci.yml` with [ci.md](ci.md) (runner selection, permissions, the public transition); the issue forms and the pull request template.
   Check that the workflow cannot reach a self-hosted runner from outside this repository, and that nothing in it has write access or a secret.

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
