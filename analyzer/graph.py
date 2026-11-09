"""Deterministic verdicts on discovered machines.

    reachable    - BFS from the discovered initial states
    unreachable  - states no path from initial can reach
    dead         - states never written at all (enum/annot only)
    terminal     - states with no outgoing transition (informational)
    unguarded    - writes with no same-function comparison against the field
    violations   - anchored (src -> dst) edges the spec forbids or omits

Nothing here uses a model. Inputs decide outputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Machine, Transition
from .spec import SpecMachine


@dataclass
class Violation:
    machine_id: str
    src: str
    dst: str
    file: str
    line: int
    via: str
    rule: str           # "deny" | "not-allowed" | "unverifiable"
    detail: str

    def anchor(self) -> str:
        return f"{self.file}:{self.line}"

    def to_dict(self) -> dict:
        return {
            "machine_id": self.machine_id, "src": self.src, "dst": self.dst,
            "file": self.file, "line": self.line, "via": self.via,
            "rule": self.rule, "detail": self.detail, "anchor": self.anchor(),
        }


@dataclass
class MachineVerdict:
    machine_id: str
    reachable: list[str]
    unreachable: list[str]
    dead: list[str]
    terminal: list[str]
    guarded_writes: int
    unguarded_writes: list[dict]
    violations: list[Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unreachable_reported: bool = True

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {
            "machine_id": self.machine_id,
            "ok": self.ok,
            "reachable": self.reachable,
            "unreachable": self.unreachable,
            "unreachable_reported": self.unreachable_reported,
            "dead": self.dead,
            "terminal": self.terminal,
            "guarded_writes": self.guarded_writes,
            "unguarded_writes": self.unguarded_writes,
            "violations": [v.to_dict() for v in self.violations],
            "warnings": self.warnings,
            "notes": self.notes,
        }


def _adjacency(machine: Machine) -> dict[str | None, list[str]]:
    adj: dict[str | None, list[str]] = {}
    for t in machine.transitions:
        if t.dst:
            adj.setdefault(t.src, []).append(t.dst)
    return adj


def analyze_machine(
    machine: Machine,
    spec: SpecMachine | None = None,
    *,
    strict: bool = False,
) -> MachineVerdict:
    states = list(machine.states)
    initial = [s for s in machine.initial if s in states]
    adjacency = _adjacency(machine)

    # --- reachability -----------------------------------------------------
    # Entry (src=None) writes are optimistic one-time seeds: a write that
    # can fire from any state may produce any of its targets. We do NOT
    # union general states in as seeds, because pretending every state is
    # an initial hides unreachable bugs - but when no initial was
    # discovered at all, reachability is unknown and we say so instead of
    # reporting an empty "unreachable".
    entry_seeds = adjacency.get(None, [])
    initial_known = bool(initial)
    seeds = initial if initial else states
    reachable: set[str] = set()
    frontier = list(seeds) + entry_seeds
    while frontier:
        cur = frontier.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        for dst in adjacency.get(cur, []):
            if dst in states:
                frontier.append(dst)

    unreachable_reported = initial_known
    unreachable = sorted(set(states) - reachable) if initial_known else []

    # --- dead states: never constructed, never written ---------------------
    incoming: set[str] = {t.dst for t in machine.transitions}
    if not machine.transitions and not machine.initial:
        dead: list[str] = []
    else:
        dead = sorted(set(states) - incoming - set(initial))

    # --- terminal states ---------------------------------------------------
    outgoing = {t.src for t in machine.transitions if t.src is not None}
    terminal = sorted(set(reachable) - outgoing)

    notes: list[str] = []
    if not initial_known:
        notes.append("no initial state discovered; unreachable-states not reported")
    if entry_seeds:
        notes.append(f"{len(entry_seeds)} unguarded write(s) treated as potentially reachable")

    # --- unguarded writes (constructor writes are expected, not findings) ---
    unguarded_all = [t for t in machine.transitions if t.guarded is False]
    unguarded = [t for t in unguarded_all if t.kind != "constructor"]
    guarded = len(machine.transitions) - len(unguarded_all)
    unguarded_out = [
        {"anchor": t.anchor(), "dst": t.dst, "via": t.via} for t in unguarded[:25]
    ]

    # --- spec enforcement ---------------------------------------------------
    violations: list[Violation] = []
    warnings: list[str] = []
    if not machine.transitions and not machine.initial:
        notes.append("enum-only machine: transitions were never anchored; "
                     "dead-state reporting disabled")

    if spec is not None:
        unknown = sorted(set(states) - set(spec.states) - set(initial)) if spec.states else []
        if unknown:
            notes.append(f"states not in spec: {', '.join(unknown)}")
        missing = sorted(set(spec.states) - set(states)) if spec.states else []
        if missing:
            warnings.append(f"spec lists states never found in code: {', '.join(missing)}")
        deny = spec.deny or set()
        allow = spec.allow
        for t in machine.transitions:
            if t.src is None:
                if t.kind == "constructor":
                    # Constructors are verifiable: the spec's initial states are
                    # the contract. In strict mode a constructor that builds a
                    # non-initial state is a real violation.
                    if strict and spec.initial and t.dst not in spec.initial:
                        violations.append(Violation(
                            machine.id, "init", t.dst, t.file, t.line, t.via, "initial",
                            f"constructor initializes '{t.dst}', not in spec initial ({', '.join(spec.initial)})"))
                    continue
                if strict:
                    violations.append(Violation(
                        machine.id, "*", t.dst, t.file, t.line, t.via, "unverifiable",
                        f"unguarded write to '{t.dst}' cannot be verified against the spec"))
                else:
                    warnings.append(f"unguarded write {t.anchor()} -> {t.dst} (not verifiable; enable strict to fail)")
                continue
            edge = (t.src, t.dst)
            if edge in deny:
                violations.append(Violation(
                    machine.id, t.src, t.dst, t.file, t.line, t.via, "deny",
                    f"spec explicitly forbids {t.src} -> {t.dst}"))
            elif allow and edge not in allow:
                violations.append(Violation(
                    machine.id, t.src, t.dst, t.file, t.line, t.via, "not-allowed",
                    f"{t.src} -> {t.dst} is not in the allowed transitions of {machine.id}"))
    else:
        if unguarded:
            notes.append(
                f"{len(unguarded)} unguarded write(s) - accept a spec with `hermes meridian spec accept` to enforce")

    return MachineVerdict(
        machine_id=machine.id,
        reachable=sorted(reachable),
        unreachable=unreachable,
        dead=dead,
        terminal=terminal,
        guarded_writes=guarded,
        unguarded_writes=unguarded_out,
        violations=violations,
        warnings=warnings,
        notes=notes,
        unreachable_reported=unreachable_reported,
    )
