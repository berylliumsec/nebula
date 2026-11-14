"""Resumable supervisor deployment. Code rollback never restores application data."""

from __future__ import annotations

import hashlib
from typing import Callable
from uuid import uuid4

# Core's definite refusal for a stale expected revision: nothing changed, and a
# request that depends only on the current state may read it and ask again.
_DEPLOYMENT_CHANGED = "DeploymentChanged"
_CHANGED_RETRIES = 5
_ATTEMPT_HISTORY = 8
# A fresh request for these actions can only confirm or create the single
# effect the step asked for, never add another: staging an immutable release;
# migrating to the release's pinned head (Core migrates under its bootstrap
# advisory lock and reports an already-current head); preparing the release's
# one candidate API incarnation (Core returns a ready one and refuses while an
# unready one exists); promoting or targeting a version the host may already
# have; and retiring a runtime that may already be draining. Preparing an
# execution worker and requesting a handoff are not here: each new request
# would start another worker or transfer.
_CONVERGENT = frozenset(
    {
        "stage",
        "migrate",
        "prepare_api",
        "promote_api",
        "target_execution",
        "rollback_code",
        "retire",
    }
)


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
        # Steps sent during this run. A refusal or an unknown outcome is
        # reported once; the operator's next run moves it forward.
        self._sent: set[str] = set()

    def _journal(self, action: str, version: str) -> dict:
        journal = self.record.get("runtime_deployment")
        if journal and journal.get("state") != "complete":
            if (
                action == "rollback"
                and journal.get("action") == "promote"
                and journal.get("previous") == version
            ):
                return self._supersede(journal, version)
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

    def _supersede(self, promotion: dict, version: str) -> dict:
        """Start a code rollback that replaces an unfinished promotion.

        The promotion's own steps stay with the rollback as its audit trail.
        Every project, API, and host state the promotion reached is read again
        from Core by the rollback, which moves each part back to ``version``.
        """
        journal = {
            "id": uuid4().hex,
            "action": "rollback",
            "version": version,
            "previous": self.record.get("active"),
            "state": "running",
            "steps": {},
            "superseded": promotion,
        }
        self.record["runtime_deployment"] = journal
        self.save(self.record)
        return journal

    def _attempt(
        self,
        journal: dict,
        step: str,
        action: str,
        parameters: dict,
        previous: dict | None = None,
    ) -> dict:
        operation_id = journal["id"] + ":" + step.replace("/", ":")
        entry = {
            "operation_id": operation_id,
            "action": action,
            "parameters": parameters,
        }
        if previous is not None:
            # A new operation ID: Core never dispatches a saved ID twice.
            attempt = int(previous.get("attempt", 1)) + 1
            history = list(previous.get("attempts", []))
            history.append(
                {
                    key: previous[key]
                    for key in ("operation_id", "parameters", "receipt", "settled")
                    if key in previous
                }
            )
            entry.update(
                operation_id=f"{operation_id}:attempt-{attempt}",
                attempt=attempt,
                attempts=history[-_ATTEMPT_HISTORY:],
            )
        journal["steps"][step] = entry
        self.save(self.record)
        return entry

    def _operation(
        self,
        journal: dict,
        step: str,
        action: str,
        parameters: dict | Callable[[], dict],
        *,
        effect: Callable[[dict], dict | None] | None = None,
        absent: Callable[[dict], bool] | None = None,
    ) -> dict:
        """Run one journaled step and return Core's authoritative result.

        ``parameters`` is a dict, or a callable that reads them afresh (a
        revision, for example) for each new attempt. An attempt's parameters
        never change once journaled, and its saved receipt makes a lost reply
        safe to send again. A step whose receipt is not ``succeeded`` moves on
        only with Core's evidence, on the operator's next run:

        * ``failed``: Core proved that the request had no effect, so it is
          asked again under a new operation ID. ``DeploymentChanged`` is asked
          again at once with a freshly read revision.
        * ``uncertain``: ``effect`` settles the step when fresh deployment
          status shows its effect; ``absent`` proves from that status that it
          had none, so it is asked again; otherwise only an action whose fresh
          request converges on the same single effect is asked again.
          Anything else stays journaled and reported, never sent again.
        """
        read = parameters if callable(parameters) else (lambda: parameters)
        entry = journal["steps"].get(step)
        if entry is None:
            entry = self._attempt(journal, step, action, read())
        elif isinstance(entry.get("settled"), dict):
            return entry["settled"]["result"]
        elif step not in self._sent:
            receipt = entry.get("receipt")
            outcome = receipt.get("state") if isinstance(receipt, dict) else None
            if outcome == "failed":
                entry = self._attempt(journal, step, action, read(), entry)
            elif outcome == "uncertain":
                status = self._status()
                evidence = effect(status) if effect is not None else None
                if evidence is not None:
                    entry["settled"] = {
                        "by": "deployment_status",
                        "evidence": evidence,
                        "result": evidence,
                        "revision": status.get("revision"),
                    }
                    self.save(self.record)
                    return evidence
                if (absent is not None and absent(status)) or action in _CONVERGENT:
                    entry = self._attempt(journal, step, action, read(), entry)
                else:
                    raise RuntimeError(
                        f"Deployment operation {entry['operation_id']} ({action}) "
                        "is uncertain, and deployment status neither shows its "
                        "effect nor proves it absent; it is never sent again"
                    )
        for retry in range(_CHANGED_RETRIES + 1):
            receipt = self._send(entry)
            changed = (
                receipt.get("state") == "failed"
                and (receipt.get("result") or {}).get("error_type")
                == _DEPLOYMENT_CHANGED
            )
            if not changed or not callable(parameters) or retry == _CHANGED_RETRIES:
                break
            entry = self._attempt(journal, step, action, read(), entry)
        self._sent.add(step)
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

    def _send(self, entry: dict) -> dict:
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
        return receipt

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

    @staticmethod
    def _retired(runtime_id: str) -> Callable[[dict], dict | None]:
        """Settle a retirement when Core lists no live incarnation for it."""

        def effect(status: dict) -> dict | None:
            processes = status.get("processes")
            if not isinstance(processes, list) or any(
                isinstance(row, dict) and row.get("runtime_id") == runtime_id
                for row in processes
            ):
                return None
            return {"runtime_id": runtime_id, "state": "retired"}

        return effect

    @staticmethod
    def _no_unowned_worker(status: dict, version: str, journaled: set) -> bool:
        """Prove that an unconfirmed worker start left no process behind.

        Core lists every unretired process. Only when no unowned execution
        process of ``version`` is reserved, starting, ready, unreachable or
        still aborting, apart from targets this deployment already holds, can
        the start be asked again without a second worker.
        """
        processes = status.get("processes")
        if not isinstance(processes, list):
            return False
        owners = {
            row.get("runtime_id")
            for row in status.get("projects", [])
            if isinstance(row, dict)
        }
        return not any(
            isinstance(row, dict)
            and row.get("role") == "execution"
            and row.get("version") == version
            and row.get("state") != "draining"
            and row.get("runtime_id") not in owners
            and row.get("runtime_id") not in journaled
            for row in processes
        )

    @staticmethod
    def _handoff_absent(
        status: dict, project_id: str, source_id: str, fence: int, version: str
    ) -> bool:
        """Prove that an unconfirmed handoff request never took effect.

        The source still owns the project under the exact fencing token the
        deployment journaled, and no transfer toward ``version`` is recorded.
        """
        project = next(
            (
                row
                for row in status.get("projects", [])
                if isinstance(row, dict) and row.get("project_id") == project_id
            ),
            None,
        )
        return (
            project is not None
            and project.get("runtime_id") == source_id
            and project.get("fencing_token") == fence
            and (
                project.get("phase") is None
                or project.get("phase") == "verified"
                and project.get("target_version") != version
            )
        )

    @staticmethod
    def _superseded_leftovers(superseded, status: dict, own: set) -> list[str]:
        """Runtimes a replaced promotion journaled that no longer have work.

        A source qualifies once its project's ownership moved past the fence
        the promotion journaled; a prepared target once it owns no project.
        Runtimes the promotion already retired, runtimes owning a project,
        and this rollback's own sources and targets are left alone.
        """
        if not isinstance(superseded, dict):
            return []
        rows = {
            row.get("project_id"): row
            for row in status.get("projects", [])
            if isinstance(row, dict)
        }
        owners = {row.get("runtime_id") for row in rows.values()}
        fences = superseded.get("source_fencing_by_project", {})
        targets = superseded.get("target_runtime_by_project", {})
        retired = {
            (entry.get("parameters") or {}).get("runtime_id")
            for key, entry in superseded.get("steps", {}).items()
            if key.startswith(("retire/", "retire-unused-target/"))
            and (entry.get("receipt") or {}).get("state") == "succeeded"
        }
        listed = status.get("processes")
        live = (
            {row.get("runtime_id") for row in listed if isinstance(row, dict)}
            if isinstance(listed, list)
            else None
        )
        candidates = []
        for project_id, source in superseded.get(
            "source_runtime_by_project", {}
        ).items():
            row = rows.get(project_id)
            if row is not None and row.get("phase") not in (None, "verified"):
                continue
            if row is None or row.get("fencing_token", 0) > fences.get(project_id, 0):
                candidates.append(source)
            candidates.append(targets.get(project_id))
        return [
            runtime_id
            for runtime_id in dict.fromkeys(candidates)
            if isinstance(runtime_id, str)
            and runtime_id not in retired
            and runtime_id not in owners
            and runtime_id not in own
            and (live is None or runtime_id in live)
        ]

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
                lambda: {
                    "version": version,
                    "expected_revision": self._status()["revision"],
                },
            )
        if self.activate_host is not None:
            activation_entry = journal.get("host_activation")
            if (
                not isinstance(activation_entry, dict)
                or activation_entry.get("version") != version
            ):
                prior_activation = self.record.get("host_activation")
                operation_id = (
                    prior_activation.get("operation_id")
                    if isinstance(prior_activation, dict)
                    and prior_activation.get("version") == version
                    and isinstance(prior_activation.get("operation_id"), str)
                    else journal["id"] + ":host-activation"
                )
                activation_entry = {
                    "version": version,
                    "operation_id": operation_id,
                    "state": "requested",
                }
                journal["host_activation"] = activation_entry
                self.save(self.record)
            elif activation_entry.get("state") not in {"requested", "complete"}:
                raise RuntimeError("Host activation journal needs reconciliation")
            activation = self.activate_host(version)
            if (
                not isinstance(activation, dict)
                or activation.get("status") != "host_activated"
                or activation.get("version") != version
                or activation.get("health_commit") != version
                or activation.get("operation_id")
                != activation_entry.get("operation_id")
            ):
                raise RuntimeError(
                    "Host activation omitted its version-pinned verified readback"
                )
            activation_entry["state"] = "complete"
            activation_entry["result"] = activation
            self.save(self.record)
        api = self._operation(journal, "api", "prepare_api", {"version": version})
        self._operation(
            journal,
            "promote-api",
            "promote_api",
            lambda: {
                "runtime_id": api["runtime_id"],
                "expected_revision": self._status()["revision"],
            },
            effect=lambda status: (
                {"api_version": version}
                if status.get("api_version") == version
                else None
            ),
        )
        if self._status().get("api_version") != version:
            raise RuntimeError(
                "The supervisor has not confirmed the selected API version"
            )
        self.record["active"] = version
        self.save(self.record)

    @staticmethod
    def _targeted(version: str) -> Callable[[dict], dict | None]:
        return lambda status: (
            {"target_execution_version": version}
            if status.get("target_execution_version") == version
            else None
        )

    def promote(self, version: str) -> dict:
        journal = self._journal("promote", version)
        self._stage_and_promote_api(journal, version)
        self._operation(
            journal,
            "target",
            "target_execution",
            {"version": version},
            effect=self._targeted(version),
        )
        return self._move_projects(journal, version)

    def rollback(self, version: str) -> dict:
        """Move host, API, target, and projects back to ``version``.

        It resumes its own journal, or replaces a promotion that has not
        finished and was started from ``version``.
        """
        journal = self._journal("rollback", version)
        self._stage_and_promote_api(journal, version, migrate=False)
        rolled_back = self._targeted(version)
        self._operation(
            journal,
            "target",
            "rollback_code",
            lambda: {
                "version": version,
                "expected_revision": self._status()["revision"],
            },
            effect=lambda status: (
                rolled_back(status) if status.get("api_version") == version else None
            ),
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
                journal,
                "retire/" + key,
                "retire",
                {"runtime_id": runtime_id},
                effect=self._retired(runtime_id),
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
                        absent=lambda value: self._no_unowned_worker(
                            value, version, set(target_by_project.values())
                        ),
                    )
                    target_id = worker.get("runtime_id")
                    if not target_id:
                        raise RuntimeError("Prepared execution omitted its runtime ID")
                    target_by_project[project_id] = target_id
                    self.save(self.record)
                fence = source_fence_by_project[project_id]
                self._operation(
                    journal,
                    "handoff/" + project_key,
                    "handoff",
                    {"project_id": project_id, "target_runtime_id": target_id},
                    effect=lambda value: (
                        {"project_id": project_id, "runtime_id": target_id}
                        if verified_target(
                            next(
                                (
                                    row
                                    for row in project_rows(value)
                                    if row.get("project_id") == project_id
                                ),
                                None,
                            ),
                            target_id,
                            fence,
                        )
                        else None
                    ),
                    absent=lambda value: self._handoff_absent(
                        value, project_id, source_id, fence, version
                    ),
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
                                effect=self._retired(target_id),
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
                        # The refused handoff is asked again on a later run,
                        # with a newly prepared target in place of the retired one.
                        target_by_project.pop(project_id, None)
                        worker_step = journal["steps"].get("worker/" + project_key)
                        if isinstance(worker_step, dict):
                            self._attempt(
                                journal,
                                "worker/" + project_key,
                                "prepare_execution",
                                {"version": version},
                                worker_step,
                            )
                        self.save(self.record)
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

        # A rollback that replaced an unfinished promotion retires what that
        # promotion left without work: the sources of transfers that moved
        # past them, and targets that never received their project.
        superseded = journal.get("superseded")
        status = self._status()
        for runtime_id in self._superseded_leftovers(
            superseded,
            status,
            {*source_by_project.values(), *target_by_project.values()},
        ):
            try:
                _, status = retire_ownerless(runtime_id, status)
            except (RuntimeError, OSError) as error:
                return waiting(
                    status,
                    [{"runtime_id": runtime_id, "detail": str(error)[:1000]}],
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
