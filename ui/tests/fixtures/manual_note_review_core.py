"""Saved inert execution and explanatory harness; never run a command or model."""

import hashlib
import json
from types import SimpleNamespace
from typing import Any

from nebula.v3 import cli
from nebula.v3.domain import (
    Engagement,
    ExecutionOrigin,
    ExecutionRuntimeSnapshot,
    HarnessProfile,
    OperatorExecution,
    ProviderPrivacy,
    utc_now,
)
from nebula.v3.harnesses import HarnessCapabilities, HarnessHealth

_create_app = cli.create_app


class InertNoteHarness:
    async def analyze_structured(self, **request):
        if request["model"] != "fixture-note-model":
            raise RuntimeError("Unexpected note model")
        return SimpleNamespace(
            id="inert-note-response",
            response=json.dumps(
                {
                    "title": "Reviewed synthetic explanation",
                    "summary": "Synthetic output is ready for operator review.",
                    "observations": ["No process or external service was used."],
                    "potential_findings": [],
                    "evidence_ids": [],
                }
            ),
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


class InertHarnessAdapter:
    async def probe(self, profile, credentials):
        return HarnessHealth(
            profile_id=profile.id,
            kind=profile.kind,
            healthy=True,
            authentication_state="verified",
            harness_version="inert-note-fixture",
            capabilities=HarnessCapabilities(models=["fixture-note-model"]),
            detail="Synthetic harness catalog; no process was opened",
        )


def create_fixture_app(*args: Any, **kwargs: Any):
    app = _create_app(*args, **kwargs)
    store = app.state.store
    artifacts = app.state.artifact_store
    if artifacts is None or app.state.execution_ai_service is None:
        raise RuntimeError(
            "Manual-note fixture requires Core artifacts and draft service"
        )
    project = store.create(
        Engagement(name="Manual note review fixture", status="active")
    )
    source_bytes = b"# Saved synthetic explanation input; never executed.\n"
    source = store.create(
        artifacts.put_bytes(
            source_bytes,
            engagement_id=project.id,
            filename="inert.txt",
            media_type="text/plain",
        )
    )
    redacted_outputs = {}
    for stream, content in (
        ("stdout", b"Synthetic explanation result.\n"),
        ("stderr", b""),
    ):
        original = store.create(
            artifacts.put_bytes(
                content,
                engagement_id=project.id,
                filename=f"inert-{stream}.txt",
                media_type="text/plain",
            )
        )
        redacted = artifacts.put_bytes(
            content,
            engagement_id=project.id,
            filename=f"inert-{stream}-redacted.txt",
            media_type="text/plain",
            parent_artifact_id=original.id,
        )
        redacted_outputs[stream] = store.create(
            redacted.model_copy(update={"redacted": True})
        )
    store.create(
        OperatorExecution(
            engagement_id=project.id,
            operator_id="synthetic-operator",
            origin=ExecutionOrigin(
                kind="selection",
                source_kind="fixture",
                source_id="inert-note",
                source_label="Saved inert explanation input",
                source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            ),
            language="python",
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            source_artifact_id=source.id,
            source_preview=source_bytes.decode(),
            runtime=ExecutionRuntimeSnapshot(
                language="python",
                interpreter="/fixture/not-a-program",
                runtime_digest="sha256:" + "a" * 64,
                image="registry.invalid/inert@sha256:" + "a" * 64,
                runner_profile_id="inert",
                runner_profile_revision=1,
                runner_runtime="podman",
                runner_isolation="rootless",
                runner_executable="/fixture/not-a-program",
                runner_platform="linux/amd64",
            ),
            preview_fingerprint="a" * 64,
            request_fingerprint="b" * 64,
            client_idempotency_key="inert-note-review",
            status="completed",
            exit_code=0,
            completed_at=utc_now(),
            redacted_stdout_artifact_id=redacted_outputs["stdout"].id,
            redacted_stderr_artifact_id=redacted_outputs["stderr"].id,
        )
    )
    store.create(
        HarnessProfile(
            name="Inert local note harness",
            kind="codex_app_server",
            executable="/fixture/codex",
            default_model="fixture-note-model",
            enabled=True,
            privacy=ProviderPrivacy(local_only=True),
        )
    )
    app.state.execution_ai_service.harness_runtime = InertNoteHarness()
    app.state.harness_runtime_service.adapter_factory = lambda kind: (
        InertHarnessAdapter()
    )
    return app


if __name__ == "__main__":
    cli.create_app = create_fixture_app
    cli.main()
