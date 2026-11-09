# Meridian

![Banner](docs/banner.svg)

**Every `status` field in your codebase is a state machine nobody wrote down.**
Meridian finds them, proves every transition against your source, and fails CI
when one of them is illegal.

*Not* a prompt, *not* a linter, *not* vibes. A deterministic analyzer with an
acceptance gate and a tamper-evident ledger — running inside your Hermes agent
or in your pipeline.

---

## The problem this solves

The bugs that hurt you most are the "impossible" ones:

- `orders.refunded = True` while nobody ever paid for the order
- a ticket that `force_close()` marks `delivered` no matter where it was
- a `WAREHOUSE_HOLD` status that exists in your enum and nowhere in your code —
  three months of "why is the SLA report full of blanks?"

Agents write more lifecycle code than humans review. Nobody documents the
machine; nobody tests the edges; and when it breaks you get a refund you
cannot explain and a customer who won't buy again.

Meridian turns `grep status ==` into an auditable, enforceable state machine:
discovered from your code, anchored to `file:line`, gated by a spec **you**
review, and checked on every change from now on.

## What it actually does (90 seconds, real output)

Point it at a freshly-merged order service with two seeded defects:

```
$ hermes meridian check --root examples/demo-order-pipeline
meridian check FAILED — 2 violation(s):
  orders.py:62  unverified_shipping -> shipped  (ship)  [not-allowed]
  orders.py:78  draft -> refunded  (refund)  [deny]
```

- `orders.py:62` — a dead branch: a guard comparing against a state nothing can
  enter (`unverified_shipping`). Dead code wearing a feature's clothes.
- `orders.py:78` — a refund path accepted from `draft`, before money ever
  moved. Impossible, and very real.

Scan it instead and you see everything it cannot legally reach:

```
Order.status — 11 states, initial draft (constructor), reachable 9
  unreachable: unverified_shipping, warehouse_hold
  dead:        unverified_shipping, warehouse_hold
  unguarded:   orders.py:84 force_close()
```

Then Meridian drafts the spec for you (`meridian spec draft`) — every edge it
proposes is an **observed, anchored** transition. You cross out the ones you
don't want. You `accept`. From that moment, `hermes meridian check` in CI is
the law, and your agent's edits are re-verified on the next turn instead of
quietly reintroducing the bug.

## Why a plugin, not a prompt

Because a prompt cannot do this, mechanically:

| You need | Prompt/skill | Meridian |
| --- | --- | --- |
| Deterministic failure on a forbidden edge | no | `check` exits 1, on commit or in CI |
| Evidence you can audit later | no | SHA-256 hash-chained, append-only ledger; `ledger verify` proves it |
| Re-verification after every agent edit | no | `post_tool_call` hook re-scans and tells the next turn |
| Zero tokens, zero network, zero model required | n/a | stdlib only, installs nothing, runs offline |

And where a human does come in — accepting the spec, writing the deny list —
it's because *legality is your call, not the AI's*. Meridian enforces exactly
the edges you accept, and says so when it can't verify one.

## Who this is for (and who it isn't)

**Install it if** you own a service with at least one status/state field
(order flows, ticketing, subscriptions, jobs, deploys, payments) *or* you run
agents on that code and want a deterministic backstop when they refactor
lifecycle code. This is the tool for your `services/`, not your `utils/`.

**Don't install it if** your repo has no lifecycle state at all — it will find
nothing to enforce, and that's correct behavior, not a bug.

## Use it

```bash
hermes plugins install unifyWeb3/meridian --enable   # or from the catalog
hermes meridian scan .
hermes meridian check --strict        # exit 1 on violations — wire into CI
hermes meridian report --out .meridian/report.md
hermes meridian ledger verify
```

In a session:

```
You: map the state machines in this repo, verify them, and show me what breaks
Agent: meridian_scan → meridian_verify → meridian_spec draft
You: accept, but cross out the warehouse_hold edges
Agent: meridian_spec accept   # from now on, CI enforces it
```

## Honest limits

- Meridian enforces the **accepted spec**. If your spec is wrong, your check
  is wrong. Review the draft before accepting.
- Python analysis is AST-based and authoritative. TypeScript/JavaScript is
  lexical: union-type alphabets are exact, other evidence is labeled
  low/medium confidence, and JS transitions are always unguarded by
  construction.
- Unguarded writes are suspicions, not guilty verdicts.
- Nested `def`/`lambda` scopes are not analyzed; a warning lists every writer.
- Unreachable states are reported only when an initial state is known.
- Enforcement covers machines in the spec; non-enum machines outside it fail
  `check` (waive with `--allow-uncovered`). Enum-only machines are exempt
  because they carry no edges.

## Develop & test

```bash
python3 -m unittest discover -s tests -v   # 28 checks, stdlib only
```

Plugin layout: `plugin.yaml` + `__init__.py` + `analyzer/` (scan → verify →
spec → ledger) + `schemas.py` + `tools.py` + `skills/audit/SKILL.md`.

## License

MIT — see [LICENSE](LICENSE).
