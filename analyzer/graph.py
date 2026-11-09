"""Deterministic verdicts on discovered machines.

    reachable    — BFS from the discovered initial states
    unreachable  — states no path from initial can reach
    dead         — states never written at all (enum/annot only)
    terminal     — states with no outgoing transition (informational)
    unguarded    — writes with no same-function comparison against the field
    violations   — anchored (src -> dst) edges the spec forbids or omits

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

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {
            "machine_id": self.machine_id,
            "ok": self.ok,
            "reachable": self.reachable,
            "unreachable": self.unreachable,
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
    seeds = initial if initial else states
    reachable: set[str] = set()
    frontier = list(seeds)
    while frontier:
        cur = frontier.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        for dst in adjacency.get(cur, []) + adjacency.get(None, []):
            if dst in states:
                frontier.append(dst)

    unreachable = sorted(set(states) - reachable)

    # --- dead states: never constructed, never written ---------------------
    incoming: set[str] = {t.dst for t in machine.transitions}
    if not machine.transitions and not machine.initial:
        dead: list[str] = []
    else:
        dead = sorted(set(states) - incoming - set(initial))

    # --- terminal states ---------------------------------------------------
    outgoing = {t.src for t in machine.transitions if t.src is not None}
    terminal = sorted(set(reachable) - outgoing)

    # --- unguarded writes ---------------------------------------------------
    unguarded = [t for t in machine.transitions if t.guarded is False]
    guarded = len(machine.transitions) - len(unguarded)
    unguarded_out = [
        {"anchor": t.anchor(), "dst": t.dst, "via": t.via} for t in unguarded[:25]
    ]

    # --- spec enforcement ---------------------------------------------------
    violations: list[Violation] = []
    warnings: list[str] = []
    notes: list[str] = []
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
                f"{len(unguarded)} unguarded write(s) — accept a spec with `hermes meridian spec accept` to enforce")

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
    )
