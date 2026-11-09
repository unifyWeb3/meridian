"""The evidence ledger: append-only, SHA-256 hash-chained JSONL.

Every scan, verdict, spec acceptance and check Meridian performs is an
entry. Each entry commits to the hash of the previous one, so quietly
editing history anywhere breaks the chain loudly.

Durability rules:
- a sidecar `.lock` file serializes read-verify-append (POSIX flock;
  msvcrt on Windows; best effort otherwise), so concurrent agents
  cannot fork the chain;
- appends re-verify incrementally (per size change), not from scratch;
- a torn final line (crash mid-write) never validates silently —
  `repair()` is the explicit recovery: it keeps the valid prefix,
  quarantines the original, and stamps a `ledger_repair` event naming
  exactly what was dropped.

What is stored: event names, machine ids, verdict counts, anchors
(file:line) — never file contents, never secrets.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .repo import ledger_path, plugin_data_dir

SCHEMA = "meridian.ledger.v1"
_GENESIS = "0" * 64

# path -> ((size, mtime_ns), verified_ok)
_VERIFY_CACHE: dict[str, tuple[tuple[int, int], bool]] = {}


class LedgerError(RuntimeError):
    pass


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _hash_entry(entry: dict) -> str:
    material = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(_canonical(material)).hexdigest()


def _read_raw_lines(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return [line for line in handle if line.strip()]
    except OSError as exc:
        raise LedgerError(f"cannot read ledger {path}: {exc}") from exc


def read_entries(path: Path | None = None) -> list[dict]:
    path = path or ledger_path()
    if not path.exists():
        return []
    entries: list[dict] = []
    for i, line in enumerate(_read_raw_lines(path), start=1):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append({"_corrupt": True, "_raw": line[:200]})
    return entries


def _chain_problems(entries: list[dict]) -> list[str]:
    problems: list[str] = []
    prev = _GENESIS
    for i, entry in enumerate(entries, start=1):
        if entry.get("_corrupt"):
            problems.append(f"entry {i}: unreadable line")
            continue
        if entry.get("seq") != i:
            problems.append(f"entry {i}: seq is {entry.get('seq')} (gap or rewrite)")
        if entry.get("hash") != _hash_entry(entry):
            problems.append(f"entry {i}: content hash mismatch (contents were edited)")
        if entry.get("prev") != prev:
            problems.append(f"entry {i}: prev link mismatch (chain reordered or truncated)")
        prev = str(entry.get("hash", prev))
    return problems


def _acquire_lock(lock_path: Path):
    """Best-effort cross-platform exclusive lock (sidecar file)."""
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except ImportError:
        pass
    try:
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return fd
    except Exception:
        return fd  # unlocked but functional; documented degraded path


def _release_lock(fd) -> None:
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        try:
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
    try:
        os.close(fd)
    except OSError:
        pass


def _verify_cached(path: Path) -> bool:
    """True when the on-disk chain is internally consistent.

    Re-verified whenever (size, mtime) changed since the last check in this
    process — every legal writer goes through append()/repair(), so any
    in-place edit registers. (verify()/repair(), the audit paths, always
    re-verify fully regardless of this cache.)
    """
    try:
        st = path.stat()
    except OSError:
        return False
    key = (st.st_size, st.st_mtime_ns)
    cached = _VERIFY_CACHE.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    problems = _chain_problems(read_entries(path))
    ok = not problems
    _VERIFY_CACHE[str(path)] = (key, ok)
    return ok


def _append_unlocked(path: Path, event: str, data: dict) -> dict:
    """Append with the caller's lock held. Never call without the sidecar lock."""
    entries = read_entries(path)
    if entries:
        if entries[-1].get("_corrupt"):
            raise LedgerError(f"ledger {path} ends with a torn/unreadable final line")
        if not _verify_cached(path):
            problems = _chain_problems(entries)
            raise LedgerError(
                f"ledger {path} chain is broken ({'; '.join(problems[:3])}) — "
                "run `hermes meridian ledger verify`; refusing to extend it")
        seq = int(entries[-1].get("seq", 0)) + 1
        prev = str(entries[-1].get("hash"))
    else:
        seq = 1
        prev = _GENESIS
    entry = {
        "schema": SCHEMA,
        "seq": seq,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        "prev": prev,
        "data": data,
    }
    entry["hash"] = _hash_entry(entry)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        _VERIFY_CACHE[str(path)] = (path.stat().st_size, True)
    except OSError:
        _VERIFY_CACHE.pop(str(path), None)
    return entry


def append(event: str, data: dict, *, path: Path | None = None) -> dict:
    path = path or ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = _acquire_lock(path.with_suffix(path.suffix + ".lock"))
    try:
        return _append_unlocked(path, event, data)
    finally:
        _release_lock(lock_fd)


def verify(path: Path | None = None) -> dict:
    path = path or ledger_path()
    entries = read_entries(path)
    problems = _chain_problems(entries)
    prev = _GENESIS
    if entries:
        last = next((e for e in reversed(entries) if not e.get("_corrupt")), None)
        prev = str(last.get("hash")) if last else None
    return {
        "ok": not problems,
        "entries": len(entries),
        "head": prev if entries else None,
        "problems": problems,
        "path": str(path),
    }


def repair(path: Path | None = None) -> dict:
    """Keep the longest valid prefix; quarantine and re-stamp.

    Explicit, user-invoked recovery for a torn or corrupt ledger. The
    original file is preserved as `ledger.jsonl.broken-<ts>`; the rebuilt
    chain gets a `ledger_repair` event naming the dropped bytes/entries.
    """
    path = path or ledger_path()
    if not path.exists():
        return {"ok": True, "repaired": 0, "note": "no ledger", "path": str(path)}
    lock_fd = _acquire_lock(path.with_suffix(path.suffix + ".lock"))
    try:
        raw_lines = _read_raw_lines(path)
        valid: list[dict] = []
        prev = _GENESIS
        dropped_lines = 0
        for i, line in enumerate(raw_lines, start=1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                dropped_lines += 1
                continue
            if (entry.get("seq") == len(valid) + 1
                    and entry.get("hash") == _hash_entry(entry)
                    and entry.get("prev") == prev):
                valid.append(entry)
                prev = str(entry["hash"])
            else:
                # first invalid entry: stop, keep prefix only
                dropped_lines += len(raw_lines) - (i - 1)
                break
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.broken-{stamp}")
        path.replace(backup)
        with path.open("x", encoding="utf-8") as handle:
            for entry in valid:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _VERIFY_CACHE.pop(str(path), None)
        result = {"ok": True, "kept": len(valid), "dropped_lines": dropped_lines,
                  "backup": str(backup), "path": str(path)}
        if dropped_lines:
            _append_unlocked(path, "ledger_repair", result)
        return result
    finally:
        _release_lock(lock_fd)


def stats(path: Path | None = None) -> dict:
    entries = read_entries(path)
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.get("event", "?")] = counts.get(entry.get("event", "?"), 0) + 1
    return {"entries": len(entries), "events": counts, "path": str(path) if path else str(ledger_path())}
