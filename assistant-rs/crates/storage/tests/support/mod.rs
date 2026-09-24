use nebula_assistant_domain::records::{AssistantKind, StoredAssistantRecord};
use nebula_assistant_storage::entities::Mutation;
use serde_json::Value;
use sqlx::{Connection, SqliteConnection, sqlite::SqliteConnectOptions};
use std::path::Path;

pub fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../../compatibility/python-storage.json"
    ))
    .unwrap()
}
pub fn payload(kind: AssistantKind) -> Value {
    fixture()["entities"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["kind"] == kind.as_str())
        .unwrap()["payload"]
        .clone()
}
pub fn record(kind: AssistantKind, id: &str) -> StoredAssistantRecord {
    let mut p = payload(kind);
    p["id"] = id.into();
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}
pub fn patch(kind: AssistantKind, id: &str, revision: i64, changes: Value) -> Mutation {
    Mutation::Patch {
        kind,
        id: id.into(),
        expected_revision: revision,
        changes: changes.as_object().unwrap().clone(),
    }
}
pub async fn raw(path: &Path) -> SqliteConnection {
    SqliteConnection::connect_with(
        &SqliteConnectOptions::new()
            .filename(path)
            .create_if_missing(true),
    )
    .await
    .unwrap()
}
pub async fn database(path: &Path) {
    let fixture = fixture();
    let mut connection = raw(path).await;
    for sql in fixture["schema_sql"].as_array().unwrap() {
        sqlx::query(sql.as_str().unwrap())
            .execute(&mut connection)
            .await
            .unwrap();
    }
    sqlx::query("INSERT INTO schema_versions (version, applied_at) VALUES (5, '2026-09-23 12:00:00.000000')").execute(&mut connection).await.unwrap();
    sqlx::query("INSERT INTO alembic_version (version_num) VALUES ('0016_chat_session_lookup')")
        .execute(&mut connection)
        .await
        .unwrap();
    for row in fixture["entities"].as_array().unwrap() {
        sqlx::query("INSERT INTO entities (id, kind, engagement_id, revision, payload, chat_session_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
            .bind(row["id"].as_str()).bind(row["kind"].as_str()).bind(row["engagement_id"].as_str()).bind(row["revision"].as_i64())
            .bind(serde_json::to_string(&row["payload"]).unwrap()).bind(row["chat_session_id"].as_str()).bind(row["created_at"].as_str()).bind(row["updated_at"].as_str())
            .execute(&mut connection).await.unwrap();
    }
    for row in fixture["search_documents"].as_array().unwrap() {
        sqlx::query("INSERT INTO search_documents (id, project_id, resource_kind, resource_id, revision, label, description, breadcrumb, content, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
            .bind(row["id"].as_str()).bind(row["project_id"].as_str()).bind(row["resource_kind"].as_str()).bind(row["resource_id"].as_str()).bind(row["revision"].as_i64())
            .bind(row["label"].as_str()).bind(row["description"].as_str()).bind(row["breadcrumb"].as_str()).bind(row["content"].as_str()).bind(row["updated_at"].as_str())
            .execute(&mut connection).await.unwrap();
    }
    connection.close().await.unwrap();
}
