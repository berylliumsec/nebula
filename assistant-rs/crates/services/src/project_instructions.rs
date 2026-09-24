//! Per-turn project instructions from a trusted workspace resolver. Reads never
//! interpret the file, mutate the workspace, or authorize provider/tool dispatch.
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    fmt, fs,
    io::{self, Read},
    path::{Component, Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

pub const MAX_BYTES: usize = 64 * 1024;
const RETAINED_CREDIT: usize = 1024 * 1024;
const FILENAME: &str = "AGENTS.md";
const OUTSIDE: &str = "AGENTS.md must stay inside the project workspace or link to an AGENTS.md in one of its parent folders";
const NOT_REGULAR: &str = "AGENTS.md must be a regular file";

#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub blocking_reads: usize,
    pub retained_bytes: usize,
    pub timeout: Duration,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            blocking_reads: 8,
            retained_bytes: 16 * RETAINED_CREDIT,
            timeout: Duration::from_secs(5),
        }
    }
}

pub enum Error {
    Source(&'static str),
    Io(io::Error),
    Capacity,
    Boundary,
    Timeout,
    Unavailable,
    Unsupported,
}
impl fmt::Debug for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Source(detail) => f
                .debug_tuple("ProjectInstructionsError")
                .field(detail)
                .finish(),
            Self::Io(e) => f
                .debug_tuple("ProjectInstructionsIo")
                .field(&e.kind())
                .finish(),
            _ => f.write_str(match self {
                Self::Capacity => "ProjectInstructionsCapacity",
                Self::Boundary => "ProjectInstructionsBoundary",
                Self::Timeout => "ProjectInstructionsTimeout",
                Self::Unavailable => "ProjectInstructionsUnavailable",
                Self::Unsupported => "ProjectInstructionsUnsupported",
                _ => unreachable!(),
            }),
        }
    }
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if let Self::Source(detail) = self {
            write!(f, "AGENTS.md could not be used: {detail}")
        } else {
            write!(f, "AGENTS.md could not be used ({self:?})")
        }
    }
}
impl std::error::Error for Error {}
impl From<io::Error> for Error {
    fn from(value: io::Error) -> Self {
        Self::Io(value)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Receipt {
    pub path: &'static str,
    pub sha256: String,
    pub size_bytes: u64,
    pub truncated: bool,
}
/// The returned Arc retains its shared byte reservation through its last owner.
/// Content and prompt are borrowed; diagnostics expose neither.
pub struct Instructions {
    receipt: Receipt,
    content: String,
    prompt: String,
    _credit: OwnedSemaphorePermit,
}
impl Instructions {
    pub fn receipt(&self) -> &Receipt {
        &self.receipt
    }
    pub fn content(&self) -> &str {
        &self.content
    }
    pub fn prompt(&self) -> &str {
        &self.prompt
    }
}
impl fmt::Debug for Instructions {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ProjectInstructions")
            .field("receipt", &self.receipt)
            .finish_non_exhaustive()
    }
}

#[derive(Clone)]
pub struct Reader {
    jobs: Arc<Semaphore>,
    bytes: Arc<Semaphore>,
    timeout: Duration,
}
impl Reader {
    pub fn new(limits: Limits) -> Result<Self, Error> {
        if !(1..=64).contains(&limits.blocking_reads)
            || !(RETAINED_CREDIT..=64 * RETAINED_CREDIT).contains(&limits.retained_bytes)
            || limits.timeout.is_zero()
            || limits.timeout > Duration::from_secs(120)
        {
            return Err(Error::Boundary);
        }
        Ok(Self {
            jobs: Arc::new(Semaphore::new(limits.blocking_reads)),
            bytes: Arc::new(Semaphore::new(limits.retained_bytes)),
            timeout: limits.timeout,
        })
    }
    /// `workspace` is an absolute host-authorized path, never a path from the
    /// completion body. Call once for every turn; there is no content cache.
    /// Caller cancellation/deadline does not release credits from a blocked OS
    /// operation. Such work retains its slot until the blocking task returns.
    pub async fn read(&self, workspace: PathBuf) -> Result<Option<Arc<Instructions>>, Error> {
        if !workspace.is_absolute() || workspace.as_os_str().len() > 4096 {
            return Err(Error::Boundary);
        }
        self.run(move |credit| load(&workspace, credit)).await
    }

