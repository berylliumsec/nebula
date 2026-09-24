//! Passive saved-context selection for preparation. In particular a new
//! conversation selects SQL NULL session identities, not a fabricated ID.
use super::*;
use nebula_assistant_domain::model_validation::{InputOrigin, Model, hydrate};

impl SqliteAssistantStore {
    /// Hydrate every selected row, including inactive rows, in one ordered SQLite
    /// read snapshot. Active filtering and the operator-context budget happen
    /// only after this completes, as in Python decisions_for/decision_snapshot.
    /// Caller supplies already-authorized scope; this does not authorize a turn.
    pub async fn preparation_decisions(
        &self,
        session_id: Option<&str>,
        project_id: &str,
    ) -> Result<Vec<StoredAssistantRecord>> {
        // Empty and NULL sessions are distinct source selectors. Bound bytes
        // without converting an empty selector into an invalid/missing session.
        if project_id.len() > 4096 || session_id.is_some_and(|id| id.len() > 4096) {
            return Err(Error::InvalidBounds);
        }
        let _permit = self.read_permit()?;
        let mut query = QueryBuilder::<Sqlite>::new(SELECT_RECORD);
        query
            .push(" WHERE kind = 'chat_decisions' AND engagement_id = ")
            .push_bind(project_id)
            .push(" AND (chat_session_id ");
        match session_id {
            Some(id) => {
                query.push("= ").push_bind(id);
            }
            None => {
                query.push("IS NULL");
            }
        }
        query.push(
            " OR json_extract(payload, '$.scope') = 'project') ORDER BY created_at, id LIMIT 10001",
        );
        let statement = query.build();
        let mut rows = statement.fetch(&self.readers);
        let mut records = Vec::new();
        let (mut raw_bytes, mut hydrated_bytes) = (0usize, 0usize);
        while let Some(row) = rows.try_next().await? {
            let size: i64 = row.try_get("payload_bytes")?;
            if size < 0 || size as u64 > MAX_RECORD_BYTES as u64 {
                return Err(RecordError::TooLarge.into());
            }
            raw_bytes += size as usize;
            if records.len() == 10000 || raw_bytes > MAX_TRANSACTION_BYTES {
                return Err(Error::ReadLimit);
            }
            let payload: &str = row.try_get("payload")?;
            let hydrated = hydrate(
                Model::ChatDecision,
                InputOrigin::RetainedJson,
                payload.as_bytes(),
            )
            .map_err(direct_record_error)?;
            let bytes = serde_json::to_vec(&hydrated).map_err(|_| RecordError::Json)?;
            hydrated_bytes = hydrated_bytes.saturating_add(bytes.len());
            if hydrated_bytes > MAX_TRANSACTION_BYTES {
                return Err(Error::ReadLimit);
            }
            let record = StoredAssistantRecord::decode(AssistantKind::Decision, &bytes)
                .map_err(direct_record_error)?;
            let p = record.payload();
            // These are retained Rust storage boundaries, not Python payload
            // validation: do not turn denormalized/corrupt rows into authority.
            if p["id"].as_str() != Some(row.try_get::<&str, _>("id")?)
                || record_revision(&record)? != row.try_get::<i64, _>("revision")?
                || p["engagement_id"].as_str() != row.try_get::<Option<&str>, _>("engagement_id")?
                || session_projection(&record)
                    != row.try_get::<Option<&str>, _>("chat_session_id")?
                || sql_time(&p["created_at"])? != row.try_get::<&str, _>("created_at")?
                || sql_time(&p["updated_at"])? != row.try_get::<&str, _>("updated_at")?
            {
                return Err(Error::CorruptEnvelope);
            }
            records.push(record);
        }
        Ok(records)
    }
}
