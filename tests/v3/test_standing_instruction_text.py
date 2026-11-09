"""Exact instruction bodies survive validation and durable goal snapshots."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import hashlib
import pytest
from nebula.v3.domain import ChatGoal
from nebula.v3.project_instructions import (
    ProjectInstructions,
    load_project_instructions,
)
from nebula.v3.skill_catalog import (
    SkillSelection,
    SkillSnapshot,
    discover_skills,
    native_skill_roots,
    snapshot_skill,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.chat_snapshot_parts import (
    split_skill_snapshots,
    stage_snapshot_parts,
    resolve_skill_snapshots,
)

BODIES = [
    "  Exact leading and trailing spaces.  ",
    "\r\n  CRLF instructions.\r\n\r\n",
    "  Exact Unicode: 你好 🐈\nFinal line.\n",
]


@pytest.mark.parametrize("body", BODIES)
def test_project_body_preserves_exact_text_on_load_validation_and_assignment(
    tmp_path, body
):
    (tmp_path / "AGENTS.md").write_bytes(body.encode())
    loaded = load_project_instructions(tmp_path)
    assert loaded.content == body
    assert loaded.sha256 == hashlib.sha256(body.encode()).hexdigest()
    reloaded = ProjectInstructions.model_validate(loaded.model_dump())
    assert reloaded.content == body
    reloaded.content = body + "\n"
    assert reloaded.content == body + "\n"
    assert (
        ProjectInstructions.model_validate(
            {**loaded.model_dump(), "path": " AGENTS.md "}
        ).path
        == "AGENTS.md"
    )


@pytest.mark.parametrize("body", BODIES)
async def test_skill_body_and_digest_survive_persisted_goal_resume(tmp_path, body):
    path = tmp_path / ".agents" / "skills" / "exact" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(body.encode())
    saved = snapshot_skill(
        SkillSelection(name="exact", path=str(path)),
        discover_skills(native_skill_roots(tmp_path)),
    )
    assert saved.instructions == body
    assert saved.sha256 == hashlib.sha256(body.encode()).hexdigest()
    store = NebulaStore(tmp_path / "instructions.db")
    entries, parts = split_skill_snapshots(
        [saved.model_dump(mode="json")],
        engagement_id="synthetic-project",
        session_id="synthetic-session",
    )
    async with store.transaction() as tx:
        await stage_snapshot_parts(tx, parts)
        await tx.add(
            ChatGoal(
                engagement_id="synthetic-project",
                session_id="synthetic-session",
                objective="Preserve exact text",
                completion_criteria=["Bytes agree"],
                skill_snapshots=entries,
            )
        )
    reopened = NebulaStore(tmp_path / "instructions.db")
    goal = (await reopened.list_session_entities(ChatGoal, "synthetic-session"))[0]
    resumed = SkillSnapshot.model_validate(
        (
            await resolve_skill_snapshots(
                reopened, goal.skill_snapshots, session_id=goal.session_id
            )
        )[0]
    )
    assert resumed.instructions == body
    assert resumed.sha256 == hashlib.sha256(resumed.instructions.encode()).hexdigest()
    resumed.instructions = body + "\n"
    assert resumed.instructions == body + "\n"


@pytest.mark.parametrize("body", ["", "  \r\n\t"])
def test_skill_still_rejects_blank_instruction_bodies(tmp_path, body):
    with pytest.raises(ValueError):
        SkillSnapshot(
            name="exact",
            path=str(tmp_path / "SKILL.md"),
            root=str(tmp_path),
            source="project",
            sha256="1" * 64,
            instructions=body,
        )
    valid = SkillSnapshot(
        name="exact",
        path=str(tmp_path / "SKILL.md"),
        root=str(tmp_path),
        source="project",
        sha256="1" * 64,
        instructions="Keep this instruction.\n",
    )
    with pytest.raises(ValueError):
        SkillSnapshot.model_validate({**valid.model_dump(), "instructions": body})
    with pytest.raises(ValueError):
        valid.instructions = body


pytestmark = pytest.mark.anyio
