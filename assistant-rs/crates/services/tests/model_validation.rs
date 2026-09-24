#[path = "../../storage/tests/support/mod.rs"]
#[allow(dead_code)]
mod support;

use nebula_assistant_domain::records::{AssistantKind as Kind, RecordError, StoredAssistantRecord};
use nebula_assistant_services::{
    AssistantRecords, Error,
    generated::{CatalogKind, GeneratedListRequest},
};
use nebula_assistant_storage::entities::{
    Config, Error as StorageError, Mutation, SqliteAssistantStore,
};
use serde_json::{Value, json};
use sqlx::Connection;

fn record(kind: Kind, id: &str) -> StoredAssistantRecord {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    p["engagement_id"] = "validation-project".into();
    if matches!(kind, Kind::Goal | Kind::Schedule) {
        p["session_id"] = "validation".into();
    }
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap()
}
fn list() -> GeneratedListRequest {
    GeneratedListRequest {
        engagement_id: Some("validation-project".into()),
        offset: 0,
        limit: 100,
    }
}

#[tokio::test]
async fn retained_validation_services_preserve_catalog_and_plan_exception_origins() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    store
        .apply(vec![
            Mutation::Create(record(Kind::Session, "validation")),
            Mutation::Create(record(Kind::Schedule, "schedule")),
            Mutation::Create(record(Kind::Goal, "goal")),
        ])
        .await
        .unwrap();
    let services = AssistantRecords::new(store.clone());
    let mut raw = support::raw(&path).await;
    let original: String = sqlx::query_scalar("SELECT payload FROM entities WHERE id='schedule'")
        .fetch_one(&mut raw)
        .await
        .unwrap();
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.next_run_at','2030-01-01T12:00:00') WHERE id='schedule'").execute(&mut raw).await.unwrap();
    match services.session_schedule("validation").await.unwrap_err() {
        Error::RetainedModelValidation(report) => {
            let errors: Value = serde_json::to_value(report).unwrap();
            assert_eq!(errors[0]["loc"], json!(["next_run_at"]));
            assert_eq!(errors[0]["input"], "2030-01-01T12:00:00");
        }
        error => panic!("expected direct model error, received {error:?}"),
    }
    assert!(matches!(
        services
            .catalog_record(CatalogKind::Schedules, "schedule")
            .await,
        Err(Error::LegacyStorageUnhandled)
    ));
    assert!(matches!(
        services.catalog(CatalogKind::Schedules, list()).await,
        Err(Error::LegacyStorageUnhandled)
    ));
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.provider_profile_id',null) WHERE id='validation'").execute(&mut raw).await.unwrap();
    assert!(
        matches!(
            services.session_schedule("validation").await,
            Err(Error::LegacyStorageUnhandled)
        ),
        "root model failure precedes direct candidate validation"
    );
    assert!(matches!(
        services
            .catalog_record(CatalogKind::Sessions, "validation")
            .await,
        Err(Error::LegacyStorageUnhandled)
    ));
    assert!(matches!(
        services.catalog(CatalogKind::Sessions, list()).await,
        Err(Error::LegacyStorageUnhandled)
    ));
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.provider_profile_id','provider') WHERE id='validation'").execute(&mut raw).await.unwrap();
    sqlx::query(
        "UPDATE entities SET payload=json_set(payload,'$.objective',json('[]')) WHERE id='goal'",
    )
    .execute(&mut raw)
    .await
    .unwrap();
    assert!(
        matches!(
            services
                .session_goal(
                    "validation",
                    chrono::DateTime::from_timestamp(0, 0).unwrap()
                )
                .await,
            Err(Error::RetainedModelValidation(_))
        ),
        "direct Goal diagnostics escape while catalogue errors stay wrapped"
    );
    assert!(matches!(
        services.catalog_record(CatalogKind::Goals, "goal").await,
        Err(Error::LegacyStorageUnhandled)
    ));
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.objective','Valid parent') WHERE id='goal'")
        .execute(&mut raw).await.unwrap();
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.objective',json('[]')) WHERE id='fixture-chat_goals'")
        .execute(&mut raw).await.unwrap();
    assert!(
        matches!(
            services.goal_children("validation").await,
            Err(Error::LegacyStorageUnhandled)
        ),
        "the global child-goal scan uses wrapped list_entities hydration"
    );
    sqlx::query("UPDATE entities SET payload=?,revision=99 WHERE id='schedule'")
        .bind(&original)
        .execute(&mut raw)
        .await
        .unwrap();
    assert!(matches!(
        services
            .catalog_record(CatalogKind::Schedules, "schedule")
            .await,
        Err(Error::Storage(StorageError::CorruptEnvelope))
    ));
    assert!(matches!(
        services.catalog(CatalogKind::Schedules, list()).await,
        Err(Error::Storage(StorageError::CorruptEnvelope))
    ));
    sqlx::query("UPDATE entities SET payload='{',revision=2 WHERE id='schedule'")
        .execute(&mut raw)
        .await
        .unwrap();
    assert!(matches!(
        services
            .catalog_record(CatalogKind::Schedules, "schedule")
            .await,
        Err(Error::Storage(StorageError::Record(RecordError::Json)))
    ));
    sqlx::query("UPDATE entities SET payload=? WHERE id='schedule'")
        .bind(" ".repeat(16 * 1024 * 1024 + 1))
        .execute(&mut raw)
        .await
        .unwrap();
    assert!(matches!(
        services
            .catalog_record(CatalogKind::Schedules, "schedule")
            .await,
        Err(Error::Storage(StorageError::ReadLimit))
    ));
    assert!(matches!(
        services.catalog(CatalogKind::Schedules, list()).await,
        Err(Error::Storage(StorageError::ReadLimit))
    ));
    assert!(matches!(
        services
            .catalog_record(CatalogKind::Schedules, "missing")
            .await,
        Err(Error::EntityNotFound {
            kind: "chat_schedules",
            ..
        })
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}
