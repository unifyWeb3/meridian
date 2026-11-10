---
name: audit
description: Audit a repository for implicit, undeclared state machines and verify lifecycle code with anchored evidence. Use when the user asks to review status/state/lifecycle code, chase an "impossible state" bug, review an agent-written change to such code, or gate a refactor with CI.
---

# Meridian audit workflow

Order matters. Evidence first, interpretation second, acceptance third.

1. **Discover** - `meridian_scan(root=<repo>)`. Lists every discovered machine:
   states, initial states, transitions with file:line anchors, unreachable and
   dead states. Never assert a state or transition that scan did not return.
2. **Verify** - `meridian_verify(root=<repo>)` (optionally `machine_id=`).
   Reports guarded/unguarded writes and, when `.meridian.yaml` exists, hard
   violations with anchors. Call `meridian_spec(action="check")` for the CI view.
3. **Interpret** - for each violation or smell, open the anchored file:line
   before commenting. Quote the anchor exactly (`path:line`). If you cannot
   anchor it, say so instead of asserting it.
4. **Accept (human in the loop)** - when the repo has no `.meridian.yaml`,
   propose `meridian_spec(action="draft")`. Show the user every edge and let
   them trim false positives. Only call `action="accept"` after the user
   confirms the edge list. Refusing to overwrite `.meridian.yaml` without
   their approval is correct behavior.
5. **After edits** - any edit to state-machine files must be followed by
   another `meridian_verify` before you report the change done. Meridian's
   auto-verify hook will do the same rescan and note violations in the next
   turn; treat that note as a failing test, not as background noise.
6. **Report** - `meridian_report` gives the Markdown summary (with ledger
   head). Attach it to the review/PR. `meridian_ledger(action="verify")`
   detects edits to the event log. The chain is unkeyed SHA-256: it is
   tamper-evident, not tamper-proof - someone with write access to the ledger
   file can recompute it.

## What Meridian cannot know

- Domain legality. Meridian enforces the *accepted* spec; if the spec is
  wrong, the check is wrong. Review the draft.
- JS/TS evidence is lexical and marked low/medium confidence; a Python AST
  scan is authoritative for .py files.
- Unguarded writes are suspicions, not guilty verdicts - treat them as
  review prompts.

## Red lines

- Never check `.meridian.yaml` in without user approval.
- Never claim "all state machines verified" - claim what the output shows.
