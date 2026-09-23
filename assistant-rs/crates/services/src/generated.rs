//! Read-only generated entity routes consumed by the existing Assistant catalog.
//! These raw records have different visibility semantics from a transcript view.
use crate::{AssistantRecords, Result};
use nebula_assistant_domain::records::{AssistantKind, StoredAssistantRecord};
use nebula_assistant_storage::entities::GeneratedListQuery;

#[derive(Clone, Copy)]
pub enum CatalogKind {
    Sessions,
    Messages,
}
impl CatalogKind {
    fn kind(self) -> AssistantKind {
        match self {
            Self::Sessions => AssistantKind::Session,
            Self::Messages => AssistantKind::Message,
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
        Ok(self
            .store
            .list_complete_page(GeneratedListQuery {
                kind: kind.kind(),
                engagement_id: request.engagement_id,
                offset: request.offset,
                limit: request.limit,
            })
            .await?)
    }

    pub async fn catalog_record(
        &self,
        kind: CatalogKind,
        id: &str,
    ) -> Result<StoredAssistantRecord> {
        self.get(kind.kind(), id).await
    }
}
