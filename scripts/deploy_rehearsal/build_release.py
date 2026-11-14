#!/usr/bin/env python3
"""Build a source runtime bundle from a Nebula worktree with a release marker.

usage: build_release.py <worktree> <name>
Writes $REHEARSAL_ROOT/releases/<name> and prints its
version and manifest digest. The marker file makes two bundles of the same
commit distinct releases, the way two deploys of different commits would be.
"""

import os
from pathlib import Path
import shutil
import sys
import tempfile

worktree, name = Path(sys.argv[1]).resolve(), sys.argv[2]
sys.path.insert(0, str(worktree / "src"))
from nebula.v3.runtime_bundle import build_source_bundle  # noqa: E402

destination = (
    Path(
        os.getenv(
            "REHEARSAL_ROOT", str(Path.home() / "nebula-scratch/deploy-continuity")
        )
    )
    / "releases"
    / name
)
if destination.exists():
    raise SystemExit(f"{destination} exists")
destination.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
    package = Path(scratch) / "nebula"
    shutil.copytree(
        worktree / "src/nebula",
        package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (package / "v3/_rehearsal_release.py").write_text(f'RELEASE = "{name}"\n')
    # Rehearsal-only accommodations, never committed:
    # 1. the separate chat_queue missing-await fix (follow-ups never run on main);
    # 2. host pressure thresholds above 100% so a CPU-saturated shared host
    #    does not hold every admission; the host pressure RPC still runs.
    queue = package / "v3/chat_queue.py"
    text = queue.read_text()
    for call in (
        "self.chat.start_provider_turn(prepared)\n",
        "self.harness.start_chat_turn(turn.id)\n",
    ):
        text = text.replace(
            "                " + call, "                await " + call, 1
        )
    text = text.replace(
        "_, _, turn = self.harness.prepare_chat(",
        "_, _, turn = await self.harness.prepare_chat(",
        1,
    )
    text = text.replace("await await ", "await ")
    queue.write_text(text)
    load = package / "v3/host_load.py"
    text = load.read_text()
    assert text.count("gt=0, le=1, allow_inf_nan=False") == 4
    load.write_text(
        text.replace(
            "gt=0, le=1, allow_inf_nan=False", "gt=0, le=100, allow_inf_nan=False"
        )
    )
    manifest, digest = build_source_bundle(
        package,
        Path(os.getenv("REHEARSAL_UI_DIR", str(Path.home() / "nebula/ui/dist"))),
        destination,
    )
print(manifest.version, digest, destination)
