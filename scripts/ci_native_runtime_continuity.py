#!/usr/bin/env python3
"""Build two pinned native packages and run the one owned GUI continuity case.

CI supplies official build dependencies, Poetry 2.1.3, and fresh source checkouts.
Only ``output/evidence`` is suitable for artifact upload. No release or install
operation is performed. Failed private runtime state is deliberately retained.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib


TARGET = "x86_64-unknown-linux-gnu"
PRODUCT = ("src/nebula/v3", "ui/src", "ui/src-tauri/src")
CASE = (
    "tests/v3/test_packaged_runtime_continuity.py::"
    "test_real_packaged_windows_preserve_held_turn_and_draft_through_code_rollback"
)
GENERATED = (
    "dist", "build", "ui/dist", "ui/node_modules", "ui/src-tauri/binaries",
    "ui/src-tauri/resources/nebula-runtime",
    "ui/src-tauri/resources/notebook-runtime",
    "ui/src-tauri/resources/playwright-browsers",
)
RECLAIM = tuple(path for path in GENERATED if path not in {"build", "ui/dist"}) + (
    "build/nebula-core", "build/nebula-runtime", "build/pyinstaller",
    "build/nebula-core-stage", "build/notebook-downloads",
)
GUI_FILES = re.compile(
    r"(?:continuity-(?:receipt|failure)\.json|supervisor\.log|"
    r"(?:baseline|candidate|rollback-baseline)-desktop\.log|"
    r"desktop-[a-z_]+-[0-9]+\.stderr|(?:shutdown|cleanup)\.(?:stdout|stderr))"
)


def git(source: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(source), *arguments], text=True
    ).strip()


def source_pin(source: Path, expected: str | None = None) -> dict:
    source = source.resolve(strict=True)
    if Path(git(source, "rev-parse", "--show-toplevel")).resolve() != source:
        raise ValueError("source must be a complete Git checkout root")
    commit = git(source, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or (expected and commit != expected):
        raise ValueError("actual source HEAD differs from its pinned build")
    if git(source, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("source checkout must be clean before building")
    for relative in GENERATED:
        if git(source, "ls-files", "--others", "--ignored", "--exclude-standard", "--", relative):
            raise ValueError("source requires fresh generated build paths: " + relative)
    locks = {}
    for relative in ("poetry.lock", "ui/package-lock.json", "ui/src-tauri/Cargo.lock"):
        git(source, "cat-file", "-e", "HEAD:" + relative)
        locks[relative] = hashlib.sha256((source / relative).read_bytes()).hexdigest()
    packages = {item["name"].lower(): item["version"] for item in tomllib.loads((source / "poetry.lock").read_text())["package"]}
    return {
        "source": str(source), "commit": commit, "locks": locks,
        "product_trees": {path: git(source, "rev-parse", f"HEAD:{path}") for path in PRODUCT},
        "gui_versions": {name: packages[name] for name in ("pytest", "httpx", "pyinstaller")},
    }


def output_path(output: Path, sources: list[Path]) -> Path:
    output = output.resolve()
    if output.exists() or any(
        output.is_relative_to(source) or source.is_relative_to(output) for source in sources
    ):
        raise ValueError("output must be a new directory separate from both sources")
    return output


def owned_file(root: Path, path: Path) -> Path:
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("evidence must be an owned regular file")
    return path


def gui_evidence(root: Path, path: Path) -> Path:
    if path.parent.name != "packaged-continuity" or not GUI_FILES.fullmatch(path.name):
        raise ValueError("file is outside the explicit GUI evidence allowlist")
    return owned_file(root, path)


def remove_owned(root: Path, path: Path):
    if path.is_symlink() or path.resolve() == root.resolve() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("cleanup must remain inside the owned private build root")
    shutil.rmtree(path)


def redact(text: str, secrets: list[str]) -> str:
    for value in sorted(set(secrets), key=len, reverse=True):
        if len(value) >= 8:
            text = text.replace(value, "<redacted>")

    def structured(value, depth=0):
        if isinstance(value, dict):
            return {
                key: "<redacted>"
                if re.fullmatch(
                    r"(?i)(?:ipc_token|operator_token|access_token|token|password|secret)",
                    key,
                )
                else structured(item, depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [structured(item, depth + 1) for item in value]
        if isinstance(value, str):
            if depth >= 16:
                return "<redacted nested data>"
            try:
                nested = json.loads(value)
            except (ValueError, RecursionError):
                return value
            if isinstance(nested, (dict, list, str)):
                return json.dumps(structured(nested, depth + 1))
        return value

    # Receipts can quote serialized errors. Their fields must remain safe after
    # confirmed driver cleanup removes the private fixture token file.
    try:
        text = json.dumps(structured(json.loads(text)), indent=2)
    except (ValueError, RecursionError):
        lines = []
        for line in text.splitlines(keepends=True):
            try:
                sanitized = json.dumps(
                    structured(json.loads(line)), separators=(",", ":")
                )
            except (ValueError, RecursionError):
                lines.append(line)
            else:
                lines.append(sanitized + ("\n" if line.endswith("\n") else ""))
        text = "".join(lines)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+", r"\1<redacted>", text)
    text = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1<redacted>@", text)
    return re.sub(
        r'''(?i)((?:["']?(?:ipc_token|operator_token|access_token|token|password|secret)["']?\s*[:=]\s*|#token=)["']?)[^\s"',}]+''',
        r"\1<redacted>", text,
    )


class NativeJob:
    def __init__(self, baseline: Path, candidate: Path, output: Path):
        self.pins = {name: source_pin(path) for name, path in (("baseline", baseline), ("candidate", candidate))}
        if self.pins["baseline"]["product_trees"] == self.pins["candidate"]["product_trees"]:
            raise ValueError("continuity requires two actual product-code revisions")
        sources = [Path(pin["source"]) for pin in self.pins.values()]
        self.candidate = sources[1]
        for relative in ("tests/v3/packaged_continuity_artifacts.py", CASE.split("::")[0], "scripts/package_audit.py"):
            git(self.candidate, "cat-file", "-e", "HEAD:" + relative)
        self.output = output_path(output, sources)
        self.output.mkdir(mode=0o700, parents=True)
        self.private, self.evidence = self.output / "private", self.output / "evidence"
        self.private.mkdir(mode=0o700)
        self.evidence.mkdir(mode=0o700)
        self.commands, self.cleanup, self.space = [], [], []
        self.secrets = [value for key, value in os.environ.items() if re.search(r"TOKEN|PASSWORD|SECRET|PRIVATE_KEY", key)]
        self.write("sources.json", self.pins)

    def write(self, name: str, value) -> Path:
        path = self.evidence / name
        path.write_text(redact(json.dumps(value, indent=2) + "\n", self.secrets))
        return path

    def run(self, name: str, argv: list[str], cwd: Path, env: dict, timeout=2700) -> str:
        argv = list(map(str, argv))
        self.commands.append({"name": name, "argv": argv, "cwd": str(cwd)})
        self.write("commands.json", self.commands)
        print("native continuity: " + name, flush=True)
        raw = self.private / (name + ".log")
        try:
            with raw.open("w") as log:
                result = subprocess.run(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        finally:
            text = raw.read_text(errors="replace")
            (self.evidence / (name + ".log")).write_text(redact(text, self.secrets))
        if result.returncode:
            raise RuntimeError(f"{name} failed with exit {result.returncode}; inspect evidence")
        return text

    def environment(self, pin: dict, venv: Path) -> dict:
        env = {key: value for key, value in os.environ.items() if key not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"} and not key.startswith("NEBULA_")}
        env.update(
            VIRTUAL_ENV=str(venv), PATH=str(venv / "bin") + os.pathsep + env["PATH"],
            POETRY_VIRTUALENVS_CREATE="true", POETRY_NO_INTERACTION="1", PYTHONNOUSERSITE="1",
            CARGO_TARGET_DIR=str(self.private / "cargo-target"), CARGO_BUILD_JOBS="2", CARGO_INCREMENTAL="0",
            NEBULA_BUILD_COMMIT=pin["commit"], NEBULA_BUILD_TARGET=TARGET,
            NEBULA_DISTRIBUTION_CHANNEL="managed", NEBULA_DISTRIBUTION="managed", NO_STRIP="1",
        )
        return env

    def build(self, name: str) -> tuple[Path, Path, dict]:
        pin = self.pins[name]
        source, venv = Path(pin["source"]), self.private / (name + "-venv")
        self.space.append({"phase": "before-" + name, **shutil.disk_usage(self.output)._asdict()})
        self.write("disk-space.json", self.space)
        env = self.environment(pin, venv)
        self.run(name + "-venv", [sys.executable, "-m", "venv", str(venv)], source, env)
        self.run(name + "-install", ["poetry", "install", "--with", "dev", "--no-interaction"], source, env)
        observed = self.run(name + "-env", ["poetry", "env", "info", "--path"], source, env).strip()
        if Path(observed).resolve() != venv:
            raise ValueError("Poetry did not use the separate owned build environment")
        self.run(name + "-npm", ["npm", "--prefix", "ui", "ci"], source, env)
        for resource, module in (("playwright-browsers", "stage_playwright_runtime"), ("notebook-runtime", "stage_notebook_runtime")):
            self.run(name + "-" + module, ["poetry", "run", "python", "-m", "scripts." + module, "ui/src-tauri/resources/" + resource, "--target", TARGET], source, env)
        self.run(name + "-ui", ["npm", "--prefix", "ui", "run", "build"], source, env)
        self.run(name + "-core", ["poetry", "run", "python", "-m", "scripts.build_nebula_core"], source, env)
        config = self.private / "skip-duplicate-ui.json"
        config.write_text('{"build":{"beforeBuildCommand":""}}\n')
        self.run(name + "-native", ["npm", "run", "tauri", "--", "build", "--ci", "--target", TARGET, "--bundles", "deb", "--config", "src-tauri/tauri.managed.conf.json", "--config", str(config), "--", "--locked"], source / "ui", env)
        packages = list((self.private / "cargo-target" / TARGET / "release/bundle/deb").glob("*.deb"))
        if len(packages) != 1:
            raise ValueError("expected exactly one actual managed DEB")
        package = packages[0]
        self.write(name + "-package.json", {"sha256": hashlib.sha256(package.read_bytes()).hexdigest(), "bytes": package.stat().st_size})
        extracted = self.output / (name + "-package")
        self.run(name + "-extract", ["dpkg-deb", "--extract", str(package), str(extracted)], source, env)
        self.run(name + "-audit", [str(venv / "bin/python"), str(self.candidate / "scripts/package_audit.py"), "--tree", str(extracted)], source, env)
        build_commands = self.write(name + "-build-command.json", [item for item in self.commands if item["name"].startswith(name + "-")])
        build_log = self.evidence / (name + "-build.log")
        build_log.write_text("\n".join((self.evidence / (item["name"] + ".log")).read_text() for item in self.commands if item["name"].startswith(name + "-")))
        receipt = self.evidence / (name + "-native-build.json")
        self.run(name + "-provenance", [str(venv / "bin/python"), str(self.candidate / "tests/v3/packaged_continuity_artifacts.py"), "record-build", "--source", str(source), "--source-commit", pin["commit"], "--desktop", str(extracted / "usr/bin/nebula-ui"), "--connector", str(source / "dist/nebula-core"), "--resources", str(extracted / "usr/lib/Nebula"), "--build-command", str(build_commands), "--build-log", str(build_log), "--output", str(receipt)], source, env)
        if git(source, "rev-parse", "HEAD") != pin["commit"] or git(source, "diff", "HEAD", "--"):
            raise ValueError("build changed the pinned tracked source")
        self.run(name + "-reclaim", ["git", "clean", "-fdX", "--", *RECLAIM], source, env)
        remove_owned(self.private, package.parent)
        self.cleanup.append({"source": str(source), "ignored_generated_paths": list(RECLAIM), "removed_owned_package_directory": str(package.parent)})
        self.write("cleanup.json", self.cleanup)
        return receipt, venv, env

    def export_gui(self):
        fixture_root = self.private / "native-journey"
        for token in fixture_root.rglob("operator-token"):
            if not token.is_symlink() and token.is_file() and token.resolve().is_relative_to(fixture_root):
                self.secrets.append(token.read_text().strip())
        for path in fixture_root.rglob("*"):
            if GUI_FILES.fullmatch(path.name):
                if path.parent.name != "packaged-continuity":
                    continue
                selected = gui_evidence(fixture_root, path)
                relative = selected.relative_to(fixture_root)
                destination = self.evidence / "native" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(redact(selected.read_text(errors="replace"), self.secrets))
        raw = self.private / "native-journey.log"
        if raw.is_file():
            (self.evidence / raw.name).write_text(redact(raw.read_text(errors="replace"), self.secrets))

    def execute(self):
        baseline, baseline_venv, _ = self.build("baseline")
        remove_owned(self.private, baseline_venv)
        candidate, candidate_venv, env = self.build("candidate")
        query = "import importlib.metadata,json;print(json.dumps({n:importlib.metadata.version(n) for n in ['pytest','httpx','pyinstaller']}))"
        versions = json.loads(self.run("gui-versions", [str(candidate_venv / "bin/python"), "-c", query], self.candidate, env))
        if versions != self.pins["candidate"]["gui_versions"]:
            raise ValueError("installed GUI dependencies differ from the candidate lock")
        constraints = self.evidence / "gui-requirements.txt"
        constraints.write_text("".join(f"{name}=={version}\n" for name, version in versions.items()))
        gui = self.private / "gui-venv"
        self.run("gui-venv", ["/usr/bin/python3", "-m", "venv", "--system-site-packages", str(gui)], self.candidate, env)
        self.run("gui-install", [str(gui / "bin/python"), "-m", "pip", "install", "-r", str(constraints)], self.candidate, env)
        inputs = self.evidence / "native-inputs.json"
        self.run("write-inputs", [str(candidate_venv / "bin/python"), str(self.candidate / "tests/v3/packaged_continuity_artifacts.py"), "write-inputs", "--baseline", str(baseline), "--candidate", str(candidate), "--output", str(inputs)], self.candidate, env)
        env.update(VIRTUAL_ENV=str(gui), PATH=str(gui / "bin") + os.pathsep + env["PATH"])
        remove_owned(self.private, candidate_venv)
        remove_owned(self.private, self.private / "cargo-target")
        self.cleanup.append({"removed_owned_paths": [str(baseline_venv), str(candidate_venv), str(self.private / "cargo-target")], "private_runtime_cleanup": "Owned driver verifies exact shutdown; unconfirmed state is retained"})
        self.write("cleanup.json", self.cleanup)
        self.space.append({"phase": "before-GUI", **shutil.disk_usage(self.output)._asdict()})
        self.write("disk-space.json", self.space)
        env.update(NEBULA_TEST_PACKAGED_CONTINUITY_INPUTS=str(inputs), PYTHONPATH=str(self.candidate), GTK_MODULES="atk-bridge", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        for inherited in ("NO_AT_BRIDGE", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
            env.pop(inherited, None)
        argv = [str(gui / "bin/python"), "-m", "pytest", "-q", CASE]
        collected = self.run("native-collect", [*argv, "--collect-only"], self.candidate, env)
        if re.search(r"\b1 test collected\b", collected) is None:
            raise ValueError("native selection must collect exactly one case")
        self.write("native-selection.json", {"sources": self.pins, "python": [CASE], "expected_tests": 1, "timeout_seconds": 900, "retries": 0, "exclusions": "No broad tests, release, signing, deployment or existing installation"})
        try:
            self.run("native-journey", ["timeout", "--kill-after=15s", "900s", "xvfb-run", "-a", "-s", "-screen 0 1440x900x24 -nolisten tcp", "dbus-run-session", "--", *argv, "--tb=short", "--basetemp=" + str(self.private / "native-journey")], self.candidate, env, timeout=930)
        finally:
            self.export_gui()
        receipts = list((self.private / "native-journey").rglob("continuity-receipt.json"))
        if len(receipts) != 1 or json.loads(owned_file(self.private, receipts[0]).read_text()).get("result") != "passed":
            raise ValueError("a passing real GUI receipt is required; skipped tests are insufficient")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline-source", "candidate-source", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    arguments = parser.parse_args()
    job = None
    try:
        job = NativeJob(arguments.baseline_source, arguments.candidate_source, arguments.output)
        job.execute()
        job.write("result.json", {"result": "passed", "evidence": str(job.evidence), "private_state": str(job.private)})
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        if job:
            job.write("result.json", {"result": "failed", "error_type": type(error).__name__, "detail": str(error), "private_state_retained": str(job.private)})
        print("native continuity failed: " + type(error).__name__ + ": " + redact(str(error), job.secrets if job else []), file=sys.stderr)
        return 1
    finally:
        if job:
            print(json.dumps({"upload_only": str(job.evidence), "private_state_do_not_upload": str(job.private)}))


if __name__ == "__main__":
    raise SystemExit(main())
