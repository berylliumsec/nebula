#[allow(dead_code)]
mod support;

use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_storage::entities::{
    Config, Error, Mutation, NavigationQuery, SqliteAssistantStore,
};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection};
use support::{database, payload, raw};

fn entity(kind: Kind, id: &str, changes: Value) -> StoredAssistantRecord {
    let mut value = payload(kind);
    value["id"] = id.into();
    value["engagement_id"] = "project".into();
    if kind != Kind::Session {
        value["session_id"] = "session".into();
    }
    for (key, field) in changes.as_object().unwrap() {
        value[key] = field.clone();
    }
    StoredAssistantRecord::decode(kind, &serde_json::to_vec(&value).unwrap()).unwrap()
}

fn search(text: &str) -> NavigationQuery {
    NavigationQuery {
        project_id: "project".into(),
        session_id: None,
        text: text.into(),
        bookmarked: false,
        offset: 0,
        limit: 50,
    }
}

async fn empty(path: &std::path::Path) -> SqliteAssistantStore {
    database(path).await;
    let mut connection = raw(path).await;
    sqlx::query("DELETE FROM entities")
        .execute(&mut connection)
        .await
        .unwrap();
    sqlx::query("DELETE FROM search_documents")
        .execute(&mut connection)
        .await
        .unwrap();
    connection.close().await.unwrap();
    SqliteAssistantStore::open(path, Config::default())
        .await
        .unwrap()
}

fn ids(records: &[StoredAssistantRecord]) -> Vec<&str> {
    records
        .iter()
        .map(|record| record.payload()["id"].as_str().unwrap())
        .collect()
}

#[tokio::test]
async fn navigation_search_preserves_literal_filters_visibility_and_stored_offsets() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = empty(&path).await;
    store
        .apply(vec![
            Mutation::Create(entity(Kind::Session, "session", json!({"metadata":{}}))),
            Mutation::Create(entity(
                Kind::Session,
                "temporary",
                json!({"metadata":{"temporary_assistant":true}}),
            )),
            Mutation::Create(entity(
                Kind::Session,
                "string-false",
                json!({"metadata":{"temporary_assistant":"false"}}),
            )),
            Mutation::Create(entity(
                Kind::Message,
                "a-retracted",
                json!({"content":"Literal %_/ TARGET", "metadata":{"retracted_at":"retained"}}),
            )),
            Mutation::Create(entity(
                Kind::Message,
                "b-literal",
                json!({"content":"Literal %_/ TARGET", "metadata":{}}),
            )),
            Mutation::Create(entity(
                Kind::Message,
                "c-wildcard",
                json!({"content":"Literal xyz/ target", "metadata":{}}),
            )),
            Mutation::Create(entity(
                Kind::Message,
                "d-unicode",
                json!({"content":"CAFÉ Straße", "metadata":{}}),
            )),
            Mutation::Create(entity(
                Kind::Message,
                "hidden-temporary",
                json!({"session_id":"temporary","content":"Literal %_/ TARGET"}),
            )),
            Mutation::Create(entity(
                Kind::Message,
                "hidden-string",
                json!({"session_id":"string-false","content":"Literal %_/ TARGET"}),
            )),
            Mutation::Create(entity(
                Kind::Message,
                "hidden-orphan",
                json!({"session_id":"missing","content":"Literal %_/ TARGET"}),
            )),
            Mutation::Create(entity(
                Kind::Message,
                "hidden-project",
                json!({"engagement_id":"other","content":"Literal %_/ TARGET"}),
            )),
            Mutation::Create(entity(
                Kind::Bookmark,
                "mark-inactive",
                json!({"message_id":"a-retracted","active":false}),
            )),
            // The Python EXISTS query scopes marks by project/message, not session.
            Mutation::Create(entity(
                Kind::Bookmark,
                "mark-active",
                json!({"session_id":"different","message_id":"b-literal","active":true}),
            )),
            Mutation::Create(entity(
                Kind::Bookmark,
                "mark-other-project",
                json!({"engagement_id":"other","message_id":"c-wildcard","active":true}),
            )),
        ])
        .await
        .unwrap();
    assert_eq!(
        ids(&store.search_messages(search("%_/")).await.unwrap().records),
        ["a-retracted", "b-literal"]
    );
    assert_eq!(
        ids(&store
            .search_messages(search("target"))
            .await
            .unwrap()
            .records),
        ["a-retracted", "b-literal", "c-wildcard"]
    );
    // SQLite's default lower()/LIKE is ASCII-only, matching the legacy query.
    assert!(
        store
            .search_messages(search("café"))
            .await
            .unwrap()
            .records
            .is_empty()
    );
    assert_eq!(
        ids(&store.search_messages(search("CAFÉ")).await.unwrap().records),
        ["d-unicode"]
    );
    let mut query = search("");
    query.limit = 1;
    let page = store.search_messages(query).await.unwrap();
    assert_eq!(ids(&page.records), ["a-retracted"]);
    assert!(page.records[0].is_replaced_message());
    assert_eq!(page.next_offset, Some(1));
    let mut query = search("");
    query.offset = 1;
    query.limit = 2;
    assert_eq!(
        ids(&store.search_messages(query).await.unwrap().records),
        ["b-literal", "c-wildcard"]
    );
    let mut query = search("");
    query.bookmarked = true;
    assert_eq!(
        ids(&store.search_messages(query).await.unwrap().records),
        ["b-literal"]
    );
    let mut query = search("");
    query.session_id = Some("temporary".into());
    assert!(
        store
            .search_messages(query)
            .await
            .unwrap()
            .records
            .is_empty()
    );
    let mut query = search("");
    query.session_id = Some("session".into());
    query.offset = 4;
    assert!(
        store
            .search_messages(query)
            .await
            .unwrap()
            .records
            .is_empty()
    );
    store.shutdown().await.unwrap();
}

