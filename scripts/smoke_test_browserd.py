"""Exercise the staged full Chromium through browserd's durable action boundary."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile


async def _respond_as_bounded_proxy(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        body = b"<html><head><title>Nebula browserd smoke</title></head><body><button>Ready</button></body></html>"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def smoke(runtime_root: Path) -> dict[str, object]:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime_root)
    from nebula.v3.browser_engine import BrowserEngineAction
    from nebula.v3.browserd import BrowserdManager, BrowserdSettings
    from nebula.v3.domain import BrowserEngineState

    proxy = await asyncio.start_server(
        _respond_as_bounded_proxy, host="127.0.0.1", port=0
    )
    port = int(proxy.sockets[0].getsockname()[1])
    with tempfile.TemporaryDirectory(prefix="nebula-browserd-smoke-") as temporary:
        manager = BrowserdManager(
            BrowserdSettings(
                token="smoke-only-token",
                profile_root=Path(temporary) / "profiles",
                policy_proxy_url=f"http://127.0.0.1:{port}",
                runtime_root=runtime_root,
                headless=True,
            )
        )
        try:
            await manager.start()
            capability = await manager.readiness()
            if capability.state != BrowserEngineState.DEGRADED:
                raise RuntimeError(
                    f"headless smoke expected degraded readiness, found {capability.state.value}"
                )
            identity = await manager.ensure_identity("smoke-identity")
            action = BrowserEngineAction(
                action_token="smoke-navigation-once",
                assessment_id="smoke-assessment",
                session_id="smoke-session",
                identity_id=identity.identity_id,
                tab_id=identity.tab_ids[0],
                kind="navigate",
                arguments={"url": "http://browserd-smoke.example.test/"},
                side_effect="possible",
            )
            receipt = await manager.execute(action)
            if receipt.state != "complete":
                raise RuntimeError(
                    f"managed navigation did not complete: {receipt.model_dump_json()}"
                )
            duplicate = await manager.execute(action)
            if duplicate != receipt:
                raise RuntimeError("duplicate action token did not return its durable receipt")
            await manager.lifecycle(action.assessment_id, "paused")
            cancelled = await manager.execute(
                action.model_copy(update={"action_token": "smoke-after-pause"})
            )
            if cancelled.state != "cancelled":
                raise RuntimeError("paused assessment accepted a new browser action")
            return {
                "state": "passed",
                "engine_state": capability.state.value,
                "engine_digest": capability.digest,
                "action_state": receipt.state,
                "duplicate_receipt": True,
                "paused_action_state": cancelled.state,
                "trace_count": len(receipt.trace_ids),
            }
        finally:
            await manager.close()
            proxy.close()
            await proxy.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("ui/src-tauri/resources/playwright-browsers"),
    )
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(smoke(arguments.runtime_root.resolve())), sort_keys=True))


if __name__ == "__main__":
    main()
