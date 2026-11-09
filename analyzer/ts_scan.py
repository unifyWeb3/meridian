"""TypeScript/JavaScript extraction - lexical and best-effort.

This is deliberately not a compiler. String-literal union types give us
exact state alphabets with high confidence. Everything else is anchored
(file:line) with `low` confidence and **never** an invented source state:
writes are emitted as unguarded, because proximity to a `===` is not a
guard. JS/TS findings are advisory; the Python AST pass is authoritative.
"""

from __future__ import annotations

import bisect
import re
from pathlib import Path

from .model import Machine, Transition
from .repo import relpath

_NAME = r"[A-Za-z_$][\w$]*"
_LIT = r"[\"'`][a-zA-Z][a-zA-Z0-9_-]{0,39}[\"'`]"
_UNION_HEAD = re.compile(rf"\btype\s+({_NAME})\s*=\s*")
_LITS = re.compile(r"[\"'`]([a-zA-Z][a-zA-Z0-9_-]{0,39})[\"'`]")
_WRITE = re.compile(
    rf"\.(status|state|phase|stage|step|disposition|lifecycle|mode|workflow|fulfillment)\s*(?::|=)\s*([\"'])([a-zA-Z][a-zA-Z0-9_-]{{0,39}})\2"
)
_CLASS = re.compile(rf"\bclass\s+({_NAME})")
_FUNC = re.compile(rf"(?:function\s+({_NAME})|({_NAME})\s*=\s*(?:async\s*)?\([^)]*\)\s*=>|({_NAME})\s*\([^)]*\)\s*{{)")
_MAX = 2000

_COMMENT_LINE = re.compile(r"^\s*(//|\*|/\*)")


def _line_index(text: str) -> list[int]:
    """Offsets of each line start, for O(log n) line lookup."""
    return [0] + [m.start() + 1 for m in re.finditer("\n", text)]


def _line_of_text(text: str, index: list[int], pos: int) -> int:
    return bisect.bisect_right(index, pos)




def _union_body(text: str, start: int) -> str:
    """The union's text: everything until a newline that does not continue
    with a pipe, a `;`, or a `{` (object type) - handles both
    `type X = "a" | "b"` and the leading-pipe-per-line idiom."""
    tail = text[start:start + 4000]
    out = []
    for ch in tail:
        if ch in ";{}":
            break
        if ch == "\n":
            rest = tail[len(out) + 1:]
            stripped = rest.lstrip(" \t")
            if stripped.startswith("|"):
                out.append("\n")
                continue
            break
        out.append(ch)
    return "".join(out)


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
        index = _line_index(text)
        lines = text.split("\n")
        commented = [bool(_COMMENT_LINE.match(ln)) for ln in lines]

        def in_comment(pos: int) -> bool:
            line_no = bisect.bisect_right(index, pos)
            return commented[line_no - 1] if 0 < line_no <= len(commented) else False

        # 1) union types -> exact state alphabets
        for m in _UNION_HEAD.finditer(text):
            if in_comment(m.start()):
                continue
            body = _union_body(text, m.end())
            states = sorted(set(_LITS.findall(body)))
            if len(states) < 2:
                continue
            machine = get_machine(f"union:{m.group(1)}", m.group(1), m.group(1), language, "high")
            machine.states = sorted(set(machine.states) | set(states))
            machine.sources.append(rel)
            machine.notes.append("states from string-literal union type")

        # 2) anchored writes - always unguarded (no dataflow in a lexical pass)
        count = 0
        for m in _WRITE.finditer(text):
            if count >= _MAX:
                warnings.append(f"{rel}: write evidence capped at {_MAX}")
                break
            if in_comment(m.start()):
                continue
            count += 1
            field_name = m.group(1)
            dst = m.group(3)
            cls_match = None
            for cm in _CLASS.finditer(text[:m.start()]):
                cls_match = cm
            cls = cls_match.group(1) if cls_match else None
            window = text[max(0, m.start() - 3000):m.start()]
            via = "anonymous"
            for fm in _FUNC.finditer(window):
                name = next((g for g in fm.groups() if g), None)
                if name:
                    via = name
            owner = cls or rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            machine_id = f"{cls}.{field_name}" if cls else f"{owner}.{field_name}"
            machine = get_machine(machine_id, owner, field_name, language, "low")
            machine.sources.append(rel)
            machine.notes.append("TS/JS lexical evidence: transitions are unguarded by construction")
            line = _line_of_text(text, index, m.start())
            machine.transitions.append(
                Transition(src=None, dst=dst, via=via, file=rel, line=line,
                           guarded=False, kind="write"))
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
