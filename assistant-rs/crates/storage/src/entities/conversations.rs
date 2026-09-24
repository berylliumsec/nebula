//! Sequential immutable dependency reads for atomic goal-conversation creation.
//! Host account lookup never holds a database connection, transaction, or reader
//! permit. Abandoned blocking work retains its admission slot until it finishes.
use super::*;
use nebula_assistant_domain::dependencies::{
    DependencyEnvironment, DependencyKind, StoredDependency,
};

pub type HomeResolver = dyn Fn(&str) -> std::result::Result<String, RecordError> + Send + Sync;
pub type LookupObserver = dyn Fn(DependencyKind, &str) + Send + Sync;

#[derive(Clone)]
pub struct ConversationDependencies {
    slots: Arc<Semaphore>,
    timeout: Duration,
    home: Arc<HomeResolver>,
    observer: Option<Arc<LookupObserver>>,
}
impl Default for ConversationDependencies {
    fn default() -> Self {
        Self::with_resolver(4, Duration::from_secs(5), Arc::new(host_home))
            .expect("constant dependency limits are valid")
    }
}
impl ConversationDependencies {
    /// Trusted host configuration. Neither resolver nor limits come from an API
    /// body; clones share the same bounded blocking admission.
    pub fn with_resolver(
        concurrency: usize,
        timeout: Duration,
        resolver: Arc<HomeResolver>,
    ) -> Result<Self> {
        if !(1..=64).contains(&concurrency)
            || timeout.is_zero()
            || timeout > Duration::from_secs(120)
        {
            return Err(Error::InvalidBounds);
        }
        Ok(Self {
            slots: Arc::new(Semaphore::new(concurrency)),
            timeout,
            home: resolver,
            observer: None,
        })
    }
    /// Passive trusted instrumentation, for recording dependency ordering.
    pub fn with_lookup_observer(mut self, observer: Arc<LookupObserver>) -> Self {
        self.observer = Some(observer);
        self
    }
}

#[derive(Default)]
pub struct DependencyReadBudget {
    rows: usize,
    bytes: usize,
}
impl DependencyReadBudget {
    fn add(&mut self, bytes: usize) -> Result<()> {
        self.bytes = self.bytes.saturating_add(bytes);
        if self.bytes > MAX_TRANSACTION_BYTES {
            return Err(Error::ReadLimit);
        }
        Ok(())
    }
}
struct Environment {
    clock: Arc<StateClock>,
    home: Arc<HomeResolver>,
}
impl DependencyEnvironment for Environment {
    fn now(&mut self) -> DateTime<Utc> {
        (self.clock)()
    }
    fn expand_user(&mut self, component: &str) -> std::result::Result<String, RecordError> {
        let home = (self.home)(component)?;
        if home.len() > MAX_RECORD_BYTES {
            return Err(RecordError::TooLarge);
        }
        Ok(home)
    }
}

