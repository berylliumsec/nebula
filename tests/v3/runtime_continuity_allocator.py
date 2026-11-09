"""Bounded physical allocator adapter, included only in acceptance bundles."""

import json
from pathlib import Path
import subprocess

from fastapi import HTTPException, Request

from nebula.v3.memory_policy import MIB, MemoryLimits
from nebula.v3.sandbox import (
    ContainerRuntimeType,
    ContainerSandboxRunner,
    RunnerIsolationMode,
    RunnerPlatform,
    RunnerProfile,
    SandboxLimits,
    SandboxRequest,
    SandboxWorkspaceAccess,
)


SOURCE = r"""
#include <stdlib.h>
#include <unistd.h>
int main(int argc,char **argv) {
    if(argc!=3 && argc!=4) return 64;
    size_t n=(size_t)atoi(argv[1])*1024*1024;
    volatile char *p=malloc(n);
    if(!p) return 71;
    for(size_t i=0;i<n;i+=4096) p[i]=1;
    if(argc==4) {
        int elapsed=0, deadline=atoi(argv[2]);
        while(access(argv[3],F_OK)!=0 && elapsed<deadline) {
            sleep(1);
            elapsed++;
        }
        if(access(argv[3],F_OK)!=0) return 75;
    } else {
        sleep(atoi(argv[2]));
    }
    free((void*)p);
    return 0;
}
"""


class AcceptanceRunner(ContainerSandboxRunner):
    async def available(self):
        output, error, code = await self._capture("info", "--format", "{{json .}}")
        if code:
            return False, error
        value = json.loads(output)
        return (
            value.get("OSType") == "linux"
            and str(value.get("CgroupVersion")) == "2"
            and value.get("MemoryLimit") is True,
            "Explicit disposable daemon only; this is not rootless production certification.",
        )


def prepare_allocator(root: Path, *, runtime: str, image: str, socket: str) -> None:
    workspace = root / "allocator-workspace"
    workspace.mkdir(mode=0o755)
    workspace.chmod(0o755)
    source = root / "allocator.c"
    source.write_text(SOURCE)
    subprocess.run(
        ["gcc", "-O0", "-static", str(source), "-o", str(workspace / "allocator")],
        check=True,
        capture_output=True,
        timeout=30,
    )
    (workspace / "allocator").chmod(0o555)
    (root / "allocator.json").write_text(
        json.dumps(
            {
                "runtime": runtime,
                "image": image,
                "socket": socket,
                "workspace": str(workspace),
            }
        )
    )


def runner_for(root: Path):
    config = root / "allocator.json"
    if not config.exists():
        return None
    values = json.loads(config.read_text())
    return AcceptanceRunner(
        profile=RunnerProfile(
            runtime_type=ContainerRuntimeType.DOCKER,
            executable=Path(values["runtime"]),
            socket=values["socket"],
            platform=RunnerPlatform.LINUX,
            isolation_mode=RunnerIsolationMode.LINUX_ROOTLESS,
        ),
        allow_unpinned_images=True,
        workspace_roots=[Path(values["workspace"])],
    )


def release_allocator(root: Path, identity: str) -> None:
    if not identity or any(
        not (item.isascii() and (item.isalnum() or item in "-_")) for item in identity
    ):
        raise ValueError("invalid isolated allocator identity")
    (root / "allocator-workspace" / ("release-" + identity)).touch(mode=0o444)


def attach_allocator(app, root: Path, project_id: str):
    runner = runner_for(root)
    if runner is None:
        return
    values = json.loads((root / "allocator.json").read_text())
    app.state.memory_api.runners["continuity-allocator"] = runner
    app.state.memory_compute.runners["continuity-allocator"] = runner

    @app.post("/api/v1/engagements/{selected_project}/acceptance-allocation")
    async def allocate(selected_project: str, request: Request):
        if selected_project != project_id:
            raise HTTPException(404)
        body = await request.json()
        identity = body["job_id"]
        if (
            not isinstance(identity, str)
            or not identity
            or len(identity) > 80
            or any(
                not (item.isascii() and (item.isalnum() or item in "-_"))
                for item in identity
            )
        ):
            raise HTTPException(400)
        command = ["/workspace/allocator", "4", "25"]
        timeout = 40
        if body.get("hold") is True:
            command = [
                "/workspace/allocator",
                "4",
                "90",
                "/workspace/release-" + identity,
            ]
            timeout = 100
        result = await app.state.memory_compute.run_sandbox(
            runner,
            SandboxRequest(
                image=values["image"],
                command=command,
                workspace=Path(values["workspace"]),
                workspace_access=SandboxWorkspaceAccess.READ,
                limits=SandboxLimits(memory_mb=32, timeout_seconds=timeout),
            ),
            job_id=identity,
            project_id=project_id,
            resource_kind="synthetic_allocator",
            resource_id=identity,
            profile_id="continuity-allocator",
            requested=MemoryLimits(max_bytes=32 * MIB),
        )
        return {
            "job_id": identity,
            "exit_code": result.exit_code,
            "outcome": result.memory_outcome,
            "stdout": result.stdout[-4096:],
            "stderr": result.stderr[-4096:],
        }
