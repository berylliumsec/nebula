#[allow(dead_code)]
mod support;
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_storage::entities::{
    Config, Error, GeneratedListQuery, Mutation, SqliteAssistantStore,
};
use serde_json::{Value, json};
use sqlx::Connection;

fn query(kind: Kind, limit: u32, offset: u64) -> GeneratedListQuery {
    GeneratedListQuery {
        kind,
        engagement_id: Some("catalog-project".into()),
        offset,
        limit,
    }
}
fn record(kind: Kind, id: &str, changes: Value) -> Mutation {
    let mut p = support::payload(kind);
    p["id"] = id.into();
    p["engagement_id"] = "catalog-project".into();
    p.as_object_mut()
        .unwrap()
        .extend(changes.as_object().unwrap().clone());
    Mutation::Create(StoredAssistantRecord::decode(kind, &serde_json::to_vec(&p).unwrap()).unwrap())
}

#[tokio::test]
async fn generated_catalog_filters_before_paging_and_does_not_read_a_lookahead() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(
        &path,
        Config {
            page_bytes: 1,
            ..Config::default()
        },
    )
    .await
    .unwrap();
    store
        .apply(vec![
            record(
                Kind::Session,
                "a-hidden",
                json!({"metadata":{"temporary_assistant":true}}),
            ),
            record(Kind::Session, "b-shown", json!({"metadata":{}})),
            record(
                Kind::Session,
                "c-shown",
                json!({"metadata":{"temporary_assistant":false}}),
            ),
            record(
                Kind::Session,
                "d-hidden",
                json!({"metadata":{"temporary_assistant":"false"}}),
            ),
            record(Kind::Session, "e-shown", json!({"metadata":{}})),
            record(Kind::Session, "f-other", json!({"engagement_id":"another"})),
        ])
        .await
        .unwrap();
    let page = store
        .list_complete_page(query(Kind::Session, 2, 0))
        .await
        .unwrap();
    assert_eq!(
        page.iter()
            .map(|r| r.payload()["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["b-shown", "c-shown"]
    );
    assert_eq!(
        store
            .list_complete_page(query(Kind::Session, 2, 1))
            .await
            .unwrap()
            .len(),
        2
    );
    let mut raw = support::raw(&path).await;
    sqlx::query("UPDATE entities SET revision=999 WHERE id='e-shown'")
        .execute(&mut raw)
        .await
        .unwrap();
    assert_eq!(
        store
            .list_complete_page(query(Kind::Session, 2, 0))
            .await
            .unwrap()
            .len(),
        2
    );
    assert!(matches!(
        store.list_complete_page(query(Kind::Session, 3, 0)).await,
        Err(Error::CorruptEnvelope)
    ));
    assert!(
        store
            .list_complete_page(query(Kind::Session, 1, i64::MAX as u64))
            .await
            .unwrap()
            .is_empty()
    );
    assert!(matches!(
        store.list_complete_page(query(Kind::Bookmark, 1, 0)).await,
        Err(Error::InvalidBounds)
    ));
    assert!(matches!(
        store
            .list_complete_page(query(Kind::Session, 1001, 0))
            .await,
        Err(Error::InvalidBounds)
    ));
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn generated_catalog_refuses_oversized_arrays_instead_of_silently_shortening_them() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    for id in ["large-first", "large-second"] {
        store
            .apply(vec![record(
                Kind::Message,
                id,
                json!({"metadata":{"opaque":"x".repeat(9*1024*1024)}}),
            )])
            .await
            .unwrap();
    }
    assert_eq!(
        store
            .list_complete_page(query(Kind::Message, 1, 0))
            .await
            .unwrap()
            .len(),
        1
    );
    assert!(matches!(
        store.list_complete_page(query(Kind::Message, 2, 0)).await,
        Err(Error::ReadLimit)
    ));
    store.shutdown().await.unwrap();
}
