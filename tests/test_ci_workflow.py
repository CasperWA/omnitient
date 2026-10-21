# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""The CI workflow's job conditions and runner selection, checked as predicates.

This evaluates only the operators the two job-level expressions use, on fixed event
contexts. It is not an executed workflow run and not a general expression
interpreter: any other token is refused.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
JOBS = ("checks", "tests")
SELF_HOSTED = ["self-hosted", "Linux", "X64", "sintef-desktop-cwa"]
HOSTED = "ubuntu-24.04"
REPO = "CasperWA/omnitient"

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<str>'[^']*')
        |(?P<fn>fromJSON)\(
        |(?P<ref>[A-Za-z_][\w.]*)
        |(?P<op>&&|\|\||==|!=|!|\(|\))
    )""",
    re.VERBOSE,
)


def evaluate(expression: str, context: dict[str, Any]) -> Any:
    """Evaluate `expression` with GitHub's `&&`/`||` operand semantics."""
    out: list[str] = []
    position = 0
    text = expression.strip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise AssertionError(f"unsupported expression text: {text[position:]!r}")
        position = match.end()
        if match["str"]:
            out.append(repr(match["str"][1:-1]))
        elif match["fn"]:
            out.append("json.loads(")
        elif match["ref"]:
            out.append(f"context.get({match['ref']!r})")
        else:
            out.append({"&&": " and ", "||": " or ", "!": " not "}.get(match["op"]))
            if out[-1] is None:
                out[-1] = match["op"]
    return eval("".join(out), {"__builtins__": {}, "json": json}, {"context": context})


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def job_block(name: str) -> str:
    match = re.search(
        rf"^  {name}:\n(?P<body>(?:    .*\n|\n)+)", workflow_text(), re.MULTILINE
    )
    assert match, f"job {name} not found"
    return match["body"]


def job_if(name: str) -> str:
    match = re.search(
        r"^    if: >-\n(?P<body>(?:      .*\n)+)", job_block(name), re.MULTILINE
    )
    assert match, f"job {name} has no folded if"
    return " ".join(line.strip() for line in match["body"].splitlines())


def job_runs_on(name: str) -> str:
    match = re.search(
        r"^    runs-on: \$\{\{ (?P<expr>.+) \}\}$", job_block(name), re.MULTILINE
    )
    assert match, f"job {name} has no runs-on expression"
    return match["expr"]


def event(
    name: str,
    *,
    private: bool,
    ref: str = "refs/heads/main",
    head_repo: str | None = None,
    actor: str = "casper",
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "github.event_name": name,
        "github.event.repository.private": private,
        "github.ref": ref,
        "github.repository": REPO,
        "github.triggering_actor": actor,
    }
    if name == "pull_request":
        context["github.event.pull_request.head.repo.full_name"] = head_repo or REPO
        context["github.ref"] = "refs/pull/7/merge"
    return context


# (case, context, eligible)
CASES = [
    ("private manual main", event("workflow_dispatch", private=True), True),
    ("public manual main", event("workflow_dispatch", private=False), True),
    (
        "private manual branch",
        event("workflow_dispatch", private=True, ref="refs/heads/ci/x"),
        False,
    ),
    (
        "public manual branch",
        event("workflow_dispatch", private=False, ref="refs/heads/ci/x"),
        False,
    ),
    (
        "private manual tag named main",
        event("workflow_dispatch", private=True, ref="refs/tags/main"),
        False,
    ),
    ("private push to main", event("push", private=True), True),
    ("public push to main", event("push", private=False), True),
    ("private same-repository PR", event("pull_request", private=True), True),
    ("public same-repository PR", event("pull_request", private=False), True),
    (
        "private fork PR",
        event("pull_request", private=True, head_repo="someone/omnitient"),
        False,
    ),
    (
        "public fork PR",
        event("pull_request", private=False, head_repo="someone/omnitient"),
        True,
    ),
    (
        "private Dependabot PR",
        event("pull_request", private=True, actor="dependabot[bot]"),
        False,
    ),
    (
        "public Dependabot PR",
        event("pull_request", private=False, actor="dependabot[bot]"),
        True,
    ),
]


@pytest.mark.parametrize("job", JOBS)
@pytest.mark.parametrize(
    ("context", "eligible"),
    [pytest.param(c, e, id=case) for case, c, e in CASES],
)
def test_job_condition_per_event(
    job: str, context: dict[str, Any], eligible: bool
) -> None:
    assert bool(evaluate(job_if(job), context)) is eligible


@pytest.mark.parametrize("job", JOBS)
@pytest.mark.parametrize(
    ("private", "selector"), [(True, SELF_HOSTED), (False, HOSTED)]
)
@pytest.mark.parametrize("name", ["workflow_dispatch", "push", "pull_request"])
def test_runner_selection_follows_visibility_for_every_event(
    job: str, private: bool, selector: object, name: str
) -> None:
    assert evaluate(job_runs_on(job), event(name, private=private)) == selector


def test_both_jobs_share_one_condition() -> None:
    assert job_if("checks") == job_if("tests")


def test_manual_trigger_has_no_inputs_and_uses_the_selected_ref() -> None:
    text = workflow_text()
    assert re.search(
        r"^on:\n(?:  .*\n)*  workflow_dispatch:\n(?!    )", text, re.MULTILINE
    )
    assert "inputs:" not in text
    assert "github.event.inputs" not in text
    assert "github.event.pull_request" not in text.split("jobs:")[0]
    for job in JOBS:
        checkout = re.search(
            r"uses: actions/checkout@v7\n        with:\n(?P<with>(?:          .*\n)+)",
            job_block(job),
        )
        assert checkout
        assert checkout["with"] == "          persist-credentials: false\n"


def test_manual_run_has_its_own_concurrency_group() -> None:
    group = re.search(r"^  group: (?P<g>.+)$", workflow_text(), re.MULTILINE)
    assert group
    # The group is a template; render its `${{ }}` parts with the same evaluator.
    parts = re.split(r"\$\{\{ (.+?) \}\}", group["g"])

    def render(context: dict[str, Any]) -> str:
        context = {**context, "github.workflow": "CI"}
        return "".join(
            str(evaluate(part, context) or "") if index % 2 else part
            for index, part in enumerate(parts)
        )

    # A manual run must not displace a pending push run for the same ref.
    assert render(event("workflow_dispatch", private=True)) == (
        "ci-CI-manual-refs/heads/main"
    )
    assert render(event("push", private=True)) == "ci-CI-refs/heads/main"


def test_permissions_and_cancellation_are_unchanged() -> None:
    text = workflow_text()
    assert "permissions:\n  contents: read\n" in text
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text
    assert text.count("fail-fast: false") == 1
    assert "pull_request_target" not in text
