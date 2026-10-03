#!/usr/bin/env python3
"""Install Nebula's tracked privacy hooks into the shared local Git hooks directory."""

import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        print("Cannot locate the Git hooks directory.", file=sys.stderr)
        return 1
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = root / common_dir
    hooks_dir = common_dir.resolve() / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    sources = {
        "pre-commit": root / ".githooks" / "pre-commit",
        "commit-msg": root / ".githooks" / "commit-msg",
        "nebula-public-commit-guard.py": root / "scripts" / "public_commit_guard.py",
    }
    for name, source in sources.items():
        target = hooks_dir / name
        if target.is_symlink() or (target.exists() and target.read_bytes() != source.read_bytes()):
            print(f"Existing hook needs manual integration: {target}", file=sys.stderr)
            return 1
    for name, source in sources.items():
        target = hooks_dir / name
        if target.exists():
            continue
        shutil.copy2(source, target)
        print(f"Installed {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
