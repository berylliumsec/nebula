# Automation runtime

Nebula gives agents a general Bash runtime instead of installing or assigning
catalogued tools. The model-visible contract is fixed:

- `run_command` executes a complete command with
  `/bin/bash --noprofile --norc -c`.
- `process_io` polls, writes to, or terminates a process.

`rg`, Python, Git, curl, shell utilities, and security programs are normal
executables on `PATH`. Their exact paths and package versions are generated from
the prepared image, included in the bounded agent capability description, and
shown under **Settings → Runtime**. The derivative explicitly includes `bash`,
Python 3, `rg`, Git, and curl in addition to Kali's headless security set.

## Kali image and session boundary

Automation uses the existing Kali headless image prepared by Nebula for the
human terminal. Preparation resolves the official Kali base, builds the verified
local derivative containing Nebula's egress helper, generates a binary inventory,
and freezes the local image ID and runner revision. Agent sessions always start
with pulling disabled.

Each chat, mission, or harness session receives one container. Only its Project
workspace is mounted. The root filesystem is read-only; `/tmp` is isolated; the
worker is non-root; capabilities are dropped; no host credentials, sockets, or
control data are inherited. Files and background processes persist until session
teardown, while `cd`, aliases, and exported shell variables do not persist across
commands. Full-screen PTY programs are not supported by the agent runtime.

Core launches the container engine and every `exec` operation with argv-based
subprocess calls. User command text is an argument to Bash inside the container
and is never interpreted by a host shell. Each command runs in its own process
group so timeout, cancellation, and session cleanup terminate descendants.

## Approval policy

Projects choose one execution policy:

- `always` requires approval for every new command.
- `on_boundary` runs workspace-only commands automatically and prompts once when
  the session first requests `network=project_scope`.
- `never` runs commands automatically and activates configured Project scope when
  explicitly requested.

Approval cannot override a disabled runner, missing runtime, expired scope, or a
filesystem/network sandbox denial.

## Project-scoped networking

Sessions start offline. A command can request `project_scope`; it cannot supply a
target address, hostname, or port. Core freezes the Project policy and installs
its complete CIDR, domain, wildcard-domain, and TCP-port boundary in the session's
network namespace.

The namespace has a policy DNS resolver. It refuses unapproved names, validates
exact and wildcard boundaries, follows only the approved query's CNAME chain,
and opens TCP rules only for validated response addresses. Private, loopback,
link-local, reserved, and rebinding answers fail closed unless the destination is
explicitly authorized by CIDR. Worker DNS to any other resolver is blocked. URL
path entries cannot constrain arbitrary shell networking and do not authorize a
session by themselves. When a frozen scope expires, Nebula destroys the session
and its background processes.

## Receipts and artifacts

Every command records the command hash, working directory, runtime digest,
runner and policy revisions, effective network grant, timestamps, status, exit
code, cancellation/timeout state, output sizes, and observed workspace changes.
Complete retained stdout and stderr are immutable artifacts. Models receive
redacted bounded results and use artifact search/read capabilities for focused
retrieval.

Use `nebula-core runtime status` to inspect readiness and
`nebula-core runtime prepare` to prepare or re-verify the Kali runtime. The main
`nebula-core doctor --json` output includes runtime diagnostics.
