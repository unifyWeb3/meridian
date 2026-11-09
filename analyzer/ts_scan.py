"""TypeScript/JavaScript extraction — lexical and best-effort.

This is deliberately not a compiler. Union string-literal types give us
exact state sets with high confidence; everything else is anchor-anchored
(file:line) with `low`/`medium` confidence so the verifier can lean on the
Python path and treat JS evidence as advisory.
"""

from __future__ import annotations

import re
from pathlib import Path

from .model import Machine, Transition
from .repo import relpath

_NAME = r"[A-Za-z_$][\w$]*"
_LIT = r"(?:\"[^\"\n]{1,40}\"|'[^'\n]{1,40}'|`[^`\n]{1,40}`)"
_UNION = re.compile(rf"\btype\s+({_NAME})\s*=\s*({_LIT}(?:\s*\|\s*{_LIT})+)", re.MULTILINE)
_WRITE = re.compile(
    rf"\.(status|state|phase|stage|step|disposition|lifecycle|mode|workflow|fulfillment)\s*(?::|=)\s*([\"'])([a-zA-Z][a-zA-Z0-9_-]{{0,39}})\2"
)
_CLASS = re.compile(rf"\bclass\s+({_NAME})")
_FUNC = re.compile(rf"(?:function\s+({_NAME})|({_NAME})\s*=\s*(?:async\s*)?\([^)]*\)\s*=>|({_NAME})\s*\([^)]*\)\s*{{)")
_MAX = 2000

def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _enclosing_name(text: str, index: int) -> str:
    window = text[max(0, index - 3000):index]
    best, best_pos = "anonymous", -1
    for m in _FUNC.finditer(window):
        name = next(g for g in m.groups() if g)
        if m.start() >= best_pos:
            best, best_pos = name, m.start()
    return best


def _enclosing_class(text: str, index: int) -> str | None:
    window = text[:index]
    match = None
    for m in _CLASS.finditer(window):
        match = m
    return match.group(1) if match else None


def _literal_values(group: str) -> list[str]:
    return re.findall(r"""["'`]([a-zA-Z][a-zA-Z0-9_-]{0,39})["'`]""", group)


def _guard_literals(text: str, index: int, window_lines: int = 120) -> list[str]:
    line = _line_of(text, index)
    lines = text.splitlines()
    lo = max(0, line - 1 - window_lines)
    hi = min(len(lines), line + 20)
    guards: list[str] = []
    for raw in lines[lo:hi]:
        for m in re.finditer(r"""(?:===?|case)\s*(["'`])([a-zA-Z][a-zA-Z0-9_-]{0,39})\1""", raw):
            guards.append(m.group(2))
    return sorted(set(guards))


def scan_typescript(ts_files: list[Path], root: Path) -> tuple[list[Machine], list[str]]:
    machines: dict[str, Machine] = {}
    warnings: list[str] = []

    def get_machine(machine_id: str, owner: str, field_name: str, language: str, conf: str) -> Machine:
        if machine_id not in machines:
            machines[machine_id] = Machine(id=machine_id, field=field_name, owner=owner,
                                           language=language, confidence=conf)
        return machines[machine_id]

    for path in ts_files:
        rel = relpath(path, root)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"{rel}: unreadable ({exc})")
            continue
        language = "typescript" if path.suffix.lower() in {".ts", ".tsx"} else "javascript"

        # 1) union types -> exact state sets
        for m in _UNION.finditer(text):
            states = sorted(set(_literal_values(m.group(2))))
            if len(states) < 2:
                continue
            machine = get_machine(f"union:{m.group(1)}", m.group(1), m.group(1), language, "high")
            machine.states = sorted(set(machine.states) | set(states))
            machine.sources.append(rel)
            machine.notes.append("states from string-literal union type")

        # 2) anchored writes
        count = 0
        for m in _WRITE.finditer(text):
            if count >= _MAX:
                warnings.append(f"{rel}: write evidence capped at {_MAX}")
                break
            count += 1
            field_name = m.group(1)
            dst = m.group(3)
            cls = _enclosing_class(text, m.start())
            via = _enclosing_name(text, m.start())
            owner = cls or rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            machine_id = f"{cls}.{field_name}" if cls else f"{owner}.{field_name}"
            machine = get_machine(machine_id, owner, field_name, language, "low")
            machine.sources.append(rel)
            guards = _guard_literals(text, m.start())
            line = _line_of(text, m.start())
            srcs = guards if guards else [None]
            for src in srcs:
                machine.transitions.append(
                    Transition(src=src, dst=dst, via=via, file=rel, line=line,
                               guarded=bool(guards), kind="write")
                )
            machine.states.append(dst)
            if via == "constructor" or via.startswith(("create", "make", "init")):
                machine.initial.append(dst)

    for machine in machines.values():
        machine.states = sorted(set(machine.states))
        machine.initial = sorted(set(s for s in machine.initial if s in machine.states))
        machine.sources = sorted(set(machine.sources))
        if machine.confidence != "high" and len(machine.states) >= 2:
            machine.confidence = "medium"
    return list(machines.values()), warnings
