#[path = "../../storage/tests/support/mod.rs"]
#[allow(dead_code)]
mod support;
use nebula_assistant_services::{
    AssistantRecords, Error,
    decision_preparation::{DecisionSnapshotEntry, decision_instructions},
    execution_context::MAX_PREPARATION_BYTES,
};
use nebula_assistant_storage::entities::{Config, Error as StorageError, SqliteAssistantStore};
use serde_json::{Value, json};
use sqlx::{Connection, Row, SqliteConnection};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-decision-preparation.json"
    ))
    .unwrap()
}
async fn seed(raw: &mut SqliteConnection, rows: &Value) {
    sqlx::query("DELETE FROM entities")
        .execute(&mut *raw)
        .await
        .unwrap();
    for row in rows.as_array().unwrap() {
        sqlx::query("INSERT INTO entities(id,kind,engagement_id,chat_session_id,revision,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?,?)")
            .bind(row["id"].as_str()).bind(row["kind"].as_str()).bind(row["engagement_id"].as_str())
            .bind(row["chat_session_id"].as_str()).bind(row["revision"].as_i64())
            .bind(row["created_at"].as_str()).bind(row["updated_at"].as_str()).bind(row["payload"].as_str())
            .execute(&mut *raw).await.unwrap();
    }
}
async fn snapshot(raw: &mut SqliteConnection) -> Value {
    let entities: Vec<_> = sqlx::query("SELECT * FROM entities ORDER BY id")
        .fetch_all(&mut *raw).await.unwrap().into_iter().map(|r| json!({
            "id":r.get::<String,_>("id"),"kind":r.get::<String,_>("kind"),
            "engagement_id":r.get::<Option<String>,_>("engagement_id"),"chat_session_id":r.get::<Option<String>,_>("chat_session_id"),
            "revision":r.get::<i64,_>("revision"),"created_at":r.get::<String,_>("created_at"),"updated_at":r.get::<String,_>("updated_at"),
            "automation_run_id":r.get::<Option<String>,_>("automation_run_id"),"automation_session_id":r.get::<Option<String>,_>("automation_session_id"),
            "automation_status":r.get::<Option<String>,_>("automation_status"),"automation_expires_at":r.get::<Option<String>,_>("automation_expires_at"),
            "payload":r.get::<String,_>("payload")
        })).collect();
    let search: Vec<_> = sqlx::query("SELECT json_object('id',id,'project_id',project_id,'resource_kind',resource_kind,'resource_id',resource_id,'revision',revision,'label',label,'description',description,'breadcrumb',breadcrumb,'content',content,'updated_at',updated_at) AS body FROM search_documents ORDER BY id")
        .fetch_all(&mut *raw).await.unwrap().into_iter().map(|r| r.get::<String,_>("body")).collect();
    json!({"entities":entities,"search":search})
}

#[tokio::test]
async fn python_decision_preparation_snapshot_and_prompt_match_source() {
    let fixture = fixture();
    assert_eq!(fixture["cases"].as_array().unwrap().len(), 25);
    let temporary = tempfile::tempdir().unwrap();
    let path = temporary.path().join("fixture.db");
    support::database(&path).await;
    let mut raw = support::raw(&path).await;
    for reopened in [false, true] {
        let store = SqliteAssistantStore::open(&path, Config::default())
            .await
            .unwrap();
        let service = AssistantRecords::new(store.clone());
        for case in fixture["cases"].as_array().unwrap() {
            seed(
                &mut raw,
                &fixture["datasets"][case["dataset"].as_str().unwrap()],
            )
            .await;
            let before = snapshot(&mut raw).await;
            let outcome = service
                .preparation_decision_snapshot(
                    case["session_id"].as_str(),
                    case["project_id"].as_str(),
                )
                .await;
            let actual = match outcome {
                Ok(entries) => {
                    json!({"accepted":true,"snapshot":serde_json::to_value(&entries).unwrap(),"instructions":decision_instructions(&entries).unwrap()})
                }
                Err(Error::Conflict(detail)) => {
                    json!({"accepted":false,"error":{"kind":"ConflictError","detail":detail}})
                }
                Err(Error::RetainedModelValidation(report)) => {
                    assert!(!format!("{report:?}").contains("invalid-kind"));
                    json!({"accepted":false,"error":{"kind":"ValidationError","errors":serde_json::to_value(report).unwrap()}})
                }
                Err(error) => panic!("{} reopen={reopened}: {error:?}", case["name"]),
            };
            let mut expected = case["expected"].clone();
            if let Some(error) = expected.get_mut("error").and_then(Value::as_object_mut) {
                error.remove("exception_preview"); // Display-only source evidence; exact issues are compared.
            }
            assert_eq!(actual, expected, "{} reopen={reopened}", case["name"]);
            assert_eq!(
                snapshot(&mut raw).await,
                before,
                "{} changed retained rows",
                case["name"]
            );
        }
        drop(service);
        store.shutdown().await.unwrap();
        drop(store);
    }
    raw.close().await.unwrap();
}

