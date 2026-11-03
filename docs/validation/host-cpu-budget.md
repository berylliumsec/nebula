# Per-host CPU budget

The CPU budget belongs to one Core host's local data directory. Projects and
connected browsers share it; another Core host has independent configuration.
Automatic uses half the detected affinity/cgroup CPU capacity. Custom accepts a
positive finite CPU-equivalent limit up to the host capacity. The active
experimental executor applies the quota and reports the observed limit. An
ordinary Core without that executor saves the policy and reports it inactive.
Saving a policy never silently enables the experimental executor.

| Journey | Invariant | Authority | Verification |
| --- | --- | --- | --- |
| Discover | Setup and Butler expose the host CPU budget and its actual application state | Core capability/settings catalog | UI, capability and product-help checks |
| Save | Save uses host identity and revision; over-capacity/invalid values are rejected | Host-local atomic configuration | Authenticated API and UI checks |
| Apply | Only an observed executor quota is called applied; failed application remains visible and retryable | Executor readback | Kernel-limited LAN fixture and API checks |
| Share/reconnect | Projects/browsers on the same host observe one policy; another host remains unchanged | Core local file | Independent Core roots, reload and restart |
| Resize | Raising capacity admits more work; lowering throttles existing work and drains extra slots without cancellation | Kernel quota and model scheduler | Held jobs and live 2/3-CPU checks |
| Unsupported | A host without the executor reports saved/inactive rather than claiming an enforced whole-Core CPU cap | Core status | UI/API regression |
| Failure/conflict | Stale revisions or a switched host cannot overwrite the current policy; pending/failed application is explicit | Core identity/revision/readback | API and component checks |
| Interface | Controls remain usable on desktop and mobile and survive refresh | Core plus browser presentation | Production bundle, LAN and selected browser matrix |

The setting limits the experimental shared background executor. It does not
establish a quota for execution paths that have not been integrated with that
executor. Native/frozen activation remains a separate experimental gate.
