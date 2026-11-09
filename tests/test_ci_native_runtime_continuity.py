"""CI ownership/provenance hazards only; no build, download or GUI proof."""

import subprocess

import pytest

from scripts import ci_native_runtime_continuity as ci


def commit(source):
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=Owned fixture", "-c",
         "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
         "commit", "-qm", "Synthetic source pin"], check=True,
    )


def repository(root, code):
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for path in ci.PRODUCT:
        directory = root / path
        directory.mkdir(parents=True)
        (directory / "fixture.txt").write_text(code)
    for name in ("ui/package-lock.json", "ui/src-tauri/Cargo.lock",
                 "tests/v3/packaged_continuity_artifacts.py", ci.CASE.split("::")[0],
                 "scripts/package_audit.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("inert preflight fixture; never executed\n")
    (root / "poetry.lock").write_text("\n".join(
        f'[[package]]\nname = "{name}"\nversion = "1.2.3"'
        for name in ("pytest", "httpx", "pyinstaller")
    ))
    commit(root)
    return root


def test_actual_git_pin_overrides_inherited_build_label(tmp_path, monkeypatch):
    baseline = repository(tmp_path / "baseline", "old product")
    candidate = repository(tmp_path / "candidate", "new product")
    monkeypatch.setenv("NEBULA_BUILD_COMMIT", "f" * 40)
    monkeypatch.setenv("NEBULA_RUNTIME_BUNDLE", "/unrelated/installation")
    job = ci.NativeJob(baseline, candidate, tmp_path / "output")
    actual = ci.git(candidate, "rev-parse", "HEAD")
    assert job.pins["candidate"]["commit"] == actual
    environment = job.environment(job.pins["candidate"], job.private / "candidate-venv")
    assert environment["NEBULA_BUILD_COMMIT"] == actual
    assert "NEBULA_RUNTIME_BUNDLE" not in environment
    with pytest.raises(ValueError, match="pinned build"):
        ci.source_pin(candidate, expected="f" * 40)


def test_document_only_relabel_and_dirty_source_are_refused(tmp_path):
    baseline = repository(tmp_path / "baseline", "same product")
    candidate = repository(tmp_path / "candidate", "same product")
    (candidate / "README.md").write_text("Documentation revision only")
    commit(candidate)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="actual product-code"):
        ci.NativeJob(baseline, candidate, output)
    assert not output.exists()
    (candidate / ci.PRODUCT[0] / "fixture.txt").write_text("uncommitted product")
    with pytest.raises(ValueError, match="clean"):
        ci.source_pin(candidate)


def test_output_and_cleanup_stay_inside_owned_directories(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "source-alias"
    alias.symlink_to(source, target_is_directory=True)
    for unsafe in (source / "output", tmp_path, alias / "output", source):
        with pytest.raises(ValueError, match="separate"):
            ci.output_path(unsafe, [source])
    owned = tmp_path / "owned"
    owned.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep").write_text("outside task")
    link = owned / "link"
    link.symlink_to(external, target_is_directory=True)
    for unsafe in (owned, external, link):
        with pytest.raises(ValueError, match="cleanup"):
            ci.remove_owned(owned, unsafe)
    assert (external / "keep").read_text() == "outside task"


def test_gui_evidence_allowlist_rejects_tokens_and_symlink_escape(tmp_path):
    private = tmp_path / "private"
    fixture = private / "case0/packaged-continuity"
    fixture.mkdir(parents=True)
    token = fixture / "operator-token"
    token.write_text("private credential")
    with pytest.raises(ValueError, match="allowlist"):
        ci.gui_evidence(private, token)
    unrelated = private / "synthetic-core/supervisor.log"
    unrelated.parent.mkdir()
    unrelated.write_text("not a top-level diagnostic export")
    with pytest.raises(ValueError, match="allowlist"):
        ci.gui_evidence(private, unrelated)
    exported = fixture / "supervisor.log"
    exported.symlink_to(token)
    with pytest.raises(ValueError, match="owned regular"):
        ci.gui_evidence(private, exported)
    exported.unlink()
    exported.write_text("bounded fixture log")
    assert ci.gui_evidence(private, exported) == exported


def test_export_redacts_literal_credentials_bearer_and_token_fragments():
    secret = "synthetic-credential-never-export"
    text = (
        f'{secret} Authorization: Bearer bearer-value\n'
        'http://127.0.0.1/#token=fragment-value\n'
        'https://account:password-value@example.invalid/index\n'
        '{"ipc_token":"json-value","result":"failed"}'
    )
    sanitized = ci.redact(text, [secret])
    for value in (secret, "bearer-value", "fragment-value", "password-value", "json-value"):
        assert value not in sanitized
    assert '"result":"failed"' in sanitized
