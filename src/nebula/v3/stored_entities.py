"""Tolerant decoding of stored entity payloads across Core releases.

Entity models forbid unknown keys so that API input and internal changes are
checked strictly. During a rolling release, though, two Core versions share one
database: release N+1 may save a field that release N does not define, and a
code rollback leaves such fields behind. Release N must still read those rows,
and when it rewrites one it must keep N+1's field instead of silently dropping
it.

This module implements that rule for stored payloads only:

* :func:`decode_stored` validates a stored payload strictly first. Only when
  validation fails does it look for keys the model forbids as unknown, set them
  aside, and validate again without them. A row without unknown keys costs one
  ordinary validation. Any other validation failure is still raised.
* The keys set aside travel with the decoded entity (``_stored_unknown``) and
  are put back by :func:`restore_unknown` when the entity's JSON form is saved.
* Changes and API input never go through this module, so they keep rejecting
  unknown keys. In an update, an unknown key is tolerated only where the stored
  row already holds the same key with the same value.

A restored key keeps its JSON path. A key inside a nested object is restored
while that object still exists. A key inside a list item follows its item: an
item with an ``id`` is found by that ``id`` wherever it now sits; an item
without one must still be at the same index with the same content. Otherwise
the key is dropped rather than attached to another item. A key the writing
release now defines is never overwritten. A new row never receives them: only a
rewrite of the row they were read from does.

Decoding sets keys aside only when every validation error is an unknown key at
a plain path. Any other error, or an unknown key reported through a union
member, leaves the row unreadable rather than guessing which keys to drop.

Release N+1 may write a field older releases do not define only once every
version that may read it decodes this way; the release manifest feature
:data:`TOLERANT_ENTITY_READ_FEATURE` declares that, and
:data:`ENTITY_FIELD_ADDITION_FEATURE` declares a release that relies on it (see
``docs/design/rolling-core-releases.md``).
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any, NamedTuple, TypeVar, get_args

from pydantic import BaseModel, TypeAdapter, ValidationError

TOLERANT_ENTITY_READ_FEATURE = "storage.tolerant-entities/v1"
ENTITY_FIELD_ADDITION_FEATURE = "storage.entity-field-additions/v1"

# Set-aside rounds: a key that hides another (e.g. inside a union member) can
# need a second pass; more than a few means the row is not merely newer.
_MAX_ROUNDS = 4

PathStep = str | int
ModelT = TypeVar("ModelT", bound=BaseModel)


class ListAnchor(NamedTuple):
    """Which list item on an unknown key's path held the key when read.

    ``item_id`` identifies an item that has an ``id``; otherwise
    ``item_known`` is the item's content without its unknown keys.
    """

    depth: int
    item_id: Any
    item_known: Any = None


class UnknownKey(NamedTuple):
    """A stored key the reading release does not define, kept for rewrites."""

    path: tuple[PathStep, ...]
    value: Any
    anchors: tuple[ListAnchor, ...] = ()


_MISSING = object()


def _child(container: Any, step: Any) -> Any:
    if isinstance(container, Mapping) and isinstance(step, str):
        return container.get(step, _MISSING)
    if (
        isinstance(container, list)
        and isinstance(step, int)
        and not isinstance(step, bool)
        and 0 <= step < len(container)
    ):
        return container[step]
    return _MISSING


def _resolve(
    container: Any, loc: tuple[Any, ...], expected: Any
) -> tuple[PathStep, ...] | None:
    """The payload path an ``extra_forbidden`` error location points to.

    Pydantic locations interleave real keys with labels that are not in the
    payload (union tags, member names, validator names). A label is skipped
    when the payload has no such key; the resolved path must end at a key of an
    object holding exactly the value pydantic reported as extra.
    """

    if not loc:
        return None
    step, rest = loc[0], loc[1:]
    if not rest:
        if (
            isinstance(container, Mapping)
            and isinstance(step, str)
            and step in container
            and container[step] == expected
        ):
            return (step,)
        return None
    child = _child(container, step)
    if child is not _MISSING:
        found = _resolve(child, rest, expected)
        if found is not None:
            return (step, *found)
    return _resolve(container, rest, expected)


def _anchors(cleaned: Any, path: tuple[PathStep, ...]) -> tuple[ListAnchor, ...]:
    """Anchors for ``path`` in ``cleaned``, the payload without unknown keys."""

    anchors: list[ListAnchor] = []
    current = cleaned
    for depth, step in enumerate(path):
        if isinstance(current, list) and isinstance(step, int):
            item = current[step]
            item_id = item.get("id") if isinstance(item, Mapping) else None
            anchors.append(
                ListAnchor(depth, item_id, None if item_id is not None else item)
            )
        current = _child(current, step)
    return tuple(anchors)


def _anchored(cleaned: Any, found: list[UnknownKey]) -> list[UnknownKey]:
    return [
        UnknownKey(key.path, key.value, _anchors(cleaned, key.path)) for key in found
    ]


def _without(payload: Any, path: tuple[PathStep, ...]) -> Any:
    """``payload`` copied along ``path`` with the final key removed."""

    step = path[0]
    if len(path) == 1:
        copy = dict(payload)
        copy.pop(step, None)
        return copy
    if isinstance(payload, list):
        copy_list = list(payload)
        copy_list[step] = _without(payload[step], path[1:])
        return copy_list
    copy = dict(payload)
    copy[step] = _without(payload[step], path[1:])
    return copy


def _unknown_keys(error: ValidationError, payload: Any) -> list[UnknownKey] | None:
    """The unknown keys an error reports, or ``None`` if it reports anything else."""

    found: dict[tuple[PathStep, ...], UnknownKey] = {}
    for detail in error.errors():
        if detail.get("type") != "extra_forbidden":
            return None
        loc = tuple(detail.get("loc", ()))
        path = _resolve(payload, loc, detail.get("input"))
        # A location with labels that are not payload keys passes through a
        # union. Pydantic then reports every member's view, including keys
        # that are valid in the matching member, so nothing is set aside.
        if path is None or len(path) != len(loc):
            return None
        found.setdefault(path, UnknownKey(path, detail.get("input")))
    return list(found.values())


def _strip(
    validate: Any, payload: Any, *, origin: Any = _MISSING
) -> tuple[Any, list[UnknownKey]]:
    """Validate ``payload``, setting aside unknown keys one round at a time.

    With ``origin``, a key is set aside only where ``origin`` holds the same key
    with the same value; any other unknown key fails validation as usual.
    """

    unknown: list[UnknownKey] = []
    current = payload
    first_error: ValidationError | None = None
    for _ in range(_MAX_ROUNDS):
        try:
            return validate(current), _anchored(current, unknown)
        except ValidationError as exc:  # diagnostic-expected: unknown keys from a newer release are set aside; anything else is re-raised
            if first_error is None:
                first_error = exc
            found = _unknown_keys(exc, current)
            if not found:
                raise first_error from None
            for key in found:
                if origin is not _MISSING:
                    stored = origin
                    for step in key.path:
                        stored = _child(stored, step)
                        if stored is _MISSING:
                            break
                    if stored is _MISSING or stored != key.value:
                        raise first_error from None
                current = _without(current, key.path)
            unknown.extend(found)
    assert first_error is not None
    raise first_error


def decode_stored(
    model: type[ModelT], payload: Mapping[str, Any], *, origin: Any = _MISSING
) -> ModelT:
    """Validate a stored entity payload, tolerating keys a newer release wrote.

    The result carries the keys set aside so a rewrite restores them. Raises
    the original ``ValidationError`` when the payload fails for any other
    reason. Use it only for payloads read from storage, never for input.
    """

    entity, unknown = _strip(model.model_validate, payload, origin=origin)
    if unknown:
        _attach(entity, tuple(unknown))
    return entity


def _attach(entity: BaseModel, unknown: tuple[UnknownKey, ...]) -> None:
    private = getattr(entity, "__pydantic_private__", None)
    if private is not None and "_stored_unknown" in private:
        private["_stored_unknown"] = unknown


def stored_unknown(entity: BaseModel) -> tuple[UnknownKey, ...]:
    """The unknown keys a stored entity was read with, if any."""

    private = getattr(entity, "__pydantic_private__", None) or {}
    return tuple(private.get("_stored_unknown") or ())


def carry_unknown(target: ModelT, source: BaseModel) -> ModelT:
    """Give ``target`` (a rewrite of ``source``) the unknown keys ``source`` kept."""

    unknown = stored_unknown(source)
    if unknown:
        merged = {key.path: key for key in stored_unknown(target)}
        for key in unknown:
            merged.setdefault(key.path, key)
        _attach(target, tuple(merged.values()))
    return target


def _anchored_index(parent: Any, step: PathStep, anchor: ListAnchor) -> Any:
    """Where the anchored item now is in ``parent``, or ``_MISSING``."""

    if not isinstance(parent, list):
        return _MISSING
    if anchor.item_id is not None:
        if (
            isinstance(step, int)
            and step < len(parent)
            and isinstance(parent[step], Mapping)
            and parent[step].get("id") == anchor.item_id
        ):
            return step
        matches = [
            index
            for index, item in enumerate(parent)
            if isinstance(item, Mapping) and item.get("id") == anchor.item_id
        ]
        return matches[0] if len(matches) == 1 else _MISSING
    if (
        isinstance(step, int)
        and step < len(parent)
        and parent[step] == anchor.item_known
    ):
        return step
    return _MISSING


def restore_unknown(
    written: dict[str, Any], unknown: tuple[UnknownKey, ...] | list[UnknownKey]
) -> dict[str, Any]:
    """Put unknown stored keys back into ``written``, an entity's JSON form.

    ``written`` is modified in place and returned. A key is restored only when
    its parent object still exists, the writer did not set the key itself, and
    each list item on its path is still the item that held it.
    """

    for key in unknown:
        anchors = {anchor.depth: anchor for anchor in key.anchors}
        parent: Any = written
        for depth, step in enumerate(key.path[:-1]):
            anchor = anchors.get(depth)
            if anchor is not None:
                step = _anchored_index(parent, step, anchor)
                if step is _MISSING:
                    parent = _MISSING
                    break
            parent = _child(parent, step)
            if parent is _MISSING:
                break
        last = key.path[-1]
        if isinstance(parent, dict) and last not in parent:
            parent[last] = key.value
    return written


def dump_with_unknown(entity: BaseModel) -> dict[str, Any]:
    """``entity``'s JSON form with any unknown stored keys it carries restored."""

    written = entity.model_dump(mode="json")
    unknown = stored_unknown(entity)
    return restore_unknown(written, unknown) if unknown else written


