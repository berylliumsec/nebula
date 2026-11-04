"""Terminal-only, paired client of an already running Nebula Core.

Keep this entry point independent of Core's API, database and runtime imports.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import ssl
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Iterator
from urllib.parse import quote, urlsplit

import httpx
import typer


app = typer.Typer(
    help="Pair with Nebula Core and chat with Butler in your terminal.",
    no_args_is_help=True,
)


class ClientError(Exception):
    """An actionable failure that can be shown without a traceback."""


def terminal_text(value: Any) -> str:
    text = str(value)
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)


def origin(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ClientError(
            "Use a Core origin such as https://nebula.example:8765."
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/", "/api/v1", "/api/v1/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ClientError(
            "Use a Core HTTP(S) origin without credentials, a query or fragment."
        )
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = (
        f":{port}" if port and port != {"http": 80, "https": 443}[parsed.scheme] else ""
    )
    return f"{parsed.scheme}://{host}{suffix}"


def loopback(value: str) -> bool:
    host = urlsplit(value).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Settings:
    path: Path
    data: dict[str, Any]
    core: str | None

    @property
    def entry(self) -> dict[str, Any]:
        return self.data.get("cores", {}).get(self.core, {})

    def save(self, entry: dict[str, Any] | None) -> None:
        self.data.setdefault("cores", {})
        if entry is None:
            self.data["cores"].pop(self.core, None)
            if self.data.get("active_core") == self.core:
                self.data["active_core"] = next(iter(self.data["cores"]), None)
        else:
            self.data["cores"][self.core] = entry
            self.data["active_core"] = self.core
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".chat-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


@app.callback()
def configure(
    ctx: typer.Context,
    core: Annotated[
        str | None,
        typer.Option(
            envvar="NEBULA_CORE_URL",
            help="Core origin; defaults to the last paired Core.",
        ),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option(
            envvar="NEBULA_CHAT_CONFIG", help="Private pairing configuration file."
        ),
    ] = None,
) -> None:
    path = (
        config
        or Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "nebula"
        / "chat.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict) or not isinstance(data.get("cores", {}), dict):
            raise ValueError("invalid configuration")
        if any(not isinstance(entry, dict) for entry in data.get("cores", {}).values()):
            raise ValueError("invalid Core entry")
        selected = core or data.get("active_core")
        ctx.obj = Settings(path, data, origin(selected) if selected else None)
    except (OSError, ValueError, ClientError) as exc:
        typer.echo(f"Cannot read pairing configuration: {terminal_text(exc)}", err=True)
        raise typer.Exit(1) from exc


def require_core(settings: Settings) -> str:
    if not settings.core:
        raise ClientError("Choose a Core with --core https://HOST:PORT, then run pair.")
    return settings.core


def check_response(response: httpx.Response, *, pairing: bool = False) -> None:
    if response.is_success:
        return
    if response.is_redirect:
        raise ClientError(
            "Core redirected the request. Use its final HTTPS origin and pair there."
        )
    try:
        detail = response.json().get("detail", "")
    except (ValueError, AttributeError):
        detail = ""
    if not isinstance(detail, str):
        detail = "Core rejected the supplied values."
    if response.status_code == 401 and not pairing:
        detail = "Pairing expired or was revoked. Run pair again for this Core."
    raise ClientError(f"Core ({response.status_code}): {terminal_text(detail)[:1500]}")


@contextmanager
def connection(
    settings: Settings,
    *,
    paired: bool = True,
    ca_cert: Path | None = None,
    allow_http: bool = False,
) -> Iterator[httpx.Client]:
    core = require_core(settings)
    entry = settings.entry if paired else {}
    if paired and not all(
        entry.get(key) for key in ("device_token", "csrf_token", "device_id")
    ):
        raise ClientError("This Core is not paired. Run pair first.")
    if core.startswith("http:") and not (allow_http or entry.get("allow_http")):
        raise ClientError(
            "Pairing requires HTTPS. For an explicitly enabled test LAN, use pair --allow-http."
        )
    certificate = ca_cert or entry.get("ca_cert")
    verification: ssl.SSLContext | bool = (
        ssl.create_default_context(cafile=str(certificate)) if certificate else True
    )
    headers = {"Origin": core, "User-Agent": "nebula-chat"}
    cookies = {}
    if paired:
        headers["X-Nebula-CSRF"] = entry["csrf_token"]
        cookies = {
            "nebula_device": entry["device_token"],
            "nebula_csrf": entry["csrf_token"],
        }
    with httpx.Client(
        base_url=core + "/api/v1/",
        headers=headers,
        cookies=cookies,
        verify=verification,
        follow_redirects=False,
        timeout=httpx.Timeout(30, read=90),
    ) as client:
        yield client


def request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    check_response(response)
    return response.json() if response.content else None


@contextmanager
def user_errors() -> Iterator[None]:
    try:
        yield
    except (ClientError, OSError, httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, httpx.HTTPError):
            message = "Cannot reach Core securely. Check its address, connection and CA certificate, then reconnect. No message was automatically retried."
        else:
            message = terminal_text(exc)
        typer.echo(message, err=True)
        raise typer.Exit(1) from exc


@app.command()
def invite(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Option(help="Name for the new terminal device.")
    ] = "Butler terminal",
    ca_cert: Annotated[
        Path | None,
        typer.Option(
            exists=True, dir_okay=False, help="CA certificate for a private Core."
        ),
    ] = None,
) -> None:
    """On the Core host, issue a single-use invitation (expires in five minutes)."""
    settings: Settings = ctx.obj
    with user_errors():
        core = require_core(settings)
        if not loopback(core):
            raise ClientError(
                "Run invite on the Core host using its loopback origin (127.0.0.1 or ::1)."
            )
        token = os.getenv("NEBULA_V3_API_TOKEN") or typer.prompt(
            "Core host bearer token", hide_input=True
        )
        with connection(
            settings, paired=False, ca_cert=ca_cert, allow_http=True
        ) as client:
            response = client.post(
                "auth/pairings",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": name},
            )
            check_response(response, pairing=True)
            offer = response.json()
        typer.echo(
            "On the client, run nebula-chat --core https://HOST:PORT pair and enter:"
        )
        typer.echo(f"Pairing secret: {offer['secret']}")
        typer.echo(f"Confirmation code: {offer['confirmation_code']}")
        typer.echo(f"Expires: {offer['expires_at']}")


@app.command()
def pair(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Option(help="Device name shown in Core's paired devices.")
    ] = "Butler terminal",
    ca_cert: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="CA certificate for a private Core; TLS verification stays enabled.",
        ),
    ] = None,
    allow_http: Annotated[
        bool,
        typer.Option(
            help="Allow unencrypted pairing on an explicitly enabled test LAN."
        ),
    ] = False,
) -> None:
    """Redeem the host's invitation and save a revocable device pairing."""
    settings: Settings = ctx.obj
    with user_errors():
        if settings.entry:
            raise ClientError(
                "This Core already has saved credentials. Run unpair before replacing them."
            )
        # Validate the connection before asking for one-time credentials.
        with connection(
            settings, paired=False, ca_cert=ca_cert, allow_http=allow_http
        ) as client:
            secret = typer.prompt("Pairing secret", hide_input=True).strip()
            code = typer.prompt("Confirmation code", hide_input=True).strip()
            response = client.post(
                "auth/pairings/redeem",
                json={"secret": secret, "confirmation_code": code, "name": name},
            )
            check_response(response, pairing=True)
            paired = response.json()
            device_token = response.cookies.get("nebula_device")
            csrf = response.cookies.get("nebula_csrf")
            if not device_token or not csrf or csrf != paired.get("csrf_token"):
                raise ClientError(
                    "Core returned an incomplete pairing. Issue a new invitation and retry."
                )
            entry = {
                "device_id": paired["device"]["id"],
                "device_name": paired["device"]["name"],
                "device_token": device_token,
                "csrf_token": csrf,
                "ca_cert": str(ca_cert.resolve()) if ca_cert else None,
                "allow_http": allow_http,
            }
            client.headers["X-Nebula-CSRF"] = csrf
            devices = request(client, "GET", "auth/devices")
            if not any(
                item["id"] == entry["device_id"] and item["current"] for item in devices
            ):
                raise ClientError(
                    "Core did not confirm the new pairing. Issue a new invitation and retry."
                )
            settings.save(entry)
        typer.echo(
            f"Paired with {settings.core} as {terminal_text(entry['device_name'])}. Run chat."
        )


