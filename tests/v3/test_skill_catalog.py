from pathlib import Path

import pytest

from nebula.v3.skill_catalog import (
    SkillSelection,
    discover_skills,
    native_skill_roots,
    read_skill_resource,
    snapshot_skill,
)


def _skill(root: Path, name: str, content: str) -> Path:
    entrypoint = root / name / "SKILL.md"
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_text(content, encoding="utf-8")
    return entrypoint.resolve()


def test_discovery_preserves_duplicate_names_and_exact_source(tmp_path):
    first_root = tmp_path / "project" / ".agents" / "skills"
    second_root = tmp_path / "managed" / ".agents" / "skills"
    first = _skill(first_root, "review", "first")
    second = _skill(second_root, "review", "second")

    available = discover_skills([(first_root, "project"), (second_root, "installed")])

    assert [(item.name, item.path, item.source) for item in available] == [
        ("review", str(first), "project"),
        ("review", str(second), "installed"),
    ]
    selected = snapshot_skill(
        SkillSelection(name="review", path=str(second)), available
    )
    assert selected.instructions == "second"
    assert (
        selected.sha256
        == "16367aacb67a4a017c8da8ab95682ccb390863780f7114dda0a0e0c55644c7c4"
    )
    assert len(selected.sha256) == 64


def test_snapshot_fails_closed_when_catalog_path_disappears(tmp_path):
    entrypoint = _skill(tmp_path / ".agents" / "skills", "review", "instructions")
    available = discover_skills(native_skill_roots(tmp_path))
    entrypoint.unlink()

    with pytest.raises(ValueError, match="could not be read"):
        snapshot_skill(SkillSelection(name="review", path=str(entrypoint)), available)


def test_snapshot_never_substitutes_same_named_skill(tmp_path):
    available_path = _skill(tmp_path / ".agents" / "skills", "review", "instructions")
    unavailable_path = tmp_path / ".agents" / "skills" / "other" / "SKILL.md"

    with pytest.raises(ValueError, match="exact source path"):
        snapshot_skill(
            SkillSelection(name="review", path=str(unavailable_path.resolve())),
            discover_skills(native_skill_roots(tmp_path)),
        )
    assert available_path.is_file()


def test_referenced_resources_are_manifested_then_loaded_by_exact_digest(tmp_path):
    root = tmp_path / ".agents" / "skills"
    entrypoint = _skill(
        root, "review", "Read [the checklist](references/checklist.md)."
    )
    resource = entrypoint.parent / "references" / "checklist.md"
    resource.parent.mkdir()
    resource.write_text("Verify the focused tests.", encoding="utf-8")
    snapshot = snapshot_skill(
        SkillSelection(name="review", path=str(entrypoint)),
        discover_skills(native_skill_roots(tmp_path)),
    )

    assert snapshot.resources[0].relative_path == "references/checklist.md"
    assert "Verify the focused tests." not in snapshot.instructions
    loaded = read_skill_resource(
        [snapshot],
        skill_path=snapshot.path,
        resource_path="references/checklist.md",
    )
    assert loaded["content"] == "Verify the focused tests."

    resource.write_text("Changed after selection.", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after selection"):
        read_skill_resource(
            [snapshot],
            skill_path=snapshot.path,
            resource_path="references/checklist.md",
        )


def test_skill_resource_references_cannot_escape_or_use_symlinks(tmp_path):
    root = tmp_path / ".agents" / "skills"
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    escaping = _skill(root, "escape", "[outside](../../../outside.md)")
    with pytest.raises(ValueError, match="inside its skill directory"):
        snapshot_skill(
            SkillSelection(name="escape", path=str(escaping)),
            discover_skills(native_skill_roots(tmp_path)),
        )

    linked = _skill(root, "linked", "[outside](reference.md)")
    (linked.parent / "reference.md").symlink_to(outside)
    with pytest.raises(ValueError, match="inside its skill directory|symlinks"):
        snapshot_skill(
            SkillSelection(name="linked", path=str(linked)),
            discover_skills(native_skill_roots(tmp_path)),
        )
