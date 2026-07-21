"""Transactional local operator-profile lifecycle and attribution invariants."""

from __future__ import annotations

from .diagnostics import record_caught_exception

from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import and_, delete, exists, insert, or_, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from .database import EntityRow, RunEventRow
from .domain import OperatorProfile, utc_now
from .storage import ConflictError, NebulaStore, NotFoundError


class OperatorProfileInvariantError(ConflictError):
    """Persisted profiles do not have exactly one active local operator."""


def _dump(profile: OperatorProfile) -> dict[str, Any]:
    return profile.model_dump(mode="json")


def _from_row(row: Any) -> OperatorProfile:
    return OperatorProfile.model_validate(row["payload"])


class OperatorProfileService:
    """Maintain exactly one active profile with atomic local transitions."""

    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    def list_profiles(self) -> list[OperatorProfile]:
        profiles: list[OperatorProfile] = []
        offset = 0
        while True:
            batch = self.store.list_entities(
                OperatorProfile,
                offset=offset,
                limit=1_000,
            )
            profiles.extend(batch)
            if len(batch) < 1_000:
                break
            offset += len(batch)
        self._assert_coherent(profiles)
        return sorted(
            profiles,
            key=lambda profile: (
                not profile.active,
                profile.display_name.casefold(),
                profile.created_at,
                profile.id,
            ),
        )

    def active_profile(self) -> OperatorProfile:
        profiles = self.list_profiles()
        if not profiles:
            raise NotFoundError("no operator profile has been configured")
        return profiles[0]

    def active_profile_or_none(self) -> OperatorProfile | None:
        profiles = self.list_profiles()
        return profiles[0] if profiles else None

    def get_profile(self, profile_id: str) -> OperatorProfile:
        return self.store.get(OperatorProfile, profile_id)

    def create_profile(
        self,
        *,
        display_name: str,
        email: str | None = None,
        role: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OperatorProfile:
        candidate = OperatorProfile(
            display_name=display_name,
            email=email,
            role=role,
            metadata=metadata or {},
        )
        try:
            with self._write_transaction() as connection:
                profiles = self._locked_profiles(connection)
                self._assert_coherent(profiles)
                if not profiles:
                    candidate = candidate.model_copy(
                        update={"active": True, "activated_at": utc_now()}
                    )
                connection.execute(
                    insert(EntityRow).values(
                        id=candidate.id,
                        kind=candidate.entity_kind,
                        engagement_id=None,
                        revision=candidate.revision,
                        payload=_dump(candidate),
                        created_at=candidate.created_at,
                        updated_at=candidate.updated_at,
                    )
                )
        except IntegrityError as exc:
            record_caught_exception(
                "projects",
                "projects.operators.caught_failure_001",
                "A handled projects operation raised an exception.",
                exc,
                stage="operators",
            )
            raise ConflictError(f"entity already exists: {candidate.id}") from exc
        return candidate

    def update_profile(
        self,
        profile_id: str,
        changes: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> OperatorProfile:
        allowed = {"display_name", "email", "role", "metadata"}
        unsupported = set(changes) - allowed
        if unsupported:
            raise ValueError(
                f"operator profile fields are not writable: {sorted(unsupported)}"
            )
        with self._write_transaction() as connection:
            profiles = self._locked_profiles(connection)
            self._assert_coherent(profiles)
            current = self._find(profiles, profile_id)
            if expected_revision is not None and current.revision != expected_revision:
                raise ConflictError(
                    f"revision conflict: expected {expected_revision}, "
                    f"found {current.revision}"
                )
            if not changes:
                return current
            return self._update_row(connection, current, changes)

    def activate_profile(
        self,
        profile_id: str,
        *,
        expected_revision: int | None = None,
    ) -> OperatorProfile:
        with self._write_transaction() as connection:
            profiles = self._locked_profiles(connection)
            target = self._find(profiles, profile_id)
            if expected_revision is not None and target.revision != expected_revision:
                raise ConflictError(
                    f"revision conflict: expected {expected_revision}, "
                    f"found {target.revision}"
                )
            active = [profile for profile in profiles if profile.active]
            if len(active) == 1 and active[0].id == target.id:
                return target

            activated_at = utc_now()
            result = target
            for profile in profiles:
                should_be_active = profile.id == target.id
                if profile.active == should_be_active:
                    continue
                changes: dict[str, Any] = {"active": should_be_active}
                if should_be_active:
                    changes["activated_at"] = activated_at
                updated = self._update_row(connection, profile, changes)
                if updated.id == target.id:
                    result = updated
            # A corrupt all-active set can leave the target unchanged while
            # deactivating others; re-read the returned target in that case.
            if result.id == target.id and result.active:
                return result
            return self._find(self._locked_profiles(connection), target.id)

    def delete_profile(
        self,
        profile_id: str,
        *,
        expected_revision: int | None = None,
    ) -> None:
        with self._write_transaction() as connection:
            profiles = self._locked_profiles(connection)
            self._assert_coherent(profiles)
            target = self._find(profiles, profile_id)
            if expected_revision is not None and target.revision != expected_revision:
                raise ConflictError(
                    f"revision conflict: expected {expected_revision}, "
                    f"found {target.revision}"
                )
            if len(profiles) == 1:
                raise OperatorProfileInvariantError(
                    "the last operator profile cannot be deleted"
                )
            if target.active:
                raise OperatorProfileInvariantError(
                    "activate another operator before deleting the active profile"
                )
            if self._has_attribution_references(connection, target.id):
                raise OperatorProfileInvariantError(
                    "operator profile cannot be deleted while durable attribution "
                    "references it"
                )
            result = connection.execute(
                delete(EntityRow).where(
                    EntityRow.id == target.id,
                    EntityRow.kind == OperatorProfile.entity_kind,
                    EntityRow.revision == target.revision,
                )
            )
            if result.rowcount != 1:
                raise ConflictError(
                    f"operator profile {target.id} changed during deletion"
                )

    @contextmanager
    def _write_transaction(self) -> Iterator[Connection]:
        connection = self.store.database.engine.connect()
        try:
            if self.store.database.engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.begin()
                # PostgreSQL row locks cannot serialize two inserts when the
                # profile set is empty. This short, rare configuration write
                # therefore takes a table lock before checking the invariant.
                connection.exec_driver_sql(
                    "LOCK TABLE entities IN SHARE ROW EXCLUSIVE MODE"
                )
            yield connection
            connection.commit()
        except Exception as caught_error:
            record_caught_exception(
                "projects",
                "projects.operators.caught_failure_002",
                "A handled projects operation raised an exception.",
                caught_error,
                stage="operators",
            )
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _locked_profiles(connection: Connection) -> list[OperatorProfile]:
        rows = (
            connection.execute(
                select(EntityRow)
                .where(EntityRow.kind == OperatorProfile.entity_kind)
                .order_by(EntityRow.created_at, EntityRow.id)
                .with_for_update()
            )
            .mappings()
            .all()
        )
        return [_from_row(row) for row in rows]

    @staticmethod
    def _find(profiles: list[OperatorProfile], profile_id: str) -> OperatorProfile:
        for profile in profiles:
            if profile.id == profile_id:
                return profile
        raise NotFoundError(f"operator profile not found: {profile_id}")

    @staticmethod
    def _assert_coherent(profiles: list[OperatorProfile]) -> None:
        if profiles and sum(profile.active for profile in profiles) != 1:
            raise OperatorProfileInvariantError(
                "operator profiles require exactly one active local operator"
            )

    @staticmethod
    def _has_attribution_references(connection: Connection, profile_id: str) -> bool:
        entity_reference = or_(
            and_(
                EntityRow.kind == "evidence",
                EntityRow.payload["captured_by"].as_string() == profile_id,
            ),
            and_(
                EntityRow.kind == "findings",
                EntityRow.payload["verifier_id"].as_string() == profile_id,
            ),
            and_(
                EntityRow.kind == "correlations",
                EntityRow.payload["analyst_id"].as_string() == profile_id,
            ),
            and_(
                EntityRow.kind == "reports",
                EntityRow.payload["signed_off_by"].as_string() == profile_id,
            ),
            and_(
                EntityRow.kind == "engagements",
                EntityRow.payload["owner_id"].as_string() == profile_id,
            ),
            and_(
                EntityRow.kind == "approvals",
                or_(
                    EntityRow.payload["decided_by"].as_string() == profile_id,
                    EntityRow.payload["requested_by"].as_string() == profile_id,
                ),
            ),
        )
        entity_exists = connection.scalar(select(exists().where(entity_reference)))
        event_exists = connection.scalar(
            select(exists().where(RunEventRow.actor_id == profile_id))
        )
        return bool(entity_exists or event_exists)

    @staticmethod
    def _update_row(
        connection: Connection,
        current: OperatorProfile,
        changes: dict[str, Any],
    ) -> OperatorProfile:
        payload = current.model_dump(mode="python")
        payload.update(changes)
        payload["id"] = current.id
        payload["created_at"] = current.created_at
        payload["updated_at"] = utc_now()
        payload["revision"] = current.revision + 1
        updated_profile = OperatorProfile.model_validate(payload)
        result = connection.execute(
            update(EntityRow)
            .where(
                EntityRow.id == current.id,
                EntityRow.kind == OperatorProfile.entity_kind,
                EntityRow.revision == current.revision,
            )
            .values(
                payload=_dump(updated_profile),
                revision=updated_profile.revision,
                updated_at=updated_profile.updated_at,
            )
        )
        if result.rowcount != 1:
            raise ConflictError(f"operator profile {current.id} changed during update")
        return updated_profile


__all__ = ["OperatorProfileInvariantError", "OperatorProfileService"]