@app.command()
def unpair(ctx: typer.Context) -> None:
    """Revoke this terminal's device session and remove its saved credentials."""
    settings: Settings = ctx.obj
    with user_errors(), connection(settings) as client:
        response = client.delete(
            f"auth/devices/{quote(settings.entry['device_id'], safe='')}"
        )
        if response.status_code != 401:
            check_response(response)
        settings.save(None)
        typer.echo("Unpaired. Local credentials removed.")


def pages(client: httpx.Client, path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        page = request(client, "GET", path, params={"offset": len(items), "limit": 100})
        items.extend(page)
        if len(page) < 100:
            return items


def choose(
    items: list[dict[str, Any]], label: str, selected: str | None = None
) -> dict[str, Any]:
    if selected:
        exact_id = next((item for item in items if item["id"] == selected), None)
        if exact_id:
            return exact_id
        items = [
            item for item in items if item["name"].casefold() == selected.casefold()
        ]
    if not items:
        raise ClientError(
            f"No matching {label}. Run projects to browse or create a project; configure a model on Core if needed."
        )
    if len(items) == 1:
        return items[0]
    for index, item in enumerate(items, 1):
        typer.echo(
            f"{index}. {terminal_text(item['name'])} ({terminal_text(item['id'])})"
        )
    while True:
        number = typer.prompt(f"Choose {label}", type=int)
        if 1 <= number <= len(items):
            return items[number - 1]
        typer.echo(f"Choose a number from 1 to {len(items)}.")


@app.command()
def projects(
    ctx: typer.Context,
    create: Annotated[
        str | None, typer.Option(help="Create a project by name.")
    ] = None,
) -> None:
    """Browse Core projects, or create a project for a new headless Core."""
    with user_errors(), connection(ctx.obj) as client:
        if create:
            project = request(client, "POST", "engagements", json={"name": create})
            observed = request(
                client, "GET", f"engagements/{quote(project['id'], safe='')}"
            )
            typer.echo(f"Created {terminal_text(observed['name'])} ({observed['id']}).")
        items = pages(client, "engagements")
        if not items:
            typer.echo('No projects yet. Run projects --create "Project name".')
        for item in items:
            typer.echo(f"{terminal_text(item['name'])} ({terminal_text(item['id'])})")


def butler_session(client: httpx.Client, project: dict[str, Any]) -> dict[str, Any]:
    session = request(
        client,
        "GET",
        "chat/control-assistant/session",
        params={"engagement_id": project["id"]},
    )
    if session:
        return session
    defaults = project.get("assistant_defaults") or {}
    options: list[dict[str, Any]] = []
    for backend, path in (("harness", "harnesses"), ("provider", "providers")):
        for profile in pages(client, path):
            if not profile.get("enabled"):
                continue
            model = (
                (
                    defaults.get("model")
                    if profile["id"]
                    in {defaults.get("provider_id"), defaults.get("harness_id")}
                    else None
                )
                or profile.get("default_model")
                or profile.get("metadata", {}).get("default_model")
                or next(iter(profile.get("model_allowlist", [])), None)
                or next(iter(profile.get("capabilities", {}).get("models", [])), None)
            )
            if model:
                options.append(
                    {
                        "id": profile["id"],
                        "name": f"{profile['name']} / {model}",
                        "backend": backend,
                        "model": model,
                    }
                )
    preferred = (
        defaults.get("harness_id")
        if defaults.get("backend") == "harness"
        else defaults.get("provider_id")
    )
    runtime = choose(
        options,
        "model runtime",
        preferred if any(item["id"] == preferred for item in options) else None,
    )
    session = request(
        client,
        "POST",
        "chat/control-assistant/session",
        json={
            "engagement_id": project["id"],
            "backend": runtime["backend"],
            "model": runtime["model"],
            "provider_id"
            if runtime["backend"] == "provider"
            else "harness_profile_id": runtime["id"],
        },
    )
    return session


def history(client: httpx.Client, session_id: str) -> None:
    messages = request(
        client,
        "GET",
        f"chat/sessions/{quote(session_id, safe='')}/messages",
        params={"include_replaced": "true", "limit": 20},
    )
    for message in messages:
        if (
            message["role"] in {"user", "assistant"}
            and message.get("content")
            and not message.get("metadata", {}).get("retracted_at")
        ):
            typer.echo(
                f"{'You' if message['role'] == 'user' else 'Butler'}: {terminal_text(message['content'])}"
            )


def server_events(response: httpx.Response) -> Iterator[tuple[str, dict[str, Any]]]:
    event = "message"
    data: list[str] = []
    for line in response.iter_lines():
        if not line:
            if data:
                payload = json.loads("\n".join(data))
                if not isinstance(payload, dict):
                    raise ClientError(
                        "Core sent an invalid stream. Reconnect and review /history before retrying."
                    )
                yield event, payload
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))


