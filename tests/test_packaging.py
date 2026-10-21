# SPDX-FileCopyrightText: 2026 Casper Welzel Andersen
# SPDX-FileCopyrightText: 2026 SINTEF Ocean AS
#
# SPDX-License-Identifier: MIT

"""The built wheel installs and works outside the source tree (offline)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.packaging

REPO_ROOT = Path(__file__).resolve().parent.parent


def run(
    args: list[str | Path], cwd: Path, **kwargs: object
) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "VIRTUAL_ENV", "PIP_INDEX_URL"}
    }
    env.update(PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1")
    return subprocess.run(
        [str(a) for a in args], cwd=cwd, env=env, text=True, capture_output=True, check=False, **kwargs  # type: ignore[arg-type]
    )


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    pytest.importorskip("flit_core")
    work = tmp_path_factory.mktemp("packaging")
    wheelhouse = work / "wheelhouse"
    built = run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--wheel-dir",
            wheelhouse,
            REPO_ROOT,
        ],
        cwd=work,
    )
    assert built.returncode == 0, built.stderr
    (wheel,) = wheelhouse.glob("omnitient-*.whl")

    venv = work / "venv"
    assert (
        run([sys.executable, "-m", "venv", "--without-pip", venv], cwd=work).returncode
        == 0
    )
    python = venv / "bin" / "python"
    result = run(
        [
            sys.executable,
            "-m",
            "pip",
            "--python",
            python,
            "install",
            "--no-index",
            "--no-deps",
            wheel,
        ],
        cwd=work,
    )
    assert result.returncode == 0, result.stderr
    return venv, wheel


def test_wheel_contents(installed: tuple[Path, Path]) -> None:
    _, wheel = installed
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata = next(
            archive.read(n).decode() for n in names if n.endswith("METADATA")
        )
        entry_points = next(
            archive.read(n).decode() for n in names if n.endswith("entry_points.txt")
        )
    assert "omnitient/data/work-package.schema.json" in names
    assert "omnitient/cli.py" in names
    assert "omnitient/data/work-package.schema.json.license" in names
    for licence in ("LICENSE", "LICENSES/MIT.txt"):
        assert any(n.endswith(f".dist-info/licenses/{licence}") for n in names)
    assert "License-Expression: MIT" in metadata
    assert not any(n.startswith(("tests/", "docs/")) for n in names)
    assert "Requires-Python: >=3.12" in metadata
    requires = [
        line for line in metadata.splitlines() if line.startswith("Requires-Dist")
    ]
    assert all('extra == "dev"' in line for line in requires)  # no runtime dependencies
    assert "omnitient=omnitient.cli:main" in entry_points.replace(" ", "")


def test_console_script_and_packaged_data_work_outside_the_source_tree(
    installed: tuple[Path, Path], tmp_path: Path
) -> None:
    venv, _ = installed
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    script = venv / "bin" / "omnitient"

    imported = run(
        [
            venv / "bin" / "python",
            "-I",
            "-c",
            "import omnitient; print(omnitient.__file__)",
        ],
        cwd=elsewhere,
    )
    assert imported.returncode == 0, imported.stderr
    assert Path(imported.stdout.strip()).is_relative_to(venv)

    assert run([script, "--version"], cwd=elsewhere).stdout.startswith("omnitient ")
    as_module = run(
        [venv / "bin" / "python", "-I", "-m", "omnitient", "--version"], cwd=elsewhere
    )
    assert as_module.returncode == 0, as_module.stderr
    assert as_module.stdout.startswith("omnitient ")
    schema = run([script, "schema"], cwd=elsewhere)
    assert schema.returncode == 0, schema.stderr
    assert json.loads(schema.stdout)["$defs"]["slug"]

    package = elsewhere / "pkg" / "work-package.json"
    created = run(
        [
            script,
            "new",
            package,
            "--project",
            "demo",
            "--initiative",
            "init",
            "--work-package",
            "wp1",
            "--title",
            "T",
        ],
        cwd=elsewhere,
    )
    assert created.returncode == 0, created.stderr
    rendered = run(
        [
            script,
            "render",
            package,
            "--stage",
            "planning",
            "--out",
            "-",
            "--format",
            "json",
        ],
        cwd=elsewhere,
    )
    assert (
        rendered.returncode == 3
    ), rendered.stderr  # scaffold is not ready: TODO objective
    assert json.loads(rendered.stdout)["status"] == "blocked"
    assert run([script, "validate", package], cwd=elsewhere).returncode == 0


def test_build_leaves_the_source_tree_clean() -> None:
    leftovers = [p.name for p in REPO_ROOT.iterdir() if p.name in {"build", "dist"}]
    assert leftovers == []
