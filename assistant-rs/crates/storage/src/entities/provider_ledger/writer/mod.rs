//! Transaction-local ledger primitives. Staged results never imply commit or dispatch.
mod canonical;
mod contracts;
use crate::entities::provider_ledger as schema;
pub use canonical::CanonicalJson;
pub use contracts::*;
use nebula_assistant_domain::provider_stream::{self, Draft, digest_parts};
use sqlx::{Connection, Row, Sqlite, SqliteConnection, Transaction};
use std::{fmt, sync::Arc};
use uuid::Uuid;

pub enum Error {
    Invalid,
    Capacity,
    Conflict,
    Corrupt,
    Lineage,
    CursorExpired,
    NotFound,
    Schema(schema::Error),
    Sql(sqlx::Error),
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Invalid => "invalid provider ledger command",
            Self::Capacity => "provider ledger capacity exceeded",
            Self::Conflict => "provider ledger identity conflict",
            Self::Corrupt => "provider ledger integrity check failed",
            Self::Lineage => "provider ledger lineage requires reconciliation",
            Self::CursorExpired => "provider ledger cursor prefix is unavailable",
            Self::NotFound => "provider ledger identity was not found",
            Self::Schema(_) => "provider ledger schema was refused",
            Self::Sql(_) => "provider ledger SQLite operation failed",
        })
    }
}
impl fmt::Debug for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(self, f)
    }
}
impl std::error::Error for Error {}
impl From<sqlx::Error> for Error {
    fn from(v: sqlx::Error) -> Self {
        Self::Sql(v)
    }
}
impl From<schema::Error> for Error {
    fn from(v: schema::Error) -> Self {
        Self::Schema(v)
    }
}
impl From<canonical::Error> for Error {
    fn from(v: canonical::Error) -> Self {
        match v {
            canonical::Error::Invalid => Self::Invalid,
            canonical::Error::Capacity => Self::Capacity,
        }
    }
}
impl From<provider_stream::Error> for Error {
    fn from(v: provider_stream::Error) -> Self {
        match v {
            provider_stream::Error::Capacity => Self::Capacity,
            _ => Self::Invalid,
        }
    }
}
pub type Result<T> = std::result::Result<T, Error>;

