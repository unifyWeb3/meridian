# Meridian

![Meridian](docs/banner.svg)

Every `status` field in your codebase is a state machine nobody wrote down.
Meridian finds them, proves each transition against your source, and fails CI
when one of them is illegal.

Not a prompt, not a linter, not vibes. A deterministic analyzer with an
acceptance gate and a hash-chained event log, running inside your Hermes agent
or in your pipeline.

---

## The problem

The bugs that hurt you the most are the "impossible" ones:

- `orders.refunded = True` from a draft, before any money moved
- `WAREHOUSE_HOLD` exists in your enum and in zero code paths, so your SLA
  report quietly fills with blanks

And the ones nobody can see at all: `force_close()` writes `delivered` from
any state, and no guard says so. Meridian reports that write as unguarded -
a suspicion, and a violation only under `--strict`.

Agents write more lifecycle code than humans review. Nobody documents the
machine, nobody tests the edges, and when it breaks you get a refund you
cannot explain.

Meridian turns `grep "status =="` into an auditable, enforceable state
machine: discovered from your code, anchored to `file:line`, gated by a spec
you review, checked on every change from then on.

## What it does

Real output, on a freshly merged order service with two seeded defects:

```
$ hermes meridian check --root examples/demo-order-pipeline
meridian check FAILED - 2 violation(s):
  orders.py:62  unverified_shipping -> shipped  (ship)  [not-allowed]
  orders.py:78  draft -> refunded  (refund)  [deny]
```

`orders.py:62` is a dead branch, a guard against a state nothing can enter.
`orders.py:78` is a refund accepted from `draft`, before money ever moved.

Scan shows the whole machine:

```
$ hermes meridian scan --root examples/demo-order-pipeline
meridian scan - 1 files, 4 machine(s), 2ms
  Order.status                     conf=medium states=11 transitions=15
  Order.payment_status             conf=medium states=4 transitions=3
  enum:OrderStatus                 conf=medium states=11 transitions=0
  enum:PaymentStatus               conf=medium states=4 transitions=0
```

Unreachable and dead states, unguarded writes, and spec violations all come
out with `file:line` anchors. Meridian then drafts the spec for you
(`meridian spec draft`); every edge it proposes is an observed, anchored
transition. You cross out what you do not want, you `accept`, and from that
moment `hermes meridian check` in CI is the law.

## What `check` tells you

`check` never prints a bare "OK". It reports one of three statuses:

| Status | Means |
| --- | --- |
| `violations` | the spec forbids something the code does; every violation is anchored to `file:line` |
| `spec-clean` | no violations, but something is still outstanding: warnings, unguarded writes, dead states, or machines the spec does not enforce |
| `healthy` | no violations and nothing outstanding |

Exit codes of `hermes meridian check`:

| Code | When |
| --- | --- |
| 0 | no violations, and every discovered machine is enforced by the spec |
| 1 | violations, or a discovered machine the spec does not enforce (`--allow-uncovered` waives the latter) |
| 2 | no `.meridian.yaml` at or above the root, or the spec has no entry for any machine in the repo - a green build that enforces nothing |

## Why a plugin, not a prompt

A prompt cannot do any of this mechanically:

| You need                                        | Prompt/skill | Meridian |
| ----------------------------------------------- | ------------ | -------- |
| Deterministic failure on a forbidden edge       | no           | `check` exits 1, in CI |
| Evidence you can audit later                    | no           | SHA-256 hash-chained log of check events (counts and machine ids) |
| Re-verification after every agent edit          | no           | advisory `post_tool_call` hook (never blocks) |
| Zero tokens, zero network, zero model required  | n/a          | stdlib only, installs nothing |

Where a human comes in (accepting the spec, writing the deny list) it is
because legality is your call, not the AI's. Meridian enforces exactly the
edges you accept, and says so when it cannot verify one.

## Who it is for

**Install it if** you own a service with mutable lifecycle state (orders,
tickets, subscriptions, jobs, deploys, payments) or you run agents on that
code and want a backstop when they refactor it. This is for your services,
not your utils.

**Do not install it if** your repo has no lifecycle state. It will find
nothing to enforce, which is correct behavior, not a bug.

## Use it

```bash
cd /path/to/your/project        # a project directory, not $HOME
hermes plugins install unifyWeb3/meridian --enable   # or from the catalog
hermes meridian scan
hermes meridian check --strict        # exit 1 on violations; wire into CI
hermes meridian report --out .meridian/report.md
hermes meridian ledger verify
```

The tools an agent calls (`meridian_scan`, `meridian_verify`, `meridian_spec`,
`meridian_report`) take a `root` argument, and that argument is model
controlled - so it is confined to the agent's working directory. A working
directory of `$HOME`, `/` or the Hermes home is refused outright:

```
the working directory /home/you is too broad to confine Meridian - run it from a project directory
```

The `hermes meridian` CLI takes its `--root` from you, the operator, and is
deliberately unconfined.

In a session:

```
You:   map the state machines in this repo, verify them, show me what breaks
Agent: meridian_scan -> meridian_verify -> meridian_spec draft
You:   accept, but cross out the warehouse_hold edges
Agent: meridian_spec accept
```

`spec accept` merges into an existing `.meridian.yaml`: every deny rule and
every edge you reviewed survives, only newly discovered machines and edges
are added, and the previous file is kept as `.meridian.yaml.bak`. It never
overwrites a reviewed rule - `force` is accepted and does nothing.

The `post_tool_call` hook is advisory. After a write-shaped tool call it
re-verifies the repo and surfaces what it finds as context on the next turn
of the same session. It never blocks a call, never approves one, and never
fails a build: the gate is `check`.

## Honest limits

- Meridian enforces the accepted spec. If your spec is wrong, your check is
  wrong. Review the draft before accepting.
- Python analysis is AST-based and authoritative. TypeScript and JavaScript
  are lexical: union-type alphabets are exact, other evidence is labeled
  low/medium confidence, and JS transitions are always unguarded.
- Unguarded writes are suspicions, not guilty verdicts. Strict mode
  (`--strict`, or `strict=true` on the tools) turns them into violations -
  but only where a `.meridian.yaml` exists, because that is where there is
  something to check the write against. Without a spec, unguarded writes stay
  notes.
- Nested `def` and `lambda` scopes are not analyzed; a warning lists every
  writer.
- Unreachable states are reported only when an initial state is known.
- `check` fails when a discovered machine is not enforced by the spec: no
  entry at all, or an entry that states no rule (non-enum machines; enum-only
  machines carry no edges and are exempt). Waive with `--allow-uncovered`.
- The ledger is an append-only log of the events Meridian ran: event names,
  machine ids and aggregate counts (files scanned, violations found). It
  stores no verdicts, no file:line anchors, no spec or source revisions and
  no file contents. Its chain is **unkeyed SHA-256**, so it detects edits to
  the log, but anyone with write access to the ledger file can recompute the
  chain. Tamper-evident, not tamper-proof.

## Develop and test

```bash
python3 -m unittest discover -s tests -v   # 28 checks, stdlib only
```

Plugin layout: `plugin.yaml` + `__init__.py` + `analyzer/` (scan, verify,
spec, ledger) + `schemas.py` + `tools.py` + `skills/audit/SKILL.md`.

## License

MIT, see [LICENSE](LICENSE).
