"""Cheap snapshots of the project's files, used to tie each run to the code it tested.

A snapshot maps each file to (size, mtime_ns). Comparing two snapshots tells which
files changed in between, so a test run from before a relevant edit can be marked
stale. Stat data is used instead of content hashes to keep tool hooks fast.
"""

import fnmatch
import os
import subprocess
from pathlib import Path

SKIP_DIRS = {".git", ".completion", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", "dist", "build"}
MAX_FILES = 50_000


def _git(project: Path, *args: str, check: bool = True) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if check and out.returncode != 0:
        return None
    return out.stdout


def is_git(project: Path) -> bool:
    return (_git(project, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def list_files(project: Path) -> list[str]:
    if is_git(project):
        out = _git(project, "ls-files", "-co", "--exclude-standard", "-z")
        if out is not None:
            return [p for p in out.split("\0") if p and not p.startswith(".completion/")][:MAX_FILES]
    files = []
    for root, dirs, names in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            files.append(os.path.relpath(os.path.join(root, name), project))
            if len(files) >= MAX_FILES:
                return files
    return files


def take(project: Path) -> dict[str, list]:
    snap = {}
    for rel in list_files(project):
        try:
            st = os.stat(project / rel)
        except OSError:
            continue
        snap[rel] = [st.st_size, st.st_mtime_ns]
    return snap


def changed(before: dict, after: dict) -> dict[str, str]:
    """Paths that differ between two snapshots: added, modified, or deleted."""
    out = {}
    for path, meta in after.items():
        if path not in before:
            out[path] = "added"
        elif before[path] != meta:
            out[path] = "modified"
    for path in before:
        if path not in after:
            out[path] = "deleted"
    return out


def relevant(paths, ignore_globs: list[str]) -> list[str]:
    return sorted(p for p in paths if not any(fnmatch.fnmatch(p, g) for g in ignore_globs))


def head_commit(project: Path) -> str | None:
    out = _git(project, "rev-parse", "HEAD")
    return out.strip() if out else None


def file_diff(project: Path, baseline: str | None, path: str, status: str, max_chars: int) -> str:
    """Diff with surrounding context for one changed file, or its content when no base exists."""
    if status == "deleted":
        return "(file deleted)"
    text = None
    if baseline and status == "modified":
        text = _git(project, "diff", "--no-color", "-U8", baseline, "--", path)
    if not text:
        try:
            raw = (project / path).read_bytes()
        except OSError:
            return "(unreadable)"
        if b"\0" in raw[:4096]:
            return "(binary file)"
        body = raw.decode("utf-8", errors="replace")
        text = ("(new file)\n" if status == "added" else "(no baseline; current content)\n") + body
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [{len(text) - max_chars} characters truncated]"
    return text
