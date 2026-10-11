#!/usr/bin/env python3
"""Install Nebula's tracked privacy hooks into the shared local Git hooks directory."""

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

PRIOR_GUARD_SHA256 = {
    "7b366e21a19ecb3f95504abde1c0273930551606dd2fba5b6b09570e88b89248",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade", action="store_true", help="replace a known earlier Nebula guard")
    args = parser.parse_args()
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
    replacements: set[str] = set()
    for name, source in sources.items():
        target = hooks_dir / name
        if target.is_symlink():
            print(f"Existing hook needs manual integration: {target}", file=sys.stderr)
            return 1
        if target.exists() and target.read_bytes() != source.read_bytes():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if not (args.upgrade and name == "nebula-public-commit-guard.py" and digest in PRIOR_GUARD_SHA256):
                print(f"Existing hook needs manual integration: {target}", file=sys.stderr)
                return 1
            replacements.add(name)
    for name, source in sources.items():
        target = hooks_dir / name
        if target.exists() and name not in replacements:
            continue
        shutil.copy2(source, target)
        print(f"{'Upgraded' if name in replacements else 'Installed'} {target}")
    config = hooks_dir / "nebula-public-commit-review.json"
    if not config.exists():
        shutil.copy2(root / ".githooks" / config.name, config)
        print(f"Installed {config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