async fn copies(connection: &mut SqliteConnection, kind: Kind, count: i64) {
    let p = entity(kind, "template", json!({})).into_payload();
    // Harmless bulk fixture construction; production writes still use the lane.
    sqlx::query("WITH RECURSIVE rows(n) AS (VALUES(1) UNION ALL SELECT n + 1 FROM rows WHERE n < ?) INSERT INTO entities (id, kind, engagement_id, revision, payload, chat_session_id, created_at, updated_at) SELECT printf(? || '-%05d',n), ?, 'project', 2, json_set(?, '$.id', printf(? || '-%05d',n)), 'session', '2026-09-23 12:00:00.000000', '2026-09-23 12:01:00.000000' FROM rows")
        .bind(count).bind(kind.as_str()).bind(kind.as_str()).bind(serde_json::to_string(&p).unwrap()).bind(kind.as_str())
        .execute(connection).await.unwrap();
}

#[tokio::test]
async fn navigation_history_and_bookmarks_read_past_one_thousand_with_exact_sequence_order() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = empty(&path).await;
    let mut connection = raw(&path).await;
    copies(&mut connection, Kind::Message, 1005).await;
    copies(&mut connection, Kind::Bookmark, 1005).await;
    store.apply(vec![
        Mutation::Create(entity(Kind::Message, "a-large-later", json!({"sequence":serde_json::from_str::<Value>("922337203685477580900000000002").unwrap()}))),
        Mutation::Create(entity(Kind::Message, "z-large-earlier", json!({"sequence":serde_json::from_str::<Value>("922337203685477580900000000001").unwrap()}))),
        Mutation::Create(entity(Kind::Message, "foreign-project-retained", json!({"engagement_id":"other","sequence":1,"metadata":{"retracted_at":"retained"}}))),
        Mutation::Create(entity(Kind::Bookmark, "inactive", json!({"active":false}))),
        Mutation::Create(entity(Kind::Bookmark, "other-project", json!({"engagement_id":"other"}))),
        Mutation::Create(entity(Kind::Bookmark, "other-session", json!({"session_id":"other"}))),
    ]).await.unwrap();
    let messages = store.session_messages("session").await.unwrap();
    assert_eq!(messages.len(), 1008);
    assert_eq!(
        ids(&messages[messages.len() - 2..]),
        ["z-large-earlier", "a-large-later"]
    );
    assert!(messages.iter().any(|record| record.is_replaced_message()));
    let marks = store.bookmarks("session", "project").await.unwrap();
    assert_eq!(marks.len(), 1006);
    assert!(
        marks
            .iter()
            .any(|record| record.payload()["active"] == false)
    );
    assert!(!ids(&marks).contains(&"other-project"));
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    assert_eq!(store.session_messages("session").await.unwrap().len(), 1008);
    assert_eq!(
        store.bookmarks("session", "project").await.unwrap().len(),
        1006
    );
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn navigation_complete_reads_refuse_truncation_and_oversized_search_pages() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = empty(&path).await;
    let mut connection = raw(&path).await;
    copies(&mut connection, Kind::Message, 10001).await;
    copies(&mut connection, Kind::Bookmark, 10001).await;
    assert!(matches!(
        store.session_messages("session").await,
        Err(Error::ReadLimit)
    ));
    assert!(matches!(
        store.bookmarks("session", "project").await,
        Err(Error::ReadLimit)
    ));
    sqlx::query("DELETE FROM entities")
        .execute(&mut connection)
        .await
        .unwrap();
    store
        .apply(vec![Mutation::Create(entity(
            Kind::Session,
            "session",
            json!({"metadata":{}}),
        ))])
        .await
        .unwrap();
    for id in ["large-a", "large-b"] {
        store
            .apply(vec![Mutation::Create(entity(
                Kind::Message,
                id,
                json!({"metadata":{"opaque":"x".repeat(9 * 1024 * 1024)}}),
            ))])
            .await
            .unwrap();
    }
    assert!(matches!(
        store.search_messages(search("")).await,
        Err(Error::ReadLimit)
    ));
    assert!(matches!(
        store.session_messages("session").await,
        Err(Error::ReadLimit)
    ));
    let mut query = search("");
    query.limit = 101;
    assert!(matches!(
        store.search_messages(query).await,
        Err(Error::InvalidBounds)
    ));
    let mut query = search("");
    query.offset = u64::MAX;
    assert!(matches!(
        store.search_messages(query).await,
        Err(Error::InvalidBounds)
    ));
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
}