    async fn run<F>(&self, load: F) -> Result<Option<Arc<Instructions>>, Error>
    where
        F: FnOnce(OwnedSemaphorePermit) -> Result<Option<Arc<Instructions>>, Error>
            + Send
            + 'static,
    {
        let slot = self
            .jobs
            .clone()
            .try_acquire_owned()
            .map_err(|_| Error::Capacity)?;
        let credit = self
            .bytes
            .clone()
            .try_acquire_many_owned(RETAINED_CREDIT as u32)
            .map_err(|_| Error::Capacity)?;
        let task = tokio::task::spawn_blocking(move || {
            let _slot = slot;
            load(credit)
        });
        tokio::time::timeout(self.timeout, task)
            .await
            .map_err(|_| Error::Timeout)?
            .map_err(|_| Error::Unavailable)?
    }
}

// Python Path.resolve(strict=False) resolves existing symlinks even when the
// final target is missing. Preserve the custom outside/not-regular distinction.
// Bounds cap link and component work; failed/looping paths never reach a read.
fn resolve(path: &Path, links: &mut usize, steps: &mut usize) -> Result<PathBuf, Error> {
    let mut result = PathBuf::from("/");
    for part in path.components() {
        *steps = steps.checked_add(1).ok_or(Error::Boundary)?;
        if *steps > 4096 {
            return Err(Error::Boundary);
        }
        match part {
            Component::RootDir | Component::CurDir => {}
            Component::ParentDir => {
                result.pop();
            }
            Component::Normal(value) => {
                result.push(value);
                match fs::symlink_metadata(&result) {
                    Ok(metadata) if metadata.file_type().is_symlink() => {
                        *links += 1;
                        if *links > 40 {
                            return Err(Error::Boundary);
                        }
                        let target = fs::read_link(&result)?;
                        result.pop();
                        result = resolve(&result.join(target), links, steps)?;
                    }
                    Ok(_) => {}
                    // Path.resolve(strict=False) treats an lstat OSError as
                    // an unresolved normal component, including permission.
                    Err(_) => {}
                }
            }
            _ => return Err(Error::Unsupported),
        }
        if result.as_os_str().len() > 16384 {
            return Err(Error::Boundary);
        }
    }
    Ok(result)
}

fn load(
    workspace: &Path,
    credit: OwnedSemaphorePermit,
) -> Result<Option<Arc<Instructions>>, Error> {
    let (mut links, mut steps) = (0, 0);
    let root = resolve(workspace, &mut links, &mut steps)?;
    let candidate = root.join(FILENAME);
    // Python os.path.lexists returns false for every lstat OSError. Once an
    // existing candidate is found, resolution/open failures remain errors.
    if fs::symlink_metadata(&candidate).is_err() {
        return Ok(None);
    }
    let resolved = resolve(&candidate, &mut links, &mut steps)?;
    let shared_parent = resolved.file_name().is_some_and(|n| n == FILENAME)
        && resolved.parent().is_some_and(|p| root.starts_with(p));
    if !resolved.starts_with(&root) && !shared_parent {
        return Err(Error::Source(OUTSIDE));
    }
    if !resolved.is_file() {
        return Err(Error::Source(NOT_REGULAR));
    }
    let anchor = if resolved.starts_with(&root) {
        &root
    } else {
        resolved.parent().ok_or(Error::Boundary)?
    };
    let file = open_verified(
        anchor,
        resolved.strip_prefix(anchor).map_err(|_| Error::Boundary)?,
    )?;
    let metadata = file.metadata()?;
    if !metadata.is_file() {
        return Err(Error::Source(NOT_REGULAR));
    }
    let mut raw = Vec::with_capacity(MAX_BYTES + 1);
    file.take((MAX_BYTES + 1) as u64).read_to_end(&mut raw)?;
    let truncated = raw.len() > MAX_BYTES;
    raw.truncate(MAX_BYTES);
    let mut content = String::from_utf8_lossy(&raw).into_owned();
    if truncated && content.ends_with('\u{fffd}') {
        // Source removes exactly one trailing replacement, even if it was
        // literal in the file. The receipt hashes the retained raw byte prefix.
        content.pop();
    }
    // ProjectInstructions inherits NebulaModel's Pydantic whitespace stripping.
    // This differs from Python str.strip for U+001C..U+001F, which are removed
    // only by the later prompt-empty check. Keep the hash on the raw prefix.
    let start = content.len() - content.trim_start().len();
    let end = content.trim_end().len();
    if start >= end {
        content.clear();
    } else {
        content.truncate(end);
        content.drain(..start);
    }
    let receipt = Receipt {
        path: FILENAME,
        sha256: format!("{:x}", Sha256::digest(&raw)),
        size_bytes: metadata.len(),
        truncated,
    };
    let prompt = prompt(&receipt, &content)?;
    if raw.capacity() + content.capacity() + prompt.capacity() + 4096 > RETAINED_CREDIT {
        return Err(Error::Capacity);
    }
    Ok(Some(Arc::new(Instructions {
        receipt,
        content,
        prompt,
        _credit: credit,
    })))
}

fn prompt(receipt: &Receipt, content: &str) -> Result<String, Error> {
    if content
        .chars()
        .all(|c| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
    {
        return Ok(String::new());
    }
    let note = if receipt.truncated {
        format!(
            " Only the first 64 KiB of {} bytes are included.",
            receipt.size_bytes
        )
    } else {
        String::new()
    };
    let text = serde_json::to_string(content).map_err(|_| Error::Boundary)?;
    Ok(format!(
        "\n\nProject instructions (AGENTS.md at the project root).{note}\n{{\"path\": \"AGENTS.md\", \"content\": {text}}}"
    ))
}

#[cfg(unix)]
fn open_verified(anchor: &Path, relative: &Path) -> Result<fs::File, Error> {
    use rustix::fs::{Mode, OFlags, open, openat};
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let mut directory = fs::File::from(open("/", flags, Mode::empty()).map_err(io::Error::from)?);
    for part in anchor
        .components()
        .filter(|p| !matches!(p, Component::RootDir))
    {
        if !matches!(part, Component::Normal(_)) {
            return Err(Error::Boundary);
        }
        directory = fs::File::from(
            openat(&directory, part.as_os_str(), flags, Mode::empty()).map_err(io::Error::from)?,
        );
    }
    let parts = relative.components().collect::<Vec<_>>();
    if parts.is_empty() || parts.iter().any(|p| !matches!(p, Component::Normal(_))) {
        return Err(Error::Boundary);
    }
    for part in &parts[..parts.len() - 1] {
        directory = fs::File::from(
            openat(&directory, part.as_os_str(), flags, Mode::empty()).map_err(io::Error::from)?,
        );
    }
    Ok(fs::File::from(
        openat(
            &directory,
            parts.last().unwrap().as_os_str(),
            OFlags::RDONLY | OFlags::NONBLOCK | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(io::Error::from)?,
    ))
}
#[cfg(not(unix))]
fn open_verified(_anchor: &Path, _relative: &Path) -> Result<fs::File, Error> {
    Err(Error::Unsupported)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn verified_project_read_refuses_substituted_symlink_components() {
        let base = tempfile::tempdir().unwrap();
        let root = base.path().join("workspace");
        let outside = base.path().join("other");
        fs::create_dir_all(root.join("sub")).unwrap();
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("note"), b"not authorized").unwrap();
        // A component replaced after resolution must not be followed at open.
        fs::remove_dir(root.join("sub")).unwrap();
        std::os::unix::fs::symlink(&outside, root.join("sub")).unwrap();
        assert!(open_verified(&root, Path::new("sub/note")).is_err());
        std::os::unix::fs::symlink(outside.join("note"), root.join("AGENTS.md")).unwrap();
        assert!(open_verified(&root, Path::new("AGENTS.md")).is_err());
        fs::remove_file(root.join("sub")).unwrap();
        fs::remove_file(root.join("AGENTS.md")).unwrap();
        fs::remove_dir(&root).unwrap();
        std::os::unix::fs::symlink(&outside, &root).unwrap();
        assert!(open_verified(&root, Path::new("note")).is_err());
    }

    #[tokio::test]
    async fn project_read_timeout_and_caller_drop_keep_blocking_ownership() {
        for cancel_caller in [false, true] {
            let reader = Reader::new(Limits {
                blocking_reads: 1,
                retained_bytes: RETAINED_CREDIT,
                timeout: if cancel_caller {
                    Duration::from_secs(30)
                } else {
                    Duration::from_millis(20)
                },
            })
            .unwrap();
            let (started, observed) = tokio::sync::oneshot::channel();
            let (release, blocked) = std::sync::mpsc::channel();
            let worker_reader = reader.clone();
            let task = tokio::spawn(async move {
                worker_reader
                    .run(move |credit| {
                        let _credit = credit;
                        let _ = started.send(());
                        blocked.recv_timeout(Duration::from_secs(5)).unwrap();
                        Ok(None)
                    })
                    .await
            });
            observed.await.unwrap();
            if cancel_caller {
                task.abort();
                assert!(task.await.unwrap_err().is_cancelled());
            } else {
                assert!(matches!(task.await.unwrap(), Err(Error::Timeout)));
            }
            assert_eq!(reader.jobs.available_permits(), 0);
            assert_eq!(reader.bytes.available_permits(), 0);
            assert!(matches!(
                reader.run(|_| Ok(None)).await,
                Err(Error::Capacity)
            ));
            release.send(()).unwrap();
            let slot = tokio::time::timeout(Duration::from_secs(5), reader.jobs.acquire())
                .await
                .unwrap()
                .unwrap();
            assert_eq!(reader.bytes.available_permits(), RETAINED_CREDIT);
            drop(slot);
            assert!(reader.run(|_| Ok(None)).await.unwrap().is_none());
        }
    }
}
