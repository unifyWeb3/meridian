"""The evidence ledger: append-only, SHA-256 hash-chained JSONL.

Every scan, verdict, spec acceptance and CI check Meridian performs is
an entry. Each entry commits to the hash of the previous one, so a quiet
edit anywhere breaks the chain loudly. `verify()` is the check a reviewer
(or you, or an auditor) runs before believing a report.

What is stored: event names, machine ids, verdict counts, anchors
(file:line) — never file contents, never secrets.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .repo import ledger_path, plugin_data_dir

SCHEMA = "meridian.ledger.v1"


class LedgerError(RuntimeError):
    pass


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _hash_entry(entry: dict) -> str:
    material = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(_canonical(material)).hexdigest()


def read_entries(path: Path | None = None) -> list[dict]:
    path = path or ledger_path()
    if not path.exists():
        return []
    entries: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    entries.append({"_corrupt": True, "_raw": line[:200]})
    except OSError as exc:
        raise LedgerError(f"cannot read ledger {path}: {exc}") from exc
    return entries


def append(event: str, data: dict, *, path: Path | None = None) -> dict:
    path = path or ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = read_entries(path)
    if entries:
        last = entries[-1]
        if last.get("_corrupt"):
            raise LedgerError(f"ledger {path} ends with a corrupt entry — refusing to extend it")
        # The whole chain must hold before we extend it: a quiet edit anywhere
        # in history is exactly what this refuses to build on.
        problems: list[str] = []
        prev = "0" * 64
        for i, entry in enumerate(entries, start=1):
            if entry.get("seq") != i:
                problems.append(f"entry {i}: seq is {entry.get('seq')}")
            if entry.get("hash") != _hash_entry(entry):
                problems.append(f"entry {i}: content hash mismatch")
            if entry.get("prev") != prev:
                problems.append(f"entry {i}: prev link mismatch")
            prev = str(entry.get("hash", prev))
        if problems:
            raise LedgerError(
                f"ledger {path} chain is broken ({problems[0]}{' …' if len(problems) > 1 else ''}) — "
                "run `hermes meridian ledger verify`; refusing to extend it"
            )
        seq = int(last.get("seq", 0)) + 1
        prev = str(last.get("hash"))
    else:
        seq = 1
        prev = "0" * 64
    entry = {
        "schema": SCHEMA,
        "seq": seq,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        "prev": prev,
        "data": data,
    }
    entry["hash"] = _hash_entry(entry)
    line = json.dumps(entry, ensure_ascii=False)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return entry


def verify(path: Path | None = None) -> dict:
    path = path or ledger_path()
    entries = read_entries(path)
    problems: list[str] = []
    prev = "0" * 64
    for i, entry in enumerate(entries, start=1):
        if entry.get("_corrupt"):
            problems.append(f"entry {i}: unreadable line")
            prev = entry.get("hash", prev) if isinstance(entry.get("hash"), str) else prev
            continue
        if entry.get("seq") != i:
            problems.append(f"entry {i}: seq is {entry.get('seq')} (gap or rewrite)")
        expected = _hash_entry(entry)
        if entry.get("hash") != expected:
            problems.append(f"entry {i}: content hash mismatch (contents were edited)")
        if entry.get("prev") != prev:
            problems.append(f"entry {i}: prev link mismatch (chain reordered or truncated)")
        prev = str(entry.get("hash", prev))
    return {
        "ok": not problems,
        "entries": len(entries),
        "head": prev if entries else None,
        "problems": problems,
        "path": str(path),
    }


def stats(path: Path | None = None) -> dict:
    entries = read_entries(path)
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.get("event", "?")] = counts.get(entry.get("event", "?"), 0) + 1
    return {"entries": len(entries), "events": counts, "path": str(path) if path else str(ledger_path())}


def data_dir() -> Path:
    return plugin_data_dir()
