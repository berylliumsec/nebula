"""Explicit client uploads and bounded Core workspace/source previews."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .transport import ClientError, key

if TYPE_CHECKING:
    from .shell import Shell

MAX_UPLOAD = 20 * 1024 * 1024


async def excerpt(
    shell: Shell, preview: dict[str, Any], source_kind: str, source_id: str
) -> None:
    text = preview.get("text", "")
    if not text:
        raise ClientError(
            "This file has no attachable text. Choose a supported document or image."
        )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    expected = preview.get("preview_sha256") or preview.get("sha256")
    if expected and expected != digest:
        raise ClientError(
            "Core's preview hash did not match its text. Refresh the source before attaching."
        )
    draft = shell.current().draft
    if (
        len(draft.contexts) >= 20
        or sum(len(item["text"]) for item in draft.contexts) + len(text) > 20_000
    ):
        raise ClientError(
            "Selected context exceeds 20 attachments or 20,000 characters. Remove an attachment first."
        )
    shell.ui.say(preview.get("label") or source_id)
    if preview.get("truncated"):
        shell.ui.say("Bounded excerpt; the complete source is not included.")
    shell.ui.say(text)
    if await shell.ui.confirm("Attach this exact excerpt to the next message?"):
        draft.contexts.append(
            {
                "source_kind": source_kind,
                "source_id": source_id,
                "source_label": preview.get("label") or source_id,
                "text": text,
                "sha256": digest,
                "truncated": bool(preview.get("truncated")),
            }
        )
        shell.ui.say("Excerpt attached to the draft.")


async def local(shell: Shell, path: str | None, *, image: bool = False) -> None:
    view = shell.current()
    selected = Path(
        path or await shell.ui.ask("File on this client", path=True)
    ).expanduser()
    if not selected.is_file():
        raise ClientError("Choose a regular file on this client.")
    with selected.open("rb") as handle:
        data = handle.read(MAX_UPLOAD + 1)
    if not data or len(data) > MAX_UPLOAD:
        raise ClientError("Choose a file between 1 byte and 20 MiB.")
    media = mimetypes.guess_type(selected.name)[0] or "application/octet-stream"
    body = {
        "engagement_id": view.project_id,
        "filename": selected.name,
        "media_type": media,
        "content_base64": base64.b64encode(data).decode("ascii"),
    }
    if image:
        if media not in {"image/png", "image/jpeg", "image/webp"}:
            raise ClientError(
                "Images must be PNG, JPEG or WebP; Core validates their bytes."
            )
        if (await shell.state()).get("busy"):
            raise ClientError("Attach images after the active response finishes.")
        await shell.configuration()
        await shell.runtimes()
        profile = shell.profiles.get(view.config["profile_id"], {})
        caps = profile.get("capabilities") or {}
        option: dict[str, Any] = next(
            (
                row
                for row in caps.get("model_options", [])
                if row.get("model") == view.config["model"]
            ),
            {},
        )
        supports = (
            option.get("image_input", False)
            if view.config["backend"] == "harness"
            else caps.get("vision", False)
        )
        if not supports:
            raise ClientError(
                "This runtime does not support image input. Select an image-capable model with /model."
            )
        if len(view.draft.images) >= 4:
            raise ClientError("Remove a pending image before attaching more.")
        result = await shell.api.request("POST", "chat/images", json=body)
        view.draft.images.append(
            {
                "type": "image",
                "artifact_id": result["artifact_id"],
                "media_type": result["media_type"],
                "metadata": {
                    "preview_artifact_id": result["preview_artifact_id"],
                    "width": result["width"],
                    "height": result["height"],
                },
                "alt": selected.name,
            }
        )
        shell.ui.say(f"Attached {selected.name}; its bytes are saved on Core.")
    else:
        if not await shell.ui.confirm(
            f"Upload {selected.name} to this project's Core knowledge library?"
        ):
            return
        source = await shell.api.request("POST", "knowledge/ingest", json=body)
        preview = await shell.api.request(
            "GET",
            f"chat/projects/{key(view.project_id)}/sources/{key(source['id'])}/preview",
        )
        await excerpt(shell, preview, "knowledge", source["id"])


async def project_file(shell: Shell) -> None:
    view = shell.current()
    path, offset = "", 0
    while True:
        page = await shell.api.request(
            "GET",
            f"engagements/{key(view.project_id)}/workspace",
            params={"path": path, "offset": offset},
        )
        options = [{"id": "__up", "name": "Parent folder"}] if path else []
        options.extend(
            {
                **item,
                "id": item["path"],
                "name": item["name"] + ("/" if item["kind"] == "directory" else ""),
            }
            for item in page["entries"]
            if item["kind"] in {"file", "directory"}
        )
        if page.get("next_offset") is not None:
            options.append({"id": "__more", "name": "More files"})
        if not options:
            raise ClientError("This Core project folder is empty.")
        item = await shell.ui.choose("Core project files: /" + path, options)
        if item["id"] == "__more":
            offset = page["next_offset"]
        elif item["id"] == "__up":
            path, offset = path.rsplit("/", 1)[0] if "/" in path else "", 0
        elif item["kind"] == "directory":
            path, offset = item["path"], 0
        else:
            preview = await shell.api.request(
                "GET",
                f"engagements/{key(view.project_id)}/workspace/preview",
                params={"path": item["path"]},
            )
            preview["label"] = item["path"]
            await excerpt(shell, preview, "workspace_file", item["path"])
            return


async def attach(shell: Shell, args: list[str]) -> None:
    actions = [
        {"id": "local", "name": "Document from this client"},
        {"id": "image", "name": "Image from this client"},
        {"id": "project", "name": "Browse Core project files"},
        {"id": "knowledge", "name": "Choose Core knowledge source"},
    ]
    action = (
        args[0]
        if args
        else (await shell.ui.choose("Attach to next message", actions))["id"]
    )
    if action in {"local", "image"}:
        await local(shell, args[1] if len(args) > 1 else None, image=action == "image")
    elif action == "project":
        await project_file(shell)
    elif action == "knowledge":
        view = shell.current()
        source = await shell.collection(
            "Project knowledge source",
            "knowledge",
            params={"engagement_id": view.project_id},
        )
        preview = await shell.api.request(
            "GET",
            f"chat/projects/{key(view.project_id)}/sources/{key(source['id'])}/preview",
        )
        await excerpt(shell, preview, "knowledge", source["id"])
    else:
        raise ClientError("Use /attach local, image, project, or knowledge.")


async def attachments(shell: Shell) -> None:
    draft = shell.current().draft
    options = [
        {"id": f"context:{index}", "name": row["source_label"]}
        for index, row in enumerate(draft.contexts)
    ]
    options.extend(
        {"id": f"image:{index}", "name": row["alt"]}
        for index, row in enumerate(draft.images)
    )
    chosen_skills = [item["name"] for item in draft.skills]
    if draft.skill:
        chosen_skills.append(draft.skill["name"])
    if chosen_skills:
        shell.ui.say("Next-turn skills: " + ", ".join(chosen_skills))
    if not options:
        shell.ui.say("No pending file attachments.")
        return
    if len(options) > 1:
        options.insert(
            0, {"id": "__all", "name": f"Remove all {len(options)} attachments"}
        )
    item = await shell.ui.choose("Pending attachments (choose one to remove)", options)
    if item["id"] == "__all":
        if await shell.ui.confirm("Remove every pending attachment from the draft?"):
            draft.images.clear()
            draft.contexts.clear()
            shell.ui.say("Removed all attachments. Uploaded sources remain on Core.")
        return
    if await shell.ui.confirm("Remove this attachment from the draft?"):
        kind, index = item["id"].split(":")
        (draft.images if kind == "image" else draft.contexts).pop(int(index))
        shell.ui.say("Removed from the draft. Uploaded sources remain on Core.")
