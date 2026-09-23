#!/usr/bin/env python3
"""Capture the Python assistant compatibility baseline without starting its lifespan.

All database/artifact paths are temporary. No provider, tool or browser work is
started. Capturing routes/schemas proves inventory only, not Rust parity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def assistant_path(path):
    return path.startswith(("/api/v1/chat/", "/api/v1/chat-")) or path == "/api/v1/chat"


def assistant_openapi(schema):
    """Retain only assistant paths and transitively referenced schemas."""
    paths = {p: v for p, v in schema["paths"].items() if assistant_path(p)}
    schemas = schema.get("components", {}).get("schemas", {})
    selected = {}

    def visit(value):
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            prefix = "#/components/schemas/"
            if ref.startswith(prefix):
                name = ref[len(prefix) :]
                if name not in selected:
                    selected[name] = schemas[name]
                    visit(selected[name])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(paths)
    return {
        "openapi": schema["openapi"],
        "info": schema["info"],
        "paths": paths,
        "components": {
            "schemas": dict(sorted(selected.items())),
            "securitySchemes": schema.get("components", {}).get("securitySchemes", {}),
        },
    }


def collect_contract():
    from fastapi.routing import APIRoute, APIWebSocketRoute

    from nebula.v3.api import create_app
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.database import CURRENT_SCHEMA_VERSION, Database
    from nebula.v3.domain import ENTITY_MODEL_BY_KIND
    from nebula.v3.storage import NebulaStore

    with tempfile.TemporaryDirectory(prefix="nebula-assistant-contract-") as directory:
        root = Path(directory)
        database = Database(root / "nebula.db")
        try:
            app = create_app(
                NebulaStore(database),
                artifact_store=ArtifactStore(root / "artifacts"),
                auth_token="inventory-only-no-listener",
                enable_executable_missions=False,
                bootstrap_workspace=False,
            )
            routes = []
            for route in app.routes:
                if not assistant_path(getattr(route, "path", "")):
                    continue
                if isinstance(route, APIRoute):
                    routes.append(
                        {
                            "path": route.path,
                            "methods": sorted(route.methods),
                            "transport": "http",
                            "endpoint": f"{route.endpoint.__module__}.{route.endpoint.__qualname__}",
                        }
                    )
                elif isinstance(route, APIWebSocketRoute):
                    routes.append(
                        {
                            "path": route.path,
                            "methods": [],
                            "transport": "websocket",
                            "endpoint": f"{route.endpoint.__module__}.{route.endpoint.__qualname__}",
                        }
                    )
            contract = {
                "format": "nebula.assistant-compatibility/v1",
                "baseline_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "application_schema_version": CURRENT_SCHEMA_VERSION,
                "routes": sorted(routes, key=lambda r: (r["path"], r["methods"])),
                "openapi": assistant_openapi(app.openapi()),
                "entities": {
                    kind: model.model_json_schema()
                    for kind, model in sorted(ENTITY_MODEL_BY_KIND.items())
                    if kind.startswith("chat_")
                },
                "source_sha256": {
                    str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((ROOT / "src/nebula/v3").glob("chat*.py"))
                },
                "rust_implemented_routes": [],
                "rust_parity_verified": False,
            }
        finally:
            database.engine.dispose()
    return contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    contract = collect_contract()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(contract, sort_keys=True, indent=2) + "\n")
    print(
        json.dumps(
            {
                "routes": len(contract["routes"]),
                "entities": len(contract["entities"]),
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
