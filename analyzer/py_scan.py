"""Python (ast) extraction for implicit state machines.

Deterministic and total: syntax errors are recorded and skipped, never
raised. Guarded writes are attributed to the branch that encloses the
write (`if`/`elif`/`while` tests only). A write with no enclosing branch
is unguarded — never assigned a fabricated source state.

Every claim carries file:line evidence. Nothing here uses a model.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .model import Machine, Transition
from .repo import relpath

_STATE_TOKEN = re.compile(
    r"(?:^|_)(status|state|phase|stage|step|disposition|lifecycle|mode|workflow|fulfillment)(?:_|$)",
    re.IGNORECASE,
)
_LITERAL = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,39}$")


def _is_state_name(name: str) -> bool:
    n = name.lstrip("_").lower()
    if n in {"status", "state", "phase", "stage", "step", "disposition", "lifecycle", "mode"}:
        return True
    return bool(_STATE_TOKEN.search("_" + n + "_"))


def _is_state_literal(value: str) -> bool:
    return bool(_LITERAL.match(value))


def _literal_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _iter_files(py_files: list[Path], root: Path) -> dict[str, ast.Module | None]:
    parsed: dict[str, ast.Module | None] = {}
    for path in py_files:
        rel = relpath(path, root)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            parsed[rel] = ast.parse(source, filename=rel)
        except Exception:  # syntax errors are data, not crashes
            parsed[rel] = None
    return parsed


def _enum_table(parsed: dict[str, ast.Module | None]) -> dict[str, dict[str, str]]:
    """enum_class_name -> {MEMBER: value} for str-like enums."""
    table: dict[str, dict[str, str]] = {}
    for tree in parsed.values():
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            is_enum = any(
                (isinstance(b, ast.Name) and b.id in {"Enum", "IntEnum", "StrEnum"})
                or (isinstance(b, ast.Attribute) and b.attr in {"Enum", "IntEnum", "StrEnum"})
                for b in node.bases
            )
            if not is_enum:
                continue
            members: dict[str, str] = {}
            for stmt in node.body:
                target = None
                value = None
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                    value = _literal_str(stmt.value)
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    target = stmt.target
                    value = _literal_str(stmt.value)
                if isinstance(target, ast.Name):
                    members[target.id] = value if value is not None else target.id.lower()
            if members:
                table[node.name] = members
    return table


def _constant_table(parsed: dict[str, ast.Module | None]) -> tuple[dict[str, str], list[str]]:
    """module-level NAME = "literal" for resolving writes like self.status = PAID.

    Drops names defined in more than one module with different values
    (silent shadowing is how verdicts get fabricated); ambiguous names are
    reported, never silently resolved.
    """
    per_name: dict[str, dict[str, str]] = {}
    for rel, tree in parsed.items():
        if tree is None:
            continue
        for stmt in tree.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and (value := _literal_str(stmt.value)) is not None
            ):
                per_name.setdefault(stmt.targets[0].id, {})[rel] = value
    table: dict[str, str] = {}
    dropped: list[str] = []
    for name, defs in per_name.items():
        distinct = set(defs.values())
        if len(distinct) == 1:
            table[name] = next(iter(distinct))
        else:
            dropped.append(name)
    return table, dropped


def _resolve_value(
    node: ast.AST | None,
    enums: dict[str, dict[str, str]],
    constants: dict[str, str],
    hits: list[str] | None = None,
) -> str | None:
    """Resolve a node to a literal state value. When an enum produced the
    value, its class name is appended to `hits` so the caller can fold in
    the enum's full alphabet."""
    if node is None:
        return None
    direct = _literal_str(node)
    if direct is not None:
        return direct
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Attribute):
        enum_name = None
        if isinstance(node.value, ast.Name):
            enum_name = node.value.id
        elif isinstance(node.value, ast.Attribute):  # cls.Status.PAID style
            enum_name = node.value.attr
        members = enums.get(enum_name) if enum_name else None
        if members and node.attr in members:
            if hits is not None and enum_name:
                hits.append(enum_name)
            return members[node.attr]
        # Not an enum member and not a plain literal: the value cannot be
        # anchored — never fabricate a state from an attribute name.
        if hits is not None and enum_name and _LITERAL.match(node.attr.lower()):
            hits.append(enum_name)  # alphabet still applies; value unverified
        return None
    if isinstance(node, ast.Call) and node.args:  # str(Enum.X)
        return _resolve_value(node.args[0], enums, constants, hits)
    return None


