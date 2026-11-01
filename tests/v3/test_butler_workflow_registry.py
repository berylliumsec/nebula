"""The Butler audit must notice new UI API actions without granting them authority."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHECK = UI / "scripts/check-butler-workflow-registry.mjs"
REGISTRY = ROOT / "docs/design/butler-workflow-registry.json"


def _check(registry: Path = REGISTRY) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(CHECK), "--registry", str(registry)],
        cwd=UI,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_reviewed_workflow_registry_matches_ui_api_calls() -> None:
    result = _check()
    assert result.returncode == 0, result.stderr
    assert "unreviewed signatures" in result.stdout


def test_registry_rejects_new_ui_api_call_without_review(tmp_path: Path) -> None:
    inventory = json.loads(REGISTRY.read_text())
    inventory["ui_api_calls"][0]["count"] += 1
    candidate = tmp_path / "drift.json"
    candidate.write_text(json.dumps(inventory))

    result = _check(candidate)
    assert result.returncode != 0
    assert "UI API calls changed; review operator workflows" in result.stderr


def test_registry_rejects_unsupported_action_marked_supported(tmp_path: Path) -> None:
    inventory = json.loads(REGISTRY.read_text())
    gap = next(
        row
        for row in inventory["actions"]
        if row["id"] == "workspace.device_and_directory"
    )
    gap["status"] = "supported"
    gap["butler"]["change"] = "missing"
    candidate = tmp_path / "false-support.json"
    candidate.write_text(json.dumps(inventory))

    result = _check(candidate)
    assert result.returncode != 0
    assert "supported action has no change operation" in result.stderr


def test_registry_rejects_operation_missing_from_core_catalog(tmp_path: Path) -> None:
    inventory = json.loads(REGISTRY.read_text())
    action = next(row for row in inventory["actions"] if row["id"] == "chat.archive")
    action["butler"]["change"] = "nebula.work.unlisted_action"
    candidate = tmp_path / "unlisted-action.json"
    candidate.write_text(json.dumps(inventory))

    result = _check(candidate)
    assert result.returncode != 0
    assert "Butler operation is not in the Core catalog" in result.stderr


def test_registry_rejects_handoff_to_unknown_destination(tmp_path: Path) -> None:
    inventory = json.loads(REGISTRY.read_text())
    action = next(
        row for row in inventory["actions"] if row["id"] == "browser.interactive_page"
    )
    action["handoff"]["target_id"] = "unlisted-browser"
    candidate = tmp_path / "unknown-handoff.json"
    candidate.write_text(json.dumps(inventory))

    result = _check(candidate)
    assert result.returncode != 0
    assert "device handoff destination is not in the Core catalog" in result.stderr


def test_registry_rejects_mutation_disguised_as_open_handoff(tmp_path: Path) -> None:
    inventory = json.loads(REGISTRY.read_text())
    action = next(
        row for row in inventory["actions"] if row["id"] == "browser.interactive_page"
    )
    action["butler"]["change"] = "nebula.work.start_mission"
    candidate = tmp_path / "mutation-handoff.json"
    candidate.write_text(json.dumps(inventory))

    result = _check(candidate)
    assert result.returncode != 0
    assert "device handoff may only open its exact catalog destination" in result.stderr


def test_registry_rejects_browser_opening_marked_as_saved_mutation(
    tmp_path: Path,
) -> None:
    inventory = json.loads(REGISTRY.read_text())
    action = next(
        row for row in inventory["actions"] if row["id"] == "browser.interactive_page"
    )
    action["status"] = "supported"
    candidate = tmp_path / "false-handoff-result.json"
    candidate.write_text(json.dumps(inventory))

    result = _check(candidate)
    assert result.returncode != 0
    assert "browser opening cannot be marked as a supported mutation" in result.stderr


def test_registry_collector_tracks_client_declarations_through_aliases(
    tmp_path: Path,
) -> None:
    ui = tmp_path / "ui"
    (ui / "src/api").mkdir(parents=True)
    (ui / "src/components").mkdir()
    (ui / "tsconfig.app.json").write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "target": "ES2022",
                    "module": "ESNext",
                    "moduleResolution": "Bundler",
                    "strict": True,
                    "noEmit": True,
                },
                "include": ["src", "external.ts"],
            }
        )
    )
    (ui / "src/api/client.ts").write_text("""export class ApiClient {
  listProjects() { return []; }
  request(path: string, init?: {method?: string}) { return path; }
  self() { this.listProjects(); }
}
export class OtherClient {
  listProjects() { return []; }
  request(path: string) { return path; }
}
""")
    (ui / "src/components/butlerTerminalStop.ts").write_text(
        "export interface TerminalStopApi { closeContainerTerminal(id: string): void; }"
    )
    (
        ui / "src/view.ts"
    ).write_text("""import {ApiClient, OtherClient} from "./api/client";
import type {TerminalStopApi} from "./components/butlerTerminalStop";
const api = new ApiClient();
api.listProjects();
const nextApi = api;
nextApi.listProjects();
const maybe: ApiClient | undefined = api;
maybe!.listProjects();
const context = {api};
context.api.listProjects();
const ref = {current: context};
ref.current.api.listProjects();
(api as ApiClient).listProjects();
(api).listProjects();
api["listProjects"]();
api.request("one");
nextApi.request("one");
context.api.request("two", {method: "PATCH"});
declare const facade: TerminalStopApi;
const wrapped = facade;
wrapped.closeContainerTerminal("one");
function unrelated() {
  const api = new OtherClient();
  api.listProjects();
  api.request("not-a-core-route");
}
const local = {listProjects() {}};
local.listProjects();
""")
    for file in ("src/ignored.test.ts", "src/ignored.spec.ts", "external.ts"):
        relative_import = (
            "./api/client" if file.startswith("src/") else "./src/api/client"
        )
        (ui / file).write_text(
            f'import {{ApiClient}} from "{relative_import}"; '
            'new ApiClient().request("excluded");'
        )
    script = (
        f"import {{collectCalls}} from {json.dumps(CHECK.as_uri())};"
        "console.log(JSON.stringify(collectCalls(process.argv[1])));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(ui)],
        cwd=UI,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert {row["key"]: row["count"] for row in json.loads(result.stdout)} == {
        "ui/src/view.ts::listProjects": 8,
        'ui/src/view.ts::request::GET "one"': 2,
        'ui/src/view.ts::request::"PATCH" "two"': 1,
        "ui/src/view.ts::closeContainerTerminal": 1,
    }
