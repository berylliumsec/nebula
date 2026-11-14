"""Forward-compatible decoding of stored entity documents.

Two releases share one database during a rolling deploy, and a code rollback
runs the older release against documents a newer one already wrote. A field
the reading release does not know must therefore neither fail validation nor
be lost when that release writes the document back.

The rule:

- A stored document is validated strictly first; that is the normal path and
  costs nothing extra.
- Only when validation fails solely because of unknown fields are those fields
  set aside, at any nesting depth, and the rest validated. They are never
  interpreted by the reading release.
- When the reading release updates the document, every set-aside field whose
  containing object it still writes is put back unchanged. A top-level field
  it rewrites replaces that whole value, including fields nested in it that it
  does not know: it owns that value now. New data meant to survive older
  releases therefore belongs in new top-level fields or free-form mappings.
- Request validation is unchanged: only documents loaded from the entity table
  (``StoredPayload``) are decoded this way, and only as their own kind.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

Path = tuple[Any, ...]
_MAX_ROUNDS = 5


class StoredPayload(dict):
    """An entity document as loaded from storage, with the kind it was stored as.

    It may carry fields written by a newer release; ``Entity.model_validate``
    decodes it with :func:`decode_stored` when the kind matches.
    """

    __slots__ = ("kind",)

    def __init__(self, payload: Any = (), *, kind: str | None = None):
        super().__init__(payload)
        self.kind = kind

    def __reduce__(self):
        return (StoredPayload, (dict(self),), {"kind": self.kind})

    def __setstate__(self, state):
        self.kind = state.get("kind")


@dataclass
class StoredDecode:
    entity: Any
    # Set-aside unknown fields as (data path, value), in document order.
    unknown: list[tuple[Path, Any]] = field(default_factory=list)


def _resolve(document: Any, loc: tuple) -> tuple[Path | None, list[tuple[Path, Any]]]:
    """Map an error location onto the document.

    Returns the data path of the named field when it is present (else None)
    and the union sites passed on the way, as (data path at the site, member
    label). A label is a union member's class name or discriminator value.
    """
    node, path, sites = document, [], []
    for element in loc[:-1]:
        if isinstance(node, dict) and element in node:
            node = node[element]
            path.append(element)
        elif (
            isinstance(node, list)
            and isinstance(element, int)
            and 0 <= element < len(node)
        ):
            node = node[element]
            path.append(element)
        else:
            sites.append((tuple(path), element))
    if isinstance(node, dict) and loc[-1] in node:
        return tuple(path) + (loc[-1],), sites
    return None, sites


def _strippable(document: Any, error: ValidationError) -> list[Path]:
    """Unknown fields to set aside.

    Behind a union, a field is set aside only when every member that failed
    there rejected it as unknown; one member rejecting a field that another
    member owns never removes it.
    """
    labels: dict[Path, set[Any]] = {}
    reported: dict[Path, dict[Path, set[Any]]] = {}
    for item in error.errors(include_url=False, include_context=False):
        loc = tuple(item["loc"])
        if not loc:
            continue
        data_path, sites = _resolve(document, loc)
        for site, label in sites:
            labels.setdefault(site, set()).add(label)
        if item["type"] == "extra_forbidden" and data_path is not None:
            by_site = reported.setdefault(data_path, {})
            for site, label in sites:
                by_site.setdefault(site, set()).add(label)
    return [
        data_path
        for data_path, by_site in reported.items()
        if all(found >= labels[site] for site, found in by_site.items())
    ]


def _pop(document: Any, path: Path) -> Any:
    node = document
    for element in path[:-1]:
        node = node[element]
    return node.pop(path[-1])


def decode_stored(model: Any, payload: Any, *args: Any, **kwargs: Any) -> StoredDecode:
    """Validate a stored document, setting aside fields this release lacks."""
    validate = model.__pydantic_validator__.validate_python
    try:
        return StoredDecode(validate(payload, *args, **kwargs))
    except ValidationError as error:
        first = error
    document = copy.deepcopy(dict(payload))
    unknown: list[tuple[Path, Any]] = []
    current = first
    for _ in range(_MAX_ROUNDS):
        paths = _strippable(document, current)
        if not paths:
            break
        # Deepest first, so popping one never moves another.
        for path in sorted(paths, key=len, reverse=True):
            unknown.append((path, _pop(document, path)))
        try:
            entity = validate(document, *args, **kwargs)
        except ValidationError as error:
            current = error
            continue
        _record(model, unknown)
        unknown.sort(key=lambda item: len(item[0]))
        return StoredDecode(entity, unknown)
    raise first


def restore_unknown(document: dict, decoded: StoredDecode, source: Any = None) -> dict:
    """Put set-aside fields back into a document dumped by this release.

    ``source`` is the document they were set aside from. A field inside a list
    is restored only into the element it came from, matched by ``id`` when the
    element has one; a field whose containing object is gone stays dropped.
    """
    for path, value in decoded.unknown:
        node: Any = document
        origin: Any = source
        placed = True
        for element in path[:-1]:
            if isinstance(node, dict) and isinstance(element, str) and element in node:
                node = node[element]
                origin = origin.get(element) if isinstance(origin, dict) else None
            elif (
                isinstance(node, list)
                and isinstance(element, int)
                and element < len(node)
            ):
                before = (
                    origin[element]
                    if isinstance(origin, list) and element < len(origin)
                    else None
                )
                node = node[element]
                if (
                    isinstance(before, dict)
                    and "id" in before
                    and (not isinstance(node, dict) or node.get("id") != before["id"])
                ):
                    placed = False
                    break
                origin = before
            else:
                placed = False
                break
        if placed and isinstance(node, dict) and path[-1] not in node:
            node[path[-1]] = value
    return document


_RECORDED: set[tuple[str, tuple[Path, ...]]] = set()


def _record(model: Any, unknown: list[tuple[Path, Any]]) -> None:
    key = (model.__name__, tuple(sorted({path for path, _ in unknown}, key=repr)))
    if key in _RECORDED or len(_RECORDED) > 512:
        return
    _RECORDED.add(key)
    from .diagnostics import record_diagnostic

    record_diagnostic(
        "info",
        "storage",
        "storage.newer_fields_kept",
        "A stored record has fields from a newer Nebula release; they are kept, not used.",
        outcome="degraded",
        metadata={
            "model": model.__name__,
            "fields": [".".join(str(part) for part in path) for path, _ in unknown][
                :20
            ],
        },
    )
