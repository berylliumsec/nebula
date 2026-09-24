#![cfg(unix)]
use nebula_assistant_services::project_instructions::{Error, Instructions, Limits, Reader};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::Path,
    sync::Arc,
};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-project-instructions.json"
    ))
    .unwrap()
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn projection(value: Option<Arc<Instructions>>) -> Value {
    let Some(value) = value else {
        return Value::Null;
    };
    json!({"receipt":value.receipt(),"content_bytes":value.content().len(),
        "content_sha256":digest(value.content().as_bytes()),"prompt_bytes":value.prompt().len(),
        "prompt_sha256":digest(value.prompt().as_bytes()),
        "content":(value.content().chars().count()<1000).then_some(value.content()),
        "prompt":(value.prompt().chars().count()<1000).then_some(value.prompt())})
}
fn body(name: &str) -> Vec<u8> {
    match name {
        "empty" => vec![],
        "whitespace" => b" \t\r\n\x1c\x1d\x1e\x1f".to_vec(),
        "unicode-json" => "Follow the project.\n\"quoted\" \\ 🌌\u{2028}\u{2029}\0\u{7f}"
            .as_bytes()
            .to_vec(),
        "invalid-utf8" => b"a\xff\xed\xa0\x80\xf4\x90\x80\x80z".to_vec(),
        "exact-cap" => vec![b'x'; 65536],
        "truncated" => vec![b'x'; 65538],
        "split-codepoint" => [vec![b'x'; 65535], "🌌".as_bytes().to_vec()].concat(),
        "literal-replacement" => [vec![b'x'; 65533], "�z".as_bytes().to_vec()].concat(),
        "control-cap" => vec![0; 65538],
        _ => panic!("unknown fixture"),
    }
}

#[tokio::test]
async fn source_project_instruction_content_receipts_and_prompt_bytes_match() {
    let reader = Reader::new(Limits::default()).unwrap();
    let root = tempfile::tempdir().unwrap();
    for case in fixture()["content_cases"].as_array().unwrap() {
        let bytes = body(case["name"].as_str().unwrap());
        assert_eq!(bytes.len(), case["body_bytes"].as_u64().unwrap() as usize);
        assert_eq!(digest(&bytes), case["body_sha256"]);
        fs::write(root.path().join("AGENTS.md"), bytes).unwrap();
        let got = reader.read(root.path().into()).await.unwrap();
        assert_eq!(projection(got), case["observation"], "{}", case["name"]);
    }
}

fn arrange(base: &Path, name: &str) -> std::path::PathBuf {
    let mut root = base.join("parent/workspace");
    fs::create_dir_all(&root).unwrap();
    let candidate = root.join("AGENTS.md");
    match name {
        "missing" => {}
        "missing-root" => root = root.join("absent"),
        "directory" => fs::create_dir(&candidate).unwrap(),
        "inside-link" => {
            let target = root.join("notes.txt");
            fs::write(&target, b"fixture\n").unwrap();
            symlink(target, candidate).unwrap();
        }
        "parent-link" | "ancestor-link" | "sibling-link" | "outside-file" => {
            let target = match name {
                "parent-link" => root.parent().unwrap().join("AGENTS.md"),
                "ancestor-link" => base.join("AGENTS.md"),
                "sibling-link" => base.join("sibling/AGENTS.md"),
                _ => base.join("private.txt"),
            };
            fs::create_dir_all(target.parent().unwrap()).unwrap();
            fs::write(&target, b"fixture\n").unwrap();
            symlink(target, candidate).unwrap();
        }
        "dangling-inside" | "dangling-outside" => symlink(
            if name == "dangling-inside" {
                root.join("missing")
            } else {
                base.join("missing")
            },
            candidate,
        )
        .unwrap(),
        "chain" => {
            fs::write(root.join("notes"), b"fixture\n").unwrap();
            symlink("notes", root.join("link")).unwrap();
            symlink("link", candidate).unwrap();
        }
        "regular" | "workspace-link" | "inaccessible" => {
            fs::write(candidate, b"fixture\n").unwrap();
            if name == "inaccessible" {
                fs::set_permissions(&root, fs::Permissions::from_mode(0o0)).unwrap();
            }
            if name == "workspace-link" {
                let alias = base.join("alias");
                symlink(root, &alias).unwrap();
                root = alias;
            }
        }
        _ => panic!("unknown fixture"),
    }
    root
}

#[tokio::test]
async fn source_project_instruction_path_rules_preserve_parent_links_and_refusals() {
    let reader = Reader::new(Limits::default()).unwrap();
    for case in fixture()["path_cases"].as_array().unwrap() {
        let dir = tempfile::tempdir().unwrap();
        let root = arrange(dir.path(), case["name"].as_str().unwrap());
        let result = reader.read(root.clone()).await;
        if case["name"] == "inaccessible" {
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let observation = match result {
            Ok(value) => json!({"accepted":true,"value":projection(value)}),
            Err(Error::Source(detail)) => {
                json!({"accepted":false,"kind":"ProjectInstructionsError","detail":detail})
            }
            Err(error) => panic!("unexpected {}: {error:?}", case["name"]),
        };
        assert_eq!(observation, case["observation"], "{}", case["name"]);
    }
}

#[tokio::test]
async fn project_instruction_rereads_and_shared_reply_credits_bound_retained_results() {
    let reader = Reader::new(Limits {
        retained_bytes: 1024 * 1024,
        ..Limits::default()
    })
    .unwrap();
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("AGENTS.md");
    fs::write(&path, b"private-first").unwrap();
    let first = reader.read(root.path().into()).await.unwrap().unwrap();
    let first_copy = first.clone();
    assert!(!format!("{first:?}").contains("private-first"));
    drop(first);
    fs::write(&path, b"second").unwrap();
    assert!(matches!(
        reader.read(root.path().into()).await,
        Err(Error::Capacity)
    ));
    assert_eq!(first_copy.content(), "private-first");
    drop(first_copy);
    let second = reader.read(root.path().into()).await.unwrap().unwrap();
    assert_eq!(second.content(), "second");
    drop(second);
    fs::remove_file(path).unwrap();
    assert!(reader.read(root.path().into()).await.unwrap().is_none());
    fs::write(root.path().join("AGENTS.md"), b"third").unwrap();
    assert_eq!(
        reader
            .read(root.path().into())
            .await
            .unwrap()
            .unwrap()
            .content(),
        "third"
    );
    assert!(matches!(
        reader.read("relative".into()).await,
        Err(Error::Boundary)
    ));
    assert!(
        Reader::new(Limits {
            blocking_reads: 0,
            ..Limits::default()
        })
        .is_err()
    );
}

#[tokio::test]
async fn project_instruction_special_files_and_resolution_work_are_bounded() {
    let reader = Reader::new(Limits::default()).unwrap();
    let root = tempfile::tempdir().unwrap();
    let candidate = root.path().join("AGENTS.md");
    rustix::fs::mknodat(
        rustix::fs::CWD,
        &candidate,
        rustix::fs::FileType::Fifo,
        rustix::fs::Mode::RUSR | rustix::fs::Mode::WUSR,
        0,
    )
    .unwrap();
    assert!(matches!(
        reader.read(root.path().into()).await,
        Err(Error::Source("AGENTS.md must be a regular file"))
    ));
    fs::remove_file(&candidate).unwrap();
    symlink("AGENTS.md", &candidate).unwrap();
    assert!(matches!(
        reader.read(root.path().into()).await,
        Err(Error::Boundary)
    ));
}
