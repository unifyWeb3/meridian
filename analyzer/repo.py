"""Repo walking + storage helpers for Meridian.

Pure stdlib. No network, no shell. Meridian writes only: `.meridian.yaml`
(via `spec accept`), paths the user names explicitly (`--out`), and its
plugin data directory (the ledger and report defaults).
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".next", ".nuxt", "dist", "build",
    "out", ".venv", "venv", "env", ".tox", ".mypy_cache", ".pytest_cache",
    "__pycache__", ".ruff_cache", ".idea", ".vscode", "site-packages",
    ".cache", "coverage", ".turbo", "target", ".gradle",
}

SUPPORTED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

MAX_FILE_BYTES = 512 * 1024
DEFAULT_MAX_FILES = 5000


class UnsafePathError(OSError):
    """A path is not a regular file or is a symlink to an untrusted target."""


def inspect_regular_file(path: Path, *, allow_missing: bool = False):
    """Return lstat metadata for a regular file, without following symlinks.

    A missing path is represented by ``None`` only when ``allow_missing`` is
    true. Symlinks and non-regular files are rejected: following either can
    move a read or write outside the directory the caller confined.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    except OSError as exc:
        raise UnsafePathError(f"cannot inspect {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePathError(f"{path} is a symlink; refusing to follow it")
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePathError(f"{path} is not a regular file")
    return info


def read_text(path: Path, *, errors: str = "strict") -> str:
    """Read a regular file without following a final symlink.

    POSIX uses ``O_NOFOLLOW``. The before/open/after identity check keeps the
    same guarantee on platforms without that flag and also rejects a file
    replaced during the read.
    """
    before = inspect_regular_file(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise UnsafePathError(f"cannot open {path}: {exc}") from exc
    with os.fdopen(fd, "r", encoding="utf-8", errors=errors) as handle:
        opened = os.fstat(handle.fileno())
        after = inspect_regular_file(path)
        if (not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(before, opened)
                or not os.path.samestat(before, after)):
            raise UnsafePathError(f"{path} changed while it was being opened")
        return handle.read()


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync after an atomic replace."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Atomically write UTF-8 text without following the target symlink.

    The final ``os.replace`` swaps the directory entry rather than opening
    the target, so a symlink (or hard link) planted at the destination is
    replaced, never written through. A same-directory temporary file keeps
    the replacement atomic and avoids exposing a partially written spec.
    Existing file modes are preserved; new files are private by default.
    """
    existing = inspect_regular_file(path, allow_missing=True)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    desired_mode = mode
    if desired_mode is None:
        desired_mode = stat.S_IMODE(existing.st_mode) if existing is not None else 0o600

    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
    )
    tmp: Path | None = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, desired_mode)
        except OSError:
            pass

        # Refuse an explicitly planted symlink. Even if it appears after this
        # check, os.replace below replaces the link itself and never follows
        # it to an outside file.
        inspect_regular_file(path, allow_missing=True)
        os.replace(tmp, path)
        tmp = None
        _fsync_directory(parent)
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def confined_output_path(root: Path, raw: str | os.PathLike[str]) -> Path:
    """Resolve an output path inside ``root`` without following its last link.

    Parent components are resolved so a symlinked directory cannot escape the
    root. The final component is deliberately not resolved: it is inspected
    with ``lstat`` and rejected when it is a symlink or any non-regular file.
    The returned path is canonical apart from that final component, so the
    atomic writer can replace a regular file without following a link.
    """
    root = root.resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.name in ("", ".", ".."):
        raise UnsafePathError("output path must name a file")
    try:
        parent = candidate.parent.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise UnsafePathError(f"cannot resolve output path {candidate}: {exc}") from exc
    target = parent / candidate.name
    if target == root or not target.is_relative_to(root):
        raise UnsafePathError(f"{target} resolves outside {root}")
    inspect_regular_file(target, allow_missing=True)
    return target


def walk_source_files(root: Path, max_files: int | None = None) -> tuple[list[Path], list[str]]:
    """Return (files, skipped_notes). Never raises on unreadable trees."""
    files: list[Path] = []
    skipped: list[str] = []
    try:
        root = root.resolve()
    except OSError:
        return [], [f"root not readable: {root}"]
    if not root.exists():
        return [], [f"root does not exist: {root}"]
    capped = False
    for dirpath, dirnames, filenames in os.walk(root):
        if max_files is not None and len(files) >= max_files:
            capped = True
            break
        kept_dirs: list[str] = []
        for name in dirnames:
            if name in SKIP_DIRS or name.startswith("."):
                continue
            path = Path(dirpath) / name
            if path.is_symlink():
                skipped.append(f"{path.relative_to(root)}: symlinked directory skipped")
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in filenames:
            if max_files is not None and len(files) >= max_files:
                capped = True
                break
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            ext = path.suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            try:
                info = path.lstat()
            except OSError:
                skipped.append(f"{rel}: unreadable")
                continue
            if stat.S_ISLNK(info.st_mode):
                skipped.append(f"{rel}: symlink skipped")
                continue
            if not stat.S_ISREG(info.st_mode):
                skipped.append(f"{rel}: not a regular file")
                continue
            if info.st_size > MAX_FILE_BYTES:
                skipped.append(f"{rel}: too large ({info.st_size} bytes)")
                continue
            files.append(path)
    if capped:
        skipped.append(f"scan capped at {max_files} files; results are partial")
    return files, skipped[:50]


def relpath(path: Path, root: Path) -> str:
    try:
        return os.path.relpath(path, root)
    except (ValueError, OSError):
        return str(path)


def plugin_data_dir() -> Path:
    """The sanctioned per-plugin data root under the active Hermes home.

    Prefers `plugins.plugin_storage.plugin_data_dir`; falls back to a plain
    filesystem location derived from HERMES_HOME so the ledger still works
    when imported outside Hermes (tests, CI, plain python).
    """
    try:
        from plugins.plugin_storage import plugin_data_dir as _pdd  # type: ignore

        try:
            return Path(str(_pdd("meridian")))
        except Exception:
            pass
    except Exception:
        pass
    home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return Path(home) / "plugin-data" / "meridian"


def ledger_path() -> Path:
    return plugin_data_dir() / "ledger.jsonl"


def find_spec(root: Path) -> Path | None:
    """Look for .meridian.yaml upwards from root (repo sentinel), bounded.

    The walk stops at any git boundary - including the start directory
    itself: a spec belongs to its repo, not to whatever sits above it.
    The starting root is checked first, so a repo whose spec is nested one
    level up is still found.
    """
    current = root.resolve()
    for _ in range(8):
        candidate = current / ".meridian.yaml"
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            info = None
        except OSError:
            info = None
        if info is not None:
            # Return a symlink deliberately: callers must fail closed on it
            # instead of silently falling back to a parent repository's spec.
            if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode):
                return candidate
        if (current / ".git").exists():
            return None
        if current.parent == current:
            break
        current = current.parent
    return None