def _field_side(node: ast.AST) -> str | None:
    """If `node` is a state-like field reference, return the field name."""
    if isinstance(node, ast.Attribute) and _is_state_name(node.attr):
        return node.attr
    if isinstance(node, ast.Name) and _is_state_name(node.id):
        return node.id
    return None


def _compare_frame(
    test: ast.AST,
    enums: dict[str, dict[str, str]],
    constants: dict[str, str],
    hits: list[str],
) -> dict[str, set[str]]:
    """{field: positive literal constraints} from one branch test.

    Only Eq/In produce constraints (the branch runs because the test was
    true). NotEq excludes values — a constraint we cannot enumerate — so it
    is ignored. Compares nested under `Not` are NOT constraints (the branch
    runs because the equality was false) and are skipped entirely.
    """
    frame: dict[str, set[str]] = {}

    def visit(node: ast.AST, negated: bool) -> None:
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            visit(node.operand, not negated)
            return
        if isinstance(node, ast.Compare):
            if not negated:
                sides = [node.left, *node.comparators]
                for i, op in enumerate(node.ops):
                    if not isinstance(op, (ast.Eq, ast.In)):
                        continue
                    left, right = sides[i], sides[i + 1]
                    for field_side, literal_side in ((left, right), (right, left)):
                        field = _field_side(field_side)
                        if field is None:
                            continue
                        candidates: list[ast.AST] = []
                        if isinstance(op, ast.In) and isinstance(literal_side, (ast.Tuple, ast.List, ast.Set)):
                            candidates.extend(literal_side.elts)
                        else:
                            candidates.append(literal_side)
                        for cand in candidates:
                            value = _resolve_value(cand, enums, constants, hits)
                            if value:
                                frame.setdefault(field, set()).add(value)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, negated)

    visit(test, False)
    return frame