#[tokio::test]
async fn navigation_project_lookup_requires_the_shared_project_kind() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let store = empty(&path).await;
    store
        .apply(vec![Mutation::Create(entity(
            Kind::Session,
            "session",
            json!({"metadata":{}}),
        ))])
        .await
        .unwrap();
    assert!(!store.project_exists("session").await.unwrap());
    assert!(!store.project_exists("missing").await.unwrap());
    let mut connection = raw(&path).await;
    sqlx::query("INSERT INTO entities (id, kind, engagement_id, revision, payload, chat_session_id, created_at, updated_at) VALUES ('project', 'engagements', NULL, 1, ?, NULL, '2026-09-23 12:00:00.000000', '2026-09-23 12:00:00.000000')")
        .bind(serde_json::to_string(&json!({"id":"project","name":"Navigation fixture","description":"","status":"draft","scope_policy_id":null,"client_name":null,"owner_id":null,"tags":[],"workspace_path":null,"metadata":{},"revision":1,"created_at":"2026-09-23T12:00:00Z","updated_at":"2026-09-23T12:00:00Z"})).unwrap())
        .execute(&mut connection).await.unwrap();
    assert!(store.project_exists("project").await.unwrap());
    connection.close().await.unwrap();
    store.shutdown().await.unwrap();
}