def pending(client: httpx.Client, session_id: str) -> dict[str, Any] | None:
    return request(
        client, "GET", f"chat/sessions/{quote(session_id, safe='')}/pending-turn"
    )


def stop(client: httpx.Client, turn_id: str) -> None:
    result = request(client, "POST", f"chat/turns/{quote(turn_id, safe='')}/cancel")
    typer.echo(f"Turn {terminal_text(result['status'])}.")


def stream(
    client: httpx.Client,
    *,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    turn_id: str | None = None,
) -> None:
    displayed = ""
    finished = False
    typer.echo("Butler: ", nl=False)
    try:
        with client.stream(
            method, path, **({"json": body} if body is not None else {})
        ) as response:
            if not response.is_success:
                response.read()
                check_response(response)
            for event, payload in server_events(response):
                turn_id = payload.get("turn_id") or turn_id
                if event in {"delta", "message_delta"}:
                    delta = terminal_text(payload.get("delta") or "")
                    displayed += delta
                    typer.echo(delta, nl=False)
                elif event == "done":
                    final = terminal_text(
                        (payload.get("message") or {}).get("content", "")
                    )
                    typer.echo(
                        final[len(displayed) :]
                        if final.startswith(displayed)
                        else "\nSaved answer: " + final,
                        nl=False,
                    )
                    finished = True
                    break
                elif event in {
                    "approval_required",
                    "input_required",
                    "interaction_required",
                } or (
                    event in {"approval", "interaction"}
                    and payload.get("item_status")
                    in {"waiting_approval", "waiting_input"}
                ):
                    typer.echo(
                        f"\nAction required: {terminal_text(payload.get('detail') or payload.get('message') or payload.get('summary') or payload.get('title') or event.replace('_', ' '))}. Review /status; /stop cancels the turn."
                    )
                    finished = True
                    break
                elif event in {"error", "cancelled"}:
                    raise ClientError(
                        terminal_text(
                            payload.get("operator_detail")
                            or payload.get("detail")
                            or payload.get("message")
                            or "Turn stopped. Review /history before retrying."
                        )
                    )
        if not finished:
            raise ClientError(
                "Connection ended before completion. Reconnect or use /follow and /history to review Core's saved turn before retrying."
            )
    except KeyboardInterrupt:
        typer.echo()
        if turn_id:
            stop(client, turn_id)
        else:
            typer.echo(
                "Detached before Core confirmed the turn. Review /status before using /stop or retrying."
            )
    finally:
        typer.echo()


