"""The evidence ledger: append-only, SHA-256 hash-chained JSONL.

Every scan, check, spec acceptance and report Meridian performs is an entry.
Each entry commits to the hash of the previous one, so quietly editing history
anywhere breaks the chain loudly.

The chain is unkeyed: it detects edits to the log, but anyone with write
access to the ledger file can recompute it. Tamper-evident, not tamper-proof.

Durability rules:
- a sidecar `.lock` file serializes read-verify-append (POSIX flock, or
  msvcrt on Windows; unsupported platforms fail loudly), so concurrent agents
  cannot fork the chain;
- appends re-use the cached verification only while the file identity is
  unchanged, and otherwise validate the current chain before extending it;
- a torn final line (crash mid-write) never validates silently:
  `repair()` is the explicit recovery: it keeps the valid prefix,
  quarantines the original, and stamps a `ledger_repair` event naming
  exactly what was dropped.

What is stored: event names, machine ids, aggregate counts (files scanned,
violations found, machines covered) and the repository/spec/report paths
involved. Never verdicts, never file:line anchors, never spec or source
revisions, never file contents, never secrets.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from . import repo as repo_mod
from .repo import ledger_path, plugin_data_dir

SCHEMA = "meridian.ledger.v1"
_GENESIS = "0" * 64

# path -> ((device, inode, size, mtime_ns, ctime_ns), verified_ok)
_VERIFY_CACHE: dict[str, tuple[tuple[int, int, int, int, int], bool]] = {}


class LedgerError(RuntimeError):
    pass


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _hash_entry(entry: dict) -> str:
    material = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(_canonical(material)).hexdigest()


def _check_ledger_target(path: Path) -> None:
    """Refuse a ledger path that is not a single-link regular file.

    The ledger lives under Hermes home, but a compromised profile, a stale
    symlink, or a hard link must not turn an append into a write to an
    arbitrary file. A missing path is allowed; callers decide whether that is
    an empty ledger or a failure.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LedgerError(f"cannot inspect ledger {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise LedgerError(f"ledger {path} is a symlink; refusing to follow it")
    if not stat.S_ISREG(info.st_mode):
        raise LedgerError(f"ledger {path} is not a regular file")
    if info.st_nlink > 1:
        raise LedgerError(f"ledger {path} has multiple hard links; refusing to write through it")


def _open_regular_file(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open a path and prove the opened file is the current regular file.

    ``O_NOFOLLOW`` closes the symlink race on POSIX. The lstat/fstat identity
    check supplies the same guarantee on platforms without that flag and also
    rejects a path replaced between inspection and open.
    """
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags, mode)
    except OSError as exc:
        raise LedgerError(f"cannot open {path}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        try:
            current = path.lstat()
        except OSError as exc:
            raise LedgerError(f"cannot inspect {path}: {exc}") from exc
        if (not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or not os.path.samestat(opened, current)
                or opened.st_nlink > 1
                or current.st_nlink > 1):
            raise LedgerError(f"{path} is not a single-link regular file")
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_raw_lines(path: Path) -> list[str]:
    try:
        text = repo_mod.read_text(path)
    except FileNotFoundError:
        return []
    except repo_mod.UnsafePathError as exc:
        raise LedgerError(f"cannot read ledger {path}: {exc}") from exc
    except UnicodeError as exc:
        raise LedgerError(f"ledger {path} is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise LedgerError(f"cannot read ledger {path}: {exc}") from exc
    return [line for line in text.splitlines() if line.strip()]


def _read_entries_unlocked(path: Path) -> list[dict]:
    entries: list[dict] = []
    for i, line in enumerate(_read_raw_lines(path), start=1):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            entries.append({"_corrupt": True, "_raw": line[:200]})
        else:
            entries.append(parsed)
    return entries


def read_entries(path: Path | None = None) -> list[dict]:
    """Read a stable snapshot while a writer may be appending."""
    path = path or ledger_path()
    _check_ledger_target(path)
    if not path.exists():
        return []
    lock_fd = _acquire_lock(path.with_suffix(path.suffix + ".lock"))
    try:
        return _read_entries_unlocked(path)
    finally:
        _release_lock(lock_fd)


def _chain_problems(entries: list[dict]) -> list[str]:
    problems: list[str] = []
    prev = _GENESIS
    for i, entry in enumerate(entries, start=1):
        if entry.get("_corrupt"):
            problems.append(f"entry {i}: unreadable line")
            continue
        if entry.get("schema") != SCHEMA:
            problems.append(f"entry {i}: unexpected schema {entry.get('schema')!r}")
        if not isinstance(entry.get("event"), str) or not entry.get("event"):
            problems.append(f"entry {i}: event must be a non-empty string")
        if not isinstance(entry.get("data"), dict):
            problems.append(f"entry {i}: data must be a mapping")
        seq = entry.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            problems.append(f"entry {i}: seq must be an integer, got {seq!r}")
        elif seq != i:
            problems.append(f"entry {i}: seq is {seq} (gap or rewrite)")
        if entry.get("hash") != _hash_entry(entry):
            problems.append(f"entry {i}: content hash mismatch (contents were edited)")
        if entry.get("prev") != prev:
            problems.append(f"entry {i}: prev link mismatch (chain reordered or truncated)")
        prev = str(entry.get("hash", prev))
    return problems


def _acquire_lock(lock_path: Path):
    """Acquire an exclusive cross-platform sidecar lock.

    Failure is loud: silently continuing without a lock could fork the
    chain when two agents append at the same time.
    """
    fd = _open_regular_file(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):
        pass
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except ImportError:
        pass
    except OSError as exc:
        os.close(fd)
        raise LedgerError(f"cannot lock ledger {lock_path}: {exc}") from exc
    try:
        import msvcrt

        # msvcrt locks a byte range; make sure the sidecar has one byte to
        # lock and start at offset zero on every platform.
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return fd
    except ImportError as exc:
        os.close(fd)
        raise LedgerError(f"no supported lock implementation for {lock_path}") from exc
    except Exception as exc:
        os.close(fd)
        raise LedgerError(f"cannot lock ledger {lock_path}: {exc}") from exc


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


def _stat_key(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _file_key(path: Path) -> tuple[int, int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"{path} is not a regular file")
    return _stat_key(info)


def _read_entries_snapshot(path: Path) -> tuple[list[dict], tuple[int, int, int, int, int] | None]:
    """Read entries and the exact file identity that produced them."""
    try:
        before = _file_key(path)
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        raise LedgerError(f"cannot inspect ledger {path}: {exc}") from exc
    entries = _read_entries_unlocked(path)
    try:
        after = _file_key(path)
    except OSError as exc:
        raise LedgerError(f"cannot inspect ledger {path}: {exc}") from exc
    if before != after:
        raise LedgerError(f"ledger {path} changed while it was being read")
    return entries, after


def _verify_cached(path: Path, entries: list[dict] | None = None,
                   file_key: tuple[int, int, int, int, int] | None = None) -> bool:
    """True when the on-disk chain is internally consistent.

    The cache key includes inode and ctime as well as size and mtime. An
    in-place edit that deliberately restores mtime therefore cannot reuse a
    stale "valid" result. The key is also tied to the exact snapshot that was
    validated, so a concurrent edit cannot attach old validity to new bytes.
    (verify()/repair(), the audit paths, always re-verify fully regardless of
    this cache.)
    """
    if entries is None:
        entries, file_key = _read_entries_snapshot(path)
    if file_key is None:
        return not entries
    try:
        current = _file_key(path)
    except OSError:
        return False
    if current != file_key:
        return False
    cached = _VERIFY_CACHE.get(str(path))
    if cached and cached[0] == current:
        return cached[1]
    problems = _chain_problems(entries)
    ok = not problems
    _VERIFY_CACHE[str(path)] = (current, ok)
    return ok


def _append_unlocked(path: Path, event: str, data: dict) -> dict:
    """Append with the caller's lock held. Never call without the sidecar lock."""
    entries, snapshot_key = _read_entries_snapshot(path)
    if entries:
        if entries[-1].get("_corrupt"):
            raise LedgerError(f"ledger {path} ends with a torn/unreadable final line")
        if not _verify_cached(path, entries, snapshot_key):
            problems = _chain_problems(entries)
            raise LedgerError(
                f"ledger {path} chain is broken ({'; '.join(problems[:3])}) - "
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
    flags = os.O_WRONLY | os.O_APPEND
    if snapshot_key is None:
        flags |= os.O_CREAT | os.O_EXCL
    fd = _open_regular_file(path, flags)
    try:
        opened_key = _stat_key(os.fstat(fd))
        if snapshot_key is not None and opened_key != snapshot_key:
            raise LedgerError(f"ledger {path} changed before it could be extended")
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1  # os.fdopen now owns the descriptor
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            written_key = _stat_key(os.fstat(handle.fileno()))
    finally:
        if fd >= 0:
            os.close(fd)
    _VERIFY_CACHE[str(path)] = (written_key, True)
    return entry


def append(event: str, data: dict, *, path: Path | None = None) -> dict:
    path = path or ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_ledger_target(path)
    lock_fd = _acquire_lock(path.with_suffix(path.suffix + ".lock"))
    try:
        return _append_unlocked(path, event, data)
    finally:
        _release_lock(lock_fd)


def verify(path: Path | None = None) -> dict:
    path = path or ledger_path()
    _check_ledger_target(path)
    if not path.exists():
        return {"ok": True, "entries": 0, "head": None, "problems": [],
                "path": str(path)}
    lock_fd = _acquire_lock(path.with_suffix(path.suffix + ".lock"))
    try:
        entries = _read_entries_unlocked(path)
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
    finally:
        _release_lock(lock_fd)


def repair(path: Path | None = None) -> dict:
    """Keep the longest valid prefix; quarantine and re-stamp.

    Explicit, user-invoked recovery for a torn or corrupt ledger. The
    original file is preserved as `ledger.jsonl.broken-<ts>`; the rebuilt
    chain gets a `ledger_repair` event naming the dropped bytes/entries.
    """
    path = path or ledger_path()
    _check_ledger_target(path)
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
            if not isinstance(entry, dict):
                dropped_lines += 1
                continue
            if (entry.get("schema") == SCHEMA
                    and isinstance(entry.get("event"), str)
                    and entry.get("event")
                    and isinstance(entry.get("data"), dict)
                    and isinstance(entry.get("seq"), int)
                    and not isinstance(entry.get("seq"), bool)
                    and entry.get("seq") == len(valid) + 1
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
        fd = _open_regular_file(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for entry in valid:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
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