#[tokio::test]
async fn decision_preparation_rejects_corrupt_envelopes_and_bounded_reads() {
    let fixture = fixture();
    let temporary = tempfile::tempdir().unwrap();
    let path = temporary.path().join("fixture.db");
    support::database(&path).await;
    let mut raw = support::raw(&path).await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let service = AssistantRecords::new(store.clone());
    let boundary = &fixture["strict_envelope_boundary_observations"][0];
    assert_eq!(boundary["expected"]["accepted"], true);
    seed(
        &mut raw,
        &fixture["datasets"][boundary["dataset"].as_str().unwrap()],
    )
    .await;
    let before = snapshot(&mut raw).await;
    assert!(matches!(
        service
            .preparation_decision_snapshot(None, Some("project"))
            .await,
        Err(Error::Storage(StorageError::CorruptEnvelope))
    ));
    assert_eq!(snapshot(&mut raw).await, before);
    // The source bypasses lookup for no project even if selected rows are corrupt.
    assert!(
        service
            .preparation_decision_snapshot(Some(&"x".repeat(5000)), None)
            .await
            .unwrap()
            .is_empty()
    );
    assert!(matches!(
        store
            .preparation_decisions(Some(&"x".repeat(5000)), "project")
            .await,
        Err(StorageError::InvalidBounds)
    ));

    let one = fixture["datasets"]["count100"][0].clone();
    seed(&mut raw, &json!([one])).await;
    sqlx::query("WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM numbers WHERE n<10000) INSERT INTO entities(id,kind,engagement_id,chat_session_id,revision,created_at,updated_at,payload) SELECT 'bounded-'||n,'chat_decisions','project',NULL,1,created_at,updated_at,json_set(payload,'$.id','bounded-'||n) FROM numbers CROSS JOIN entities WHERE id='count-000'")
        .execute(&mut raw).await.unwrap();
    assert!(matches!(
        store.preparation_decisions(None, "project").await,
        Err(StorageError::ReadLimit)
    ));
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM entities")
            .fetch_one(&mut raw)
            .await
            .unwrap(),
        10001
    );

    // Even discarded history contributes to the read's aggregate byte budget.
    let mut rows = Vec::new();
    for i in 0..17 {
        let mut row = one.clone();
        let id = format!("bytes-{i:02}");
        row["id"] = id.clone().into();
        let mut payload: Value = serde_json::from_str(row["payload"].as_str().unwrap()).unwrap();
        payload["id"] = id.into();
        payload["history"] = json!([{"opaque":"x".repeat(1024*1024)}]);
        row["payload"] = serde_json::to_string(&payload).unwrap().into();
        rows.push(row);
    }
    seed(&mut raw, &rows.into()).await;
    assert!(matches!(
        store.preparation_decisions(None, "project").await,
        Err(StorageError::ReadLimit)
    ));
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM entities")
            .fetch_one(&mut raw)
            .await
            .unwrap(),
        17
    );
    drop(service);
    store.shutdown().await.unwrap();
    raw.close().await.unwrap();
}

#[test]
fn decision_instruction_encoding_preserves_order_unicode_and_bounded_output() {
    let fixture = fixture();
    assert_eq!(fixture["format_vectors"].as_array().unwrap().len(), 3);
    for vector in fixture["format_vectors"].as_array().unwrap() {
        let entries: Vec<DecisionSnapshotEntry> =
            serde_json::from_value(vector["snapshot"].clone()).unwrap();
        assert_eq!(decision_instructions(&entries).unwrap(), vector["expected"]);
        for entry in &entries {
            assert!(!format!("{entry:?}").contains("quoted"));
            assert!(!format!("{entry:?}").contains("origin"));
        }
    }
    let mut value = fixture["format_vectors"][0]["snapshot"][0].clone();
    value["source_session_id"] = "x".repeat(MAX_PREPARATION_BYTES).into();
    let entry: DecisionSnapshotEntry = serde_json::from_value(value).unwrap();
    assert!(matches!(
        decision_instructions(&[entry]),
        Err(Error::Invalid(_))
    ));
    let entry: DecisionSnapshotEntry =
        serde_json::from_value(fixture["format_vectors"][0]["snapshot"][0].clone()).unwrap();
    assert!(matches!(
        decision_instructions(&vec![entry; 101]),
        Err(Error::Conflict(_))
    ));
}
