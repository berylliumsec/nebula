#[allow(dead_code)]
mod support;
use chrono::{DateTime, Utc};
use nebula_assistant_domain::{dependencies::DependencyKind as Kind, records::RecordError};
use nebula_assistant_storage::entities::{
    Config, ConversationDependencies, DependencyReadBudget, Error, SqliteAssistantStore,
};
use serde_json::{Value, json};
use sqlx::{Connection, SqliteConnection};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicUsize, Ordering},
};
use std::time::Duration;

fn now() -> DateTime<Utc> {
    DateTime::parse_from_rfc3339("2030-01-01T12:00:00Z")
        .unwrap()
        .with_timezone(&Utc)
}
fn base(id: &str, fields: Value) -> Value {
    let mut p = json!({"id":id,"revision":1,"created_at":"2020-01-01T00:00:00Z","updated_at":"2020-01-01T00:00:00Z"});
    p.as_object_mut()
        .unwrap()
        .extend(fields.as_object().unwrap().clone());
    p
}
async fn insert(db: &mut SqliteConnection, kind: Kind, id: &str, fields: Value) {
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES(?,?,?,1,?,NULL,'2020-01-01 00:00:00.000000','2020-01-01 00:00:00.000000')")
        .bind(id).bind(kind.as_str()).bind((kind==Kind::Engagement).then_some(id)).bind(base(id,fields).to_string()).execute(db).await.unwrap();
}
fn environment() -> ConversationDependencies {
    ConversationDependencies::with_resolver(
        2,
        Duration::from_secs(2),
        Arc::new(|s| {
            assert_eq!(s, "~fixture");
            Ok("/fixture/users/fixture".into())
        }),
    )
    .unwrap()
}

