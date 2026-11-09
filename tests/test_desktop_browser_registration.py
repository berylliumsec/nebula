"""Exercise the actual Electron renewal function without a graphical desktop."""

import json
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("statuses", [[409, 200], [503, 200], [200, 200]])
def test_browser_renewal_never_reclaims_a_conflicting_attachment(statuses):
    source = Path("ui/electron/main.mjs").read_text()
    function = source.split("async function refreshBrowserBridge()", 1)[1].split(
        "\nfunction requireShell", 1
    )[0]
    script = r"""
import vm from 'node:vm';
const input = JSON.parse(process.argv[1]);
const calls = [];
const context = {
  core: {endpoint:'http://127.0.0.1:23456/api/v1', token:'synthetic'},
  bridge: {url:'http://127.0.0.1:23457', token:'synthetic-browser'},
  browserRegistration:'original', browserRegistrationLost:false, bridgeHeartbeat:1,
  clearInterval: () => {}, AbortSignal,
  deviceId: async () => 'fixture-device',
  fetch: async (_url, request) => {
    calls.push(JSON.parse(request.body));
    const status = input.statuses[calls.length - 1];
    return {ok:status===200, status, json:async()=>({registration_id:'original'})};
  },
};
vm.createContext(context);
vm.runInContext('async function refreshBrowserBridge()'+input.function, context);
for(let i=0;i<2;i++) { try { await context.refreshBrowserBridge(); } catch {} }
process.stdout.write(JSON.stringify({calls, fenced:context.browserRegistrationLost, registration:context.browserRegistration}));
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            json.dumps({"function": function, "statuses": statuses}),
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
    )
    observed = json.loads(result.stdout)
    assert len(observed["calls"]) == (1 if statuses[0] == 409 else 2)
    assert all(item["registration_id"] == "original" for item in observed["calls"])
    assert observed["registration"] == "original"
    assert observed["fenced"] is (statuses[0] == 409)
