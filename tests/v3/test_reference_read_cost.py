import pytest
import os
from uuid import uuid4
from sqlalchemy import update

from nebula.v3 import storage as storage_module
from nebula.v3.api_validation import ApiEntityValidator
from nebula.v3.database import EntityRow
from nebula.v3.domain import Artifact, Engagement, Finding, Observation
from nebula.v3.storage import ConflictError, NebulaStore


def _store(tmp_path):
    store = NebulaStore(tmp_path / "references.db")
    store.create_many(
        [
            Engagement(id="project", name="Synthetic"),
            Engagement(id="foreign", name="Other"),
        ]
    )
    note = store.create(
        Observation(
            id='note-λ"\\_%',
            engagement_id="project",
            observation_type="note",
            title="Synthetic",
        )
    )
    return store, note


def test_delete_does_not_hydrate_unrelated_history_and_keeps_legacy_foreign_guards(
    tmp_path, monkeypatch
):
    store, note = _store(tmp_path)
    store.create_many(
        [
            Finding(id=f"finding-{i}", engagement_id="project", title="Unrelated")
            for i in range(1200)
        ]
    )
    original = storage_module.readable_entities
    hydrated = []

    def observe(rows, model):
        rows = list(rows)
        hydrated.extend(row.id for row in rows)
        return original(rows, model)

    monkeypatch.setattr(storage_module, "readable_entities", observe)
    validator = ApiEntityValidator(store)
    validator.validate_delete(note)
    assert hydrated == []
    # Legacy rows can predate API ownership enforcement; still retain their target.
    store.create(
        Finding(
            id="legacy-foreign",
            engagement_id="foreign",
            title="Retains note",
            observation_ids=[note.id],
        )
    )
    with pytest.raises(ConflictError, match="observation_ids"):
        validator.validate_delete(note)
    assert hydrated == ["legacy-foreign"]
    store.database.engine.dispose()


def test_array_prefilter_is_exact_and_skips_only_unreadable_matches(tmp_path):
    store, note = _store(tmp_path)
    good = Finding(
        id="good", engagement_id="project", title="Exact", observation_ids=[note.id]
    )
    bad = Finding(
        id="bad", engagement_id="project", title="Unreadable", observation_ids=[note.id]
    )
    store.create_many(
        [
            good,
            bad,
            Finding(
                id="substring",
                engagement_id="project",
                title="Different",
                observation_ids=[note.id + "-suffix"],
            ),
        ]
    )
    payload = bad.model_dump(mode="json")
    payload["severity"] = "synthetic-invalid-severity"
    with store.database.session() as session:
        session.execute(
            update(EntityRow).where(EntityRow.id == bad.id).values(payload=payload)
        )
    assert [
        row.id
        for row in store.iter_reference_candidates(
            Finding, [("observation_ids", note.id, True)]
        )
    ] == [good.id]
    assert list(store.iter_reference_candidates(Finding, [])) == []
    store.database.engine.dispose()


def test_scalar_references_are_exact(tmp_path):
    store, _ = _store(tmp_path)

    def artifact(identity, parent=None):
        return Artifact(
            id=identity,
            engagement_id="project",
            sha256="0" * 64,
            size=0,
            storage_path="synthetic",
            parent_artifact_id=parent,
        )

    store.create_many(
        [
            artifact("parent"),
            artifact("child", "parent"),
            artifact("different", "parent-suffix"),
        ]
    )
    assert [
        row.id
        for row in store.iter_reference_candidates(
            Artifact, [("parent_artifact_id", "parent", False)]
        )
    ] == ["child"]
    store.database.engine.dispose()


def test_reference_scan_preserves_all_matches_past_one_page(tmp_path):
    store, note = _store(tmp_path)
    items = [
        Finding(
            id=f"finding-{i:04}",
            engagement_id="project",
            title="Synthetic",
            observation_ids=[note.id],
        )
        for i in range(1005)
    ]
    store.create_many(items)
    assert [
        row.id
        for row in store.iter_reference_candidates(
            Finding, [("observation_ids", note.id, True)]
        )
    ] == [row.id for row in items]
    store.database.engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("NEBULA_TEST_POSTGRES_URL"),
    reason="requires disposable PostgreSQL fixture",
)
def test_postgres_reference_membership_matches_exact_ids_and_handles_invalid_arrays():
    store = NebulaStore(os.environ["NEBULA_TEST_POSTGRES_URL"])
    prefix = uuid4().hex
    project = store.create(Engagement(id=prefix, name="Synthetic PostgreSQL"))
    note_id = prefix + '-λ"\\_%'
    store.create(
        Observation(
            id=note_id,
            engagement_id=project.id,
            observation_type="note",
            title="Synthetic",
        )
    )
    good = Finding(
        id=prefix + "-good",
        engagement_id=project.id,
        title="Exact",
        observation_ids=[note_id],
    )
    invalid = Finding(
        id=prefix + "-invalid", engagement_id=project.id, title="Bad array"
    )
    store.create_many(
        [
            good,
            invalid,
            Finding(
                id=prefix + "-other",
                engagement_id=project.id,
                title="Different",
                observation_ids=[note_id + "-suffix"],
            ),
        ]
    )
    payload = invalid.model_dump(mode="json")
    payload["observation_ids"] = "not-an-array"
    with store.database.session() as session:
        session.execute(
            update(EntityRow).where(EntityRow.id == invalid.id).values(payload=payload)
        )
    try:
        assert [
            item.id
            for item in store.iter_reference_candidates(
                Finding, [("observation_ids", note_id, True)], engagement_id=project.id
            )
        ] == [good.id]
        assert (
            list(
                store.iter_reference_candidates(
                    Finding,
                    [("remediation_id", prefix, False)],
                    engagement_id=project.id,
                )
            )
            == []
        )
    finally:
        store.database.engine.dispose()
