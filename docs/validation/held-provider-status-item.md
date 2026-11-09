# Local held-provider status check

The user explicitly narrowed final verification: fix and run only the failed
item locally, run no more tests in CI, then merge when normal required checks
permit it. The complete packaged desktop journey is not being rerun.

## Confirmed cause

Candidate `29766d18`, CI run `42694559151`, native job `129422974625` failed
`assert_held` before any bootstrap window close. Its expected pending-turn status
was `running`. The status API returns `ChatTurnStatus` unchanged, whose admitted
provider state is `routing`. The scheduler's separate queue row uses `running`.
Both current Core and baseline `fac1d729` have these same contracts.

Separate fresh-context diagnosis independently confirmed this enum-layer mixup
before repair. A valid admitted provider response necessarily failed the old
comparison. The generic CI error did not record the other booleans, so it does
not exclude an additional simultaneous issue in that full journey.

## Narrow repair and local evidence

The existing held assertion is extracted to `held_provider_turn` so its exact
failed item can run without opening a desktop or building packages. Existing
call sites, error ordering, status GET, release/follower/single-provider guards,
exact turn/session identity and receipt remain. The sole predicate repair is
`running` to `routing`; product code is unchanged.

Exactly one new nonparametrized node was collected and executed:

```text
tests/test_packaged_continuity_driver.py::test_held_provider_turn_uses_core_pending_status
```

It passes Core's real `PendingChatTurnStatus` JSON through the driver's actual
HTTP-reading assertion helper, with the original IDs, one provider start, no
release signal and an unfinished follower. It also verifies the exact GET and
returned receipt. No Core service, native process, browser or package is started.

- Before the comparison repair: **1 failed in 3.26 seconds**, with the same
  assertion message as CI.
- After the repair: **1 passed in 2.44 seconds**. No further execution followed.

Raw local logs are `/tmp/nebula-held-item-red.log` and
`/tmp/nebula-held-item-green.log`; their separate diff-bound selection receipts
have the corresponding `red` and `green` names under `/tmp/nebula-held-item-*`.
Independent final static review found no guard or call-site regression.

## Verification limits and CI scope

This proves the failed assertion's wire contract only. Live provider retention,
the full native desktop close/reopen/promotion/rollback journey and all later
packaged phases remain unverified by this local check. The earlier full packaged
case is still a failed receipt; it is not relabeled as passing.

All CI test arrays and counts are explicitly empty for the final push and merge,
as requested. Required selection, security metadata, omission and empty-browser
gate checks may run; no tests, desktop builds or previously passing cases do.
The earlier eight native browser and all tools-browser passes stay excluded.
PR #960 records the final check and merge results without another test push.
