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
        activate_host: Callable[[str], dict] | None = None,
    ):
        self.record, self.get, self.post, self.save, self.verify = (
            record,
            get,
            post,
            save,
            verify,
        )
        self.activate_host = activate_host

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
                "operation_id": journal["id"] + ":" + step.replace("/", ":"),
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
        if self.activate_host is not None:
            activation = self.activate_host(version)
            if (
                not isinstance(activation, dict)
                or activation.get("status") != "host_activated"
                or activation.get("version") != version
                or activation.get("health_commit") != version
                or not activation.get("operation_id")
            ):
                raise RuntimeError(
                    "Host activation omitted its version-pinned verified readback"
                )
            journal["host_activation"] = {
                "version": version,
                "result": activation,
            }
            self.save(self.record)
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
        sources = journal.setdefault("source_runtime_ids", [])
        source_by_project = journal.setdefault("source_runtime_by_project", {})
        source_fence_by_project = journal.setdefault("source_fencing_by_project", {})
        target_by_project = journal.setdefault("target_runtime_by_project", {})
        pinned = []

        def project_rows(value):
            return value.get("projects", [])

        def owner_ids(value):
            return {
                item.get("runtime_id")
                for item in project_rows(value)
                if item.get("runtime_id")
            }

        def verified_target(project, target_id, expected_fence):
            if (
                project is None
                or project.get("phase") != "verified"
                or project.get("target_version") != version
                or project.get("fencing_token", 0) <= expected_fence
            ):
                return False
            return (
                project.get("runtime_id") == target_id
                and project.get("runtime_version") == version
            ) or (
                project.get("runtime_id") is None
                and project.get("runtime_version") is None
            )

        def waiting(value, pending, pinned_rows):
            self.record["active"] = version
            journal["waiting_for_handoffs"] = {
                "pending": pending,
                "pinned": pinned_rows,
            }
            self.save(self.record)
            return {
                "status": "waiting_for_handoffs",
                "commit": version,
                "pending": pending,
                "pinned": pinned_rows,
                "deployment": value,
            }

        def retire_ownerless(runtime_id, value):
            if runtime_id in owner_ids(value):
                return None, value
            key = hashlib.sha256(runtime_id.encode()).hexdigest()
            result = self._operation(
                journal, "retire/" + key, "retire", {"runtime_id": runtime_id}
            )
            if result.get("runtime_id") != runtime_id:
                raise RuntimeError("Retirement receipt named a different runtime")
            fresh = self._status()
            if runtime_id in owner_ids(fresh):
                raise RuntimeError(
                    "The retired runtime still owns a project in fresh deployment status"
                )
            return result, fresh

        # Resolve transfers that completed before an interrupted deployer turn.
        # A verified owner transfer is the only point at which its exact old
        # runtime can be retired safely.
        for project_id, source_id in list(source_by_project.items()):
            status = self._status()
            project = next(
                (
                    row
                    for row in project_rows(status)
                    if row.get("project_id") == project_id
                ),
                None,
            )
            target_id = target_by_project.get(project_id)
            if project is None or target_id is None:
                continue
            expected_fence = source_fence_by_project.get(project_id)
            if expected_fence is None:
                return waiting(
                    status,
                    [
                        {
                            "project_id": project_id,
                            "detail": "Missing journaled source fence",
                        }
                    ],
                    pinned,
                )
            source_id = source_by_project[project_id]
            if project.get("runtime_id") == source_id:
                continue
            if project.get("runtime_id") not in (target_id, None):
                return waiting(
                    status,
                    [
                        {
                            **project,
                            "detail": "Project owner changed during handoff reconciliation",
                        }
                    ],
                    pinned,
                )
            if not verified_target(project, target_id, expected_fence):
                return waiting(
                    status,
                    [
                        {
                            **project,
                            "detail": "Target ownership is not yet durably verified; source retained",
                        }
                    ],
                    pinned,
                )
            if source_id not in owner_ids(status):
                try:
                    _, status = retire_ownerless(source_id, status)
                except (RuntimeError, OSError) as error:
                    return waiting(
                        status,
                        [{"runtime_id": source_id, "detail": str(error)[:1000]}],
                        pinned,
                    )
            else:
                return waiting(
                    status,
                    [
                        {
                            "project_id": project_id,
                            "runtime_id": source_id,
                            "detail": "Source still owns another project; retain it and reconcile before preparing another target",
                        }
                    ],
                    pinned,
                )

        # Move one project at a time. The next target is not prepared until the
        # current source has become ownerless, retired, and disappeared from
        # fresh ownership status.
        for original in projects:
            project_id = original.get("project_id")
            if not project_id:
                continue
            status = self._status()
            project = next(
                (
                    row
                    for row in project_rows(status)
                    if row.get("project_id") == project_id
                ),
                None,
            )
            if project is None or not project.get("runtime_id"):
                continue
            if project.get("runtime_version") == version:
                if project.get("phase") not in (None, "verified"):
                    return waiting(
                        status,
                        [
                            {
                                **project,
                                "detail": "Target handoff is still in progress; source retained",
                            }
                        ],
                        pinned,
                    )
                continue

            if project.get("blockers") and not project.get("admission_held", True):
                pinned.append(project)
                continue
            # Another in-flight handoff owns the project's transition. Do not
            # prepare a competing target or change its immutable operation.
            if project.get("phase") not in (None, "verified"):
                return waiting(
                    status,
                    [
                        {
                            **project,
                            "detail": "A different or unfinished handoff must be reconciled first",
                        }
                    ],
                    pinned,
                )

            source_id = project["runtime_id"]
            source_projects = [
                row
                for row in project_rows(status)
                if row.get("runtime_id") == source_id
            ]
            if len(source_projects) > 1:
                pinned.append(
                    {
                        **project,
                        "deployment_blocker": "Source runtime serves multiple projects and cannot be retired after one handoff",
                    }
                )
                continue
            saved_source = source_by_project.get(project_id)
            if saved_source and saved_source != source_id:
                return waiting(
                    status,
                    [
                        {
                            **project,
                            "detail": "Project owner changed since this deployment journaled its source",
                        }
                    ],
                    pinned,
                )
            if not saved_source:
                source_by_project[project_id] = source_id
                source_fence_by_project[project_id] = project.get("fencing_token", 0)
                if source_id not in sources:
                    sources.append(source_id)
                self.save(self.record)

            project_key = hashlib.sha256(project_id.encode()).hexdigest()
            target_id = target_by_project.get(project_id)
            try:
                if target_id is None:
                    worker = self._operation(
                        journal,
                        "worker/" + project_key,
                        "prepare_execution",
                        {"version": version},
                    )
                    target_id = worker.get("runtime_id")
                    if not target_id:
                        raise RuntimeError("Prepared execution omitted its runtime ID")
                    target_by_project[project_id] = target_id
                    self.save(self.record)
                self._operation(
                    journal,
                    "handoff/" + project_key,
                    "handoff",
                    {"project_id": project_id, "target_runtime_id": target_id},
                )
            except (RuntimeError, OSError) as error:
                step = journal["steps"].get("handoff/" + project_key, {})
                receipt = step.get("receipt", {})
                status = self._status()
                current = next(
                    (
                        row
                        for row in project_rows(status)
                        if row.get("project_id") == project_id
                    ),
                    None,
                )
                expected_fence = source_fence_by_project[project_id]
                if target_id and verified_target(current, target_id, expected_fence):
                    journal.setdefault("reconciled_handoffs", {})[project_id] = {
                        "operation_id": step.get("operation_id"),
                        "runtime_id": target_id,
                        "fencing_token": current["fencing_token"],
                        "source_runtime_id": source_id,
                    }
                    self.save(self.record)
                else:
                    if receipt.get("state") != "failed":
                        return waiting(
                            status,
                            [{"project_id": project_id, "detail": str(error)[:1000]}],
                            pinned,
                        )

                    # A definite failed operation leaves no transferred owner.
                    # A prepared target is retired only after fresh status proves
                    # no project owns it; otherwise preserve it and stop.
                    if target_id and target_id not in owner_ids(status):
                        target_key = hashlib.sha256(target_id.encode()).hexdigest()
                        try:
                            retired = self._operation(
                                journal,
                                "retire-unused-target/" + target_key,
                                "retire",
                                {"runtime_id": target_id},
                            )
                            if retired.get("runtime_id") != target_id:
                                raise RuntimeError(
                                    "Unused-target retirement named a different runtime"
                                )
                            status = self._status()
                            if target_id in owner_ids(status):
                                raise RuntimeError(
                                    "Unused target acquired a project before retirement was verified"
                                )
                        except (RuntimeError, OSError) as retire_error:
                            return waiting(
                                status,
                                [
                                    {
                                        "project_id": project_id,
                                        "runtime_id": target_id,
                                        "detail": str(retire_error)[:1000],
                                    }
                                ],
                                pinned,
                            )
                    pinned.append({**project, "deployment_blocker": str(error)[:1000]})
                    continue

            status = self._status()
            transferred = next(
                (
                    row
                    for row in project_rows(status)
                    if row.get("project_id") == project_id
                ),
                None,
            )
            expected_fence = source_fence_by_project[project_id]
            if not verified_target(transferred, target_id, expected_fence):
                return waiting(
                    status,
                    [
                        {
                            **(transferred or {"project_id": project_id}),
                            "detail": "Waiting for verified target ownership and a newer fencing token",
                        }
                    ],
                    pinned,
                )

            if source_id in owner_ids(status):
                return waiting(
                    status,
                    [
                        {
                            "project_id": project_id,
                            "runtime_id": source_id,
                            "detail": "Source runtime still owns a project; retain it before preparing another target",
                        }
                    ],
                    pinned,
                )
            try:
                _, status = retire_ownerless(source_id, status)
            except (RuntimeError, OSError) as error:
                return waiting(
                    status,
                    [
                        {
                            "project_id": project_id,
                            "runtime_id": source_id,
                            "detail": str(error)[:1000],
                        }
                    ],
                    pinned,
                )

        status = self._status()
        remaining = [
            project
            for project in project_rows(status)
            if project.get("runtime_id") and project.get("runtime_version") != version
        ]
        self.record["active"] = version
        if remaining or pinned:
            return waiting(status, remaining, pinned)

        journal["state"] = "complete"
        journal.pop("waiting_for_handoffs", None)
        self.record.update(
            previous=journal.get("previous"), pending=None, promoting=None
        )
        self.save(self.record)
        return {
            "status": "promoted",
            "commit": version,
        }
