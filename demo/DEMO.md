# Meridian - 90-second demo script

Everything below is pre-baked. Rehearse once, record once. Do not type on
camera beyond the lines marked.

**Setup (before the take):**

- Terminal font >= 18pt, dark theme, window about 110x30.
- A clone of the repo at `meridian/examples/demo-order-pipeline/`.
- A GUI chat window running Hermes with Meridian enabled, side by side with the terminal.
- `sed -n '70,85p' orders.py` ready in a spare tab (the `refund()` function).
- The demo repo ships `.meridian.yaml` with the two harmful edges deliberately
  excluded: `draft -> refunded` (deny) and `unverified_shipping -> shipped`.

**Beat table:**

| t     | Action                                                                            | Voice-over (short)                          |
| ----- | --------------------------------------------------------------------------------- | ------------------------------------------- |
| 0:00  | Terminal: `ls` shows `orders.py` and `.meridian.yaml`                             | "Two files. A checkout flow. No machine docs." |
| 0:05  | GUI chat: "Show me the state machines in this repo and check them."               | "Hermes, on the right."                     |
| 0:10  | Hermes calls `meridian_scan` -> table of 4 machines                               | "Meridian maps every status field. AST, not vibes." |
| 0:18  | Hermes calls `meridian_verify` -> 2 violations + unreachable state                 | "Two findings: a dead branch nobody can enter, and a refund that never moved money." |
| 0:30  | Terminal view: `sed -n '74,80p' orders.py` highlights the `DRAFT` in the tuple     | "Line 78. The guard had DRAFT in the refund list." |
| 0:40  | Back to chat: "Fix the refund bug." Hermes writes the fix, saves.                 | "Watch Hermes fix it."                      |
| 0:50  | Ask: "did that edit break anything?" Hermes calls `meridian_verify` -> still red on `orders.py:62` | "It answers with evidence, not confidence." |
| 0:58  | Terminal: `hermes meridian check` -> still red: `orders.py:62 ... [not-allowed]`   | "One fix is not enough. It refuses to rubber-stamp." |
| 1:08  | Hermes removes the dead `UNVERIFIED_SHIPPING` branch, `hermes meridian check` -> OK | "Green. That check now runs in CI."          |
| 1:16  | `hermes meridian ledger verify` -> "OK - N entries"                                | "Every verdict, chained. Nobody edits it quietly." |
| 1:22  | `meridian_diagram Order.status` for one second (Mermaid)                           | "Also: the diagram, generated from the code." |
| 1:30  | End card: MERIDIAN - "The machines your codebase already has."                     | "Meridian. Open source. MIT."               |

**Post to X** with the video, tag `@NousResearch`, mention the catalog PR.

**Insurance (if anything fails live):**

- `python3 orders.py` runs the happy path with no Meridian at all.
- `python3 -m unittest discover -s tests -v` proves the plugin suite is green (28 checks).
