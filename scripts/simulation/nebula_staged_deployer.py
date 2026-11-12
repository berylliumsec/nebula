"""Resumable supervisor deployment. Code rollback never restores application data."""

from __future__ import annotations

import hashlib
from typing import Callable
from uuid import uuid4


class StagedDeployer:
    def __init__(
        self,
        record: dict,
        *,
        get: Callable,
        post: Callable,
        save: Callable,
        verify: Callable,
    ):
        self.record, self.get, self.post, self.save, self.verify = (
            record,
            get,
            post,
            save,
            verify,
        )

    def _journal(self, action: str, version: str) -> dict:
        journal = self.record.get("runtime_deployment")
        if journal and journal.get("state") != "complete":
            if (journal.get("action"), journal.get("version")) != (action, version):
                raise RuntimeError(
                    "Another deployment needs reconciliation before starting this request"
                )
            return journal
        journal = {
            "id": uuid4().hex,
            "action": action,
            "version": version,
            "previous": self.record.get("active"),
            "state": "running",
            "steps": {},
        }
        self.record["runtime_deployment"] = journal
        self.save(self.record)
        return journal

    def _operation(
        self, journal: dict, step: str, action: str, parameters: dict
    ) -> dict:
        entry = journal["steps"].get(step)
        if entry is None:
            entry = {
                "operation_id": journal["id"] + ":" + step,
                "action": action,
                "parameters": parameters,
            }
            journal["steps"][step] = entry
            self.save(self.record)
        # Parameters, including revision, are immutable once journaled. The
        # server receipt makes a lost POST response safe to submit again.
        receipt = self.post(
            "deployment/operations",
            {key: entry[key] for key in ("operation_id", "action", "parameters")},
        )
        if (
            not isinstance(receipt, dict)
            or receipt.get("operation_id") != entry["operation_id"]
        ):
            raise RuntimeError("Deployment operation returned an unrelated receipt")
        entry["receipt"] = receipt
        self.save(self.record)
        if receipt.get("state") != "succeeded":
            detail = (receipt.get("result") or {}).get(
                "detail", "Read fresh deployment status before continuing"
            )
            raise RuntimeError(
                "Deployment operation " + str(receipt.get("state")) + ": " + detail
            )
        result = receipt.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Deployment operation omitted its authoritative result")
        return result

    def _status(self) -> dict:
        status = self.get("deployment/status")
        if (
            not isinstance(status, dict)
            or status.get("protocol") != "nebula.runtime/v1"
        ):
            raise RuntimeError(
                "The live service does not provide the supervisor deployment protocol"
            )
        return status

    def _stage_and_promote_api(
        self, journal: dict, version: str, *, migrate: bool = True
    ) -> None:
        release, digest = self.verify(version)
        self._operation(
            journal,
            "stage",
            "stage",
            {"source": str(release), "manifest_sha256": digest},
        )
        if migrate:
            self._operation(
                journal,
                "migrate",
                "migrate",
                {"version": version, "expected_revision": self._status()["revision"]},
            )
        api = self._operation(journal, "api", "prepare_api", {"version": version})
        self._operation(
            journal,
            "promote-api",
            "promote_api",
            {
                "runtime_id": api["runtime_id"],
                "expected_revision": self._status()["revision"],
            },
        )
        if self._status().get("api_version") != version:
            raise RuntimeError(
                "The supervisor has not confirmed the selected API version"
            )
        self.record["active"] = version
        self.save(self.record)

    def promote(self, version: str) -> dict:
        journal = self._journal("promote", version)
        self._stage_and_promote_api(journal, version)
        self._operation(journal, "target", "target_execution", {"version": version})
        return self._move_projects(journal, version)

    def rollback(self, version: str) -> dict:
        journal = self._journal("rollback", version)
        self._stage_and_promote_api(journal, version, migrate=False)
        self._operation(
            journal,
            "target",
            "rollback_code",
            {
                "version": version,
                "expected_revision": self._status()["revision"],
            },
        )
        return self._move_projects(journal, version)

    def _move_projects(self, journal: dict, version: str) -> dict:
        status = self._status()
        resident = status.get("resident_versions", [])
        if version not in resident and len(resident) >= 2:
            return {
                "status": "api_promoted_execution_staged",
                "commit": version,
                "deployment": status,
            }
        projects = status.get("projects", [])
        if len(projects) > 10_000:
            raise RuntimeError(
                "Deployment project inventory exceeds its reviewed bound"
            )
        for project in projects:
            if (
                not project.get("runtime_id")
                or project.get("runtime_version") == version
            ):
                continue
            project_id = project["project_id"]
            sources = journal.setdefault("source_runtime_ids", [])
            if project["runtime_id"] not in sources:
                sources.append(project["runtime_id"])
                self.save(self.record)
            project_key = hashlib.sha256(project_id.encode()).hexdigest()
            worker = self._operation(
                journal,
                "worker/" + project_key,
                "prepare_execution",
                {"version": version},
            )
            self._operation(
                journal,
                "handoff/" + project_key,
                "handoff",
                {
                    "project_id": project_id,
                    "target_runtime_id": worker["runtime_id"],
                },
            )
        status = self._status()
        pending, pinned = [], []
        for project in status.get("projects", []):
            if (
                not project.get("runtime_id")
                or project.get("runtime_version") == version
            ):
                continue
            if project.get("blockers") and not project.get("admission_held", True):
                pinned.append(project)
            else:
                pending.append(project)
        self.record["active"] = version
        if pending:
            self.save(self.record)
            return {
                "status": "waiting_for_handoffs",
                "commit": version,
                "pending": pending,
                "pinned": pinned,
            }
        owners = {project.get("runtime_id") for project in status.get("projects", [])}
        cleanup = []
        for runtime_id in journal.get("source_runtime_ids", []):
            if runtime_id in owners:
                continue
            key = hashlib.sha256(runtime_id.encode()).hexdigest()
            try:
                self._operation(
                    journal, "retire/" + key, "retire", {"runtime_id": runtime_id}
                )
            except (RuntimeError, OSError) as error:
                cleanup.append({"runtime_id": runtime_id, "detail": str(error)[:1000]})
        journal["state"] = "complete"
        journal["cleanup_blockers"] = cleanup
        self.record.update(
            previous=journal.get("previous"), pending=None, promoting=None
        )
        self.save(self.record)
        return {
            "status": "promoted_cleanup_incomplete"
            if cleanup
            else "promoted_with_pinned_projects"
            if pinned
            else "promoted",
            "commit": version,
            "pinned": pinned,
            "cleanup_blockers": cleanup,
        }