def turn_body(
    client: httpx.Client, project_id: str, session: dict[str, Any], text: str
) -> dict[str, Any]:
    config = request(
        client, "GET", f"chat-sessions/{quote(session['id'], safe='')}/assistant-config"
    )["settings"]
    backend = config["backend"]
    values = {
        key: value
        for key, value in config.items()
        if key not in {"profile_id", "backend"} and value is not None
    }
    # Core validates runtime-specific controls. Provider requests cannot carry
    # harness-only options (the normalized config exposes both kinds).
    if backend == "provider":
        values = {
            key: value
            for key, value in values.items()
            if not key.startswith("harness_")
        }
    return {
        **values,
        "backend": backend,
        "provider_id" if backend == "provider" else "harness_profile_id": config[
            "profile_id"
        ],
        "control_assistant": True,
        "engagement_id": project_id,
        "session_id": session["id"],
        "messages": [{"role": "user", "content": text}],
        "stream": True,
    }


@app.command()
def chat(
    ctx: typer.Context,
    project: Annotated[
        str | None,
        typer.Option(help="Project name or ID; otherwise choose from Core's projects."),
    ] = None,
) -> None:
    """Open or resume the selected project's saved Butler conversation."""
    settings: Settings = ctx.obj
    with user_errors(), connection(settings) as client:
        selected = choose(pages(client, "engagements"), "project", project)
        session = butler_session(client, selected)
        if (
            session.get("engagement_id") != selected["id"]
            or session.get("metadata", {}).get("control_assistant") is not True
        ):
            raise ClientError(
                "Core returned a conversation outside this project's Butler. Reconnect before sending."
            )
        typer.echo(f"{terminal_text(selected['name'])} — Butler at {settings.core}")
        typer.echo(
            "/help for commands; Ctrl-C stops a response; /quit leaves Core work running."
        )
        history(client, session["id"])
        active = pending(client, session["id"])
        if active:
            typer.echo(
                f"Existing turn: {terminal_text(active['status'])}. Use /follow, /status or /stop."
            )
        while True:
            try:
                text = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                typer.echo()
                return
            if not text:
                continue
            if text in {"/quit", "/exit"}:
                return
            if text == "/help":
                typer.echo(
                    "/history — recent saved messages\n/status — current turn\n/follow — attach to existing output\n/stop — cancel the current turn\n/quit — exit; Core work continues"
                )
                continue
            try:
                if text == "/history":
                    history(client, session["id"])
                    continue
                active = pending(client, session["id"])
                if text == "/status":
                    typer.echo(
                        f"Turn: {terminal_text(active['status'])}."
                        if active
                        else "Butler is ready."
                    )
                elif text == "/stop":
                    stop(client, active["id"]) if active else typer.echo(
                        "No active turn."
                    )
                elif text == "/follow":
                    stream(
                        client,
                        method="GET",
                        path=f"chat/turns/{quote(active['id'], safe='')}/events",
                        turn_id=active["id"],
                    ) if active else history(client, session["id"])
                elif text.startswith("/"):
                    typer.echo("Unknown command. Use /help.")
                elif active:
                    typer.echo(
                        "A turn is already active. Use /follow, /status or /stop before sending another message."
                    )
                else:
                    stream(
                        client,
                        method="POST",
                        path="chat/completions",
                        body=turn_body(client, selected["id"], session, text),
                    )
            except (ClientError, httpx.HTTPError, ValueError) as exc:
                if isinstance(exc, httpx.HTTPError):
                    typer.echo(
                        "Connection lost. Use /follow or /history to review Core's saved state before retrying."
                    )
                else:
                    typer.echo(terminal_text(exc))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
