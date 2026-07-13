"""Compare Toolbox catalogs by their architecture-independent interface contract."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Sequence

from nebula.v3.tool_interfaces import ToolInterfaceCatalog, load_interface_catalog


_PARENTHETICAL_LIST = re.compile(r"\(([^()\n]+)\)")


class InterfaceCatalogMismatch(RuntimeError):
    """Raised when two image catalogs expose different model-facing interfaces."""


def _normalize_unordered_parenthetical_lists(description: str) -> str:
    """Stabilize help lists emitted from unordered sets in upstream CLIs."""

    def replace(match: re.Match[str]) -> str:
        values = [item.strip() for item in match.group(1).split(",")]
        if len(values) < 3 or any(not item or " " in item for item in values):
            return match.group(0)
        return f"({', '.join(sorted(values, key=str.casefold))})"

    return _PARENTHETICAL_LIST.sub(replace, description)


def architecture_independent_contract(
    catalog: ToolInterfaceCatalog,
) -> dict[str, Any]:
    """Return fields that must be identical across platform-native images.

    Raw help evidence can contain architecture triples and upstream tools may emit
    unordered lists in help text. Uncatalogued executable inventories also differ
    across Debian architectures. Neither is used to build a selected command's
    strict model-facing interface.
    """

    tools = copy.deepcopy(catalog.payload["tools"])
    for tool in tools:
        for command in tool["commands"]:
            command.pop("help_documents")
            for option in command["options"]:
                option["description"] = _normalize_unordered_parenthetical_lists(
                    option["description"]
                )
    catalogued_inventory = sorted(
        (
            copy.deepcopy(item)
            for item in catalog.payload["inventory"]
            if item["catalogued"] is True
        ),
        key=lambda item: (item["name"], item["path"]),
    )
    return {
        "protocol": catalog.payload["protocol"],
        "interface_protocol": catalog.payload["interface_protocol"],
        "toolbox_version": catalog.payload["toolbox_version"],
        "tools": tools,
        "catalogued_inventory": catalogued_inventory,
    }


def _first_difference(left: Any, right: Any, path: str = "$") -> str | None:
    if type(left) is not type(right):
        return f"{path} has different types"
    if isinstance(left, dict):
        if set(left) != set(right):
            return f"{path} has different fields"
        for key in sorted(left):
            difference = _first_difference(left[key], right[key], f"{path}.{key}")
            if difference is not None:
                return difference
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path} has different list lengths"
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=True)):
            difference = _first_difference(left_item, right_item, f"{path}[{index}]")
            if difference is not None:
                return difference
        return None
    if left != right:
        return f"{path} differs: {left!r} != {right!r}"
    return None


def compare_interface_catalogs(
    amd64_catalog: Path, arm64_catalog: Path
) -> dict[str, object]:
    """Validate both catalogs and require equal model-facing contracts."""

    try:
        amd64 = load_interface_catalog(amd64_catalog.read_bytes())
        arm64 = load_interface_catalog(arm64_catalog.read_bytes())
    except Exception as exc:
        raise InterfaceCatalogMismatch("an interface catalog is invalid") from exc

    amd64_contract = architecture_independent_contract(amd64)
    arm64_contract = architecture_independent_contract(arm64)
    difference = _first_difference(amd64_contract, arm64_contract)
    if difference is not None:
        raise InterfaceCatalogMismatch(
            f"Toolbox platform interface contracts differ: {difference}"
        )
    encoded = json.dumps(
        amd64_contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return {
        "contract_sha256": hashlib.sha256(encoded).hexdigest(),
        "tool_count": len(amd64.tools),
        "amd64_catalog_sha256": amd64.digest,
        "arm64_catalog_sha256": arm64.digest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amd64", type=Path, required=True)
    parser.add_argument("--arm64", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = compare_interface_catalogs(args.amd64, args.arm64)
    except InterfaceCatalogMismatch as exc:
        _parser().error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
