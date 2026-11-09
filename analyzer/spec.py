"""The `.meridian.yaml` spec: Meridian's decision procedure input.

The spec is what makes a violation *illegal* rather than merely unusual.
Discovery works without one; enforcement needs one. Contains no code,
only sets of allowed/forbidden edges, so reviewing it is a five-minute
job — by a human or by the AI, which proposes and the verifier gates.

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
        return raw[1:-1]
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
    """Subset for the Meridian schema: top-level `version`/`machines`,
    machines as a list of maps with scalar or list values."""
    root: dict[str, Any] = {}
    current: dict[str, Any] | None = None
    machine_list: list[dict[str, Any]] | None = None
    pending_key: str | None = None

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = indent_of(line)
        stripped = line.strip()
        if indent == 0 and not stripped.startswith("-"):
            key, sep, value = stripped.partition(":")
            if not sep:
                raise SpecError(f"line {lineno}: expected 'key: value'")
            key = key.strip()
            value = value.strip()
            if value:
                root[key] = _parse_inline_list(value) if value.startswith("[") else _parse_scalar(value)
                pending_key = None
            else:
                if key == "machines":
                    machine_list = []
                    root["machines"] = machine_list
                    current = None
                pending_key = key
            continue
        if stripped.startswith("- "):
            item = stripped[2:].strip()
            if indent >= 2 and machine_list is not None and pending_key == "machines":
                current = {}
                machine_list.append(current)
                kv = item.partition(":")
                if kv[1]:
                    current[kv[0].strip()] = _parse_inline_list(kv[2]) if kv[2].strip().startswith("[") else _parse_scalar(kv[2])
                pending_key = None
                continue
            if current is None:
                raise SpecError(f"line {lineno}: '-' item with no parent")
            value: Any
            parsed_list = _parse_inline_list(item)
            value = parsed_list if parsed_list is not None else _parse_scalar(item)
            if pending_key:
                current.setdefault(pending_key, [])
                if isinstance(current[pending_key], list):
                    current[pending_key].append(value)
            else:
                raise SpecError(f"line {lineno}: list item with no key")
            continue
        # subsequent keys of a machine entry
        kv = stripped.partition(":")
        if not kv[1]:
            raise SpecError(f"line {lineno}: expected 'key: value'")
        key, value_raw = kv[0].strip(), kv[2].strip()
        if current is None:
            raise SpecError(f"line {lineno}: key outside of a machine entry")
        if value_raw:
            current[key] = _parse_inline_list(value_raw) if value_raw.startswith("[") else _parse_scalar(value_raw)
            pending_key = None
        else:
            current[key] = []
            pending_key = key
    return root


def _coerce_spec(data: Any, path: Path) -> Spec:
    if not isinstance(data, dict):
        raise SpecError("spec must be a mapping with 'version' and 'machines'")
    version = data.get("version", 1)
    if not isinstance(version, int):
        version = int(version)
    machines: list[SpecMachine] = []
    for i, raw in enumerate(data.get("machines", []) or []):
        if not isinstance(raw, dict):
            raise SpecError(f"machines[{i}] must be a mapping")
        states = [str(s) for s in (raw.get("states") or [])]
        initial = [str(s) for s in (raw.get("initial") or [])]
        def _edges(value: Any) -> set[tuple[str, str]]:
            edges: set[tuple[str, str]] = set()
            for item in value or []:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    edges.add((str(item[0]), str(item[1])))
            return edges
        machines.append(SpecMachine(
            id=str(raw.get("id", f"machines[{i}]")),
            states=states,
            initial=initial,
            allow=_edges(raw.get("allow")),
            deny=_edges(raw.get("deny")),
            notes=[str(n) for n in (raw.get("notes") or [])],
        ))
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
    # 2) PyYAML when available
    try:
        import yaml  # type: ignore

        return _coerce_spec(yaml.safe_load(text), path)
    except ImportError:
        pass
    except Exception as exc:
        if isinstance(exc, SpecError):
            raise
        raise SpecError(f"YAML parse failed for {path}: {exc}") from exc
    # 3) built-in subset parser
    return _coerce_spec(_mini_yaml(text), path)


def draft_spec(machines: list[Any]) -> str:
    lines = ["version: 1", "# Drafted by Meridian from anchored evidence. Review every edge before",
             "# committing — the verifier enforces exactly what you accept here.", "machines:"]
    for machine in machines:
        lines.append(f"  - id: {machine.id}")
        if machine.states:
            lines.append("    states: [" + ", ".join(machine.states) + "]")
        if machine.initial:
            lines.append("    initial: [" + ", ".join(machine.initial) + "]")
        allow = sorted({(t.src, t.dst) for t in machine.transitions if t.src})
        if allow:
            lines.append("    allow:")
            for src, dst in allow:
                lines.append(f"      - [{src}, {dst}]")
        else:
            lines.append("    allow: []")
        lines.append("    deny: []")
    return "\n".join(lines) + "\n"


def write_spec(root: Path, content: str, *, force: bool = False) -> Path:
    target = root / ".meridian.yaml"
    if target.exists() and not force:
        raise SpecError(f"{target} exists — pass force=True to overwrite (a .bak is kept)")
    if target.exists():
        target.with_suffix(".yaml.bak").write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.write_text(content, encoding="utf-8")
    return target
