//! Read-only generated entity routes consumed by the existing Assistant catalog.
//! These raw records have different visibility semantics from a transcript view.
use crate::{AssistantRecords, Error, Result};
use nebula_assistant_domain::records::{AssistantKind, RecordError, StoredAssistantRecord};
use nebula_assistant_storage::entities::{Error as StorageError, GeneratedListQuery};

fn wrapped_catalog_error(error: Error) -> Error {
    // Generated GET/list use Store.get/list_entities, whose model hydration
    // failures become CorruptRecordError. Keep this scope separate from direct
    // model reads, SQL/envelope integrity failures and resource limits.
    match error {
        Error::Storage(StorageError::Record(RecordError::TooLarge)) => {
            StorageError::ReadLimit.into()
        }
        Error::Storage(StorageError::Record(
            RecordError::Shape(_) | RecordError::Invariant(_) | RecordError::ModelValidation(_),
        )) => Error::LegacyStorageUnhandled,
        error => error,
    }
}

#[derive(Clone, Copy)]
pub enum CatalogKind {
    Sessions,
    Messages,
    Goals,
    GoalUsageCharges,
    Schedules,
    Subagents,
}
impl CatalogKind {
    fn kind(self) -> AssistantKind {
        match self {
            Self::Sessions => AssistantKind::Session,
            Self::Messages => AssistantKind::Message,
            Self::Goals => AssistantKind::Goal,
            Self::GoalUsageCharges => AssistantKind::GoalUsageCharge,
            Self::Schedules => AssistantKind::Schedule,
            Self::Subagents => AssistantKind::Subagent,
        }
    }
}

pub struct GeneratedListRequest {
    pub engagement_id: Option<String>,
    pub offset: u64,
    pub limit: u32,
}

impl AssistantRecords {
    pub async fn catalog(
        &self,
        kind: CatalogKind,
        request: GeneratedListRequest,
    ) -> Result<Vec<StoredAssistantRecord>> {
        self.store
            .list_complete_page(GeneratedListQuery {
                kind: kind.kind(),
                engagement_id: request.engagement_id,
                offset: request.offset,
                limit: request.limit,
            })
            .await
            .map_err(|error| wrapped_catalog_error(error.into()))
    }

    pub async fn catalog_record(
        &self,
        kind: CatalogKind,
        id: &str,
    ) -> Result<StoredAssistantRecord> {
        self.get(kind.kind(), id)
            .await
            .map_err(wrapped_catalog_error)
    }
}