def scan_python(py_files: list[Path], root: Path) -> tuple[list[Machine], list[str]]:
    parsed = _iter_files(py_files, root)
    enums = _enum_table(parsed)
    constants, dropped_constants = _constant_table(parsed)
    warnings: list[str] = []
    for name in dropped_constants:
        warnings.append(f"constant {name} defined with different values in multiple modules — not used to resolve writes")
    machines: dict[str, Machine] = {}
    enum_hits: dict[str, set[str]] = {}

    def get_machine(machine_id: str, cls: ast.ClassDef | None, field_name: str) -> Machine:
        if machine_id not in machines:
            owner = cls.name if cls else machine_id.split(":", 1)[-1].rsplit(".", 1)[0]
            machines[machine_id] = Machine(
                id=machine_id, field=field_name, owner=owner,
                language="python", confidence="low",
            )
        return machines[machine_id]

    def note_enum_hit(machine_id: str, hits: list[str]) -> None:
        if hits:
            enum_hits.setdefault(machine_id, set()).update(hits)

    # ---- pass A: class-level defaults and annotations --------------------
    for rel, tree in parsed.items():
        if tree is None:
            warnings.append(f"{rel}: syntax error, file skipped")
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for stmt in cls.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    stmt_name = stmt.target.id
                    if not _is_state_name(stmt_name):
                        continue
                    machine = get_machine(f"{cls.name}.{stmt_name}", cls, stmt_name)
                    machine.sources.append(rel)
                    ann = stmt.annotation
                    enum_name = None
                    if isinstance(ann, ast.Name) and ann.id in enums:
                        enum_name = ann.id
                    elif isinstance(ann, ast.Attribute) and ann.attr in enums:
                        enum_name = ann.attr
                    if enum_name:
                        machine.states.extend(enums[enum_name].values())
                        machine.notes.append(f"typed as {enum_name}")
                        machine.confidence = "high"
                    if stmt.value is not None:
                        hits: list[str] = []
                        value = _resolve_value(stmt.value, enums, constants, hits)
                        note_enum_hit(machine.id, hits)
                        if value:
                            machine.states.append(value)
                            machine.initial.append(value)
                            machine.notes.append("class-level default")
                elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                    if isinstance(target, ast.Name) and _is_state_name(target.id):
                        hits = []
                        value = _resolve_value(stmt.value, enums, constants, hits)
                        if value:
                            machine = get_machine(f"{cls.name}.{target.id}", cls, target.id)
                            note_enum_hit(machine.id, hits)
                            machine.states.append(value)
                            machine.initial.append(value)
                            machine.sources.append(rel)
                            machine.notes.append("class-level default")

    # ---- pass B: function scopes ----------------------------------------
    def walk_function(fn: ast.FunctionDef | ast.AsyncFunctionDef, rel: str,
                      cls: ast.ClassDef | None) -> None:
        fn_name = fn.name

        def machine_for(field: str) -> Machine:
            machine_id = f"{cls.name}.{field}" if cls else f"fn:{fn_name}.{field}"
            return get_machine(machine_id, cls, field)

        def record_frame(frame: dict[str, set[str]]) -> None:
            for field, literals in frame.items():
                machine = machine_for(field)
                machine.states.extend(literals)
                machine.sources.append(rel)

        def union_frames(frames: list[dict[str, set[str]]], field: str) -> set[str]:
            out: set[str] = set()
            for frame in frames:
                out |= frame.get(field, set())
            return out

        def maybe_write(target: ast.AST, value_node: ast.AST, lineno: int,
                        frames: list[dict[str, set[str]]]) -> None:
            if not (isinstance(target, ast.Attribute) and _is_state_name(target.attr)):
                return
            field = target.attr
            machine = machine_for(field)
            machine.sources.append(rel)
            hits: list[str] = []
            resolved = _resolve_value(value_node, enums, constants, hits)
            note_enum_hit(machine.id, hits)
            if resolved is None:
                warnings.append(f"{rel}:{lineno}: unresolved value written to {field}")
                return
            guards = union_frames(frames, field)
            kind = "constructor" if fn_name == "__init__" else "write"
            machine.states.extend(guards | {resolved})
            if guards:
                for src in sorted(guards):
                    machine.transitions.append(Transition(
                        src=src, dst=resolved, via=fn_name, file=rel,
                        line=lineno, guarded=True, kind=kind))
            else:
                if kind == "write":
                    machine.notes.append(f"unguarded write at {rel}:{lineno} ({fn_name})")
                machine.transitions.append(Transition(
                    src=None, dst=resolved, via=fn_name, file=rel,
                    line=lineno, guarded=False, kind=kind))
            if kind == "constructor":
                machine.initial.append(resolved)

        def visit_expr(expr: ast.AST, frames) -> None:
            if isinstance(expr, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return  # nested scopes fold into the parent, as documented
            if isinstance(expr, ast.IfExp):
                hits: list[str] = []
                frame = _compare_frame(expr.test, enums, constants, hits)
                record_frame(frame)
                for field in frame:
                    note_enum_hit(machine_for(field).id, hits)
                visit_expr(expr.body, [*frames, frame])
                visit_expr(expr.orelse, frames)
                return
            for child in ast.iter_child_nodes(expr):
                visit_expr(child, frames)

        def visit_stmt(stmt: ast.stmt, frames) -> None:
            if isinstance(stmt, ast.If):
                hits: list[str] = []
                frame = _compare_frame(stmt.test, enums, constants, hits)
                record_frame(frame)
                # hits belong to machines mentioned in the frame
                for field in frame:
                    note_enum_hit(machine_for(field).id, hits)
                walk_block(stmt.body, [*frames, frame])
                walk_block(stmt.orelse, frames)  # else branch: no positive constraint
                return
            if isinstance(stmt, ast.While):
                hits = []
                frame = _compare_frame(stmt.test, enums, constants, hits)
                record_frame(frame)
                for field in frame:
                    note_enum_hit(machine_for(field).id, hits)
                walk_block(stmt.body, [*frames, frame])
                walk_block(stmt.orelse, frames)
                return
            if isinstance(stmt, (ast.For, ast.AsyncFor)):
                walk_block(stmt.body, frames)
                walk_block(stmt.orelse, frames)
                return
            if isinstance(stmt, (ast.With, ast.AsyncWith)):
                walk_block(stmt.body, frames)
                return
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    maybe_write(target, stmt.value, stmt.lineno, frames)
                visit_expr(stmt.value, frames)
                return
            if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                maybe_write(stmt.target, stmt.value, stmt.lineno, frames)
                visit_expr(stmt.value, frames)
                return
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return  # nested scopes fold into the parent
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt):
                    visit_stmt(child, frames)
                else:
                    visit_expr(child, frames)

        def walk_block(statements, frames) -> None:
            for stmt in statements:
                if isinstance(stmt, ast.stmt):
                    visit_stmt(stmt, frames)

        # nested scopes are not analyzed (documented); disclose the writers
        for sub in ast.walk(fn):
            if sub is fn:
                continue
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                name = getattr(sub, "name", "<lambda>")
                for inner in ast.walk(sub):
                    if isinstance(inner, ast.Assign):
                        for t in inner.targets:
                            if isinstance(t, ast.Attribute) and _is_state_name(t.attr):
                                warnings.append(
                                    f"{rel}:{sub.lineno}: nested scope '{name}' writes state field "
                                    f"'{t.attr}' — not analyzed")
        walk_block(fn.body, [])

    for rel, tree in parsed.items():
        if tree is None:
            continue
        class_stack: list[ast.ClassDef] = []

        def visit_node(node: ast.AST) -> None:
            if isinstance(node, ast.ClassDef):
                class_stack.append(node)
                for child in node.body:
                    visit_node(child)
                class_stack.pop()
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                current_cls = class_stack[-1] if class_stack else None
                walk_function(node, rel, current_cls)
                return
            for child in ast.iter_child_nodes(node):
                visit_node(child)

        visit_node(tree)

    # ---- pass C: enum-only machines (states seen; no write sites) --------
    for enum_name, members in enums.items():
        if not (_is_state_name(enum_name) or enum_name.lower().endswith(("status", "state", "phase"))):
            continue
        machine_id = f"enum:{enum_name}"
        if machine_id in machines:
            continue
        machine = Machine(id=machine_id, field=enum_name, owner=enum_name,
                          language="python", confidence="medium")
        machine.states = sorted(members.values())
        machine.notes.append("enum-only machine: no write sites anchored yet")
        machines[machine_id] = machine

    kept: list[Machine] = []
    dropped = 0
    for machine in machines.values():
        contributing = enum_hits.get(machine.id, set())
        for enum_name in sorted(contributing):
            members = enums.get(enum_name)
            if members:
                machine.states.extend(members.values())
                machine.notes.append(f"state alphabet from enum {enum_name}")
        machine.states = sorted({s for s in machine.states if _is_state_literal(s)})
        machine.initial = sorted({s for s in machine.initial if s in machine.states})
        if not machine.states and not machine.transitions and not machine.initial:
            dropped += 1
            continue
        kept.append(machine)
    machines = {m.id: m for m in kept}
    if dropped:
        warnings.append(f"{dropped} machine(s) produced no anchored states and were dropped")
    for machine in kept:
        seen: set[tuple[str | None, str, str, str, int]] = set()
        deduped: list[Transition] = []
        for t in machine.transitions:
            key = (t.src, t.dst, t.via, t.file, t.line)
            if key not in seen:
                seen.add(key)
                deduped.append(t)
        machine.transitions = deduped
        machine.sources = sorted(set(machine.sources))
        if machine.confidence != "high" and len(machine.states) >= 2:
            machine.confidence = "medium"
    return list(machines.values()), warnings
