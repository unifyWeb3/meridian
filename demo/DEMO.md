# Meridian — 90-second demo script

Everything below is pre-baked. Rehearse once; record once. No typing on camera
beyond the lines marked.

**Setup (before recording):**
- Terminal font ≥ 18pt, dark theme, window ~110×30.
- `cd` into `examples/demo-order-pipeline/` from a clone of the repo.
- Have `sed -n '70,85p' orders.py` ready in a second tab (the `refund()` function) — used at 0:35.
- The demo repo ships with `.meridian.yaml` (spec accepted with the two
  harmful edges deliberately excluded: `draft → refunded` and `unverified_shipping → shipped`).

**Script:**

| t | Screen action | Voice-over (short!) |
|---|---|---|
| 0:00 | `ls` — one ordinary `orders.py` and a `.meridian.yaml` | "Two files. One checkout pipeline. Zero state machine docs." |
| 0:05 | Launch `hermes` in the repo | "Hermes, find the state machines in this app." |
| 0:10 | Agent calls `meridian_scan` → 4 machines; `Order.status`: 11 states, 2 unreachable | "Meridian maps every status field. No model assertions — AST. The unreachable one should never exist." |
| 0:20 | Agent calls `meridian_verify` → deny on `draft → refunded` + dead branch | "Two findings: an unreachable state… and a refund path that money never touched." |
| 0:30 | Switch to file view: `refund()` contains `OrderStatus.DRAFT` in the tuple | "Line 78. The guard list had DRAFT in it. That's the bug — nobody can see it by reading code." |
| 0:40 | Agent calls `meridian_spec check` → FAILS with `orders.py:78 draft → refunded [deny]` | "The spec — which the human reviewed — makes it a hard stop." |
| 0:50 | Fix one: delete `OrderStatus.DRAFT` from the refund tuple → re-run check → still red: `orders.py:62 unverified_shipping → shipped [not-allowed]` | "One finding down. Meridian doesn't rubber-stamp a half-fix." |
| 0:55 | Fix two: delete the dead `UNVERIFIED_SHIPPING` branch in `ship()` → `hermes meridian check` → OK, exit 0 | "Green. That check now runs in CI." |
| 1:05 | `hermes meridian ledger verify` → "OK — N entries" | "Every verdict, chained. Nobody edits it quietly." |
| 1:15 | Show Mermaid diagram output (`meridian_diagram Order.status`) for one second | "Also: diagrams you can paste in docs." |
| 1:22 | End card: name + tagline + "Zero deps. Zero network. Open source." | "Meridian. The machines your codebase already has — finally on the record." |

**Recording needs:** a terminal, ~90 seconds, the two Meridian outputs.
**Post on X** with the video, tag `@NousResearch`, and mention `plugin-catalog/meridian.yaml`.

**Demo insurance (if something fails live):**
- `python3 orders.py` runs the happy path with no Meridian at all — proves nothing else broke.
- `python3 -m unittest discover -s tests -v` — 28 checks, all green.