pub struct LedgerTx<'a, 'db> {
    tx: &'a mut Transaction<'db, Sqlite>,
    identity: schema::Identity,
    capacity: i64,
}
impl<'a, 'db> LedgerTx<'a, 'db> {
    pub async fn bind(
        tx: &'a mut Transaction<'db, Sqlite>,
        database_uuid: Uuid,
        limits: schema::SchemaLimits,
    ) -> Result<Self> {
        let capacity = i64::try_from(limits.capacity_bytes)
            .ok()
            .filter(|v| *v > 0)
            .ok_or(Error::Invalid)?;
        // A lowered admission quota must not revoke already-reserved settlement or replay.
        let schema::Status::Installed(identity) = schema::inspect(
            tx,
            schema::SchemaLimits {
                capacity_bytes: i64::MAX as u64,
            },
        )
        .await?
        else {
            return Err(Error::Lineage);
        };
        if identity.database_uuid != database_uuid {
            return Err(Error::Conflict);
        }
        Ok(Self {
            tx,
            identity,
            capacity,
        })
    }
    /// Initialize ledger metadata only; caller composes actual entity admission and receipt.
    /// Total reservation includes the stream row charge. This never makes work runnable.
    pub async fn open_stream(
        &mut self,
        scope: &StreamFence,
        total_reservation: i64,
        turn_epoch: i64,
        now: i64,
    ) -> Result<StreamState> {
        scope.validate()?;
        let cost = stream_charge(scope)?;
        if turn_epoch < 0 || total_reservation < cost + CONTROL_FLOOR {
            return Err(Error::Capacity);
        }
        let mut tx = self.tx.begin().await?;
        let updated=sqlx::query("UPDATE assistant_provider_budget SET used_bytes=used_bytes+?,reserved_bytes=reserved_bytes+? WHERE singleton=1 AND used_bytes<=?-reserved_bytes-?")
            .bind(cost).bind(total_reservation-cost).bind(self.capacity).bind(total_reservation).execute(&mut *tx).await?;
        if updated.rows_affected() != 1 {
            return Err(Error::Capacity);
        }
        sqlx::query("INSERT INTO assistant_provider_streams(turn_id,stream_id,session_id,project_id,state,event_bytes,reserved_bytes,receipt_head_sha256,observed_turn_epoch,created_at_us,updated_at_us) VALUES(?,?,?,?,'active',0,?,?,?, ?,?)")
            .bind(&scope.turn_id).bind(&scope.stream_id).bind(&scope.session_id).bind(&scope.project_id).bind(total_reservation-cost).bind(Digest::ZERO.hex()).bind(turn_epoch).bind(now).bind(now).execute(&mut *tx).await?;
        let state = load_stream(&mut tx, scope).await?;
        tx.commit().await?;
        Ok(state)
    }
    /// Only explicit new reservations may replenish quota; existing used bytes are retained.
    pub async fn reserve_more(&mut self, scope: &StreamFence, bytes: i64, now: i64) -> Result<()> {
        if bytes <= 0 {
            return Err(Error::Invalid);
        }
        let mut tx = self.tx.begin().await?;
        let state = load_stream(&mut tx, scope).await?;
        require_active(&state)?;
        if state.reserved_bytes.checked_add(bytes).is_none() {
            return Err(Error::Capacity);
        }
        let updated=sqlx::query("UPDATE assistant_provider_budget SET reserved_bytes=reserved_bytes+? WHERE singleton=1 AND used_bytes<=?-reserved_bytes-?")
            .bind(bytes).bind(self.capacity).bind(bytes).execute(&mut *tx).await?;
        if updated.rows_affected() != 1 {
            return Err(Error::Capacity);
        }
        sqlx::query("UPDATE assistant_provider_streams SET reserved_bytes=reserved_bytes+?,updated_at_us=? WHERE turn_id=?")
            .bind(bytes).bind(now).bind(&scope.turn_id).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }
    pub async fn add_attempt(
        &mut self,
        scope: &StreamFence,
        fence: &AttemptFence,
        predecessor: Option<&str>,
        now: i64,
    ) -> Result<()> {
        fence.validate()?;
        if let Some(id) = predecessor {
            valid_id(id)?;
        }
        let mut tx = self.tx.begin().await?;
        require_active(&load_stream(&mut tx, scope).await?)?;
        spend(
            &mut tx,
            scope,
            attempt_charge(scope, fence, predecessor)?,
            false,
            now,
        )
        .await?;
        sqlx::query("INSERT INTO assistant_provider_attempts(attempt_id,turn_id,ordinal,predecessor_attempt_id,owner_id,claim_id,request_sha256,route_sha256,phase) VALUES(?,?,?,?,?,?,?,?,'admitted')")
            .bind(&fence.attempt_id).bind(&scope.turn_id).bind(fence.ordinal).bind(predecessor).bind(&fence.owner_id).bind(&fence.claim_id).bind(fence.request_sha256.hex()).bind(fence.route_sha256.hex()).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }
    pub async fn stream(&mut self, scope: &StreamFence) -> Result<StreamState> {
        load_stream(self.tx, scope).await
    }
    pub async fn attempt_phase(
        &mut self,
        scope: &StreamFence,
        fence: &AttemptFence,
    ) -> Result<String> {
        load_stream(self.tx, scope).await?;
        check_attempt(self.tx, scope, Some(fence))
            .await?
            .ok_or(Error::NotFound)
    }
    /// Register derived physical locators before entity mutation, preserving existing epochs.
    /// Registration does not validate entity kind, payload hash or execution authority.
    pub async fn register_watches(
        &mut self,
        scope: &StreamFence,
        ids: &[String],
        now: i64,
    ) -> Result<Vec<WatchedEntity>> {
        if ids.is_empty() || ids.len() > MAX_WATCHES {
            return Err(Error::Invalid);
        }
        for id in ids {
            valid_id(id)?;
        }
        let mut tx = self.tx.begin().await?;
        require_active(&load_stream(&mut tx, scope).await?)?;
        let mut result = Vec::with_capacity(ids.len());
        for id in ids {
            let current: Option<i64> = sqlx::query_scalar("SELECT rowid FROM entities WHERE id=?")
                .bind(id)
                .fetch_optional(&mut *tx)
                .await?;
            let prior=sqlx::query("SELECT mutation_epoch,present,retained_rowid FROM assistant_provider_entity_epochs WHERE entity_id=?").bind(id).fetch_optional(&mut *tx).await?;
            let epoch = if let Some(prior) = prior {
                let old: Option<i64> = prior.try_get("retained_rowid")?;
                if old != current
                    || prior.try_get::<i64, _>("present")? != i64::from(current.is_some())
                {
                    return Err(Error::Lineage);
                }
                prior.try_get("mutation_epoch")?
            } else {
                spend(&mut tx, scope, watch_charge(id)?, false, now).await?;
                sqlx::query("INSERT INTO assistant_provider_entity_epochs(entity_id,present,retained_rowid) VALUES(?,?,?)").bind(id).bind(i64::from(current.is_some())).bind(current).execute(&mut *tx).await?;
                0
            };
            result.push(WatchedEntity {
                id: id.clone(),
                epoch,
                rowid: current,
            });
        }
        tx.commit().await?;
        Ok(result)
    }
    pub async fn append_receipt(
        &mut self,
        scope: &StreamFence,
        attempt: Option<&AttemptFence>,
        command: ReceiptCommand,
        now: i64,
    ) -> Result<StagedReceipt> {
        valid_id(&command.key)?;
        let (kind, payload, control) = match command.payload {
            ReceiptPayload::Ordinary { kind, payload } => (kind.as_str(), payload, false),
            ReceiptPayload::Control(value) => (
                value.kind(),
                value.canonical(scope, command.turn_epoch)?,
                true,
            ),
        };
        let command_hash = Digest(digest_parts(&[
            b"nebula.assistant-provider-command/v1",
            command.turn_epoch.to_string().as_bytes(),
            command.command.bytes(),
        ]));
        let mut tx = self.tx.begin().await?;
        let state = load_stream(&mut tx, scope).await?;
        check_attempt(&mut tx, scope, attempt).await?;
        let attempt_id = attempt.map(|v| v.attempt_id.as_str());
        if let Some(row) = load_receipt_key(&mut tx, &scope.turn_id, &command.key).await? {
            verify_receipt(&self.identity, scope, &row)?;
            if row.kind != kind
                || row.attempt_id.as_deref() != attempt_id
                || row.command_hash != command_hash
                || row.json.as_bytes() != payload.bytes()
            {
                return Err(Error::Conflict);
            }
            return Ok(StagedReceipt {
                sequence: row.sequence,
                receipt_hash: row.hash,
                duplicate: true,
            });
        }
        if state.state == "deleted" || (!control && state.state != "active") {
            return Err(Error::Lineage);
        }
        check_turn(&mut tx, scope, command.turn_epoch).await?;
        check_receipt_head(&mut tx, &self.identity, scope, &state).await?;
        if control {
            let count:i64=sqlx::query_scalar("SELECT count(*) FROM (SELECT 1 FROM assistant_provider_receipts WHERE turn_id=? AND kind IN ('answer_saved','completed','released','cancelled','failed','recovery_classified','lineage_invalidated','retention_boundary') LIMIT 9)")
                .bind(&scope.turn_id).fetch_one(&mut *tx).await?;
            if count >= MAX_CONTROL_RECEIPTS {
                return Err(Error::Capacity);
            }
        }
        let sequence = state
            .receipt_sequence
            .checked_add(1)
            .ok_or(Error::Capacity)?;
        let row = ReceiptRow {
            sequence,
            attempt_id: attempt_id.map(str::to_owned),
            kind: kind.to_owned(),
            key: command.key,
            command_hash,
            json: payload.text().to_owned(),
            previous: state.receipt_hash,
            hash: Digest::ZERO,
            at: now,
        };
        let hash = receipt_hash(&self.identity, scope, &row);
        spend(&mut tx, scope, receipt_charge(scope, &row)?, control, now).await?;
        sqlx::query("INSERT INTO assistant_provider_receipts(turn_id,receipt_sequence,attempt_id,kind,command_key,command_sha256,receipt_json,previous_sha256,receipt_sha256,committed_at_us) VALUES(?,?,?,?,?,?,?,?,?,?)")
            .bind(&scope.turn_id).bind(sequence).bind(attempt_id).bind(kind).bind(&row.key).bind(command_hash.hex()).bind(&row.json).bind(row.previous.hex()).bind(hash.hex()).bind(now).execute(&mut *tx).await?;
        sqlx::query("UPDATE assistant_provider_streams SET receipt_head_sequence=?,receipt_head_sha256=?,observed_turn_epoch=? WHERE turn_id=?")
            .bind(sequence).bind(hash.hex()).bind(command.turn_epoch).bind(&scope.turn_id).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(StagedReceipt {
            sequence,
            receipt_hash: hash,
            duplicate: false,
        })
    }
    pub async fn append_event(
        &mut self,
        scope: &StreamFence,
        attempt: Option<&AttemptFence>,
        command: EventCommand,
        now: i64,
    ) -> Result<StagedEvent> {
        valid_id(&command.key)?;
        let mut tx = self.tx.begin().await?;
        let state = load_stream(&mut tx, scope).await?;
        check_attempt(&mut tx, scope, attempt).await?;
        let attempt_id = attempt.map(|v| v.attempt_id.as_str());
        if let Some(existing) = load_event_key(&mut tx, &scope.turn_id, &command.key).await? {
            verify_event(&self.identity, scope, &existing)?;
            if existing.settlement.map(|v| (v.sequence, v.hash))
                != command.settlement.map(|v| (v.sequence, v.hash))
            {
                return Err(Error::Conflict);
            }
            if let Some(reference) = existing.settlement {
                verify_settlement(
                    &mut tx,
                    &self.identity,
                    scope,
                    existing.attempt_id.as_deref(),
                    &existing.event_type,
                    reference,
                    Some(command.turn_epoch),
                )
                .await?;
            }
            let proposed = command.draft.encode(existing.sequence)?;
            if existing.attempt_id.as_deref() != attempt_id
                || existing.json != proposed.json_bytes()
                || existing.sse != proposed.sse_bytes()
                || existing.event_type != proposed.event_type()
            {
                return Err(Error::Conflict);
            }
            return Ok(StagedEvent {
                event: Arc::new(existing),
                duplicate: true,
            });
        }
        require_active(&state)?;
        check_turn(&mut tx, scope, command.turn_epoch).await?;
        let sequence = state
            .last_sequence
            .checked_add(1)
            .filter(|v| *v <= provider_stream::MAX_SEQUENCE)
            .ok_or(Error::Capacity)?;
        let previous = check_event_head(&mut tx, &self.identity, scope, &state).await?;
        let encoded = command.draft.encode(sequence)?;
        let terminal = matches!(
            encoded.event_type(),
            "done" | "error" | "cancelled" | "interrupted"
        );
        if terminal != command.settlement.is_some() {
            return Err(Error::Invalid);
        }
        if let Some(reference) = command.settlement {
            if reference.sequence != state.receipt_sequence || reference.hash != state.receipt_hash
            {
                return Err(Error::Conflict);
            }
            verify_settlement(
                &mut tx,
                &self.identity,
                scope,
                attempt_id,
                encoded.event_type(),
                reference,
                Some(command.turn_epoch),
            )
            .await?;
        }
        let mut event = RetainedEvent {
            sequence,
            settlement: command.settlement,
            event_type: encoded.event_type().to_owned(),
            event_key: command.key,
            attempt_id: attempt_id.map(str::to_owned),
            json: encoded.json_bytes().to_vec(),
            sse: encoded.sse_bytes().to_vec(),
            content_hash: Digest(*encoded.content_sha256()),
            previous_hash: previous,
            event_hash: Digest::ZERO,
            committed_at_us: now,
        };
        event.event_hash = event_hash(&self.identity, scope, &event);
        let cost = event_charge(scope, &event)?;
        spend(&mut tx, scope, cost, terminal, now).await?;
        sqlx::query("INSERT INTO assistant_provider_events(turn_id,sequence,attempt_id,event_type,event_key,event_json,sse_bytes,sse_bytes_len,content_sha256,previous_sha256,event_sha256,committed_at_us,settlement_receipt_sequence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)")
            .bind(&scope.turn_id).bind(sequence as i64).bind(attempt_id).bind(&event.event_type).bind(&event.event_key).bind(std::str::from_utf8(&event.json).map_err(|_|Error::Invalid)?).bind(&event.sse).bind(event.sse.len() as i64).bind(event.content_hash.hex()).bind(previous.hex()).bind(event.event_hash.hex()).bind(now).bind(event.settlement.map(|v|v.sequence)).execute(&mut *tx).await?;
        sqlx::query("UPDATE assistant_provider_streams SET last_sequence=?,event_bytes=event_bytes+?,observed_turn_epoch=?,state=CASE WHEN ? THEN 'terminal' ELSE state END,terminal_sequence=CASE WHEN ? THEN ? ELSE terminal_sequence END WHERE turn_id=?")
            .bind(sequence as i64).bind(cost).bind(command.turn_epoch).bind(terminal).bind(terminal).bind(sequence as i64).bind(&scope.turn_id).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(StagedEvent {
            event: Arc::new(event),
            duplicate: false,
        })
    }
    /// Return unused logical quota only after the caller has completed all separate
    /// entity outcome/release phases. This neither prunes evidence nor repairs state.
    /// Recovery-only/uncertain settlement retains its reserve. A staged result is
    /// effective only when the caller commits the enclosing transaction.
    pub async fn release_unused_reservation(
        &mut self,
        scope: &StreamFence,
        attempt: Option<&AttemptFence>,
        turn_epoch: i64,
        settlement: ReceiptReference,
        now: i64,
    ) -> Result<i64> {
        let mut tx = self.tx.begin().await?;
        let state = load_stream(&mut tx, scope).await?;
        if state.state != "terminal" || state.terminal_sequence != Some(state.last_sequence) {
            return Err(Error::Lineage);
        }
        check_attempt(&mut tx, scope, attempt).await?;
        check_turn(&mut tx, scope, turn_epoch).await?;
        if settlement.sequence != state.receipt_sequence || settlement.hash != state.receipt_hash {
            return Err(Error::Conflict);
        }
        check_receipt_head(&mut tx, &self.identity, scope, &state).await?;
        check_event_head(&mut tx, &self.identity, scope, &state).await?;
        let event = load_event_sequence(&mut tx, &scope.turn_id, state.last_sequence)
            .await?
            .ok_or(Error::Corrupt)?;
        let attempt_id = attempt.map(|v| v.attempt_id.as_str());
        if event.attempt_id.as_deref() != attempt_id {
            return Err(Error::Conflict);
        }
        let definitive = verify_settlement(
            &mut tx,
            &self.identity,
            scope,
            attempt_id,
            &event.event_type,
            settlement,
            Some(turn_epoch),
        )
        .await?;
        if !definitive {
            return Err(Error::Lineage);
        }
        let released = state.reserved_bytes;
        if released != 0 {
            let changed=sqlx::query("UPDATE assistant_provider_budget SET reserved_bytes=reserved_bytes-? WHERE singleton=1 AND reserved_bytes>=?")
                .bind(released).bind(released).execute(&mut *tx).await?;
            if changed.rows_affected() != 1 {
                return Err(Error::Corrupt);
            }
            sqlx::query("UPDATE assistant_provider_streams SET reserved_bytes=0,updated_at_us=? WHERE turn_id=?")
                .bind(now).bind(&scope.turn_id).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(released)
    }
    pub async fn replay(
        &mut self,
        scope: &StreamFence,
        cursor: ReplayCursor,
        limits: PageLimits,
    ) -> Result<ReplayPage> {
        if limits.events == 0
            || limits.events > 256
            || limits.bytes == 0
            || limits.bytes > 4 * 1024 * 1024
        {
            return Err(Error::Invalid);
        }
        let state = load_stream(self.tx, scope).await?;
        if matches!(state.state.as_str(), "quarantined" | "deleted") {
            return Err(Error::Lineage);
        }
        let expected: i64 = sqlx::query_scalar(
            "SELECT observed_turn_epoch FROM assistant_provider_streams WHERE turn_id=?",
        )
        .bind(&scope.turn_id)
        .fetch_one(&mut **self.tx)
        .await?;
        check_turn(self.tx, scope, expected).await?;
        // End/future cursors must not hide a missing or corrupted retained head.
        check_receipt_head(self.tx, &self.identity, scope, &state).await?;
        check_event_head(self.tx, &self.identity, scope, &state).await?;
        let ReplayCursor::After(after) = cursor else {
            return Ok(ReplayPage {
                events: vec![],
                next: cursor,
                has_more: false,
                stream: state,
                retained_bytes: 0,
            });
        };
        if after.saturating_add(1) < state.first_retained_sequence {
            return Err(Error::CursorExpired);
        }
        if after >= state.last_sequence {
            return Ok(ReplayPage {
                events: vec![],
                next: cursor,
                has_more: false,
                stream: state,
                retained_bytes: 0,
            });
        }
        let metadata=sqlx::query("SELECT sequence,length(CAST(event_json AS BLOB))+length(sse_bytes)+length(CAST(event_type AS BLOB))+length(CAST(event_key AS BLOB))+coalesce(length(CAST(attempt_id AS BLOB)),0)+256 AS bytes FROM assistant_provider_events WHERE turn_id=? AND sequence>? ORDER BY sequence LIMIT ?")
            .bind(&scope.turn_id).bind(after as i64).bind(i64::from(limits.events)+1).fetch_all(&mut **self.tx).await?;
        let mut selected = 0u64;
        let mut retained_bytes = 0usize;
        for row in metadata.iter().take(limits.events as usize) {
            let sequence: i64 = row.try_get("sequence")?;
            let size: i64 = row.try_get("bytes")?;
            if sequence <= 0
                || sequence as u64 != after + selected + 1
                || sequence as u64 > state.last_sequence
                || size < 0
            {
                return Err(Error::Corrupt);
            }
            let size = usize::try_from(size).map_err(|_| Error::Capacity)?;
            if size > limits.bytes.saturating_sub(retained_bytes) {
                if selected == 0 {
                    return Err(Error::Capacity);
                }
                break;
            }
            retained_bytes += size;
            selected += 1;
        }
        if selected == 0 {
            return Err(Error::Corrupt);
        }
        let mut previous = if after == 0 {
            Digest::ZERO
        } else {
            let event = load_event_sequence(self.tx, &scope.turn_id, after)
                .await?
                .ok_or(Error::Corrupt)?;
            verify_event(&self.identity, scope, &event)?;
            event.event_hash
        };
        let mut events = Vec::with_capacity(selected as usize);
        for sequence in after + 1..=after + selected {
            let event = load_event_sequence(self.tx, &scope.turn_id, sequence)
                .await?
                .ok_or(Error::Corrupt)?;
            verify_event(&self.identity, scope, &event)?;
            if let Some(reference) = event.settlement {
                verify_settlement(
                    self.tx,
                    &self.identity,
                    scope,
                    event.attempt_id.as_deref(),
                    &event.event_type,
                    reference,
                    None,
                )
                .await?;
            }
            if event.previous_hash != previous {
                return Err(Error::Corrupt);
            }
            previous = event.event_hash;
            events.push(Arc::new(event));
        }
        Ok(ReplayPage {
            events,
            next: ReplayCursor::After(after + selected),
            has_more: after + selected < state.last_sequence,
            stream: state,
            retained_bytes,
        })
    }
}

fn charge(parts: &[usize]) -> Result<i64> {
    let size = parts
        .iter()
        .try_fold(ROW_OVERHEAD, |n, v| n.checked_add(*v))
        .ok_or(Error::Capacity)?;
    i64::try_from(size).map_err(|_| Error::Capacity)
}
pub fn stream_charge(scope: &StreamFence) -> Result<i64> {
    scope.validate()?;
    charge(&[
        scope.turn_id.len(),
        scope.stream_id.len(),
        scope.session_id.len(),
        scope.project_id.len(),
        64,
    ])
}
pub fn attempt_charge(
    scope: &StreamFence,
    fence: &AttemptFence,
    predecessor: Option<&str>,
) -> Result<i64> {
    scope.validate()?;
    fence.validate()?;
    charge(&[
        scope.turn_id.len(),
        fence.attempt_id.len(),
        fence.owner_id.len(),
        fence.claim_id.len(),
        predecessor.map_or(0, str::len),
        128,
    ])
}
pub fn watch_charge(id: &str) -> Result<i64> {
    valid_id(id)?;
    charge(&[id.len()])
}
fn receipt_charge(scope: &StreamFence, row: &ReceiptRow) -> Result<i64> {
    charge(&[
        scope.turn_id.len(),
        row.attempt_id.as_ref().map_or(0, String::len),
        row.kind.len(),
        row.key.len(),
        row.json.len(),
        192,
    ])
}
fn event_charge(scope: &StreamFence, row: &RetainedEvent) -> Result<i64> {
    charge(&[
        scope.turn_id.len(),
        row.attempt_id.as_ref().map_or(0, String::len),
        row.event_type.len(),
        row.event_key.len(),
        row.json.len(),
        row.sse.len(),
        192,
    ])
}
fn require_active(state: &StreamState) -> Result<()> {
    if state.state == "active" {
        Ok(())
    } else {
        Err(Error::Lineage)
    }
}
async fn spend(
    db: &mut SqliteConnection,
    scope: &StreamFence,
    charge: i64,
    control: bool,
    now: i64,
) -> Result<()> {
    let required = charge
        .checked_add(if control { 0 } else { CONTROL_FLOOR })
        .ok_or(Error::Capacity)?;
    let changed=sqlx::query("UPDATE assistant_provider_streams SET reserved_bytes=reserved_bytes-?,updated_at_us=? WHERE turn_id=? AND reserved_bytes>=?").bind(charge).bind(now).bind(&scope.turn_id).bind(required).execute(&mut *db).await?;
    if changed.rows_affected() != 1 {
        return Err(Error::Capacity);
    }
    let changed=sqlx::query("UPDATE assistant_provider_budget SET used_bytes=used_bytes+?,reserved_bytes=reserved_bytes-? WHERE singleton=1 AND reserved_bytes>=? AND used_bytes<=9223372036854775807-?").bind(charge).bind(charge).bind(charge).bind(charge).execute(db).await?;
    if changed.rows_affected() != 1 {
        return Err(Error::Corrupt);
    }
    Ok(())
}
async fn load_stream(db: &mut SqliteConnection, scope: &StreamFence) -> Result<StreamState> {
    scope.validate()?;
    let row=sqlx::query("SELECT stream_id,session_id,project_id,state,last_sequence,first_retained_sequence,receipt_head_sequence,receipt_head_sha256,reserved_bytes,terminal_sequence FROM assistant_provider_streams WHERE turn_id=? AND length(CAST(stream_id AS BLOB))<=800 AND length(CAST(session_id AS BLOB))<=800 AND length(CAST(project_id AS BLOB))<=800 AND length(state)<=16 AND length(receipt_head_sha256)=64").bind(&scope.turn_id).fetch_optional(db).await?.ok_or(Error::NotFound)?;
    if row.try_get::<&str, _>("stream_id")? != scope.stream_id
        || row.try_get::<&str, _>("session_id")? != scope.session_id
        || row.try_get::<&str, _>("project_id")? != scope.project_id
    {
        return Err(Error::Conflict);
    }
    let last: i64 = row.try_get("last_sequence")?;
    let first: i64 = row.try_get("first_retained_sequence")?;
    let receipt_sequence: i64 = row.try_get("receipt_head_sequence")?;
    let reserved_bytes: i64 = row.try_get("reserved_bytes")?;
    let terminal: Option<i64> = row.try_get("terminal_sequence")?;
    if last < 0
        || last as u64 > provider_stream::MAX_SEQUENCE
        || first < 1
        || first > last + 1
        || receipt_sequence < 0
        || reserved_bytes < 0
        || terminal.is_some_and(|v| v < 1 || v > last)
    {
        return Err(Error::Corrupt);
    }
    Ok(StreamState {
        scope: scope.clone(),
        state: row.try_get("state")?,
        last_sequence: last as u64,
        first_retained_sequence: first as u64,
        receipt_sequence,
        receipt_hash: Digest::parse(row.try_get("receipt_head_sha256")?)?,
        reserved_bytes,
        terminal_sequence: terminal.map(|v| v as u64),
    })
}
async fn check_attempt(
    db: &mut SqliteConnection,
    scope: &StreamFence,
    fence: Option<&AttemptFence>,
) -> Result<Option<String>> {
    let Some(fence) = fence else { return Ok(None) };
    fence.validate()?;
    let row=sqlx::query("SELECT ordinal,owner_id,claim_id,request_sha256,route_sha256,phase FROM assistant_provider_attempts WHERE turn_id=? AND attempt_id=? AND length(CAST(owner_id AS BLOB))<=800 AND length(CAST(claim_id AS BLOB))<=800 AND length(request_sha256)=64 AND length(route_sha256)=64 AND length(phase)<=32")
        .bind(&scope.turn_id).bind(&fence.attempt_id).fetch_optional(db).await?.ok_or(Error::NotFound)?;
    if row.try_get::<i64, _>("ordinal")? != fence.ordinal
        || row.try_get::<&str, _>("owner_id")? != fence.owner_id
        || row.try_get::<&str, _>("claim_id")? != fence.claim_id
        || Digest::parse(row.try_get("request_sha256")?)? != fence.request_sha256
        || Digest::parse(row.try_get("route_sha256")?)? != fence.route_sha256
    {
        return Err(Error::Conflict);
    }
    Ok(Some(row.try_get("phase")?))
}
async fn check_turn(db: &mut SqliteConnection, scope: &StreamFence, epoch: i64) -> Result<()> {
    if epoch < 0 {
        return Err(Error::Invalid);
    }
    let matched:i64=sqlx::query_scalar("SELECT count(*) FROM assistant_provider_entity_epochs w JOIN entities e ON e.id=w.entity_id WHERE w.entity_id=? AND w.present=1 AND w.mutation_epoch=? AND w.retained_rowid=e.rowid AND e.kind='chat_turns'").bind(&scope.turn_id).bind(epoch).fetch_one(db).await?;
    if matched == 1 {
        Ok(())
    } else {
        Err(Error::Lineage)
    }
}
struct ReceiptRow {
    sequence: i64,
    attempt_id: Option<String>,
    kind: String,
    key: String,
    command_hash: Digest,
    json: String,
    previous: Digest,
    hash: Digest,
    at: i64,
}
fn receipt_hash(identity: &schema::Identity, scope: &StreamFence, row: &ReceiptRow) -> Digest {
    Digest(digest_parts(&[
        b"nebula.assistant-provider-receipt/v1",
        identity.database_uuid.hyphenated().to_string().as_bytes(),
        scope.stream_id.as_bytes(),
        scope.turn_id.as_bytes(),
        row.attempt_id.as_deref().unwrap_or("").as_bytes(),
        row.sequence.to_string().as_bytes(),
        row.kind.as_bytes(),
        row.key.as_bytes(),
        &row.command_hash.0,
        row.json.as_bytes(),
        &row.previous.0,
        row.at.to_string().as_bytes(),
    ]))
}
fn verify_receipt(
    identity: &schema::Identity,
    scope: &StreamFence,
    row: &ReceiptRow,
) -> Result<()> {
    if row.sequence < 1 || receipt_hash(identity, scope, row) != row.hash {
        return Err(Error::Corrupt);
    }
    if CanonicalJson::from_raw(&row.json)?.bytes() != row.json.as_bytes() {
        return Err(Error::Corrupt);
    }
    Ok(())
}
fn receipt_from_row(row: sqlx::sqlite::SqliteRow) -> Result<ReceiptRow> {
    Ok(ReceiptRow {
        sequence: row.try_get("receipt_sequence")?,
        attempt_id: row.try_get("attempt_id")?,
        kind: row.try_get("kind")?,
        key: row.try_get("command_key")?,
        command_hash: Digest::parse(row.try_get("command_sha256")?)?,
        json: row.try_get("receipt_json")?,
        previous: Digest::parse(row.try_get("previous_sha256")?)?,
        hash: Digest::parse(row.try_get("receipt_sha256")?)?,
        at: row.try_get("committed_at_us")?,
    })
}
const RECEIPT_COLUMNS: &str = "receipt_sequence,attempt_id,kind,command_key,command_sha256,receipt_json,previous_sha256,receipt_sha256,committed_at_us";
const RECEIPT_BOUNDS: &str = "length(CAST(receipt_json AS BLOB))<=1048576 AND coalesce(length(CAST(attempt_id AS BLOB)),0)<=800 AND length(CAST(command_key AS BLOB))<=800 AND length(kind)<=64 AND length(command_sha256)=64 AND length(previous_sha256)=64 AND length(receipt_sha256)=64";
async fn load_receipt_key(
    db: &mut SqliteConnection,
    turn: &str,
    key: &str,
) -> Result<Option<ReceiptRow>> {
    let sql = format!(
        "SELECT {RECEIPT_COLUMNS} FROM assistant_provider_receipts WHERE turn_id=? AND command_key=? AND {RECEIPT_BOUNDS}"
    );
    sqlx::query(&sql)
        .bind(turn)
        .bind(key)
        .fetch_optional(db)
        .await?
        .map(receipt_from_row)
        .transpose()
}
async fn load_receipt_sequence(
    db: &mut SqliteConnection,
    turn: &str,
    sequence: i64,
) -> Result<Option<ReceiptRow>> {
    let sql = format!(
        "SELECT {RECEIPT_COLUMNS} FROM assistant_provider_receipts WHERE turn_id=? AND receipt_sequence=? AND {RECEIPT_BOUNDS}"
    );
    sqlx::query(&sql)
        .bind(turn)
        .bind(sequence)
        .fetch_optional(db)
        .await?
        .map(receipt_from_row)
        .transpose()
}
async fn check_receipt_head(
    db: &mut SqliteConnection,
    identity: &schema::Identity,
    scope: &StreamFence,
    state: &StreamState,
) -> Result<()> {
    if state.receipt_sequence == 0 {
        return if state.receipt_hash == Digest::ZERO {
            Ok(())
        } else {
            Err(Error::Corrupt)
        };
    }
    let head = load_receipt_sequence(db, &scope.turn_id, state.receipt_sequence)
        .await?
        .ok_or(Error::Corrupt)?;
    verify_receipt(identity, scope, &head)?;
    if head.hash != state.receipt_hash {
        return Err(Error::Corrupt);
    }
    let expected = if head.sequence == 1 {
        Digest::ZERO
    } else {
        let prior = load_receipt_sequence(db, &scope.turn_id, head.sequence - 1)
            .await?
            .ok_or(Error::Corrupt)?;
        verify_receipt(identity, scope, &prior)?;
        prior.hash
    };
    if head.previous != expected {
        return Err(Error::Corrupt);
    }
    Ok(())
}
fn control_epoch(control: &ControlReceipt) -> Option<i64> {
    match control {
        ControlReceipt::AnswerSaved { turn, .. }
        | ControlReceipt::Completed { turn, .. }
        | ControlReceipt::Released { turn, .. }
        | ControlReceipt::Cancelled { turn, .. }
        | ControlReceipt::Failed { turn, .. }
        | ControlReceipt::RecoveryClassified { turn, .. } => Some(turn.mutation_epoch),
        ControlReceipt::LineageInvalidated { observed_epoch, .. } => Some(*observed_epoch),
        ControlReceipt::RetentionBoundary { .. } => None,
    }
}
fn decode_control(scope: &StreamFence, row: &ReceiptRow) -> Result<ControlReceipt> {
    if row.json.len() > MAX_CONTROL_RECEIPT_BYTES {
        return Err(Error::Corrupt);
    }
    let control: ControlReceipt = serde_json::from_str(&row.json).map_err(|_| Error::Corrupt)?;
    let epoch = control_epoch(&control).ok_or(Error::Corrupt)?;
    if control.kind() != row.kind
        || control
            .canonical(scope, epoch)
            .map_err(|_| Error::Corrupt)?
            .bytes()
            != row.json.as_bytes()
    {
        return Err(Error::Corrupt);
    }
    Ok(control)
}
/// Terminal events refer to a typed immutable settlement in this stream/attempt.
/// Historical proof epochs stay historical; only a new append supplies a current epoch.
async fn verify_settlement(
    db: &mut SqliteConnection,
    identity: &schema::Identity,
    scope: &StreamFence,
    attempt: Option<&str>,
    event_type: &str,
    reference: ReceiptReference,
    expected_epoch: Option<i64>,
) -> Result<bool> {
    let row = load_receipt_sequence(db, &scope.turn_id, reference.sequence)
        .await?
        .ok_or(Error::Corrupt)?;
    verify_receipt(identity, scope, &row)?;
    if row.hash != reference.hash || row.attempt_id.as_deref() != attempt {
        return Err(Error::Conflict);
    }
    let control = decode_control(scope, &row)?;
    let epoch = control_epoch(&control).ok_or(Error::Corrupt)?;
    if expected_epoch.is_some_and(|expected| expected != epoch) {
        return Err(Error::Conflict);
    }
    let permitted = if let ControlReceipt::Released {
        settlement_receipt, ..
    } = &control
    {
        // Eight typed controls maximum across the stream, including all attempts.
        // A release can settle success, cancellation or failure; it never turns a
        // failed/interrupted source state into successful completion.
        let sql = format!(
            "SELECT {RECEIPT_COLUMNS} FROM assistant_provider_receipts WHERE turn_id=? AND kind IN ('completed','cancelled','failed','recovery_classified','lineage_invalidated') AND {RECEIPT_BOUNDS} LIMIT 9"
        );
        let rows = sqlx::query(&sql)
            .bind(&scope.turn_id)
            .fetch_all(&mut *db)
            .await?;
        if rows.len() > MAX_CONTROL_RECEIPTS as usize {
            return Err(Error::Corrupt);
        }
        let mut matched = None;
        for previous in rows {
            let previous = receipt_from_row(previous)?;
            if previous.hash != *settlement_receipt {
                continue;
            }
            verify_receipt(identity, scope, &previous)?;
            let settlement = decode_control(scope, &previous)?;
            if previous.attempt_id.as_deref() == attempt
                && previous.sequence < row.sequence
                && control_epoch(&settlement).is_some_and(|v| v <= epoch)
            {
                matched = terminal_classification(event_type, &settlement, true);
            }
        }
        matched
    } else {
        terminal_classification(event_type, &control, false)
    };
    permitted.ok_or(Error::Conflict)
}
// Some(true): definitive settlement; Some(false): valid recovery/uncertainty.
// The latter may be replayed but cannot release its future settlement reserve.
fn terminal_classification(
    event_type: &str,
    control: &ControlReceipt,
    released: bool,
) -> Option<bool> {
    match (event_type, control) {
        ("done", ControlReceipt::Completed { .. }) if released => Some(true),
        ("cancelled", ControlReceipt::Cancelled { .. }) => Some(true),
        ("error", ControlReceipt::Failed { .. }) => Some(true),
        ("error", ControlReceipt::LineageInvalidated { .. }) => Some(false),
        (
            "error" | "interrupted",
            ControlReceipt::RecoveryClassified {
                classification: RecoveryClass::Interrupted,
                ..
            },
        ) => Some(false),
        (
            "interrupted",
            ControlReceipt::RecoveryClassified {
                classification: RecoveryClass::ClaimedUncertain | RecoveryClass::UnknownLineage,
                ..
            },
        ) => Some(false),
        _ => None,
    }
}
fn event_hash(identity: &schema::Identity, scope: &StreamFence, event: &RetainedEvent) -> Digest {
    let settlement_sequence = event
        .settlement
        .map(|v| v.sequence.to_string())
        .unwrap_or_default();
    let settlement_hash = event.settlement.map(|v| v.hash).unwrap_or(Digest::ZERO);
    Digest(digest_parts(&[
        b"nebula.assistant-provider-event/v1",
        settlement_sequence.as_bytes(),
        &settlement_hash.0,
        identity.database_uuid.hyphenated().to_string().as_bytes(),
        scope.stream_id.as_bytes(),
        scope.turn_id.as_bytes(),
        event.attempt_id.as_deref().unwrap_or("").as_bytes(),
        event.sequence.to_string().as_bytes(),
        event.event_key.as_bytes(),
        &event.previous_hash.0,
        &event.content_hash.0,
        &event.sse,
        event.committed_at_us.to_string().as_bytes(),
    ]))
}
fn verify_event(
    identity: &schema::Identity,
    scope: &StreamFence,
    event: &RetainedEvent,
) -> Result<()> {
    if matches!(
        event.event_type.as_str(),
        "done" | "error" | "cancelled" | "interrupted"
    ) != event.settlement.is_some()
    {
        return Err(Error::Corrupt);
    }
    if event.sequence == 0
        || event.sequence > provider_stream::MAX_SEQUENCE
        || event_hash(identity, scope, event) != event.event_hash
    {
        return Err(Error::Corrupt);
    }
    let suffix = format!(",\"sequence\":{}}}", event.sequence);
    let raw = std::str::from_utf8(&event.json).map_err(|_| Error::Corrupt)?;
    let prefix = raw.strip_suffix(&suffix).ok_or(Error::Corrupt)?;
    let mut draft = String::with_capacity(prefix.len() + 1);
    draft.push_str(prefix);
    draft.push('}');
    let encoded = Draft::from_raw(&event.event_type, &draft)
        .map_err(|_| Error::Corrupt)?
        .encode(event.sequence)
        .map_err(|_| Error::Corrupt)?;
    if encoded.json_bytes() != event.json
        || encoded.sse_bytes() != event.sse
        || Digest(*encoded.content_sha256()) != event.content_hash
    {
        return Err(Error::Corrupt);
    }
    Ok(())
}
fn event_from_row(row: sqlx::sqlite::SqliteRow) -> Result<RetainedEvent> {
    let sequence: i64 = row.try_get("sequence")?;
    let json: String = row.try_get("event_json")?;
    let sse: Vec<u8> = row.try_get("sse_bytes")?;
    if sequence < 1 || row.try_get::<i64, _>("sse_bytes_len")? != sse.len() as i64 {
        return Err(Error::Corrupt);
    }
    let settlement_sequence: Option<i64> = row.try_get("settlement_receipt_sequence")?;
    let settlement = match settlement_sequence {
        Some(sequence) => {
            let hash: Option<&str> = row.try_get("settlement_receipt_hash")?;
            Some(ReceiptReference {
                sequence,
                hash: Digest::parse(hash.ok_or(Error::Corrupt)?)?,
            })
        }
        None => None,
    };
    Ok(RetainedEvent {
        sequence: sequence as u64,
        settlement,
        event_type: row.try_get("event_type")?,
        event_key: row.try_get("event_key")?,
        attempt_id: row.try_get("attempt_id")?,
        json: json.into_bytes(),
        sse,
        content_hash: Digest::parse(row.try_get("content_sha256")?)?,
        previous_hash: Digest::parse(row.try_get("previous_sha256")?)?,
        event_hash: Digest::parse(row.try_get("event_sha256")?)?,
        committed_at_us: row.try_get("committed_at_us")?,
    })
}
const EVENT_COLUMNS: &str = "sequence,attempt_id,event_type,event_key,event_json,sse_bytes,sse_bytes_len,content_sha256,previous_sha256,event_sha256,committed_at_us,settlement_receipt_sequence,(SELECT CASE WHEN length(CAST(r.receipt_sha256 AS BLOB))=64 THEN r.receipt_sha256 END FROM assistant_provider_receipts r WHERE r.turn_id=assistant_provider_events.turn_id AND r.receipt_sequence=assistant_provider_events.settlement_receipt_sequence) AS settlement_receipt_hash";
const EVENT_BOUNDS: &str = "length(CAST(event_json AS BLOB))<=1048576 AND length(sse_bytes)<=1048576 AND coalesce(length(CAST(attempt_id AS BLOB)),0)<=800 AND length(CAST(event_key AS BLOB))<=800 AND length(event_type)<=64 AND length(content_sha256)=64 AND length(previous_sha256)=64 AND length(event_sha256)=64";
async fn load_event_key(
    db: &mut SqliteConnection,
    turn: &str,
    key: &str,
) -> Result<Option<RetainedEvent>> {
    let sql = format!(
        "SELECT {EVENT_COLUMNS} FROM assistant_provider_events WHERE turn_id=? AND event_key=? AND {EVENT_BOUNDS}"
    );
    sqlx::query(&sql)
        .bind(turn)
        .bind(key)
        .fetch_optional(db)
        .await?
        .map(event_from_row)
        .transpose()
}
async fn load_event_sequence(
    db: &mut SqliteConnection,
    turn: &str,
    sequence: u64,
) -> Result<Option<RetainedEvent>> {
    let sequence = i64::try_from(sequence).map_err(|_| Error::Invalid)?;
    let sql = format!(
        "SELECT {EVENT_COLUMNS} FROM assistant_provider_events WHERE turn_id=? AND sequence=? AND {EVENT_BOUNDS}"
    );
    sqlx::query(&sql)
        .bind(turn)
        .bind(sequence)
        .fetch_optional(db)
        .await?
        .map(event_from_row)
        .transpose()
}
async fn check_event_head(
    db: &mut SqliteConnection,
    identity: &schema::Identity,
    scope: &StreamFence,
    state: &StreamState,
) -> Result<Digest> {
    if state.last_sequence == 0 {
        return Ok(Digest::ZERO);
    }
    let event = load_event_sequence(db, &scope.turn_id, state.last_sequence)
        .await?
        .ok_or(Error::Corrupt)?;
    verify_event(identity, scope, &event)?;
    if let Some(reference) = event.settlement {
        verify_settlement(
            db,
            identity,
            scope,
            event.attempt_id.as_deref(),
            &event.event_type,
            reference,
            None,
        )
        .await?;
    }
    let expected = if event.sequence == 1 {
        Digest::ZERO
    } else {
        let prior = load_event_sequence(db, &scope.turn_id, event.sequence - 1)
            .await?
            .ok_or(Error::Corrupt)?;
        verify_event(identity, scope, &prior)?;
        prior.event_hash
    };
    if event.previous_hash != expected {
        return Err(Error::Corrupt);
    }
    Ok(event.event_hash)
}
