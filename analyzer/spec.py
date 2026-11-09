"""The `.meridian.yaml` spec: Meridian's decision procedure input.

The spec is what makes a violation *illegal* rather than merely unusual.
Discovery works without one; enforcement needs one. Contains no code,
only sets of allowed/forbidden edges, so reviewing it is a five-minute
job - by a human or by the AI, which proposes and the verifier gates.

Loader order: JSON (valid YAML) -> PyYAML (if present) -> a small
built-in parser for this exact schema. Writers always emit plain YAML.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SpecError(ValueError):
    pass


@dataclass
class SpecMachine:
    id: str
    states: list[str] = field(default_factory=list)
    initial: list[str] = field(default_factory=list)
    allow: set[tuple[str, str]] = field(default_factory=set)
    deny: set[tuple[str, str]] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


@dataclass
class Spec:
    path: Path
    version: int
    machines: list[SpecMachine]

    def machine(self, machine_id: str) -> SpecMachine | None:
        for m in self.machines:
            if m.id == machine_id:
                return m
        return None

    def ids(self) -> set[str]:
        return {m.id for m in self.machines}


def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw == "":
        return ""
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        body = raw[1:-1]
        if raw.startswith('"'):
            # sequential unescape exactly as the writer emits it
            out: list[str] = []
            i = 0
            while i < len(body):
                if body[i] == "\\" and i + 1 < len(body) and body[i + 1] in ('"', "\\"):
                    out.append(body[i + 1])
                    i += 2
                    continue
                out.append(body[i])
                i += 1
            body = "".join(out)
        return body
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    if raw.isdigit():
        return int(raw)
    return raw


def _parse_inline_list(raw: str) -> list[Any] | None:
    m = re.match(r"^\[(.*)\]$", raw.strip())
    if not m:
        return None
    inner = m.group(1).strip()
    if not inner:
        return []
    return [_parse_scalar(part) for part in inner.split(",")]


def _mini_yaml(text: str) -> dict[str, Any]:
    """Subset parser for exactly the Meridian spec schema:

        version: 1
        machines:
          - id: Order.status          # indent 2  → new machine
            states: [a, b]            # indent 4  → machine key
            allow:                    # indent 4, empty value → open a list
              - [a, b]                # indent 6+ → list item for that key
    """
    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def inline_or_scalar(value: str):
        return _parse_inline_list(value) if value.startswith("[") else _parse_scalar(value)

    def kv(stripped: str, lineno: int) -> tuple[str, str]:
        key, sep, value = stripped.partition(":")
        if not sep:
            raise SpecError(f"line {lineno}: expected 'key: value'")
        return key.strip(), value.strip()

    root: dict[str, Any] = {}
    machine_list: list[dict[str, Any]] | None = None
    current: dict[str, Any] | None = None
    pending_key: str | None = None
    in_machines = False

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent_text = line[: len(line) - len(line.lstrip(" "))]
        if "\t" in indent_text:
            raise SpecError(f"line {lineno}: tabs are not supported in .meridian.yaml; use spaces")
        indent = indent_of(line)
        stripped = line.strip()

        if indent == 0:
            key, value = kv(stripped, lineno)
            if key == "machines" and value == "":
                machine_list = []
                root["machines"] = machine_list
                in_machines, current, pending_key = True, None, None
                continue
            in_machines = False
            root[key] = inline_or_scalar(value) if value else None
            continue

        if in_machines:
            if indent == 2 and stripped.startswith("- "):
                # new machine entry: "- key: value" (usually "- id: X")
                current = {}
                machine_list.append(current)  # type: ignore[union-attr]
                key, value = kv(stripped[2:].strip(), lineno)
                current[key] = inline_or_scalar(value)
                pending_key = None
                continue
            if indent == 4 and not stripped.startswith("- "):
                if current is None:
                    raise SpecError(f"line {lineno}: key before any machine entry")
                key, value = kv(stripped, lineno)
                if value:
                    current[key] = inline_or_scalar(value)
                    pending_key = None
                else:
                    current[key] = []
                    pending_key = key
                continue
            if indent >= 6 and stripped.startswith("- "):
                if current is None or pending_key is None:
                    raise SpecError(f"line {lineno}: list item with no open list")
                item = stripped[2:].strip()
                current[pending_key].append(inline_or_scalar(item))
                continue
            raise SpecError(f"line {lineno}: unexpected indent/content in machines block")
            continue

        raise SpecError(f"line {lineno}: content outside of 'machines'")

    return root


def _coerce_spec(data: Any, path: Path) -> Spec:
    """Validate the spec strictly. Anything malformed raises SpecError:
    silently dropping bad edges would make enforcement a no-op with a
    green exit code, which is the one failure mode this plugin must never
    have."""
    if not isinstance(data, dict):
        raise SpecError("spec must be a mapping with 'version' and 'machines'")
    try:
        version = data.get("version", 1)
        version = version if isinstance(version, int) and not isinstance(version, bool) else int(version)
    except (TypeError, ValueError) as exc:
        raise SpecError(f"version must be an integer: {exc}") from exc

    machines_raw = data.get("machines", [])
    if machines_raw is None:
        machines_raw = []
    if not isinstance(machines_raw, list):
        raise SpecError("'machines' must be a list")

    errors: list[str] = []
    machines: list[SpecMachine] = []

    def _str_list(value: Any, field_name: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            errors.append(f"{field_name} must be a list")
            return []
        out = []
        for i, item in enumerate(value):
            if isinstance(item, bool) or not isinstance(item, (str, int)):
                errors.append(f"{field_name}[{i}] must be a string, got {item!r}")
            else:
                out.append(str(item))
        return out

    def _edges(value: Any, field_name: str, machine_id: str) -> set[tuple[str, str]]:
        if value is None:
            return set()
        if not isinstance(value, list):
            errors.append(f"{machine_id}: {field_name} must be a list of [src, dst] pairs")
            return set()
        edges: set[tuple[str, str]] = set()
        for i, item in enumerate(value):
            if not (isinstance(item, (list, tuple)) and len(item) == 2
                    and all(isinstance(p, str) for p in item)):
                errors.append(f"{machine_id}: {field_name}[{i}] must be a [src, dst] pair of strings, got {item!r}")
                continue
            edges.add((item[0], item[1]))
        return edges

    for i, raw in enumerate(machines_raw):
        if not isinstance(raw, dict):
            errors.append(f"machines[{i}] must be a mapping, got {raw!r}")
            continue
        machine_id = raw.get("id")
        if not isinstance(machine_id, str) or not machine_id.strip():
            errors.append(f"machines[{i}]: id must be a non-empty string")
            machine_id = f"machines[{i}]"
        states = _str_list(raw.get("states"), f"{machine_id}.states")
        initial = _str_list(raw.get("initial"), f"{machine_id}.initial")
        allow = _edges(raw.get("allow"), "allow", machine_id)
        deny = _edges(raw.get("deny"), "deny", machine_id)
        notes = _str_list(raw.get("notes"), f"{machine_id}.notes")
        known = {"id", "states", "initial", "allow", "deny", "notes"}
        for key in raw:
            if key not in known:
                notes.append(f"ignored unknown key: {key}")
        machines.append(SpecMachine(id=machine_id.strip(), states=states,
                                    initial=initial, allow=allow,
                                    deny=deny, notes=notes))
    ids = [m.id for m in machines]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        errors.append(f"duplicate machine id(s): {', '.join(duplicates)}")
    if errors:
        raise SpecError("invalid spec:\n  - " + "\n  - ".join(errors[:20]))
    return Spec(path=path, version=version, machines=machines)


def load_spec(path: Path) -> Spec:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"cannot read {path}: {exc}") from exc
    # 1) JSON (valid YAML 1.2)
    try:
        return _coerce_spec(json.loads(text), path)
    except json.JSONDecodeError:
        pass
    except SpecError:
        raise
    except Exception as exc:  # TypeError from odd JSON shapes
        raise SpecError(f"invalid spec in {path}: {exc}") from exc
    # 2) PyYAML when available
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
    except ImportError:
        data = None
    except Exception as exc:
        raise SpecError(f"YAML parse failed for {path}: {exc}") from exc
    if data is not None:
        try:
            return _coerce_spec(data, path)
        except SpecError:
            raise
        except Exception as exc:
            raise SpecError(f"invalid spec in {path}: {exc}") from exc
    # 3) built-in subset parser
    try:
        return _coerce_spec(_mini_yaml(text), path)
    except SpecError:
        raise
    except Exception as exc:
        raise SpecError(f"invalid spec in {path}: {exc}") from exc


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def draft_spec(machines: list[Any]) -> str:
    def qlist(items) -> str:
        return "[" + ", ".join(_yaml_quote(str(i)) for i in items) + "]"

    lines = ["version: 1",
             "# Drafted by Meridian from anchored evidence. Review every edge before",
             "# committing - the verifier enforces exactly what you accept here.",
             "machines:"]
    for machine in machines:
        lines.append(f"  - id: {_yaml_quote(machine.id)}")
        if machine.states:
            lines.append(f"    states: {qlist(machine.states)}")
        if machine.initial:
            lines.append(f"    initial: {qlist(machine.initial)}")
        allow = sorted({(t.src, t.dst) for t in machine.transitions if t.src})
        if allow:
            lines.append("    allow:")
            for src, dst in allow:
                lines.append(f"      - [{_yaml_quote(src)}, {_yaml_quote(dst)}]")
        else:
            lines.append("    allow: []")
        lines.append("    deny: []")
    return "\n".join(lines) + "\n"


def write_spec(root: Path, content: str, *, force: bool = False) -> Path:
    target = root / ".meridian.yaml"
    if target.exists() and not force:
        raise SpecError(f"{target} exists - pass force=True to overwrite (a .bak is kept)")
    if target.exists():
        target.with_suffix(".yaml.bak").write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.write_text(content, encoding="utf-8")
    return target
