"""The client wheel/sdist must build and install without Core or its backend."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from rich.text import Text

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built_client(tmp_path_factory):
    output = tmp_path_factory.mktemp("client-dist")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.build_terminal_client",
            "--no-isolation",
            "--out-dir",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def test_wheel_has_only_allowlisted_client_modules_and_small_dependencies(built_client):
    wheel = next(built_client.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "nebula/terminal/notes.py" in names
        assert "nebula/terminal/board.py" in names
        assert "nebula/terminal/conversations.py" in names
        assert "nebula/terminal/followups.py" in names
        assert "nebula/terminal/projects.py" in names
        assert not any(name.startswith("nebula/v3/") for name in names)
        assert all(
            name.startswith(("nebula/terminal/", "nebula_client-"))
            or name in {"nebula/__init__.py", "nebula/chat_cli.py"}
            for name in names
        )
        metadata = archive.read(
            next(name for name in names if name.endswith("/METADATA"))
        ).decode()
        dependencies = {
            Requirement(line.split(": ", 1)[1]).name
            for line in metadata.splitlines()
            if line.startswith("Requires-Dist:")
        }
        assert dependencies == {"httpx", "typer", "prompt-toolkit", "rich"}
        assert (
            "nebula.chat_cli:main"
            in archive.read(
                next(name for name in names if name.endswith("/entry_points.txt"))
            ).decode()
        )
        assert "License-Expression: BSD-2-Clause" in metadata
    assert wheel.stat().st_size < 200_000


def test_source_distribution_is_self_contained_and_rebuilds(built_client, tmp_path):
    source = next(built_client.glob("*.tar.gz"))
    with tarfile.open(source) as archive:
        assert not any("/v3/" in name for name in archive.getnames())
        archive.extractall(tmp_path, filter="data")
    tree = next(path for path in tmp_path.iterdir() if path.is_dir())
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", str(tree)],
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list((tree / "dist").glob("*.whl"))


def test_clean_install_imports_no_core_and_exposes_shell(built_client, tmp_path):
    environment = tmp_path / "client-env"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "bin" / "python"
    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            str(next(built_client.glob("*.whl"))),
        ],
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    code = "import sys,json; import nebula.chat_cli, nebula.terminal.shell, nebula.terminal.tui; assert not any(m in sys.modules for m in ('nebula.v3','sqlalchemy','chromadb','playwright','PySide6')); import importlib.metadata as m; print(json.dumps(sorted(d.metadata['Name'] for d in m.distributions())))"
    result = subprocess.run(
        [str(python), "-I", "-c", code], text=True, capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert not {"nebula-ai", "SQLAlchemy", "chromadb", "playwright"} & set(
        json.loads(result.stdout)
    )
    help_result = subprocess.run(
        [str(environment / "bin" / "nebula-chat"), "shell", "--help"],
        text=True,
        capture_output=True,
        timeout=10,
        env={**os.environ, "GITHUB_ACTIONS": "true"},
    )
    help_text = Text.from_ansi(help_result.stdout).plain
    assert (
        help_result.returncode == 0
        and "--project" in help_text
        and "--chat" in help_text
    ), help_result.stdout + help_result.stderr
    assert not (tmp_path / "nebula.db").exists()
