"""Command line: the hook entry point, installer, and report viewer.

  hook                 read one Claude Code hook event on stdin, answer on stdout
  install PROJECT      add the hooks to PROJECT/.claude/settings.local.json
  report [SESSION]     print the latest decision for a session (default: newest)
"""

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

from .config import load_config
from .store import state_root

REPO = Path(__file__).resolve().parent.parent


def _log_error(event: str) -> None:
    path = state_root() / "errors.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {event}\n{traceback.format_exc()}\n")


def run_hook(stdin_text: str) -> dict | None:
    payload = json.loads(stdin_text or "{}")
    event = payload.get("hook_event_name", "")
    from .recorder import on_permission_denied, on_post_tool, on_pre_tool, on_user_prompt, project_dir

    try:
        config = load_config(project_dir(payload))
        if event == "UserPromptSubmit":
            return on_user_prompt(payload, config)
        if event == "PreToolUse":
            return on_pre_tool(payload, config)
        if event == "PostToolUse":
            return on_post_tool(payload, config, failed=False)
        if event == "PostToolUseFailure":
            return on_post_tool(payload, config, failed=True)
        if event == "PermissionDenied":
            return on_permission_denied(payload, config)
        if event == "Stop":
            from .stop import on_stop

            return on_stop(payload, config)
    except Exception:
        _log_error(event)
        if event == "Stop":
            # A crash must never look like a pass: tell the user plainly.
            return {"systemMessage": f"Completion checker failed ({sys.exc_info()[0].__name__}); this completion was NOT verified. See {state_root() / 'errors.log'}."}
    return None


HOOK_EVENTS = {
    "UserPromptSubmit": (None, 30),
    "PreToolUse": ("^(Bash|Edit|Write|MultiEdit|NotebookEdit)$", 30),
    "PostToolUse": ("^(Bash|Edit|Write|MultiEdit|NotebookEdit)$|^mcp__", 30),
    "PostToolUseFailure": ("^(Bash|Edit|Write|MultiEdit|NotebookEdit)$|^mcp__", 30),
    "PermissionDenied": ("^Bash$", 30),
    "Stop": (None, 120),
}
MARK = "strands-decider-code-checker"


def install(project: Path, mode: str) -> None:
    python = REPO / ".venv" / "bin" / "python"
    if not python.exists():
        sys.exit(f"Run `uv sync` in {REPO} first.")
    settings_path = project / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    if settings_path.exists():
        shutil.copy(settings_path, settings_path.with_suffix(".json.bak"))
    hooks = settings.setdefault("hooks", {})
    for event, (matcher, timeout) in HOOK_EVENTS.items():
        groups = [g for g in hooks.get(event, []) if not any(MARK in json.dumps(h) for h in g.get("hooks", []))]
        handler = {"type": "command", "command": str(python), "args": [str(REPO / "hook.py")], "timeout": timeout}
        if event == "Stop":
            handler["statusMessage"] = "Checking completion against evidence (Decider 2B, local)"
        group = {"hooks": [handler]}
        if matcher:
            group["matcher"] = matcher
        hooks[event] = groups + [group]
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    config_path = project / ".completion" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(json.dumps({"mode": mode, "max_attempts": 2, "required_validation": []}, indent=2) + "\n")
    gitignore = project / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    if ".completion/" not in lines:
        gitignore.write_text("\n".join(lines + [".completion/"]) + "\n")
    print(f"Installed hooks in {settings_path}\nConfig: {config_path} (mode={json.loads(config_path.read_text())['mode']})")


def report(session: str | None) -> None:
    root = state_root() / "sessions"
    dirs = [root / session] if session else sorted(root.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dirs:
        path = d / "decisions.jsonl"
        if path.exists():
            last = json.loads(path.read_text().splitlines()[-1])
            print(f"session {d.name}: {last['outcome']} ({last['mode']}, action={last['action']})")
            for r in last["evaluation"]["requirements"]:
                print(f"  {r['id']:<6} {r['kind']:<9} {r['status']:<11} " + "; ".join(x["detail"] for x in r["reasons"][:2]))
            for i in last["evaluation"]["issues"]:
                print(f"  {i['code']}: {i['detail']}")
            return
    print("No decisions recorded yet.")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="completion_checker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook")
    p = sub.add_parser("install")
    p.add_argument("project", type=Path)
    p.add_argument("--mode", choices=["observe", "enforce"], default="observe")
    r = sub.add_parser("report")
    r.add_argument("session", nargs="?")
    args = parser.parse_args(argv)
    if args.cmd == "hook":
        try:
            out = run_hook(sys.stdin.read())
        except Exception:
            _log_error("unparsable hook input")
            out = None
        if out:
            sys.stdout.write(json.dumps(out))
    elif args.cmd == "install":
        install(args.project.resolve(), args.mode)
    elif args.cmd == "report":
        report(args.session)