#[cfg(unix)]
fn host_home(component: &str) -> std::result::Result<String, RecordError> {
    use nix::unistd::{User, getuid};
    // Match pathlib.expanduser: HOME is relevant only for bare '~'; '~name'
    // uses the host account service. Do not stat/canonicalize workspace paths.
    let directory = if component == "~" {
        match std::env::var_os("HOME") {
            Some(home) => home,
            None => User::from_uid(getuid())
                .map_err(|_| RecordError::Invariant("host account lookup failed"))?
                .ok_or(RecordError::Invariant("host account is unavailable"))?
                .dir
                .into_os_string(),
        }
    } else {
        let name = component
            .strip_prefix('~')
            .filter(|name| !name.is_empty() && !name.contains('/'))
            .ok_or(RecordError::Invariant("invalid host account component"))?;
        User::from_name(name)
            .map_err(|_| RecordError::Invariant("host account lookup failed"))?
            .ok_or(RecordError::Invariant("host account is unavailable"))?
            .dir
            .into_os_string()
    };
    let directory = directory
        .into_string()
        .map_err(|_| RecordError::Invariant("host account home is not Unicode"))?;
    Ok(normalized_host_home(directory))
}
#[cfg(unix)]
fn normalized_host_home(mut directory: String) -> String {
    // CPython 3.12 posixpath.expanduser strips trailing '/' from HOME/pw_dir
    // before appending the original suffix, with '/' as the empty fallback.
    // This resolver receives only pathlib's first component (no suffix).
    directory.truncate(directory.trim_end_matches('/').len());
    if directory.is_empty() {
        directory.push('/');
    }
    directory
}
#[cfg(not(unix))]
fn host_home(_: &str) -> std::result::Result<String, RecordError> {
    Err(RecordError::Invariant(
        "host account expansion is unsupported on this platform",
    ))
}
fn model_error(error: RecordError) -> Error {
    match error {
        RecordError::TooLarge => Error::ReadLimit,
        error @ (RecordError::Shape(_)
        | RecordError::Invariant(_)
        | RecordError::ModelValidation(_)) => Error::WrappedRecord(error),
        error => Error::Record(error),
    }
}
// Count serialized hydrated bytes without retaining a second complete copy.
struct Count(usize);
impl std::io::Write for Count {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(bytes.len());
        if self.0 > MAX_TRANSACTION_BYTES {
            return Err(std::io::Error::other(
                "dependency response exceeds byte bound",
            ));
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
impl SqliteAssistantStore {
    pub async fn conversation_dependency(
        &self,
        kind: DependencyKind,
        id: &str,
        environment: &ConversationDependencies,
        clock: Arc<StateClock>,
        budget: &mut DependencyReadBudget,
    ) -> Result<StoredDependency> {
        if !matches!(
            kind,
            DependencyKind::Engagement
                | DependencyKind::ProviderProfile
                | DependencyKind::McpServerProfile
        ) {
            return Err(Error::InvalidBounds);
        }
        if let Some(observer) = &environment.observer {
            observer(kind, id);
        }
        if id.is_empty() || id.chars().count() > 200 {
            return Err(Error::NotFound);
        }
        budget.rows = budget.rows.saturating_add(1);
        if budget.rows > 66 {
            return Err(Error::ReadLimit);
        }
        budget.add(id.len())?;
        let slot = environment
            .slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| Error::DependencyUnavailable)?;
        let row = {
            let _read = self.read_permit()?;
            sqlx::query(&format!("{SELECT_RECORD} WHERE id=? AND kind=?"))
                .bind(id)
                .bind(kind.as_str())
                .fetch_optional(&self.readers)
                .await?
                .ok_or(Error::NotFound)?
        };
        let bytes: i64 = row.try_get("payload_bytes")?;
        if bytes < 0 || bytes as u64 > MAX_RECORD_BYTES as u64 {
            return Err(Error::ReadLimit);
        }
        budget.add(bytes as usize)?;
        let mut environment_state = Environment {
            clock,
            home: environment.home.clone(),
        };
        let task = tokio::task::spawn_blocking(move || {
            let _slot = slot;
            let raw: &str = row.try_get("payload")?;
            let record = StoredDependency::decode_with_environment(
                kind,
                raw.as_bytes(),
                &mut environment_state,
            )
            .map_err(model_error)?;
            let p = record.payload();
            let project = if kind == DependencyKind::Engagement {
                p["id"].as_str()
            } else {
                None
            };
            if p["id"].as_str() != Some(row.try_get("id")?)
                || p["revision"].as_i64() != Some(row.try_get("revision")?)
                || project != row.try_get::<Option<&str>, _>("engagement_id")?
                || row.try_get::<Option<&str>, _>("chat_session_id")?.is_some()
                || sql_time(&p["created_at"])? != row.try_get::<&str, _>("created_at")?
                || sql_time(&p["updated_at"])? != row.try_get::<&str, _>("updated_at")?
            {
                return Err(Error::CorruptEnvelope);
            }
            let mut count = Count(0);
            serde_json::to_writer(&mut count, record.payload()).map_err(|_| Error::ReadLimit)?;
            Ok((record, count.0))
        });
        let (record, bytes) = tokio::time::timeout(environment.timeout, task)
            .await
            .map_err(|_| Error::DependencyTimeout)?
            .map_err(|_| Error::DependencyUnavailable)??;
        budget.add(bytes)?;
        Ok(record)
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn default_home_expansion_preserves_python_workspace_root_boundary() {
        let now = || {
            DateTime::parse_from_rfc3339("2030-01-01T12:00:00Z")
                .unwrap()
                .with_timezone(&Utc)
        };
        for (raw_home, bare, child) in [
            ("", None, Some("/sub")),
            ("/", None, Some("/sub")),
            ("//", None, Some("/sub")),
            ("///", None, Some("/sub")),
            (
                "/fixture/home///",
                Some("/fixture/home"),
                Some("/fixture/home/sub"),
            ),
            (
                "//fixture/home///",
                Some("//fixture/home"),
                Some("//fixture/home/sub"),
            ),
            ("relative///", None, None),
        ] {
            for (path, expected) in [("~", bare), ("~/sub", child)] {
                let directory = normalized_host_home(raw_home.to_owned());
                let mut environment = Environment {
                    clock: Arc::new(now),
                    home: Arc::new(move |first| {
                        assert_eq!(first, "~");
                        Ok(directory.clone())
                    }),
                };
                let input = json!({"id":"project", "revision":1,
                    "created_at":"2020-01-01T00:00:00Z", "updated_at":"2020-01-01T00:00:00Z",
                    "name":"Fixture", "workspace_path":path});
                let result = StoredDependency::decode_with_environment(
                    DependencyKind::Engagement,
                    &serde_json::to_vec(&input).unwrap(),
                    &mut environment,
                );
                match expected {
                    Some(expected) => assert_eq!(
                        result.unwrap().payload()["workspace_path"],
                        expected,
                        "{raw_home:?}, {path}"
                    ),
                    None => assert!(result.is_err(), "{raw_home:?}, {path}"),
                }
            }
        }
        // The default host result is normalized once. Injected callbacks are
        // already expanduser results and retain intentional double-root paths.
        let mut injected = Environment {
            clock: Arc::new(now),
            home: Arc::new(|_| Ok("//".into())),
        };
        assert_eq!(injected.expand_user("~").unwrap(), "//");
    }
}
