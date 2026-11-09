"""Meridian — data model.

A *machine* is one state-like attribute of one class (or one module-level
string field) together with every state it can hold and every transition
between those states we can anchor in source. Everything here is plain,
JSON-serializable data: the AI on top of Hermes proposes, nothing reaches
a verdict unless `analyzer.graph` accepts it deterministically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

SCHEMA = 1


@dataclass
class Transition:
    src: str | None          # source state; None = unguarded/unknown entry
    dst: str
    via: str                 # function/method name
    file: str                # repo-relative path
    line: int
    guarded: bool            # True when the enclosing branch compared the field
    kind: str = "write"      # write | constructor

    def anchor(self) -> str:
        return f"{self.file}:{self.line}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "src": self.src, "dst": self.dst, "via": self.via,
            "file": self.file, "line": self.line,
            "guarded": self.guarded, "kind": self.kind,
        }


@dataclass
class Machine:
    id: str                     # e.g. "Order.status"
    field: str                  # attribute name, e.g. "status"
    owner: str                  # class name or module stem
    language: str               # python | typescript | javascript
    confidence: str             # high | medium | low
    states: list[str] = field(default_factory=list)
    initial: list[str] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)   # files contributing evidence
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "id": self.id, "field": self.field, "owner": self.owner,
            "language": self.language, "confidence": self.confidence,
            "states": list(self.states), "initial": list(self.initial),
            "transitions": [t.to_dict() for t in self.transitions],
            "sources": list(self.sources), "notes": list(self.notes),
        }


@dataclass
class ScanResult:
    root: str
    files_scanned: int
    files_skipped: list[str]
    machines: list[Machine]
    warnings: list[str]
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "warnings": self.warnings,
            "elapsed_ms": self.elapsed_ms,
            "machines": [m.to_dict() for m in self.machines],
        }


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=False)


def cap_list(items: list[Any], limit: int) -> tuple[list[Any], int]:
    if len(items) <= limit:
        return list(items), 0
    return items[:limit], len(items) - limit
