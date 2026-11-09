# Standing-instruction continuity contract

The operator saves context in a conversation, selects skills, starts or resumes a
goal, and sends a message through the ordinary chat completion entry point. The
active instructions must remain available verbatim while the saved transcript
and native runtime continue across turns, reconnect, Core restart and compaction.

| Journey step | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Discover/select | Existing Context, skills and goal controls retain their meaning | Core database and workspace | Existing interface; no UI changes planned |
| Save/use | One current instruction version is effective for each retained native context | Core snapshot and native instruction channel | Actual synthetic requests and harness events |
| Subsequent turn | An unchanged version does not accumulate in native conversation inputs | Provider/harness external session | Sequential accepted input capture |
| Edit/remove | Current instructions supersede the prior version deterministically | Saved context revisions and workspace | Update/removal lifecycle regression |
| Stream/interrupt | Failed or cancelled delivery does not consume the next delivery | Native outcome plus Core durable state | Rejection/cancellation/retry regression |
| Refresh/reconnect | Delivery is scoped to the actual native retained context, not a process connection | External session plus instruction version | Reconnect/restart capture |
| Goal resume | Full current instructions survive resumption independently of a summary | Core goal, skill snapshots and saved context | Goal continuation regression |
| Forced compaction | Exact active instructions survive lossy history compaction | Durable separate channel or deterministic reinjection | Compaction request/event capture |
| Concurrent/forked use | Independent retained contexts each receive their own current version | Turn admission and session identity | Concurrent/fork regression |
| Stateless requests | Each independently assembled request includes the full current instructions once | Core request assembly | Provider wire payload capture |
| Delete/revoke | Removed context is absent from future effective instruction versions | Core context status | Removal regression |

State owners are Core database records (`ChatDecision`, `ChatGoal`, skill
snapshots, `ChatTurn`, `HarnessTurn`, `HarnessSession`), the linked workspace for
project instructions, and vendor retained context. The browser does not own an
instruction delivery receipt. No controls, CSS, pairing, permission boundaries or
mobile layout changes are planned.

Required validation is focused Python lifecycle coverage plus a disposable
authenticated real-Core LAN journey using inert providers/harnesses. Raw
assembled requests/events must be retained with synthetic data only. Browser
geometry and production UI rebuild gates do not apply unless implementation
changes interface code; existing UI controls still need one real-Core journey.
Actual vendor-side compaction semantics cannot be established by an inert fixture;
any unverified native protocol guarantee must be stated as a remaining gap.
