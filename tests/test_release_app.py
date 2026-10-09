"""Release App scope/lifecycle and real shell actor-gate regression checks.

No GitHub calls, credentials or keys are created. Signing is stubbed; actor
checks execute against a local fake gh command, never the authenticated CLI.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_app


@pytest.fixture
def app(monkeypatch):
    config = {
        "ID": "123",
        "CLIENT_ID": "Iv1.fixture",
        "INSTALLATION_ID": "456",
        "SLUG": "nebula-release-fixture",
        "BOT_ID": "789",
        "PRIVATE_KEY": "NOT A KEY",
    }
    for name, value in config.items():
        monkeypatch.setenv(release_app.PREFIX + name, value)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/nebula")
    monkeypatch.setenv("GITHUB_REPOSITORY_ID", "42")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    state = {
        "app": {
            "id": 123,
            "slug": config["SLUG"],
            "owner": {"login": "example", "type": "Organization"},
            "permissions": {
                "contents": "write",
                "actions": "write",
                "metadata": "read",
            },
        },
        "installation": {
            "app_id": 123,
            "account": {"login": "example"},
            "repository_selection": "selected",
            "suspended_at": None,
            "permissions": {
                "contents": "write",
                "actions": "write",
                "metadata": "read",
            },
        },
        "bot": {"id": 789, "type": "Bot"},
        "repositories": {
            "total_count": 1,
            "repositories": [{"id": 42, "full_name": "example/nebula"}],
        },
        "calls": [],
        "issued": 0,
    }

    def api(method, endpoint, token, data=None):
        state["calls"].append((method, endpoint, token, data))
        if state.get("denied") == endpoint:
            raise RuntimeError("HTTP 403")
        if endpoint == "app":
            return state["app"]
        if endpoint == "app/installations/456":
            return state["installation"]
        if endpoint.startswith("users/"):
            assert token is None  # JWTs cannot authenticate ordinary REST endpoints.
            return state["bot"]
        if endpoint.endswith("access_tokens"):
            assert data["repository_ids"] == [42]
            assert len(data["permissions"]) == 1
            state["issued"] += 1
            return {
                "token": f"ghs_fixture_{state['issued']}",
                "permissions": state.get(
                    "token_permissions", {**data["permissions"], "metadata": "read"}
                ),
                "expires_at": (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=state.get("lease_seconds", 3600))
                ).isoformat(),
            }
        if endpoint == "installation/repositories":
            return state["repositories"]
        assert method == "DELETE" and endpoint == "installation/token"

    monkeypatch.setattr(release_app, "api", api)
    monkeypatch.setattr(release_app, "jwt", lambda *_args: "fixture-jwt")
    return state


@pytest.mark.parametrize("permission", ["contents", "actions"])
def test_each_operation_mints_and_revokes_fresh_single_permission_token(
    app, permission
):
    for count in (1, 2):
        with release_app.installation_token(permission) as token:
            assert token == f"ghs_fixture_{count}"
        assert app["calls"][-1][:3] == ("DELETE", "installation/token", token)
    assert app["issued"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", 9),
        ("slug", "another-app"),
        ("owner", {"login": "other"}),
        ("permissions", {"administration": "write"}),
    ],
)
def test_wrong_app_or_expanded_permission_fails_before_mint(app, field, value):
    app["app"][field] = value
    with pytest.raises(RuntimeError, match="do not match"):
        with release_app.installation_token("contents"):
            pytest.fail("must not yield")
    assert app["issued"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("app_id", 9),
        ("account", {"login": "other"}),
        ("repository_selection", "all"),
        ("suspended_at", "2026-10-09"),
        ("permissions", {"contents": "write", "actions": "read", "metadata": "read"}),
    ],
)
def test_wrong_or_suspended_installation_fails_before_mint(app, field, value):
    app["installation"][field] = value
    with pytest.raises(RuntimeError, match="approved scope"):
        with release_app.installation_token("contents"):
            pytest.fail("must not yield")
    assert app["issued"] == 0


@pytest.mark.parametrize("field,value", [("id", 9), ("type", "User")])
def test_wrong_bot_fails_before_mint(app, field, value):
    app["bot"][field] = value
    with pytest.raises(RuntimeError, match="bot identity"):
        with release_app.installation_token("actions"):
            pytest.fail("must not yield")
    assert app["issued"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("total_count", 2),
        ("repositories", [{"id": 9, "full_name": "example/nebula"}]),
        ("repositories", [{"id": 42, "full_name": "other/nebula"}]),
    ],
)
def test_scope_mismatch_revokes_before_operation(app, field, value):
    app["repositories"][field] = value
    with pytest.raises(RuntimeError, match="repository scope"):
        with release_app.installation_token("actions"):
            pytest.fail("must not yield")
    assert app["calls"][-1][:2] == ("DELETE", "installation/token")


@pytest.mark.parametrize("seconds", [60, -1, 7200])
def test_stale_or_unexpected_token_lifetime_revokes(app, seconds):
    app["lease_seconds"] = seconds
    with pytest.raises(RuntimeError, match="one-hour lease"):
        with release_app.installation_token("contents"):
            pytest.fail("must not yield")
    assert app["calls"][-1][:2] == ("DELETE", "installation/token")


def test_excess_token_permissions_revoked(app):
    app["token_permissions"] = {
        "contents": "write",
        "actions": "write",
        "metadata": "read",
    }
    with pytest.raises(RuntimeError, match="exceed"):
        with release_app.installation_token("contents"):
            pytest.fail("must not yield")
    assert app["calls"][-1][:2] == ("DELETE", "installation/token")


def test_command_failure_also_revokes(app):
    with pytest.raises(RuntimeError, match="uncertain push"):
        with release_app.installation_token("contents"):
            raise RuntimeError("uncertain push")
    assert app["calls"][-1][:2] == ("DELETE", "installation/token")


@pytest.mark.parametrize(
    "endpoint", ["app", "installation/repositories", "installation/token"]
)
def test_api_denial_stops_without_permission_escalation(app, endpoint):
    app["denied"] = endpoint
    with pytest.raises(RuntimeError, match="HTTP 403"):
        with release_app.installation_token("contents"):
            pass
    if endpoint == "installation/repositories":
        assert app["calls"][-1][:2] == ("DELETE", "installation/token")


@pytest.mark.parametrize(
    "missing", ["ID", "CLIENT_ID", "INSTALLATION_ID", "SLUG", "BOT_ID", "PRIVATE_KEY"]
)
def test_missing_configuration_fails_closed(app, monkeypatch, missing):
    monkeypatch.delenv(release_app.PREFIX + missing)
    with pytest.raises(RuntimeError, match="configuration"):
        with release_app.installation_token("contents"):
            pytest.fail("must not yield")
    assert not app["calls"]


def test_jwt_clock_claims_private_file_cleanup_and_environment(monkeypatch):
    monkeypatch.setenv("NEBULA_RELEASE_APP_PRIVATE_KEY", "NOT A KEY")
    monkeypatch.setenv("GH_TOKEN", "fixture-job-token")
    paths = []

    def sign(args, **kwargs):
        key = Path(args[-1])
        paths.append(key)
        assert key.stat().st_mode & 0o777 == 0o600
        assert key.read_text() == "NOT A KEY"
        assert "NOT A KEY" not in kwargs["env"].values()
        assert "GH_TOKEN" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=b"stub-signature")

    with (
        patch.object(release_app.time, "time", return_value=1000),
        patch.object(release_app.subprocess, "run", side_effect=sign),
    ):
        token = release_app.jwt("Iv1.fixture", "NOT A KEY")
    assert all(not path.exists() for path in paths)
    header, payload, _signature = token.split(".")

    def decode(value):
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))

    assert decode(header) == {"alg": "RS256", "typ": "JWT"}
    assert decode(payload) == {"iat": 940, "exp": 1540, "iss": "Iv1.fixture"}


WORKFLOWS = (
    "nebula3-release.yml",
    "nebula3-release-finalize.yml",
    "publish-updater-manifest.yml",
)


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize(
    "case,accepted",
    [
        ("app", True),
        ("human-admin", True),
        ("human-writer", False),
        ("human-admin-rerun", True),
        ("human-writer-rerun", False),
        ("wrong-id", False),
        ("wrong-sender", False),
        ("wrong-slug", False),
        ("push-bot", False),
        ("missing-config", False),
        ("writer-rerun", False),
        ("admin-rerun", True),
    ],
)
def test_actor_gate_executes_exact_identity_and_admin_fallback(
    tmp_path, workflow, case, accepted
):
    source = Path(__file__).resolve().parents[1] / ".github/workflows" / workflow
    jobs = yaml.safe_load(source.read_text())["jobs"]
    step = next(
        step
        for job in jobs.values()
        for step in job["steps"]
        if "RELEASE_ACTOR_ID" in step.get("env", {})
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/sh\ncase "$2" in */collaborators/unauthorized/permission) echo write;; '
        '*) printf "%s\\n" "$FAKE_PERMISSION";; esac\n'
    )
    gh.chmod(0o700)
    env = {
        **release_app.child_env(),
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "GITHUB_REPOSITORY": "example/nebula",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "RELEASE_ACTOR": "nebula-release-fixture[bot]",
        "RELEASE_ACTOR_ID": "789",
        "RELEASE_SENDER_ID": "789",
        "RELEASE_SENDER_TYPE": "Bot",
        "RELEASE_TRIGGERING_ACTOR": "nebula-release-fixture[bot]",
        "RELEASE_APP_BOT_ID": "789",
        "RELEASE_APP_SLUG": "nebula-release-fixture",
        "FAKE_PERMISSION": "write",
    }
    if case.startswith("human"):
        env.update(
            RELEASE_ACTOR="human",
            RELEASE_ACTOR_ID="1",
            RELEASE_SENDER_ID="1",
            RELEASE_SENDER_TYPE="User",
            RELEASE_TRIGGERING_ACTOR="human",
        )
        env["FAKE_PERMISSION"] = "write" if case == "human-writer" else "admin"
    if case == "wrong-id":
        env["RELEASE_ACTOR_ID"] = "1"
    if case == "wrong-sender":
        env["RELEASE_SENDER_ID"] = "1"
    if case == "wrong-slug":
        env["RELEASE_ACTOR"] = "unrelated[bot]"
    if case == "push-bot":
        env["GITHUB_EVENT_NAME"] = "push"
    if case == "missing-config":
        env["RELEASE_APP_BOT_ID"] = ""
    if case in {"writer-rerun", "admin-rerun"}:
        env["RELEASE_TRIGGERING_ACTOR"] = "human"
        env["FAKE_PERMISSION"] = "admin" if case == "admin-rerun" else "write"
    if case == "human-admin-rerun":
        env["RELEASE_TRIGGERING_ACTOR"] = "another-admin"
    if case == "human-writer-rerun":
        env["RELEASE_TRIGGERING_ACTOR"] = "unauthorized"
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert (result.returncode == 0) == accepted, result.stderr


@pytest.mark.parametrize(
    "actor,trigger,accepted",
    [
        ("admin", "admin", True),
        ("admin", "writer", False),
        ("writer", "admin", False),
        ("writer", "writer", False),
        ("admin", "admin-two", True),
    ],
)
def test_daily_initiator_and_rerun_gate_before_key_access(
    tmp_path, actor, trigger, accepted
):
    source = (
        Path(__file__).resolve().parents[1]
        / ".github/workflows/daily-stable-release.yml"
    )
    step = yaml.safe_load(source.read_text())["jobs"]["validate-admin"]["steps"][0]
    gh = tmp_path / "gh"
    gh.write_text(
        '#!/bin/sh\ncase "$2" in */collaborators/admin/permission|*/collaborators/admin-two/permission) echo admin;; *) echo write;; esac\n'
    )
    gh.chmod(0o700)
    env = {
        **release_app.child_env(),
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "GITHUB_REPOSITORY": "example/nebula",
        "RELEASE_ACTOR": actor,
        "RELEASE_TRIGGERING_ACTOR": trigger,
    }
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert (result.returncode == 0) == accepted, result.stderr


def test_api_errors_do_not_expose_authorization_or_response_body():
    def fail(request, timeout):
        assert timeout == 30
        assert request.get_header("Authorization") == "Bearer fixture-secret"
        raise HTTPError(
            request.full_url, 403, "fixture-secret in server body", None, None
        )

    with patch.object(
        release_app, "build_opener", return_value=SimpleNamespace(open=fail)
    ):
        with pytest.raises(RuntimeError) as error:
            release_app.api("GET", "app", "fixture-secret")
    assert str(error.value) == "GitHub App API GET app: HTTP 403"
    assert error.value.__suppress_context__ is True


def test_api_redirects_cannot_forward_credentials():
    assert (
        release_app.NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://example.invalid"
        )
        is None
    )


def test_signing_failure_hides_key_and_removes_temporary_file():
    paths = []

    def fail(args, **kwargs):
        paths.append(Path(args[-1]))
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"NOT A KEY")

    with patch.object(release_app.subprocess, "run", side_effect=fail):
        with pytest.raises(RuntimeError, match="Cannot sign") as error:
            release_app.jwt("Iv1.fixture", "NOT A KEY")
    assert "NOT A KEY" not in str(error.value)
    assert all(not path.exists() for path in paths)


def test_unsupported_permission_cannot_mint_a_token(app):
    with pytest.raises(RuntimeError, match="Unsupported"):
        with release_app.installation_token("administration"):
            pytest.fail("must not yield")
    assert not app["calls"]
