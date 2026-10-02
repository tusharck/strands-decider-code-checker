"""Hooks that record trusted evidence while the agent works.

- UserPromptSubmit: keeps the user's exact words (for traceability) and tells the agent
  where its acceptance checklist goes.
- PreToolUse / PostToolUse / PostToolUseFailure: record each shell command with its exit
  code, output tail, and the file snapshots before and after it ran.

These records come from Claude Code, not from the agent's own statements.
"""

import os
import re
from pathlib import Path

from . import snapshot
from .config import Config
from .redact import redact
from .store import SessionStore, state_root

FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

PROTOCOL = """\
[completion-checker] A completion checker reviews your work before you finish, using
trusted records of your commands and file changes. Keep an acceptance checklist at:
  {path}
JSON shape:
{{
  "requirements": [
    {{"id": "R1", "text": "<what must be true>", "source_quote": "<exact words from the user's message>",
      "kind": "required" | "optional", "verification": "execution" | "runtime" | "inspection"}}
  ],
  "validation": [{{"command": "<check command>", "requirements": ["R1"]}}],
  "blockers": [{{"requirement": "R2", "description": "<external prerequisite that stops you>"}}],
  "amendments": [{{"requirement": "R1", "user_quote": "<the user's later words that change it>"}}]
}}
Write this file BEFORE changing any project file (in enforce mode, edits are refused until
it exists). Rules: give every user requirement and constraint a stable ID; mark suggestions "optional";
use "execution"/"runtime" when running code is the appropriate check and "inspection" when
reading the change is enough. Do not delete or weaken a requirement unless the user said
so (then record the amendment with their exact words). Run checks after your final edit;
earlier runs of changed code do not count. If something outside your control blocks you,
record it under "blockers" and say so plainly in your final message."""


def project_dir(payload: dict) -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".").resolve()


def checklist_path(project: Path, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return project / ".completion" / safe / "checklist.json"


def on_user_prompt(payload: dict, config: Config) -> dict | None:
    store = SessionStore(payload.get("session_id", "unknown"))
    project = project_dir(payload)
    prompts = store.events("prompt")
    store.append("prompt", index=len(prompts) + 1, text=redact(payload.get("prompt", "")))
    with store.state() as state:
        state["attempts"] = 0  # a new user message starts a new round of corrections
        state["checklist_nudges"] = 0
        if "baseline_snapshot" not in state:
            state["baseline_snapshot"] = store.save_snapshot(snapshot.take(project))
            state["baseline_commit"] = snapshot.head_commit(project)
            state["project_dir"] = str(project)
    path = checklist_path(project, store.session_id)
    if not prompts:
        context = PROTOCOL.format(path=path)
    else:
        context = f"[completion-checker] User message #{len(prompts) + 1} recorded. Add any new requirements or clarifications to {path}."
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}


def on_pre_tool(payload: dict, config: Config) -> dict | None:
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if tool in FILE_TOOLS:
        target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        try:
            inside = Path(target).resolve().is_relative_to(state_root().resolve())
        except (OSError, ValueError):
            inside = False
        if inside:
            return _deny("The completion checker's trusted records are read-only.")
        if config.mode == "enforce" and config.checklist_first:
            project = project_dir(payload)
            path = checklist_path(project, payload.get("session_id", "unknown"))
            try:
                resolved = Path(target).resolve()
                in_project = resolved.is_relative_to(project) and not resolved.is_relative_to(project / ".completion")
            except (OSError, ValueError):
                in_project = False
            if in_project and not path.exists():
                # The checklist must come before the work, so it reflects the request, not the result.
                return _deny("Write the acceptance checklist before changing project files.\n" + PROTOCOL.format(path=path))
        return None
    if tool == "Bash":
        store = SessionStore(payload.get("session_id", "unknown"))
        snap = store.save_snapshot(snapshot.take(project_dir(payload)))
        store.append("tool_start", tool_use_id=payload.get("tool_use_id"), snapshot=snap, command=redact(tool_input.get("command", "")), agent_id=payload.get("agent_id"))
    return None


_EXIT_RE = re.compile(r"^Exit code (-?\d+)")


def _tail(text: str, limit: int) -> str:
    text = redact(text or "")
    return text if len(text) <= limit else "... " + text[-limit:]


def on_permission_denied(payload: dict, config: Config) -> dict | None:
    """Auto mode refused a tool call: trusted evidence for a genuine blocker."""
    if payload.get("tool_name") != "Bash":
        return None
    store = SessionStore(payload.get("session_id", "unknown"))
    snap = store.save_snapshot(snapshot.take(project_dir(payload)))
    store.append(
        "command",
        tool_use_id=payload.get("tool_use_id"),
        agent_id=payload.get("agent_id"),
        command=redact((payload.get("tool_input") or {}).get("command", "")),
        exit_code=None,
        status="denied",
        output_tail=_tail(f"denied by the permission system: {payload.get('reason', '')}", config.max_output_tail_chars),
        snapshot_before=snap,
        snapshot_after=snap,
        source="claude_code_hook",
    )
    return None


def on_post_tool(payload: dict, config: Config, failed: bool) -> dict | None:
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    store = SessionStore(payload.get("session_id", "unknown"))
    project = project_dir(payload)
    limit = config.max_output_tail_chars

    if tool == "Bash":
        start = next(
            (e for e in reversed(store.events("tool_start")) if e.get("tool_use_id") == payload.get("tool_use_id")),
            None,
        )
        after = store.save_snapshot(snapshot.take(project))
        if failed:
            error = payload.get("error") or ""
            match = _EXIT_RE.match(error)
            exit_code = int(match.group(1)) if match else None
            output = error
            status = "interrupted" if payload.get("is_interrupt") else "failed"
        else:
            response = payload.get("tool_response") or {}
            if isinstance(response, dict):
                output = "\n".join(filter(None, [response.get("stdout", ""), response.get("stderr", "")]))
                interrupted = bool(response.get("interrupted"))
            else:
                output, interrupted = str(response), False
            backgrounded = bool(tool_input.get("run_in_background")) or (
                isinstance(response, dict) and bool(response.get("backgroundTaskId"))
            )
            exit_code = None if (interrupted or backgrounded) else 0
            status = "interrupted" if interrupted else "backgrounded" if backgrounded else "passed"
        store.append(
            "command",
            tool_use_id=payload.get("tool_use_id"),
            agent_id=payload.get("agent_id"),
            command=redact(tool_input.get("command", "")),
            exit_code=exit_code,
            status=status,
            output_tail=_tail(output, limit),
            snapshot_before=start.get("snapshot") if start else None,
            snapshot_after=after,
            source="claude_code_hook",
        )
        return None

    if any(re.search(p, tool) for p in config.runtime_tool_patterns):
        response = payload.get("error") if failed else payload.get("tool_response")
        store.append(
            "runtime",
            tool_use_id=payload.get("tool_use_id"),
            tool=tool,
            input=_tail(str(tool_input), 500),
            status="failed" if failed else "passed",
            output_tail=_tail(str(response), limit),
            snapshot_after=store.save_snapshot(snapshot.take(project)),
            source="claude_code_hook",
        )
        return None

    if tool in FILE_TOOLS and not failed:
        path = Path(tool_input.get("file_path") or "")
        if path.name == "checklist.json" and path.parent.parent.name == ".completion":
            try:
                store.append("checklist_seen", content=path.read_text()[:100_000])
            except OSError:
                pass
    return None
