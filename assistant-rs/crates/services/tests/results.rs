#[path = "../../storage/tests/support/mod.rs"]
#[allow(dead_code)]
mod support;

use chrono::{DateTime, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_services::{
    AssistantRecords, Error,
    artifact_preview::{ArtifactPreview, UNAVAILABLE_PREVIEW},
    results::ResultsQuery,
};
use nebula_assistant_storage::entities::{
    Config, Error as StorageError, Mutation, SqliteAssistantStore,
};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection};
use std::{path::Path, time::Duration};

fn oracle() -> Value {
    serde_json::from_str(include_str!("../../../compatibility/python-results.json")).unwrap()
}
async fn insert(raw: &mut SqliteConnection, kind: &str, p: &Value) {
    let time = |field: &str| {
        DateTime::parse_from_rfc3339(p[field].as_str().unwrap())
            .unwrap()
            .with_timezone(&Utc)
            .format("%Y-%m-%d %H:%M:%S%.6f")
            .to_string()
    };
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
        .bind(p["id"].as_str()).bind(kind).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64())
        .bind(serde_json::to_string(p).unwrap()).bind(p["chat_session_id"].as_str()).bind(time("created_at")).bind(time("updated_at"))
        .execute(raw).await.unwrap();
}
async fn setup(path: &Path, root: &Path) -> (SqliteAssistantStore, AssistantRecords) {
    support::database(path).await;
    let fixture = oracle();
    let mut raw = support::raw(path).await;
    sqlx::query("DELETE FROM entities")
        .execute(&mut raw)
        .await
        .unwrap();
    sqlx::query("DELETE FROM search_documents")
        .execute(&mut raw)
        .await
        .unwrap();
    for row in fixture["projects"]
        .as_array()
        .unwrap()
        .iter()
        .chain(fixture["dependency_records"].as_array().unwrap())
    {
        insert(&mut raw, row["kind"].as_str().unwrap(), &row["payload"]).await;
    }
    raw.close().await.unwrap();
    let store = SqliteAssistantStore::open(path, Config::default())
        .await
        .unwrap();
    for batch in fixture["initial_records"].as_array().unwrap().chunks(64) {
        store
            .apply(
                batch
                    .iter()
                    .map(|row| {
                        Mutation::Create(
                            StoredAssistantRecord::decode(
                                Kind::try_from(row["kind"].as_str().unwrap()).unwrap(),
                                &serde_json::to_vec(&row["payload"]).unwrap(),
                            )
                            .unwrap(),
                        )
                    })
                    .collect(),
            )
            .await
            .unwrap();
    }
    std::fs::create_dir_all(root).unwrap();
    for blob in fixture["artifact_blobs"].as_array().unwrap() {
        let path = root.join(blob["storage_path"].as_str().unwrap());
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let bytes: Vec<_> = blob["hex"]
            .as_str()
            .unwrap()
            .as_bytes()
            .chunks_exact(2)
            .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
            .collect();
        std::fs::write(path, bytes).unwrap();
    }
    (store.clone(), AssistantRecords::new(store))
}
async fn retained_rows(path: &Path) -> Vec<(String, String, i64, String, String, String)> {
    let mut raw = support::raw(path).await;
    let rows = sqlx::query_as(
        "SELECT id,kind,revision,payload,created_at,updated_at FROM entities ORDER BY id",
    )
    .fetch_all(&mut raw)
    .await
    .unwrap();
    raw.close().await.unwrap();
    rows
}
async fn projection(services: &AssistantRecords, case: &Value, preview: &ArtifactPreview) -> Value {
    let step = &case["service"];
    let session = step["session_id"].as_str().unwrap();
    match step["kind"].as_str().unwrap() {
        "results" => services
            .results(
                session,
                ResultsQuery {
                    offset: step["offset"].as_u64().unwrap(),
                    limit: step["limit"].as_u64().unwrap() as u32,
                },
                (case["artifacts_enabled"] != false).then_some(preview),
            )
            .await
            .unwrap(),
        "context_sources" => services
            .context_sources(session, step["offset"].as_u64().unwrap())
            .await
            .unwrap(),
        other => panic!("Unknown projection {other}"),
    }
}

