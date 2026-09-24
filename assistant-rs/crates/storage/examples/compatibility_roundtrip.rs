//! Test driver for tests/test_assistant_storage_interop.py; never a Core entry point.
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_storage::entities::{Config, Error, Mutation, SqliteAssistantStore};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection, sqlite::SqliteConnectOptions};
use std::path::PathBuf;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args_os().skip(1);
    let path = PathBuf::from(args.next().ok_or("missing isolated database path")?);
    let mode = args.next().ok_or("missing mutate/verify mode")?;
    if args.next().is_some() || !["mutate", "verify"].iter().any(|value| mode == *value) {
        return Err("usage: compatibility_roundtrip ISOLATED_DB mutate|verify".into());
    }
    // Refuse ordinary Nebula databases. Only the Python test constructs this marker.
    let mut probe = SqliteConnection::connect_with(
        &SqliteConnectOptions::new().filename(&path).read_only(true),
    )
    .await?;
    let marker: String = sqlx::query_scalar("SELECT identity FROM assistant_interop_fixture")
        .fetch_one(&mut probe)
        .await?;
    if marker != "python-rust-python-test/v1" {
        return Err("not the interoperability fixture".into());
    }
    probe.close().await?;
    let store = SqliteAssistantStore::open(&path, Config::default()).await?;
    if mode == "mutate" {
        let fixture: Value =
            serde_json::from_str(include_str!("../../../compatibility/python-storage.json"))?;
        let mut bookmark = fixture["entities"]
            .as_array()
            .ok_or("fixture entities")?
            .iter()
            .find(|row| row["kind"] == "chat_bookmarks")
            .ok_or("fixture bookmark")?["payload"]
            .clone();
        bookmark["id"] = "rust-bookmark".into();
        bookmark["session_id"] = "fixture-chat_sessions".into();
        bookmark["message_id"] = "fixture-chat_messages".into();
        let results=store.apply(vec![
            Mutation::Patch {kind:Kind::Session,id:"fixture-chat_sessions".into(),expected_revision:2,changes:json!({"title":"  From Rust 🦀  ","metadata":{"opaque":"  retain  ","large":18446744073709551616_u128}}).as_object().unwrap().clone()},
            Mutation::Create(StoredAssistantRecord::decode(Kind::Bookmark,&serde_json::to_vec(&bookmark)?)?),
            Mutation::Patch {kind:Kind::Message,id:"fixture-chat_messages".into(),expected_revision:2,changes:json!({"session_id":"fixture-chat_sessions","metadata":{"retracted_at":"2026-09-23T12:02:00Z"}}).as_object().unwrap().clone()},
        ]).await?;
        assert_eq!(results.len(), 3);
    } else {
        let session = store.get(Kind::Session, "fixture-chat_sessions").await?;
        assert_eq!(session.payload()["title"], "Back in Python");
        assert_eq!(session.payload()["revision"], 4);
        assert_eq!(session.payload()["metadata"]["opaque"], "  retain  ");
        assert_eq!(
            session.payload()["metadata"]["large"].to_string(),
            "18446744073709551616"
        );
        assert!(matches!(
            store.get(Kind::Bookmark, "rust-bookmark").await,
            Err(Error::NotFound)
        ));
        assert!(
            store
                .get(Kind::Message, "fixture-chat_messages")
                .await?
                .is_replaced_message()
        );
    }
    store.shutdown().await?;
    Ok(())
}