def _forbids_extra(annotation: Any, seen: set[int]) -> bool:
    if id(annotation) in seen:
        return False
    seen.add(id(annotation))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.model_config.get("extra") == "forbid" or any(
            _forbids_extra(field.annotation, seen)
            for field in annotation.model_fields.values()
        )
    return any(_forbids_extra(arg, seen) for arg in get_args(annotation))


@lru_cache(maxsize=1024)
def _field_adapter(model: type[BaseModel], name: str) -> TypeAdapter[Any] | None:
    """A validator for field ``name``, or ``None`` when it cannot hold unknown keys.

    Only a field whose type contains a model that forbids extra keys can hold
    a key this release does not define; free-form dicts and scalar lists keep
    every key as data, so the update path never pays for scanning them.
    """

    field = model.model_fields.get(name)
    if field is None or not _forbids_extra(field.annotation, set()):
        return None
    return TypeAdapter(field.annotation)


def unknown_in_fields(
    model: type[BaseModel],
    stored: Mapping[str, Any],
    names: Any,
) -> list[UnknownKey]:
    """Unknown keys nested in the stored values of ``names``.

    An update that rewrites a field replaces its stored value, so unknown keys
    inside it are no longer in the merged payload; this finds them in the
    stored value so the rewrite can restore them. Only container values are
    checked, and a field the update leaves equal is skipped by the caller.
    """

    found: list[UnknownKey] = []
    aliases = {
        (field.alias or name): name for name, field in model.model_fields.items()
    }
    for key in names:
        value = stored.get(key, _MISSING)
        if not isinstance(value, (dict, list)) or not value:
            continue
        adapter = _field_adapter(model, aliases.get(key, key))
        if adapter is None:
            continue
        try:
            adapter.validate_python(value)
            continue
        except ValidationError as exc:  # diagnostic-expected: only the unknown keys it reports are used; the field's other failures belong to the merged validation
            keys = _unknown_keys(exc, value)
        except Exception:  # diagnostic-expected: a field validator that needs its whole entity cannot run alone; the merged validation already checked the field
            continue
        if not keys:
            continue
        cleaned = value
        for item in keys:
            cleaned = _without(cleaned, item.path)
        found.extend(
            UnknownKey(
                (key, *item.path),
                item.value,
                tuple(
                    ListAnchor(anchor.depth + 1, anchor.item_id, anchor.item_known)
                    for anchor in _anchors(cleaned, item.path)
                ),
            )
            for item in keys
        )
    return found


