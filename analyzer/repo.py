"""Repo walking + storage helpers for Meridian.

Pure stdlib. No network, no shell. Meridian writes only: `.meridian.yaml`
(via `spec accept`), paths the user names explicitly (`--out`), and its
plugin data directory (the ledger and report defaults).
"""

from __future__ import annotations

import os
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
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
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
                size = path.stat().st_size
            except OSError:
                skipped.append(f"{rel}: unreadable")
                continue
            if size > MAX_FILE_BYTES:
                skipped.append(f"{rel}: too large ({size} bytes)")
                continue
            files.append(path)
    if capped:
        skipped.append(f"scan capped at {max_files} files; results are partial")
    return files, skipped[:50]


def relpath(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
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

    The walk stops at any git boundary — including the start directory
    itself: a spec belongs to its repo, not to whatever sits above it.
    The starting root is checked first, so a repo whose spec is nested one
    level up is still found.
    """
    current = root.resolve()
    for _ in range(8):
        candidate = current / ".meridian.yaml"
        if candidate.is_file():
            return candidate
        if (current / ".git").exists():
            return None
        if current.parent == current:
            break
        current = current.parent
    return None
