# Host and global route integration

The remaining reviewed extraction repairs are integrated in `3aee2561`, rather
than left on the implementation worktrees. This is source integration with
limited runtime verification under the user's failed-case-only instruction.

- Host-owned runtime detection/selection, folder browsing/creation and guarded
  account refresh use the authenticated host facade and durable host effects.
- Project image preparation, status, events, cancellation and terminal capacity
  retain the exact Project identity. The closed Butler observation carries that
  identity too. Readiness can recover from current persisted host observations
  without replacing an existing image operation.
- Capacity remains unknown until Core answers. Both failed and cancelled reads
  offer an in-place check; stale or aborted Project responses cannot admit a
  terminal using invented counts.
- Global resource search, canonical identity and action resolution use SQL
  authority. No synthetic execution Project is introduced.
- Global Library writes use the existing host index and exact request/digest
  publication guards. A single upload capture slot bounds the expanded 20 MiB
  envelope; other request/vault defaults remain bounded. Verified global artifact
  streaming retains actor ownership and joins descriptor cleanup.
- Host, retained-kernel, closed Butler and automatic quota account reads share
  one protected reader boundary. Unsupported stdio readers remain unavailable;
  lease loss propagates and timeout cleanup is joined. API peers receive no new
  host execution capability.

Independent static review identified and rechecked these ownership/recovery
boundaries. The final source passed Mypy across 355 v3 modules, Ruff checks and
formatting, the diagnostic audit, and a clean production UI build. The bundled
guidance index is rebuilt and verified at 419 passages, source digest prefix
`1a5d44efab7e`; asset SHA-256 is
`d530b48df2d70a9ea8bc36574eb76def35213c0d3ecd6675f0b16698292c888f`.
The source trees match the reviewed build at `7444d940` (Python) and `2b0055c7`
(UI). Formatting changes have an explicit identical-AST receipt.

The first worktree Mypy invocation exposed five unchanged terminal annotations
because its shared editable environment pointed at another checkout. Fresh
review established this path difference; using the current source path produced
the 355-module pass. No terminal code or Mypy suppression was changed.

New regression sources are committed, but their additional test cases and new
operator journeys were **not executed or added to CI** under the user's current
restriction. Earlier seven operator checks and 28 frontend checks have separate
older-source receipts; they are not final integration acceptance. Library large
uploads/index publication, native account reader variants, and complete Project
setup/capacity/assistant journeys retain verification gaps. Installed vendor and
other-platform native support is not established by source review.

Only the specific still-failed cases in the current retry receipt may run on the
next push. Do not treat these static checks or omitted tests as runtime evidence.