#[tokio::test]
async fn python_results_oracle_preserves_projection_order_and_retained_records() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let root = temp.path().join("artifacts");
    let (store, services) = setup(&path, &root).await;
    let preview = ArtifactPreview::new(&root, 4, Duration::from_secs(3)).unwrap();
    let before = retained_rows(&path).await;
    let mut raw = support::raw(&path).await;
    sqlx::query("INSERT INTO session_projections(session_id,revision,digest) VALUES ('results',47,'unchanged-results')")
        .execute(&mut raw).await.unwrap();
    raw.close().await.unwrap();
    let fixture = oracle();
    let cases: Vec<_> = fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|case| case["expected"]["status"] == 200 && case["service"].is_object())
        .collect();
    assert!(
        cases.len() >= 60,
        "The captured oracle must exercise complete projections"
    );
    for case in &cases {
        assert_eq!(
            projection(&services, case, &preview).await,
            case["expected"]["body"],
            "{}",
            case["name"]
        );
    }
    assert_eq!(
        retained_rows(&path).await,
        before,
        "Read projections must retain every source byte and revision"
    );
    let mut raw = support::raw(&path).await;
    let watermark: (i64, String) = sqlx::query_as(
        "SELECT revision,digest FROM session_projections WHERE session_id='results'",
    )
    .fetch_one(&mut raw)
    .await
    .unwrap();
    assert_eq!(watermark, (47, "unchanged-results".into()));
    raw.close().await.unwrap();
    store.shutdown().await.unwrap();
    drop(services);
    let reopened = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let services = AssistantRecords::new(reopened.clone());
    for kind in ["results", "context_sources"] {
        let case = cases
            .iter()
            .find(|case| case["service"]["kind"] == kind)
            .unwrap();
        assert_eq!(
            projection(&services, case, &preview).await,
            case["expected"]["body"]
        );
    }
    assert_eq!(retained_rows(&path).await, before);
    reopened.shutdown().await.unwrap();
}

fn message(id: &str, sequence: usize, content: &str, metadata: Value) -> StoredAssistantRecord {
    let mut p = support::payload(Kind::Message);
    p["id"] = id.into();
    p["engagement_id"] = "project".into();
    p["session_id"] = "empty".into();
    p["sequence"] = sequence.into();
    p["role"] = "assistant".into();
    p["content"] = content.into();
    p["content_blocks"] = json!([]);
    p["citations"] = json!([]);
    p["metadata"] = metadata;
    StoredAssistantRecord::decode(Kind::Message, &serde_json::to_vec(&p).unwrap()).unwrap()
}

#[tokio::test]
async fn expanded_result_limits_preserve_retained_sources() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    let root = temp.path().join("artifacts");
    let (store, services) = setup(&path, &root).await;
    let fences = "```\nx\n```".repeat(10_001);
    store
        .apply(vec![Mutation::Create(message(
            "many-fences",
            1,
            &fences,
            json!({}),
        ))])
        .await
        .unwrap();
    assert!(matches!(
        services
            .results(
                "empty",
                ResultsQuery {
                    offset: 0,
                    limit: 1
                },
                None
            )
            .await,
        Err(Error::Storage(StorageError::ReadLimit))
    ));
    assert_eq!(
        store
            .get(Kind::Message, "many-fences")
            .await
            .unwrap()
            .payload()["content"],
        fences
    );

    let digest = "bc".repeat(32);
    let relative = format!("sha256/bc/bc/{digest}");
    let blob = root.join(&relative);
    std::fs::create_dir_all(blob.parent().unwrap()).unwrap();
    std::fs::write(&blob, vec![0xff; 8192]).unwrap();
    let preview = ArtifactPreview::new(&root, 1, Duration::from_secs(3)).unwrap();
    let fixture = oracle();
    let template = &fixture["dependency_records"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["kind"] == "artifacts")
        .unwrap()["payload"];
    let mut raw = support::raw(&path).await;
    for index in 0..24 {
        let mut artifact = template.clone();
        artifact["id"] = format!("expanded-diff-{index}").into();
        artifact["sha256"] = digest.clone().into();
        artifact["storage_path"] = relative.clone().into();
        artifact["size"] = 8192.into();
        artifact["metadata"] = json!({"harness_turn_id":"expanded-diffs"});
        insert(&mut raw, "artifacts", &artifact).await;
    }
    raw.close().await.unwrap();
    store
        .apply(
            (0..32)
                .map(|index| {
                    Mutation::Create(message(
                        &format!("expanded-{index:02}"),
                        index + 2,
                        "",
                        json!({"harness_turn_id":"expanded-diffs"}),
                    ))
                })
                .collect(),
        )
        .await
        .unwrap();
    let before = retained_rows(&path).await;
    assert!(
        matches!(
            services
                .results(
                    "empty",
                    ResultsQuery {
                        offset: 1,
                        limit: 32
                    },
                    Some(&preview)
                )
                .await,
            Err(Error::Storage(StorageError::ReadLimit))
        ),
        "UTF-8 expansion must hit the response byte bound before returning partial data"
    );
    let smaller = services
        .results(
            "empty",
            ResultsQuery {
                offset: 1,
                limit: 1,
            },
            Some(&preview),
        )
        .await
        .unwrap();
    assert_eq!(smaller["items"].as_array().unwrap().len(), 24);
    assert_eq!(
        smaller["items"][0]["text"].as_str().unwrap().len(),
        8192 * 3
    );
    assert_eq!(smaller["next_offset"], 2);
    assert_eq!(retained_rows(&path).await, before);
    store.shutdown().await.unwrap();
}

