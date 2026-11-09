"""Repo walking + storage helpers for Meridian.

Pure stdlib. No network, no shell, no writes outside the target repo
(.meridian/) and the plugin data directory.
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


def walk_source_files(root: Path) -> tuple[list[Path], list[str]]:
    """Return (files, skipped_notes). Never raises on unreadable trees."""
    files: list[Path] = []
    skipped: list[str] = []
    try:
        root = root.resolve()
    except OSError:
        return [], [f"root not readable: {root}"]
    if not root.exists():
        return [], [f"root does not exist: {root}"]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
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
    return files, skipped


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


def scan_cache_path() -> Path:
    return plugin_data_dir() / "scan-cache.json"


def ledger_path() -> Path:
    return plugin_data_dir() / "ledger.jsonl"


def find_spec(root: Path) -> Path | None:
    """Look for .meridian.yaml upwards from root (repo sentinel), bounded."""
    current = root.resolve()
    for _ in range(8):
        candidate = current / ".meridian.yaml"
        if candidate.is_file():
            return candidate
        if current.parent == current:
            break
        # stop at git boundary even if not a meridian home
        if (current / ".git").exists() and current != root.resolve():
            return None
        current = current.parent
    return None
