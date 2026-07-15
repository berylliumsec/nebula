#!/usr/bin/env python3
"""Source audit for unclassified Nebula 3 failure paths.

The Python check uses the AST, the frontend check delegates to the TypeScript
compiler API, and the Rust check enforces must-use Results plus a small source
audit.  ``diagnostic-expected:`` is intentionally reviewable and is the only
escape hatch for expected control flow that neither logs nor rethrows.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "src" / "nebula" / "v3"
RUST_ROOT = ROOT / "ui" / "src-tauri" / "src"

CLASSIFYING_CALLS = {
    "record_caught_exception",
    "record_diagnostic",
    "create_diagnostic_task",
    "emit_diagnostic",
    "stream_error_frame",
}


def _call_name(node: ast.Call) -> str | None:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _is_classifying_call(node: ast.Call, path: Path) -> bool:
    name = _call_name(node)
    if name in CLASSIFYING_CALLS:
        return True
    return path.name == "diagnostics.py" and name in {
        "record",
        "_emergency_sink_failure",
        "_mark_degraded",
    }


def _is_contextlib_suppress(node: ast.withitem) -> bool:
    expression = node.context_expr
    return (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and isinstance(expression.func.value, ast.Name)
        and expression.func.value.id == "contextlib"
        and expression.func.attr == "suppress"
    )


def audit_python() -> list[str]:
    failures: list[str] = []
    for path in sorted(PYTHON_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            failures.append(f"{path.relative_to(ROOT)}:{exc.lineno}: syntax error")
            continue
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            end = node.end_lineno or node.lineno
            handler_text = "\n".join(lines[node.lineno - 1 : end])
            if "diagnostic-expected:" in handler_text:
                continue
            classified = any(
                isinstance(child, ast.Raise)
                or (isinstance(child, ast.Call) and _is_classifying_call(child, path))
                for statement in node.body
                for child in ast.walk(statement)
            )
            if not classified:
                failures.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: unclassified except handler"
                )

        for node in ast.walk(tree):
            if not isinstance(node, (ast.With, ast.AsyncWith)) or not any(
                _is_contextlib_suppress(item) for item in node.items
            ):
                continue
            end = node.end_lineno or node.lineno
            block = "\n".join(lines[node.lineno - 1 : end])
            if "diagnostic-expected:" not in block:
                failures.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: unclassified contextlib.suppress"
                )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_task"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "asyncio"
            ):
                continue
            if path.name == "diagnostics.py":
                continue
            start = max(0, node.lineno - 3)
            end = min(len(lines), (node.end_lineno or node.lineno) + 1)
            nearby = "\n".join(lines[start:end])
            if "diagnostic-expected:" not in nearby:
                failures.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: raw asyncio.create_task is not supervised"
                )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if any(
                keyword.arg == "ignore_errors"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                failures.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: cleanup errors are ignored"
                )
    return failures


def audit_typescript() -> list[str]:
    completed = subprocess.run(
        ["node", str(ROOT / "scripts" / "audit_typescript_diagnostics.mjs")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        if completed.stdout.strip():
            print(completed.stdout.strip())
        return []
    output = (completed.stderr or completed.stdout).strip()
    return [output or "TypeScript diagnostic audit failed without output"]


def audit_rust() -> list[str]:
    failures: list[str] = []
    ignored = re.compile(r"\blet\s+_\s*=|\.ok\(\)\s*;")
    for path in sorted(RUST_ROOT.rglob("*.rs")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, 1):
            if ignored.search(line) and "diagnostic-expected:" not in line:
                failures.append(
                    f"{path.relative_to(ROOT)}:{line_number}: ignored Rust result"
                )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", action="store_true", dest="check_python")
    parser.add_argument("--typescript", action="store_true", dest="check_typescript")
    parser.add_argument("--rust", action="store_true", dest="check_rust")
    args = parser.parse_args()
    if not (args.check_python or args.check_typescript or args.check_rust):
        args.check_python = args.check_typescript = args.check_rust = True

    failures: list[str] = []
    if args.check_python:
        failures.extend(audit_python())
    if args.check_typescript:
        failures.extend(audit_typescript())
    if args.check_rust:
        failures.extend(audit_rust())
    if failures:
        print("Diagnostic blind-spot audit failed:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("Diagnostic blind-spot audit: zero unclassified handlers or ignored results.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