fn artifact(digest: &str) -> Value {
    json!({"sha256":digest,"storage_path":format!("sha256/{}/{}/{digest}",&digest[..2],&digest[2..4])})
}
fn write_blob(root: &Path, artifact: &Value, bytes: &[u8]) -> std::path::PathBuf {
    let path = root.join(artifact["storage_path"].as_str().unwrap());
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(&path, bytes).unwrap();
    path
}

#[cfg(unix)]
#[tokio::test]
async fn artifact_preview_enforces_digest_paths_and_bounded_regular_files() {
    use std::os::unix::fs::symlink;
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("artifacts");
    let valid = artifact(&"ac".repeat(32));
    let content = format!("{}€ignored", "x".repeat(8191));
    let path = write_blob(&root, &valid, content.as_bytes());
    let preview = ArtifactPreview::new(&root, 1, Duration::from_secs(3)).unwrap();
    assert_eq!(
        preview.read_diff(&valid).await.unwrap(),
        format!("{}�", "x".repeat(8191))
    );
    let mut absolute = valid.clone();
    absolute["storage_path"] = path.to_str().unwrap().into();
    assert_eq!(
        preview.read_diff(&absolute).await.unwrap(),
        preview.read_diff(&valid).await.unwrap()
    );
    for invalid_path in ["../outside", "sha256/aa/aa/wrong", "/not-the-artifact-root"] {
        let mut invalid = valid.clone();
        invalid["storage_path"] = invalid_path.into();
        assert_eq!(
            preview.read_diff(&invalid).await.unwrap(),
            UNAVAILABLE_PREVIEW
        );
    }
    assert_eq!(
        preview
            .read_diff(&artifact(&"ad".repeat(32)))
            .await
            .unwrap(),
        UNAVAILABLE_PREVIEW
    );
    std::fs::remove_file(&path).unwrap();
    let external = temp.path().join("outside-fixture");
    std::fs::write(&external, "not a retained artifact").unwrap();
    symlink(&external, &path).unwrap();
    assert_eq!(
        preview.read_diff(&valid).await.unwrap(),
        UNAVAILABLE_PREVIEW
    );
    std::fs::remove_file(&path).unwrap();
    std::fs::create_dir(&path).unwrap();
    assert_eq!(
        preview.read_diff(&valid).await.unwrap(),
        UNAVAILABLE_PREVIEW
    );
    std::fs::remove_dir(&path).unwrap();
    rustix::fs::mkfifoat(
        rustix::fs::CWD,
        &path,
        rustix::fs::Mode::RUSR | rustix::fs::Mode::WUSR,
    )
    .unwrap();
    assert_eq!(
        preview.read_diff(&valid).await.unwrap(),
        UNAVAILABLE_PREVIEW
    );
    std::fs::remove_file(&path).unwrap();
    std::fs::remove_dir(path.parent().unwrap()).unwrap();
    symlink(temp.path(), path.parent().unwrap()).unwrap();
    assert_eq!(
        preview.read_diff(&valid).await.unwrap(),
        UNAVAILABLE_PREVIEW
    );
    for (limit, deadline) in [
        (0, Duration::from_secs(1)),
        (65, Duration::from_secs(1)),
        (1, Duration::ZERO),
        (1, Duration::from_secs(121)),
    ] {
        assert_eq!(
            ArtifactPreview::new(&root, limit, deadline)
                .err()
                .unwrap()
                .kind(),
            std::io::ErrorKind::InvalidInput
        );
    }
}

#[cfg(unix)]
#[test]
fn preview_timeout_retains_blocking_admission_until_work_finishes() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .max_blocking_threads(1)
        .build()
        .unwrap();
    let temp = tempfile::tempdir().unwrap();
    let retained = artifact(&"ae".repeat(32));
    write_blob(temp.path(), &retained, b"retained bytes");
    let preview = ArtifactPreview::new(temp.path(), 1, Duration::from_millis(250)).unwrap();
    runtime.block_on(async {
        let (release, released) = std::sync::mpsc::channel();
        let (started, waiting) = tokio::sync::oneshot::channel();
        let blocker = tokio::task::spawn_blocking(move || {
            started.send(()).unwrap();
            released.recv_timeout(Duration::from_secs(3)).unwrap();
        });
        waiting.await.unwrap();
        assert!(matches!(preview.read_diff(&retained).await, Err(Error::Timeout(_))));
        assert!(matches!(preview.read_diff(&retained).await, Err(Error::Unavailable(_))),
            "Timed-out queued reads must retain their admission permit until the blocking task completes");
        release.send(()).unwrap();
        blocker.await.unwrap();
        tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                match preview.read_diff(&retained).await {
                    Ok(value) => { assert_eq!(value, "retained bytes"); break; }
                    Err(Error::Unavailable(_)) => tokio::time::sleep(Duration::from_millis(1)).await,
                    other => panic!("Unexpected result after releasing the blocking worker: {other:?}"),
                }
            }
        }).await.unwrap();
    });
}
