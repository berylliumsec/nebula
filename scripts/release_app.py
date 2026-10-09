"""Short-lived, single-repository GitHub App credentials for release operations.

Mint immediately before each operation, so multi-hour release waits never reuse
an expired token. Keep signing keys out of child environments and API errors.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

PREFIX = "NEBULA_RELEASE_APP_"


def child_env() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(PREFIX) and name != "NEBULA_RELEASE_TOKEN"
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(method: str, endpoint: str, token: str | None, data: dict | None = None):
    request = Request(
        f"https://api.github.com/{endpoint}",
        data=json.dumps(data).encode() if data is not None else None,
        method=method,
        headers={
            **({"Authorization": f"Bearer {token}"} if token else {}),
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except HTTPError as error:
        raise RuntimeError(
            f"GitHub App API {method} {endpoint}: HTTP {error.code}"
        ) from None
    except (URLError, OSError, ValueError):
        raise RuntimeError(f"GitHub App API {method} {endpoint} failed") from None


def jwt(client_id: str, private_key: str) -> str:
    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    now = int(time.time())
    message = ".".join(
        encode(json.dumps(value, separators=(",", ":")).encode())
        for value in (
            {"alg": "RS256", "typ": "JWT"},
            {"iat": now - 60, "exp": now + 540, "iss": client_id},
        )
    )
    # The temporary directory is private (0700); the key itself is 0600 and
    # removed before any API request or release subprocess starts.
    with tempfile.TemporaryDirectory() as directory:
        key = Path(directory, "key.pem")
        key.touch(mode=0o600)
        key.write_text(private_key)
        env = child_env()
        env.pop("GH_TOKEN", None)
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key)],
            input=message.encode(),
            capture_output=True,
            env=env,
            check=False,
            timeout=30,
        )
    if result.returncode or not result.stdout:
        raise RuntimeError("Cannot sign GitHub App identity")
    return f"{message}.{encode(result.stdout)}"


def configuration() -> dict:
    names = ("ID", "CLIENT_ID", "INSTALLATION_ID", "SLUG", "BOT_ID", "PRIVATE_KEY")
    values = {name: os.environ.get(PREFIX + name, "") for name in names}
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    repository_id = os.environ.get("GITHUB_REPOSITORY_ID", "")
    if (
        not all(values.values())
        or any(
            not values[name].isdigit() for name in ("ID", "INSTALLATION_ID", "BOT_ID")
        )
        or not repository_id.isdigit()
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not re.fullmatch(r"[a-z0-9-]+", values["SLUG"])
    ):
        raise RuntimeError("Protected GitHub App configuration is required")
    values["repository"] = repository
    values["repository_id"] = int(repository_id)
    return values


@contextmanager
def installation_token(permission: str):
    if permission not in {"contents", "actions"}:
        raise RuntimeError("Unsupported release App permission")
    config = configuration()
    identity = jwt(config["CLIENT_ID"], config["PRIVATE_KEY"])
    app = api("GET", "app", identity)
    owner = config["repository"].split("/")[0]
    expected_permissions = {"contents": "write", "actions": "write", "metadata": "read"}
    if (
        app.get("id") != int(config["ID"])
        or app.get("slug") != config["SLUG"]
        or app.get("owner", {}).get("login") != owner
        or app.get("owner", {}).get("type") != "Organization"
        or app.get("permissions") != expected_permissions
    ):
        raise RuntimeError("Release App identity, owner or permissions do not match")
    installation = api(
        "GET", f"app/installations/{config['INSTALLATION_ID']}", identity
    )
    if (
        installation.get("app_id") != int(config["ID"])
        or installation.get("account", {}).get("login") != owner
        or installation.get("repository_selection") != "selected"
        or installation.get("permissions") != expected_permissions
        or installation.get("suspended_at") is not None
    ):
        raise RuntimeError("Release App installation does not match the approved scope")
    bot = api("GET", f"users/{quote(config['SLUG'] + '[bot]', safe='')}", None)
    if bot.get("id") != int(config["BOT_ID"]) or bot.get("type") != "Bot":
        raise RuntimeError("Release App bot identity does not match")
    response = api(
        "POST",
        f"app/installations/{config['INSTALLATION_ID']}/access_tokens",
        identity,
        {
            "repository_ids": [config["repository_id"]],
            "permissions": {permission: "write"},
        },
    )
    token = response.get("token", "")
    if not re.fullmatch(r"[A-Za-z0-9_]+", token):
        raise RuntimeError("Invalid installation token response")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{token}", flush=True)
    try:
        if response.get("permissions") != {permission: "write", "metadata": "read"}:
            raise RuntimeError("Installation token permissions exceed the operation")
        expiry = datetime.fromisoformat(response["expires_at"].replace("Z", "+00:00"))
        remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
        if not 120 <= remaining <= 3660:
            raise RuntimeError(
                "Installation token lifetime is not a fresh one-hour lease"
            )
        repositories = api("GET", "installation/repositories", token)
        if repositories.get("total_count") != 1 or [
            (repo.get("id"), repo.get("full_name"))
            for repo in repositories.get("repositories", [])
        ] != [(config["repository_id"], config["repository"])]:
            raise RuntimeError("Installation token repository scope does not match")
        yield token
    finally:
        # Revoke even after validation or command failure. A revocation failure
        # stops the driver; it never silently advances to the next release phase.
        api("DELETE", "installation/token", token)
