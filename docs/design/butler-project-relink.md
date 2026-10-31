# Butler existing-project folder relink contract

An operator asks interactive Butler to relink an existing project's shared
folder. Butler first lists projects and reads the exact project's current
revision. It prepares a pending browser action without receiving or choosing a
host path. The requesting browser uses the Nebula-host folder picker, shows the
current and proposed folders, confirms the workspace change, and sends the
selected path to authenticated Core. Core resolves the folder, checks the
project revision and refuses a change while any session or work that can still
use the old workspace is active or reusable, including queued chat follow-ups
and running native hooks. Historical interrupted work with no recovery path
does not block the change. The browser then refreshes the selected project
context from Core and opens Files for inspection.

| Step | Observable invariant | State authority | Check |
| --- | --- | --- | --- |
| Discover | Butler offers relink only in an interactive project session, with a current target and revision | Core project catalog | Butler tool test |
| Select | The model does not supply an absolute host path; the browser picker chooses a directory on the Nebula host | Requesting browser and Core host-folder catalog | Component and paired LAN browser |
| Confirm | The dialog names both folders and warns that future workspace operations use the proposed folder; cancelling saves nothing | Requesting browser | Component and paired LAN browser |
| Change | Core rejects stale revisions, unavailable folders, and in-use projects; generic project replacement cannot bypass the relink guard | Core project and workload state | API and real Core |
| Read back | The project path, Workbench workspace and Butler outcome agree after refresh | Core project record and workspace service | Paired LAN browser |
| Recover | An uncertain response requires reading the project before any retry; a busy project reports which work must be closed or completed | Core project and workload state | API and paired LAN browser |

The project record owns the workspace path and revision. The URL owns the
selected project; React only holds the unsaved folder choice and confirmation.
An existing command or harness session can retain its original working folder,
so relinking requires a quiescent project. Host mode consent remains the
project's one-time OS-user permission under the current product policy; the
confirmation makes the new linked folder visible but does not silently change
that policy. The old folder is not deleted or copied.
An existing Notebook file index is disconnected without deleting its source
file; it must be reconnected for the new folder. Cached Notebook listings and
idle kernels from the old folder cannot be reused.

Core serializes durable work admission with the project row used by relink.
Provider chat turns, queued follow-ups, command sessions, harness sessions and
turns, and both native and harness missions compare the workspace selected at
preparation with the current project path before saving work. Native hook
executions make the same comparison before their running record is inserted;
this also covers a cancelled turn whose terminal hook starts after the turn
became terminal. Relink waits for admissions that acquired the project row
first, then scans their busy records. The standard single-process Core also
coordinates Notebook kernels, reviewed executions and terminals with local
locks. Multiple Core server processes sharing one database are not verified by
this relink acceptance evidence.

This workflow does not create a project, run a command, or move existing files.
After a successful relink, new workspace access starts from the new folder.