def validate_update(
    model: type[ModelT],
    stored: Mapping[str, Any],
    merged: Mapping[str, Any],
    changes: Mapping[str, Any],
) -> tuple[ModelT, tuple[UnknownKey, ...]]:
    """Validate ``merged`` (``stored`` with ``changes`` applied) for a rewrite.

    Returns the updated entity and the unknown stored keys its JSON form must
    keep. An unknown key in ``changes`` is rejected; one that the stored row
    holds unchanged is tolerated. Unknown keys nested in a field the change
    rewrites are found in the stored value so they can be restored.
    """

    try:
        updated = model.model_validate(merged)
        unknown: list[UnknownKey] = []
    except ValidationError:  # diagnostic-expected: retried tolerating only the stored row's unknown keys; any other failure is re-raised
        updated, unknown = _strip(model.model_validate, merged, origin=stored)
    if any(isinstance(stored.get(key), (dict, list)) for key in changes):
        seen = {key.path for key in unknown}
        unknown.extend(
            key
            for key in unknown_in_fields(
                model,
                stored,
                [
                    key
                    for key in changes
                    if isinstance(stored.get(key), (dict, list))
                    and stored.get(key) is not merged.get(key)
                ],
            )
            if key.path not in seen
        )
    if unknown:
        _attach(updated, tuple(unknown))
    return updated, tuple(unknown)


def _models_in(annotation: Any, seen: set[type[BaseModel]]) -> None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return
        seen.add(annotation)
        for field in annotation.model_fields.values():
            _models_in(field.annotation, seen)
        return
    for arg in get_args(annotation):
        _models_in(arg, seen)


def entity_fields_digest(models: Any) -> str:
    """A digest of every stored field name in ``models`` and their nested models.

    ``runtime-compatibility.json`` records the reviewed value, so a release
    cannot add a stored-entity field without a review that also declares
    :data:`ENTITY_FIELD_ADDITION_FEATURE` (see ``runtime_bundle``).
    """

    import hashlib

    seen: set[type[BaseModel]] = set()
    for model in models:
        _models_in(model, seen)
    lines = sorted(
        f"{model.__module__}.{model.__qualname__}.{field.alias or name}"
        for model in seen
        for name, field in model.model_fields.items()
    )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()
