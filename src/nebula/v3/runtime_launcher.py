"""Private local discovery; viewer pipes never own the runtime's lifetime."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx

from .process_identity import same_process
from .release_store import MANIFEST_NAME, ReleaseStore
from .runtime_bundle import build_source_bundle

DESKTOP_PROTOCOL = "nebula-desktop-runtime-v1"
CONNECTION_NAME = "connection.json"


def private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("Runtime directories cannot be symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    details = path.stat()
    if details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise ValueError(
            f"Runtime directory must be owned by this user and private: {path}"
        )
    return path


def runtime_root(data_root: Path) -> Path:
    return private_directory(private_directory(data_root) / "runtime")


def read_private(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or details.st_size > 65536
        ):
            raise ValueError(
                "Runtime discovery and credentials must be private regular files"
            )
        return stream.read(65537)


def write_private(path: Path, value: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".private-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def operator_token(data_root: Path, requested: str | None = None) -> str:
    path = runtime_root(data_root) / "operator-token"
    try:
        token = read_private(path).decode().strip()
    except FileNotFoundError:
        token = requested or secrets.token_urlsafe(32)
        if len(token) < 32:
            raise ValueError(
                "A Core administration token requires at least 32 characters"
            )
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            return operator_token(data_root, requested)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    if len(token) < 32 or (
        requested is not None and not hmac.compare_digest(requested, token)
    ):
        raise ValueError(
            "Existing Core uses a different administration credential; reconnect with that credential"
        )
    return token


@contextmanager
def startup_lock(data_root: Path):
    path = runtime_root(data_root) / "launcher.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        details = os.fstat(descriptor)
        if (
            details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or not stat.S_ISREG(details.st_mode)
        ):
            raise ValueError("Runtime launch lock is not private")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def discovery(data_root: Path) -> dict | None:
    try:
        value = json.loads(read_private(runtime_root(data_root) / CONNECTION_NAME))
    except FileNotFoundError:
        return None
    if (
        not isinstance(value, dict)
        or value.get("protocol") != DESKTOP_PROTOCOL
        or value.get("data_root") != str(data_root.resolve())
        or type(value.get("pid")) is not int
        or not isinstance(value.get("process_birth"), str)
        or not isinstance(value.get("endpoint"), str)
    ):
        raise ValueError("Runtime discovery is invalid; preserve it for recovery")
    return value


def health(connection: dict, token: str, *, timeout: float = 2) -> dict:
    with httpx.Client(
        trust_env=False, timeout=timeout, follow_redirects=False
    ) as client:
        response = client.get(
            connection["endpoint"] + "/health",
            headers={"Authorization": "Bearer " + token},
        )
        response.raise_for_status()
        value = response.json()
    if value.get("status") not in {"ok", "starting", "degraded"}:
        raise ValueError("The discovered Core returned an invalid health response")
    return value


def packaged_bundle() -> tuple[Path, str] | None:
    executable = Path(sys.executable).resolve()
    configured = os.getenv("NEBULA_RUNTIME_BUNDLE")
    candidates = ([Path(configured)] if configured else []) + [
        executable.parent.parent,
        executable.parent / "nebula-runtime",
        executable.parent.parent / "lib/Nebula/nebula-runtime",
    ]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys._MEIPASS) / "nebula-runtime")
    for candidate in candidates:
        if (candidate / MANIFEST_NAME).is_file():
            digest = (candidate / "nebula-runtime.sha256").read_text().strip()
            ReleaseStore.verify(candidate, expected_manifest_sha256=digest)
            return candidate, digest
    return None


def retain_bundle(
    data_root: Path, *, static_dir: Path | None = None
) -> tuple[Path, str]:
    bundled = packaged_bundle()
    if bundled is not None:
        source, digest = bundled
        _, installed = ReleaseStore(runtime_root(data_root) / "releases").install(
            source, expected_manifest_sha256=digest
        )
        return installed, digest
    if getattr(sys, "frozen", False):
        raise ValueError("The installed Core is missing its verified runtime bundle")
    source_package = Path(__file__).resolve().parents[1]
    frontend = static_dir or Path(
        os.getenv("NEBULA_V3_UI_DIR", source_package.parents[1] / "ui/dist")
    )
    staging = Path(
        tempfile.mkdtemp(prefix="bundle-source-", dir=runtime_root(data_root))
    )
    try:
        _, digest = build_source_bundle(source_package, frontend, staging / "payload")
        _, installed = ReleaseStore(runtime_root(data_root) / "releases").install(
            staging / "payload", expected_manifest_sha256=digest
        )
        return installed, digest
    finally:
        import shutil

        shutil.rmtree(staging)


def desktop_connection(
    data_root: Path, *, static_dir: Path | None = None, timeout: float = 60
) -> dict:
    if not sys.platform.startswith("linux"):
        raise ValueError(
            "Independent local Core startup currently requires Linux pidfd support; configure a remote Core on this platform"
        )
    data_root = data_root.expanduser().resolve()
    with startup_lock(data_root):
        token = operator_token(data_root)
        existing = discovery(data_root)
        if existing is not None and same_process(
            existing["pid"], existing["process_birth"]
        ):
            health(existing, token)
            return connection_frame(existing, token)
        bundle, digest = retain_bundle(data_root, static_dir=static_dir)
        log_path = runtime_root(data_root) / "supervisor.log"
        descriptor = os.open(
            log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "ab") as output:
            from .runtime_processes import detached_worker_environment

            process = subprocess.Popen(
                [
                    str(bundle / "bin/nebula-core"),
                    "runtime-service",
                    "--data-dir",
                    str(data_root),
                    "--bundle",
                    str(bundle),
                    "--manifest-sha256",
                    digest,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                cwd=data_root,
                env=detached_worker_environment(bundle),
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            candidate = discovery(data_root)
            if (
                candidate
                and candidate["pid"] == process.pid
                and same_process(candidate["pid"], candidate["process_birth"])
            ):
                try:
                    health(candidate, token)
                    return connection_frame(candidate, token)
                except (httpx.HTTPError, ValueError):
                    pass
            if process.poll() is not None:
                raise RuntimeError(
                    f"Core startup failed; private diagnostics: {log_path}"
                )
            time.sleep(0.05)
        # A service may already have admitted work. A viewer timeout must not
        # signal it or discard its discovery state.
        raise TimeoutError(
            f"Core is still starting; retry connection. Private diagnostics: {log_path}"
        )


def connection_frame(value: dict, token: str) -> dict:
    from urllib.parse import urlsplit

    endpoint = urlsplit(value["endpoint"])
    if (
        endpoint.scheme != "http"
        or endpoint.hostname != "127.0.0.1"
        or not endpoint.port
    ):
        raise ValueError(
            "Desktop discovery requires an authenticated loopback endpoint"
        )
    return {
        "protocol": DESKTOP_PROTOCOL,
        "host": "127.0.0.1",
        "port": endpoint.port,
        "ipc_token": token,
    }


def shutdown_runtime(data_root: Path, *, timeout: float = 30) -> dict:
    value = discovery(data_root)
    if value is None or not same_process(value["pid"], value["process_birth"]):
        return {"state": "stopped"}
    token = operator_token(data_root)
    with httpx.Client(trust_env=False, timeout=timeout) as client:
        headers = {"Authorization": "Bearer " + token}
        status = client.get(value["endpoint"] + "/deployment/status", headers=headers)
        status.raise_for_status()
        response = client.post(
            value["endpoint"] + "/deployment/operations",
            headers=headers,
            json={
                "operation_id": str(uuid4()),
                "action": "shutdown",
                "parameters": {"expected_revision": status.json()["revision"]},
            },
        )
        response.raise_for_status()
        return response.json()
