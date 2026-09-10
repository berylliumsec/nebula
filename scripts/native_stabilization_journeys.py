"""Permanent, bounded native acceptance against the package's bundled Core."""

import base64
import json
from pathlib import Path
import shutil

import httpx


async def exercise(
    *,
    webdriver,
    prefix,
    backend,
    profile,
    project,
    evidence_root,
    execute,
    wait_for,
    element,
    click,
):
    fixture = profile / "stabilization_acp.py"
    shutil.copyfile(Path(__file__).parent / "fixtures/stabilization_acp.py", fixture)
    fixture.chmod(0o700)
    async with httpx.AsyncClient(
        base_url=backend["endpoint"].rstrip("/") + "/",
        headers={"Authorization": "Bearer " + backend["token"]},
        timeout=60,
    ) as core:
        # Setup data is disposable; all operator decisions and sends below use
        # native WebDriver controls, not API substitutes or synthetic JS clicks.
        created = await core.post(
            "harnesses",
            json={
                "name": "Inert native acceptance",
                "kind": "grok_acp",
                "executable": str(fixture),
                "default_model": "stabilization-fixture",
                "privacy": {"local_only": True, "permits_sensitive_data": True},
                "native_capabilities": {"skills": True},
            },
        )
        created.raise_for_status()
        health = await core.post(f"harnesses/{created.json()['id']}/health")
        health.raise_for_status()
        assert health.json()["healthy"], health.text

        async def named_click(name, within="document"):
            found = await execute(
                f"const root={within}; const e=[...root.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')===arguments[0] || b.textContent.trim()===arguments[0]); if(!e)return false; e.setAttribute('data-native-action','true');return true;",
                name,
            )
            assert found, f"No visible operator control named {name}"
            await click("[data-native-action]")
            await execute(
                "document.querySelector('[data-native-action]')?.removeAttribute('data-native-action');return true;"
            )

        await execute(
            "location.href='/projects/'+encodeURIComponent(arguments[0])+'/workbench?view=chat';return true;",
            project["id"],
        )
        await wait_for(
            "return new URL(location.href).searchParams.get('view')==='chat' && [...document.querySelectorAll('button')].some(b=>b.getAttribute('aria-label')==='New chat' || b.textContent.trim()==='New chat');"
        )
        await named_click("New chat")
        sessions = []
        for decision, answer in [
            ("Approve", "NATIVE_APPROVAL_ACCEPTED_ONCE"),
            ("Reject", "NATIVE_APPROVAL_DECLINED"),
        ]:
            await wait_for(
                "return Boolean(document.querySelector('#analyst-message:not(:disabled)'));"
            )
            composer = await element("#analyst-message")
            (
                await webdriver.post(
                    prefix + f"/element/{composer}/value",
                    json={"text": "Review the inert fixture. No command executes."},
                )
            ).raise_for_status()
            await named_click("Send message")
            await wait_for(
                "return Boolean(document.querySelector('[aria-label=\"Review pending actions\"]'));"
            )
            await named_click("Review pending actions")
            await wait_for(
                "return Boolean(document.querySelector('[aria-label=\"Approval required\"]'));"
            )
            (await webdriver.post(prefix + "/refresh", json={})).raise_for_status()
            await wait_for(
                "return Boolean(document.querySelector('[aria-label=\"Approval required\"]'));"
            )
            await named_click(
                decision, "document.querySelector('[aria-label=\"Approval required\"]')"
            )
            await wait_for(
                f"return document.querySelector('.chat-message.assistant:last-of-type')?.textContent.includes('{answer}') || [...document.querySelectorAll('.assistant-markdown')].some(e=>e.textContent.includes('{answer}'));"
            )
            await wait_for(
                "return !document.querySelector('[aria-label=\"Review pending actions\"]') && !document.querySelector('button[aria-label=\"Stop response\"]');"
            )
            session_id = await execute(
                "return new URL(location.href).searchParams.get('session');"
            )
            state = await core.get(f"chat/sessions/{session_id}/state")
            state.raise_for_status()
            snapshot = state.json()
            assert snapshot["execution"] == "complete", snapshot
            assert snapshot["pending"] == [], snapshot
            approval = snapshot["decisions"][-1]
            assert approval["continuation"]["status"] == "delivered", snapshot
            repeated = await core.post(
                f"approvals/{approval['approval_id']}/decision",
                json={"decision": decision.lower()},
            )
            repeated.raise_for_status()
            (await webdriver.post(prefix + "/refresh", json={})).raise_for_status()
            await wait_for(
                f"return [...document.querySelectorAll('.assistant-markdown')].filter(e=>e.textContent.includes('{answer}')).length===1;"
            )
            assert await execute(
                "return !document.querySelector('[aria-label=\"Review pending actions\"]');"
            )
            screenshot = await webdriver.get(prefix + "/screenshot")
            screenshot.raise_for_status()
            (evidence_root / f"native-{decision.lower()}.png").write_bytes(
                base64.b64decode(screenshot.json()["value"])
            )
            sessions.append(
                {"session_id": session_id, "decision": decision, "state": snapshot}
            )
            if decision == "Approve":
                await named_click("New chat")
        receipts = [
            json.loads(line)
            for line in fixture.with_suffix(".receipts.jsonl").read_text().splitlines()
        ]
        assert len(receipts) == 2 and [r["allowed"] for r in receipts] == [
            True,
            False,
        ], receipts
        result = {
            "journeys": sessions,
            "adapter_receipts": receipts,
            "tools_executed": 0,
            "runtime": "inert ACP peer through the packaged Core's real Grok adapter",
            "reload_while_waiting": True,
            "reload_after_completion": True,
            "duplicate_decision_did_not_redeliver": True,
        }
        (evidence_root / "native-approval-evidence.json").write_text(
            json.dumps(result, indent=2)
        )
        print(
            "Packaged desktop: approve, reject, reload and exact-once adapter receipts passed",
            flush=True,
        )
        return result
