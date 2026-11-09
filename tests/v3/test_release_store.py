"""Retained bundle identity and asset integrity survive application replacement."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import shutil

import pytest

from nebula.v3.deployment_protocol import ReleaseManifest
from nebula.v3.release_store import MANIFEST_NAME, ReleaseIntegrityError, ReleaseStore


def bundle(directory, *, version="v1", code=b"retained executable", features=()):
    directory.mkdir(parents=True)
    (directory / "ui").mkdir()
    (directory / "nebula-core").write_bytes(code)
    (directory / "nebula-core").chmod(0o755)
    (directory / "ui/index.html").write_text("<html>retained UI</html>")
    manifest = ReleaseManifest(
        version=version,
        schema_family=5,
        schema_min=5,
        schema_max=5,
        additive_migrations=True,
        migration_head="0033_deployment_lifecycle",
        core_executable="nebula-core",
        ui_directory="ui",
        files={
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("nebula-core", "ui/index.html")
        },
        features=list(features),
    )
    raw = manifest.model_dump_json().encode()
    (directory / MANIFEST_NAME).write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def store(tmp_path):
    result = ReleaseStore(tmp_path / "retained")
    try:
        yield result
    finally:
        for directory in result.root.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o700)


def test_installed_assets_survive_the_original_application_directory(store, tmp_path):
    source = tmp_path / "application"
    digest = bundle(source)
    manifest, retained = store.install(source, expected_manifest_sha256=digest)
    shutil.rmtree(source)
    assert store.verify(retained, expected_manifest_sha256=digest) == manifest
    assert (retained / "nebula-core").read_bytes() == b"retained executable"
    assert (retained / "nebula-core").stat().st_mode & 0o777 == 0o555


def test_a_version_cannot_be_replaced_by_different_assets(store, tmp_path):
    first, changed = tmp_path / "first", tmp_path / "changed"
    digest = bundle(first)
    alternative = bundle(changed, code=b"different")
    store.install(first, expected_manifest_sha256=digest)
    with pytest.raises(ReleaseIntegrityError):
        store.install(changed, expected_manifest_sha256=alternative)
    assert (store.root / "v1/nebula-core").read_bytes() == b"retained executable"


def test_tampered_or_symlinked_assets_never_install(store, tmp_path):
    source = tmp_path / "application"
    digest = bundle(source)
    (source / "nebula-core").write_bytes(b"tampered")
    with pytest.raises(ReleaseIntegrityError):
        store.install(source, expected_manifest_sha256=digest)
    (source / "nebula-core").unlink()
    (source / "nebula-core").symlink_to(source / "ui/index.html")
    with pytest.raises(ReleaseIntegrityError, match="symlink"):
        store.install(source, expected_manifest_sha256=digest)
    assert not (store.root / "v1").exists()


def test_concurrent_installation_keeps_one_immutable_version(store, tmp_path):
    source = tmp_path / "application"
    digest = bundle(source)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: store.install(source, expected_manifest_sha256=digest),
                range(2),
            )
        )
    assert results[0] == results[1]
    assert not list(store.root.glob(".staging-*"))


def test_retained_root_symlink_cannot_refer_back_to_replaceable_assets(store, tmp_path):
    source = tmp_path / "application"
    digest = bundle(source)
    (store.root / "v1").symlink_to(source, target_is_directory=True)
    with pytest.raises(ReleaseIntegrityError, match="symlink"):
        store.install(source, expected_manifest_sha256=digest)


def test_retained_store_cannot_be_inside_replaceable_application(tmp_path):
    source = tmp_path / "application"
    digest = bundle(source)
    store = ReleaseStore(source / "retained")
    with pytest.raises(ReleaseIntegrityError, match="outside"):
        store.install(source, expected_manifest_sha256=digest)