#[tokio::test]
async fn conversation_dependencies_preserve_source_order_envelopes_and_purity() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let mut db = support::raw(&path).await;
    insert(
        &mut db,
        Kind::Engagement,
        "project",
        json!({"name":"Project","workspace_path":"~fixture/work/../work"}),
    )
    .await;
    insert(&mut db,Kind::ProviderProfile,"provider",json!({"name":"Provider","provider_type":"openai","capability_verifications":{"model":{"model":"model","status":"verified"}}})).await;
    insert(&mut db,Kind::McpServerProfile,"mcp",json!({"name":"mcp","transport":"stdio","command":"/fixture/never-run","enabled":true,"trusted_stdio":true})).await;
    let before: Vec<String> = sqlx::query_scalar("SELECT payload FROM entities ORDER BY id")
        .fetch_all(&mut db)
        .await
        .unwrap();
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let events = Arc::new(Mutex::new(Vec::new()));
    let seen = events.clone();
    let env = environment().with_lookup_observer(Arc::new(move |kind, id| {
        seen.lock().unwrap().push((kind, id.to_owned()))
    }));
    let ticks = Arc::new(AtomicUsize::new(0));
    let count = ticks.clone();
    let clock: Arc<nebula_assistant_storage::entities::StateClock> = Arc::new(move || {
        count.fetch_add(1, Ordering::SeqCst);
        now()
    });
    let mut budget = DependencyReadBudget::default();
    let project = store
        .conversation_dependency(
            Kind::Engagement,
            "project",
            &env,
            clock.clone(),
            &mut budget,
        )
        .await
        .unwrap();
    assert_eq!(
        project.payload()["workspace_path"],
        "/fixture/users/fixture/work/../work"
    );
    let provider = store
        .conversation_dependency(
            Kind::ProviderProfile,
            "provider",
            &env,
            clock.clone(),
            &mut budget,
        )
        .await
        .unwrap();
    assert_eq!(
        provider.payload()["capability_verifications"]["model"]["checked_at"],
        "2030-01-01T12:00:00Z"
    );
    assert_eq!(ticks.load(Ordering::SeqCst), 1);
    store
        .conversation_dependency(
            Kind::McpServerProfile,
            "mcp",
            &env,
            clock.clone(),
            &mut budget,
        )
        .await
        .unwrap();
    assert_eq!(
        *events.lock().unwrap(),
        [
            (Kind::Engagement, "project".into()),
            (Kind::ProviderProfile, "provider".into()),
            (Kind::McpServerProfile, "mcp".into())
        ]
    );
    assert_eq!(
        before,
        sqlx::query_scalar::<_, String>("SELECT payload FROM entities ORDER BY id")
            .fetch_all(&mut db)
            .await
            .unwrap()
    );
    assert!(matches!(
        store
            .conversation_dependency(
                Kind::ProviderProfile,
                "project",
                &env,
                clock.clone(),
                &mut budget
            )
            .await,
        Err(Error::NotFound)
    ));
    sqlx::query("UPDATE entities SET payload=json_set(payload,'$.privacy.local_only',true,'$.is_local',false) WHERE id='provider'").execute(&mut db).await.unwrap();
    assert!(matches!(
        store
            .conversation_dependency(
                Kind::ProviderProfile,
                "provider",
                &env,
                clock.clone(),
                &mut budget
            )
            .await,
        Err(Error::WrappedRecord(_))
    ));
    sqlx::query("UPDATE entities SET revision=2 WHERE id='project'")
        .execute(&mut db)
        .await
        .unwrap();
    assert!(matches!(
        store
            .conversation_dependency(Kind::Engagement, "project", &env, clock, &mut budget)
            .await,
        Err(Error::CorruptEnvelope)
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    store.shutdown().await.unwrap();
    db.close().await.unwrap();
}

#[tokio::test]
async fn conversation_dependency_blocking_admission_survives_timeout_without_reader_leases() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let mut db = support::raw(&path).await;
    insert(
        &mut db,
        Kind::Engagement,
        "project",
        json!({"name":"Project","workspace_path":"~fixture/work"}),
    )
    .await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    // Initialize shared schema validators before the deliberately short timeout.
    store
        .conversation_dependency(
            Kind::Engagement,
            "project",
            &environment(),
            Arc::new(now),
            &mut DependencyReadBudget::default(),
        )
        .await
        .unwrap();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let release_rx = Mutex::new(release_rx);
    let started = Arc::new(AtomicUsize::new(0));
    let signal = started.clone();
    let env = ConversationDependencies::with_resolver(
        1,
        Duration::from_millis(250),
        Arc::new(move |_| {
            signal.fetch_add(1, Ordering::SeqCst);
            release_rx
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(3))
                .map_err(|_| RecordError::Invariant("fixture guard timeout"))?;
            Ok("/fixture/home".into())
        }),
    )
    .unwrap();
    assert!(matches!(
        store
            .conversation_dependency(
                Kind::Engagement,
                "project",
                &env,
                Arc::new(now),
                &mut DependencyReadBudget::default()
            )
            .await,
        Err(Error::DependencyTimeout)
    ));
    assert_eq!(started.load(Ordering::SeqCst), 1);
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    assert!(matches!(
        store
            .conversation_dependency(
                Kind::Engagement,
                "project",
                &env,
                Arc::new(now),
                &mut DependencyReadBudget::default()
            )
            .await,
        Err(Error::DependencyUnavailable)
    ));
    release_tx.send(()).unwrap();
    // Wait only for the existing closure to release its slot. Each subsequent
    // resolver call is pre-released, so this tests recovery without IO timing.
    release_tx.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            match store
                .conversation_dependency(
                    Kind::Engagement,
                    "project",
                    &env,
                    Arc::new(now),
                    &mut DependencyReadBudget::default(),
                )
                .await
            {
                Ok(_) => break,
                Err(Error::DependencyUnavailable) => tokio::task::yield_now().await,
                other => panic!("unexpected recovery result: {other:?}"),
            }
        }
    })
    .await
    .unwrap();
    assert_eq!(started.load(Ordering::SeqCst), 2);
    let cancelled_store = store.clone();
    let cancelled_env = env.clone();
    let request = tokio::spawn(async move {
        cancelled_store
            .conversation_dependency(
                Kind::Engagement,
                "project",
                &cancelled_env,
                Arc::new(now),
                &mut DependencyReadBudget::default(),
            )
            .await
    });
    tokio::time::timeout(Duration::from_secs(2), async {
        while started.load(Ordering::SeqCst) != 3 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    request.abort();
    let _ = request.await;
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    assert!(matches!(
        store
            .conversation_dependency(
                Kind::Engagement,
                "project",
                &env,
                Arc::new(now),
                &mut DependencyReadBudget::default()
            )
            .await,
        Err(Error::DependencyUnavailable)
    ));
    release_tx.send(()).unwrap();
    release_tx.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            match store
                .conversation_dependency(
                    Kind::Engagement,
                    "project",
                    &env,
                    Arc::new(now),
                    &mut DependencyReadBudget::default(),
                )
                .await
            {
                Ok(_) => break,
                Err(Error::DependencyUnavailable) => tokio::task::yield_now().await,
                other => panic!("unexpected cancellation recovery: {other:?}"),
            }
        }
    })
    .await
    .unwrap();
    assert_eq!(started.load(Ordering::SeqCst), 4);
    store.shutdown().await.unwrap();
    db.close().await.unwrap();
}

#[tokio::test]
async fn conversation_dependency_budget_bounds_retained_and_defaulted_payloads() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nebula.db");
    support::database(&path).await;
    let mut db = support::raw(&path).await;
    insert(
        &mut db,
        Kind::Engagement,
        "large",
        json!({"name":"Project","description":"x".repeat(9*1024*1024)}),
    )
    .await;
    insert(
        &mut db,
        Kind::Engagement,
        "small",
        json!({"name":"Project"}),
    )
    .await;
    let store = SqliteAssistantStore::open(&path, Config::default())
        .await
        .unwrap();
    let env = environment();
    assert!(matches!(
        store
            .conversation_dependency(
                Kind::Engagement,
                "large",
                &env,
                Arc::new(now),
                &mut DependencyReadBudget::default()
            )
            .await,
        Err(Error::ReadLimit)
    ));
    let mut budget = DependencyReadBudget::default();
    for _ in 0..66 {
        store
            .conversation_dependency(Kind::Engagement, "small", &env, Arc::new(now), &mut budget)
            .await
            .unwrap();
    }
    assert!(matches!(
        store
            .conversation_dependency(Kind::Engagement, "small", &env, Arc::new(now), &mut budget)
            .await,
        Err(Error::ReadLimit)
    ));
    assert!(matches!(
        ConversationDependencies::with_resolver(
            0,
            Duration::from_secs(1),
            Arc::new(|_| Ok("/fixture".into()))
        ),
        Err(Error::InvalidBounds)
    ));
    assert_eq!(
        store.admission().available_reads,
        Config::default().read_capacity
    );
    store.shutdown().await.unwrap();
    db.close().await.unwrap();
}
