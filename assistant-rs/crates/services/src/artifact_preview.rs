//! Bounded reads of retained diff blobs. No artifact content is executed and no
//! filesystem path supplied by a request becomes an ambient read capability.
use crate::{Error, Result};
use serde_json::Value;
#[cfg(unix)]
use std::{fs::File, io::Read};
use std::{
    io,
    path::{Component, Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::sync::Semaphore;

pub const UNAVAILABLE_PREVIEW: &str = "Preview unavailable: the recorded diff artifact is missing.";

#[derive(Clone)]
pub struct ArtifactPreview {
    root: Arc<Root>,
    slots: Arc<Semaphore>,
    timeout: Duration,
}
struct Root {
    path: PathBuf,
    #[cfg(unix)]
    directory: File,
}

impl ArtifactPreview {
    /// The host supplies an existing artifact root at startup. Creating or
    /// modifying directories and blobs remains outside this read-only service.
    pub fn new(root: &Path, concurrent_reads: usize, timeout: Duration) -> io::Result<Self> {
        if !(1..=64).contains(&concurrent_reads)
            || timeout.is_zero()
            || timeout > Duration::from_secs(120)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Invalid artifact preview limits",
            ));
        }
        #[cfg(unix)]
        {
            use rustix::fs::{Mode, OFlags, open};
            let path = root.canonicalize()?;
            let directory = File::from(open(
                &path,
                OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                Mode::empty(),
            )?);
            Ok(Self {
                root: Arc::new(Root { path, directory }),
                slots: Arc::new(Semaphore::new(concurrent_reads)),
                timeout,
            })
        }
        #[cfg(not(unix))]
        {
            let _ = root;
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "Artifact previews require verified directory-relative file access on this platform",
            ))
        }
    }

    pub async fn read_diff(&self, artifact: &Value) -> Result<String> {
        let Some(relative) = checked_path(&self.root.path, artifact) else {
            return Ok(UNAVAILABLE_PREVIEW.into());
        };
        let permit = self.slots.clone().try_acquire_owned().map_err(|_| {
            Error::Unavailable(
                "Artifact preview capacity is full; retry after another preview completes",
            )
        })?;
        let root = self.root.clone();
        let task = tokio::task::spawn_blocking(move || {
            // A dropped/timed-out caller cannot release admission while its
            // blocking task is still running. File contents never exceed 8 KiB.
            let _permit = permit;
            read_file(&root, &relative).unwrap_or_else(|_| UNAVAILABLE_PREVIEW.into())
        });
        tokio::time::timeout(self.timeout, task)
            .await
            .map_err(|_| {
                Error::Timeout("Artifact preview deadline exceeded; retry the retained result")
            })?
            .map_err(|_| {
                Error::Unavailable("Artifact preview could not complete; retry the retained result")
            })
    }
}

fn checked_path(root: &Path, artifact: &Value) -> Option<PathBuf> {
    let digest = artifact["sha256"].as_str()?;
    if digest.len() != 64
        || !digest
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    {
        return None;
    }
    let expected = Path::new("sha256")
        .join(&digest[..2])
        .join(&digest[2..4])
        .join(digest);
    let supplied = Path::new(artifact["storage_path"].as_str()?);
    let supplied = if supplied.is_absolute() {
        supplied.strip_prefix(root).ok()?
    } else {
        supplied
    };
    let mut relative = PathBuf::new();
    for component in supplied.components() {
        match component {
            Component::Normal(part) => relative.push(part),
            Component::CurDir => {}
            Component::ParentDir => {
                if !relative.pop() {
                    return None;
                }
            }
            _ => return None,
        }
    }
    (relative == expected).then_some(expected)
}

#[cfg(unix)]
fn read_file(root: &Root, relative: &Path) -> io::Result<String> {
    use rustix::fs::{Mode, OFlags, openat};
    let mut directory = root.directory.try_clone()?;
    let components: Vec<_> = relative.components().collect();
    for component in &components[..components.len() - 1] {
        directory = File::from(openat(
            &directory,
            component.as_os_str(),
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )?);
    }
    let file = File::from(openat(
        &directory,
        components.last().unwrap().as_os_str(),
        OFlags::RDONLY | OFlags::NONBLOCK | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?);
    if !file.metadata()?.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Artifact blob is not a regular file",
        ));
    }
    let mut bytes = Vec::with_capacity(8192);
    file.take(8192).read_to_end(&mut bytes)?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}
#[cfg(not(unix))]
fn read_file(_root: &Root, _relative: &Path) -> io::Result<String> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "Artifact preview platform is unavailable",
    ))
}
