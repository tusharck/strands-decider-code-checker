"""Per-session storage for trusted records, kept outside the project.

The ledger holds what the harness observed (prompts, tool runs, snapshots). It lives
outside the project so the agent's normal file edits do not touch it, and the
PreToolUse recorder refuses Edit/Write calls aimed at it.
"""

import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path


def state_root() -> Path:
    base = os.environ.get("DECIDER_CHECKER_STATE_DIR")
    if base:
        return Path(base).expanduser()
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "strands-decider-code-checker"


def _safe(session_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return cleaned[:128] or "unknown"


class SessionStore:
    def __init__(self, session_id: str, root: Path | None = None):
        self.session_id = session_id
        self.dir = (root or state_root()) / "sessions" / _safe(session_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "snapshots").mkdir(exist_ok=True)

    # --- append-only event ledger -------------------------------------------------
    def append(self, kind: str, **data) -> dict:
        event = {"kind": kind, "at": time.time(), **data}
        line = (json.dumps(event, sort_keys=True) + "\n").encode()
        fd = os.open(self.dir / "ledger.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)  # one write per event: appends stay whole under concurrency
        finally:
            os.close(fd)
        return event

    def events(self, kind: str | None = None) -> list[dict]:
        path = self.dir / "ledger.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line never becomes evidence
            if kind is None or event.get("kind") == kind:
                out.append(event)
        return out

    # --- content-addressed file snapshots -------------------------------------------
    def save_snapshot(self, files: dict[str, list]) -> str:
        payload = json.dumps(files, sort_keys=True).encode()
        digest = hashlib.sha256(payload).hexdigest()[:20]
        path = self.dir / "snapshots" / f"{digest}.json"
        if not path.exists():
            path.write_bytes(payload)
        return digest

    def load_snapshot(self, digest: str | None) -> dict[str, list] | None:
        if not digest:
            return None
        path = self.dir / "snapshots" / f"{digest}.json"
        return json.loads(path.read_text()) if path.exists() else None

    # --- small mutable state (attempt counter, baseline) -----------------------------
    @contextmanager
    def state(self):
        path = self.dir / "state.json"
        lock = open(self.dir / "state.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = json.loads(path.read_text()) if path.exists() else {}
            yield data
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
            tmp.replace(path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def read_state(self) -> dict:
        path = self.dir / "state.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def log_decision(self, record: dict) -> None:
        with open(self.dir / "decisions.jsonl", "a") as f:
            f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
