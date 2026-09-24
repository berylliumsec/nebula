"""Freezer entry point for the Nebula Tauri sidecar."""

import sys
from pathlib import Path

GATEWAY_COMMAND = "mcp-gateway"
# The frozen build bundles the standard-library-only shim source here.
GATEWAY_SHIM = Path("nebula") / "v3" / "mcp_gateway.py"


def run_gateway_shim(arguments: list[str]) -> int:
    """Serve one harness MCP connection without importing Nebula Core.

    Every harness connection starts a shim and waits for it before the first
    turn. Importing the Core (API, storage, providers) would cost seconds and
    hundreds of MB per connection, and importing any ``nebula.v3`` module runs
    the package's domain imports, so the bundled source runs as a script.
    """

    import runpy

    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1] / "src"))
    namespace = runpy.run_path(str(root / GATEWAY_SHIM), run_name="nebula_mcp_gateway")
    return int(namespace["main"](arguments))


if __name__ == "__main__":
    if sys.argv[1:2] == [GATEWAY_COMMAND]:
        raise SystemExit(run_gateway_shim(sys.argv[2:]))
    from nebula.v3.cli import main

    main()
