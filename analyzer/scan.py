"""Scan orchestrator: walk -> language analyzers -> merged machines."""

from __future__ import annotations

import time
from pathlib import Path

from .model import Machine, ScanResult
from .py_scan import scan_python
from .repo import DEFAULT_MAX_FILES, walk_source_files
from .ts_scan import scan_typescript


def _merge(machines: list[Machine]) -> list[Machine]:
    merged: dict[str, Machine] = {}
    for machine in machines:
        key = machine.id
        if key not in merged:
            merged[key] = machine
            continue
        target = merged[key]
        target.states = sorted({*target.states, *machine.states})
        target.initial = sorted({*target.initial, *machine.initial})
        target.transitions = target.transitions + machine.transitions
        target.sources = sorted({*target.sources, *machine.sources})
        target.notes = list(dict.fromkeys([*target.notes, *machine.notes]))
        if machine.confidence == "high":
            target.confidence = "high"
        elif target.confidence == "low" and machine.confidence == "medium":
            target.confidence = "medium"
    for machine in merged.values():
        seen: set[tuple] = set()
        deduped = []
        for t in machine.transitions:
            key = (t.src, t.dst, t.via, t.file, t.line)
            if key not in seen:
                seen.add(key)
                deduped.append(t)
        machine.transitions = deduped
    return list(merged.values())


def scan_root(root_path: str | Path, *, max_files: int | None = DEFAULT_MAX_FILES) -> ScanResult:
    started = time.monotonic()
    root = Path(root_path).expanduser()
    files, skipped = walk_source_files(root, max_files=max_files)
    py_files = [f for f in files if f.suffix.lower() == ".py"]
    other_files = [f for f in files if f.suffix.lower() != ".py"]

    machines: list[Machine] = []
    warnings: list[str] = []
    if py_files:
        found, warns = scan_python(py_files, root)
        machines.extend(found)
        warnings.extend(warns)
    if other_files:
        found, warns = scan_typescript(other_files, root)
        machines.extend(found)
        warnings.extend(warns)

    merged = _merge(machines)
    elapsed = int((time.monotonic() - started) * 1000)
    return ScanResult(
        root=str(root.resolve()),
        files_scanned=len(files),
        files_skipped=skipped,
        machines=merged,
        warnings=warnings[:50],
        elapsed_ms=elapsed,
    )
