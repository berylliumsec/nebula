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
    relaunch,
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
                f"const root={within}; const e=[...root.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')===arguments[0] || b.textContent.trim()===arguments[0] || b.querySelector('strong')?.textContent===arguments[0]); if(!e)return false; e.setAttribute('data-native-action','true');return true;",
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
            ("Stop response", None),
        ]:
            await wait_for(
                "return Boolean(document.querySelector('#analyst-message:not(:disabled)'));"
            )
            composer = await element("#analyst-message")
            (
                await webdriver.post(
                    prefix + f"/element/{composer}/value",
                    json={
                        "text": f"Native {decision} journey. Review the inert fixture. No command executes."
                    },
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
            if answer is None:
                await named_click("Stop response")
            else:
                await named_click(
                    decision,
                    "document.querySelector('[aria-label=\"Approval required\"]')",
                )
                await wait_for(
                    f"return [...document.querySelectorAll('.assistant-markdown')].some(e=>e.textContent.includes('{answer}'));"
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
            assert snapshot["execution"] == (
                "cancelled" if answer is None else "complete"
            ), snapshot
            assert snapshot["pending"] == [], snapshot
            if answer is None:
                assert await execute(
                    "return !document.querySelector('[aria-label=\"Approval required\"]');"
                )
                sessions.append(
                    {"session_id": session_id, "decision": "Stop", "state": snapshot}
                )
                break
            approval = snapshot["decisions"][-1]
            assert approval["continuation"]["status"] == "delivered", snapshot
            assert approval["continuation"]["adapter_handoff"] == "transport_write", (
                snapshot
            )
            assert approval["continuation"]["adapter_status"] == "sent", snapshot
            assert approval["progress"] == "observed", snapshot
            assert (
                approval["progress_sequence"]
                > approval["continuation"]["progress_after_sequence"]
            ), snapshot
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
            await named_click("New chat")

        await named_click("More Workbench actions")
        await named_click("Enter focus mode")
        await wait_for(
            "return Boolean(document.querySelector('.sessions-page.full-screen'));"
        )
        fullscreen = await execute(
            "const r=document.querySelector('.sessions-page.full-screen').getBoundingClientRect();return {top:r.top,left:r.left,width:r.width,height:r.height,viewportWidth:innerWidth,viewportHeight:innerHeight};"
        )
        assert abs(fullscreen["top"]) <= 1 and abs(fullscreen["left"]) <= 1, fullscreen
        assert abs(fullscreen["width"] - fullscreen["viewportWidth"]) <= 1, fullscreen
        assert abs(fullscreen["height"] - fullscreen["viewportHeight"]) <= 1, fullscreen
        await named_click("Exit full screen workbench")
        await wait_for("return !document.querySelector('.sessions-page.full-screen');")

        objects = [
            {
                "op": "put_object",
                "id": f"native-mechanism-{i}",
                "label": f"Local mechanism {i:02d}",
                "classification": {"value": "Page", "status": "hypothesized"},
                "authentication_context": "anonymous",
                "properties": {
                    "purpose": {
                        "value": "Synthetic local workflow responsibility; no external site involved."
                    }
                },
            }
            for i in range(12)
        ]
        edges = [
            {
                "op": "put_relationship",
                "id": f"native-edge-{i}",
                "type": "contains",
                "source": "native-mechanism-0",
                "target": f"native-mechanism-{i}",
                "claim": {"value": True, "status": "hypothesized"},
            }
            for i in range(1, 12)
        ]
        seeded = await core.post(
            f"engagements/{project['id']}/application-model/transactions",
            json={
                "expected_revision": 0,
                "idempotency_key": "native-model-fixture",
                "operations": objects + edges,
            },
        )
        seeded.raise_for_status()
        await named_click("Application model")
        await wait_for(
            "return Boolean(document.querySelector('.am-category summary'));"
        )
        await click(".am-category summary")
        await wait_for(
            "return document.querySelectorAll('.am-outline .am-object').length===12;"
        )
        await click(".am-outline .am-object")
        await named_click("Expand relationships")
        await wait_for(
            "return Boolean(document.querySelector('.am-map.am-fullscreen'));"
        )
        graph_measure = """
          const panel=document.querySelector('.am-map.am-fullscreen'); if(!panel)return null;
          const r=panel.getBoundingClientRect(),s=panel.querySelector('.am-graph-scroll').getBoundingClientRect();
          const nodes=[...panel.querySelectorAll('.am-node')].map(n=>n.getBoundingClientRect());
          const labels=[...panel.querySelectorAll('.am-edge-label')].map(n=>n.getBoundingClientRect());
          const overlap=(a,b)=>Math.min(a.right,b.right)>Math.max(a.left,b.left)+1 && Math.min(a.bottom,b.bottom)>Math.max(a.top,b.top)+1;
          return {top:r.top,left:r.left,width:r.width,height:r.height,viewportWidth:innerWidth,viewportHeight:innerHeight,
            spread:(Math.max(...nodes.map(n=>n.right))-Math.min(...nodes.map(n=>n.left)))/s.width,
            labelCollisions:labels.filter((b,i)=>nodes.some(n=>overlap(b,n))||labels.slice(0,i).some(n=>overlap(b,n))).length,
            labelMinimum:Math.min(...labels.flatMap(b=>[b.width,b.height])),
            count:nodes.length,selected:panel.querySelector('.am-node.selected')?.getAttribute('data-node-id')};
        """
        await wait_for(
            "const e=document.querySelector('.am-fullscreen .am-graph-scroll');const n=[...e.querySelectorAll('.am-node')].map(n=>n.getBoundingClientRect());return n.length===12 && (Math.max(...n.map(n=>n.right))-Math.min(...n.map(n=>n.left)))/e.clientWidth>.7;"
        )
        graph_bounds = await execute(graph_measure)
        assert graph_bounds["labelCollisions"] == 0, graph_bounds
        assert graph_bounds["labelMinimum"] >= 44, graph_bounds
        assert graph_bounds["selected"] == "native-mechanism-0", graph_bounds
        assert graph_bounds["spread"] > 0.7 and graph_bounds["count"] == 12, (
            graph_bounds
        )
        assert abs(graph_bounds["top"]) <= 1 and abs(graph_bounds["left"]) <= 1, (
            graph_bounds
        )
        assert abs(graph_bounds["width"] - graph_bounds["viewportWidth"]) <= 1, (
            graph_bounds
        )
        assert abs(graph_bounds["height"] - graph_bounds["viewportHeight"]) <= 1, (
            graph_bounds
        )
        original_window = (await webdriver.get(prefix + "/window/rect")).json()["value"]
        resized = await webdriver.post(
            prefix + "/window/rect",
            json={
                "width": 1024 if graph_bounds["viewportWidth"] > 1200 else 1440,
                "height": 768,
            },
        )
        resized.raise_for_status()
        await wait_for(f"return innerWidth!=={graph_bounds['viewportWidth']};")
        await wait_for(
            "const e=document.querySelector('.am-fullscreen .am-graph-scroll');const n=[...e.querySelectorAll('.am-node')].map(n=>n.getBoundingClientRect());return (Math.max(...n.map(n=>n.right))-Math.min(...n.map(n=>n.left)))/e.clientWidth>.7;"
        )
        graph_resized = await execute(graph_measure)
        assert graph_resized["labelCollisions"] == 0, graph_resized
        assert graph_resized["labelMinimum"] >= 44, graph_resized
        assert graph_resized["selected"] == graph_bounds["selected"], graph_resized
        assert abs(graph_resized["width"] - graph_resized["viewportWidth"]) <= 1, (
            graph_resized
        )
        assert abs(graph_resized["height"] - graph_resized["viewportHeight"]) <= 1, (
            graph_resized
        )
        screenshot = await webdriver.get(prefix + "/screenshot")
        screenshot.raise_for_status()
        (evidence_root / "native-model-fullscreen-resized.png").write_bytes(
            base64.b64decode(screenshot.json()["value"])
        )
        (
            await webdriver.post(prefix + "/window/rect", json=original_window)
        ).raise_for_status()
        await named_click("Restore relationships")
        await wait_for(
            "return !document.querySelector('.am-fullscreen') && document.activeElement?.getAttribute('aria-label')==='Expand relationships';"
        )
        assert (
            await execute(
                "return document.querySelector('.am-node.selected')?.getAttribute('data-node-id');"
            )
            == graph_bounds["selected"]
        )

        # The reset boundary must also work in the actual desktop package. This
        # project's model is synthetic; its completed chats must survive reset.
        await named_click("Start over")
        await wait_for("return Boolean(document.querySelector('[role=dialog]'));")
        await named_click("Cancel", "document.querySelector('[role=dialog]')")
        preview = await core.get(
            f"engagements/{project['id']}/application-model/reset-preview"
        )
        preview.raise_for_status()
        assert preview.json()["objects"] == 12, preview.text
        await named_click("Start over")
        await wait_for("return Boolean(document.querySelector('[role=dialog]'));")
        await named_click(
            "Clear and start over", "document.querySelector('[role=dialog]')"
        )
        await wait_for(
            "return [...document.querySelectorAll('[role=status]')].some(e=>e.textContent.includes('Model and browser captures cleared'));"
        )
        preview = await core.get(
            f"engagements/{project['id']}/application-model/reset-preview"
        )
        preview.raise_for_status()
        assert preview.json()["objects"] == 0 and preview.json()["captures"] == 0, (
            preview.text
        )
        reset_result = {"preview": preview.json(), "chat_sessions_preserved": []}
        for saved_session in sessions:
            history = await core.get(
                f"chat/sessions/{saved_session['session_id']}/messages"
            )
            history.raise_for_status()
            assert history.json(), "Model reset must preserve the conversation's messages"
            reset_result["chat_sessions_preserved"].append(saved_session["session_id"])
        (evidence_root / "native-model-reset.json").write_text(
            json.dumps(reset_result, indent=2)
        )
        screenshot = await webdriver.get(prefix + "/screenshot")
        screenshot.raise_for_status()
        (evidence_root / "native-model-reset.png").write_bytes(
            base64.b64decode(screenshot.json()["value"])
        )

        # Close and launch the actual native application, retaining only this
        # disposable profile. Rediscover the completed chat through its list.
        prefix, backend = await relaunch()
        await execute(
            "location.href='/projects/'+encodeURIComponent(arguments[0])+'/workbench?view=chat';return true;",
            project["id"],
        )
        await wait_for(
            "return Boolean(document.querySelector('.session-conversations-toggle'));"
        )
        if not await execute(
            "return Boolean(document.querySelector('.session-list'));"
        ):
            await named_click("Show conversations")
        selected = f'.session-select[data-session-id="{sessions[0]["session_id"]}"]'
        await wait_for(f"return Boolean(document.querySelector('{selected}'));")
        await click(selected)
        await wait_for(
            "return [...document.querySelectorAll('.assistant-markdown')].filter(e=>e.textContent.includes('NATIVE_APPROVAL_ACCEPTED_ONCE')).length===1;"
        )
        assert (
            await execute("return new URL(location.href).searchParams.get('session');")
            == sessions[0]["session_id"]
        )
        if await execute(
            "return Boolean(document.querySelector('button[aria-label=\"Hide conversations\"]'));"
        ):
            await named_click("Hide conversations")
        scroll = await execute(
            "const e=document.querySelector('.chat-scroll'),r=e.getBoundingClientRect();return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2),top:e.scrollTop,range:e.scrollHeight-e.clientHeight};"
        )
        assert scroll["range"] > 100, scroll
        direction = -1 if scroll["top"] > 50 else 1
        moved = await webdriver.post(
            prefix + "/actions",
            json={
                "actions": [
                    {
                        "type": "wheel",
                        "id": "native-transcript",
                        "actions": [
                            {
                                "type": "scroll",
                                "origin": "viewport",
                                "x": scroll["x"],
                                "y": scroll["y"],
                                "deltaX": 0,
                                "deltaY": direction * 400,
                                "duration": 250,
                            }
                        ],
                    }
                ]
            },
        )
        moved.raise_for_status()
        await wait_for(
            f"return Math.abs(document.querySelector('.chat-scroll').scrollTop-{scroll['top']})>50;"
        )
        composer_bounds = await execute(
            "const r=document.querySelector('.chat-composer').getBoundingClientRect(),p=document.querySelector('main').getBoundingClientRect();return {bottom:r.bottom,boundary:p.bottom};"
        )
        assert composer_bounds["bottom"] <= composer_bounds["boundary"], composer_bounds
        async with httpx.AsyncClient(
            base_url=backend["endpoint"].rstrip("/") + "/",
            headers={"Authorization": "Bearer " + backend["token"]},
        ) as reopened:
            for saved in sessions:
                restored = await reopened.get(
                    f"chat/sessions/{saved['session_id']}/state"
                )
                restored.raise_for_status()
                assert restored.json()["execution"] == saved["state"]["execution"], (
                    restored.text
                )
                assert restored.json()["pending"] == [], restored.text
        screenshot = await webdriver.get(prefix + "/screenshot")
        screenshot.raise_for_status()
        (evidence_root / "native-relaunch-scroll.png").write_bytes(
            base64.b64decode(screenshot.json()["value"])
        )

        # Inject an outage only into this disposable native supervisor. The
        # recovery itself uses the visible connection control, never an API
        # substitute. Retain draft/URL and prove completed work is not replayed.
        outage_url = await execute("return location.href;")
        composer = await element("#analyst-message")
        (
            await webdriver.post(
                prefix + f"/element/{composer}/value",
                json={"text": "Unsent native reconnect draft. Do not send."},
            )
        ).raise_for_status()
        stopped = await webdriver.post(
            prefix + "/execute/async",
            json={
                "script": "const done=arguments[arguments.length-1];window.__TAURI_INTERNALS__.invoke('stop_local_backend').then(()=>done({stopped:true})).catch(e=>done({error:String(e)}));",
                "args": [],
            },
        )
        stopped.raise_for_status()
        assert stopped.json()["value"] == {"stopped": True}, stopped.text
        await wait_for(
            "return document.querySelector('.connection-chip')?.getAttribute('aria-label')==='Nebula Core failed. Retry connection';"
        )
        assert await execute("return location.href;") == outage_url
        assert (
            await execute("return document.querySelector('#analyst-message')?.value;")
            == "Unsent native reconnect draft. Do not send."
        )
        await click(".connection-chip")
        await wait_for(
            "return /Nebula Core (ready|degraded)/.test(document.querySelector('.connection-chip')?.getAttribute('aria-label')||'');"
        )
        assert await execute("return location.href;") == outage_url
        assert (
            await execute("return document.querySelector('#analyst-message')?.value;")
            == "Unsent native reconnect draft. Do not send."
        )
        await wait_for(
            "return [...document.querySelectorAll('.assistant-markdown')].filter(e=>e.textContent.includes('NATIVE_APPROVAL_ACCEPTED_ONCE')).length===1;"
        )
        screenshot = await webdriver.get(prefix + "/screenshot")
        screenshot.raise_for_status()
        (evidence_root / "native-core-reconnected.png").write_bytes(
            base64.b64decode(screenshot.json()["value"])
        )
        if not await execute(
            "const e=document.querySelector('a[href=\"/settings\"]');return Boolean(e?.getClientRects().length && e.getBoundingClientRect().width);"
        ):
            await named_click("Show sidebar")
        await click('a[href="/settings"]')
        await wait_for(
            "return Boolean(document.querySelector('a[aria-label=\"Diagnostics settings and recent errors\"]'));"
        )
        await click('a[aria-label="Diagnostics settings and recent errors"]')
        await wait_for(
            "return document.querySelector('.build-identity strong')?.textContent==='Builds match';"
        )
        identity = await execute(
            "const e=document.querySelector('.build-identity');return {text:e.textContent,commits:[...e.querySelectorAll('code')].map(n=>n.title)};"
        )
        assert len(identity["commits"]) == 3 and len(set(identity["commits"])) == 1, (
            identity
        )
        assert len(identity["commits"][0]) == 40, identity
        screenshot = await webdriver.get(prefix + "/screenshot")
        screenshot.raise_for_status()
        (evidence_root / "native-build-identity.png").write_bytes(
            base64.b64decode(screenshot.json()["value"])
        )
        receipts = [
            json.loads(line)
            for line in fixture.with_suffix(".receipts.jsonl").read_text().splitlines()
        ]
        assert [r["allowed"] for r in receipts[:2]] == [
            True,
            False,
        ], receipts
        # Stop may send one cancellation/rejection receipt. It must never
        # produce another allow or replay a completed request after relaunch.
        assert len(receipts) in {2, 3} and all(
            not r["allowed"] for r in receipts[2:]
        ), receipts
        result = {
            "journeys": sessions,
            "adapter_receipts": receipts,
            "tools_executed": 0,
            "runtime": "inert ACP peer through the packaged Core's real Grok adapter",
            "reload_while_waiting": True,
            "reload_after_completion": True,
            "duplicate_decision_did_not_redeliver": True,
            "explicit_stop_preserved": True,
            "actual_app_relaunch": True,
            "saved_chat_rediscovered": True,
            "native_transcript_scroll": True,
            "native_core_outage_recovery": True,
            "native_reconnect_draft_retained": True,
            "fullscreen_workbench": fullscreen,
            "fullscreen_model": graph_bounds,
            "resized_model": graph_resized,
            "displayed_build_identity": identity,
        }
        (evidence_root / "native-approval-evidence.json").write_text(
            json.dumps(result, indent=2)
        )
        print(
            "Packaged desktop: approve, reject, stop, fullscreen, relaunch, scrolling and exact-once delivery passed",
            flush=True,
        )
        return result
