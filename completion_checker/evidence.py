"""Assemble the evidence packet: IDs, freshness, and gaps that code can decide alone.

A run counts as *current* only when (1) snapshots exist from just before and just after
it, (2) the run itself changed no relevant files, and (3) no relevant file changed
between the run and now. Anything else is stale and can never establish a pass.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import snapshot
from .checklist import Checklist, normalize
from .config import Config
from .redact import redact
from .store import SessionStore

INJECTION_PATTERNS = [
    r"(ignore|disregard|forget)\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|guidance)",
    r"\b(checker|evaluator|reviewer|grader|judge|jev|typesafe|decider|strands)\b[^\n]{0,80}\b(mark|treat|consider|answer|return|report|output|say)\b",
    r"\b(mark|treat|consider|report)\b[^\n]{0,40}\b(as\s+)?(satisfied|complete[d]?|passed|verified|done)\b[^\n]{0,40}\b(regardless|anyway|even if|no matter)\b",
    r"\byou are now\b|\bnew instructions\b|\bsystem prompt\b",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


@dataclass
class Packet:
    files: list[dict] = field(default_factory=list)
    commands: list[dict] = field(default_factory=list)  # every recorded run, with freshness
    runtime: list[dict] = field(default_factory=list)
    prompts: list[dict] = field(default_factory=list)
    final_response: str = ""
    validation: list[dict] = field(default_factory=list)  # code-decided status per required check
    injection_hits: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def commands_for_model(self, limit: int) -> list[dict]:
        """Current check runs and unresolved failures only, newest last, each once."""
        live = [c for c in self.commands if c["current"] and not c.get("superseded_by")]
        failing = [c for c in live if c["status"] in ("failed", "interrupted")]
        checks = [c for c in live if c not in failing and (c["is_check"] or c["status"] != "passed")]
        room = limit - len(failing)
        keep = {c["id"] for c in failing} | {c["id"] for c in (checks[-room:] if room > 0 else [])}
        return [
            {k: c[k] for k in ("id", "command", "exit_code", "result", "output_tail")}
            for c in live
            if c["id"] in keep
        ]

    def failing_current(self) -> list[dict]:
        return [c for c in self.commands if c["current"] and not c.get("superseded_by") and c["status"] in ("failed", "interrupted")]


def _matches(patterns: list[str], command: str) -> bool:
    return any(re.search(p, command) for p in patterns)


def build(store: SessionStore, project: Path, config: Config, checklist: Checklist | None, final_response: str) -> Packet:
    packet = Packet(final_response=redact(final_response or "")[:6000])
    state = store.read_state()
    now = snapshot.take(project)
    ignore = config.staleness_ignore

    # Code changes since the session started.
    baseline = store.load_snapshot(state.get("baseline_snapshot"))
    if baseline is None:
        packet.notes.append("no session baseline was recorded; code changes cannot be listed")
    else:
        changes = snapshot.changed(baseline, now)
        per_file = max(2000, config.max_diff_chars // max(1, len(changes)))
        for i, (path, status) in enumerate(sorted(changes.items()), 1):
            diff = snapshot.file_diff(project, state.get("baseline_commit"), path, status, per_file)
            packet.files.append({"id": f"F{i}", "path": path, "change": status, "diff": redact(diff)})

    # Command runs, with freshness decided against the current snapshot. A start with no
    # result means Claude Code refused the call before it ran (permission rejection).
    finished = store.events("command")
    seen = {e.get("tool_use_id") for e in finished}
    orphans = [
        dict(e, kind="command", status="not_run", exit_code=None, snapshot_before=e.get("snapshot"), snapshot_after=e.get("snapshot"), output_tail="")
        for e in store.events("tool_start")
        if e.get("tool_use_id") not in seen and e.get("command")
    ]
    for i, ev in enumerate(sorted(finished + orphans, key=lambda e: e.get("at", 0)), 1):
        before = store.load_snapshot(ev.get("snapshot_before"))
        after = store.load_snapshot(ev.get("snapshot_after"))
        reason = None
        if before is None or after is None:
            reason = "no snapshot around the run"
        else:
            during = snapshot.relevant(snapshot.changed(before, after), ignore)
            since = snapshot.relevant(snapshot.changed(after, now), ignore)
            if during:
                reason = f"the run itself changed {', '.join(during[:3])}"
            elif since:
                reason = f"changed after the run: {', '.join(since[:3])}"
        cmd = ev.get("command", "")
        result = {
            "passed": "passed (exit 0)",
            "failed": f"failed (exit {ev.get('exit_code')})",
            "denied": "not run: denied by the permission system",
            "not_run": "not run: the call was refused before it executed (for example, permission not granted)",
            "backgrounded": "started in the background; result unknown",
        }.get(ev["status"], ev["status"])
        packet.commands.append(
            {
                "id": f"C{i}",
                "command": cmd,
                "exit_code": ev.get("exit_code"),
                "status": ev["status"],
                "result": result,
                "output_tail": ev.get("output_tail", ""),
                "current": reason is None,
                "stale_reason": reason,
                "is_check": _matches(config.check_command_patterns, cmd),
                "is_test": _matches(config.test_command_patterns, cmd),
                "subagent": bool(ev.get("agent_id")),
            }
        )
    # A failure is resolved by a later current pass of the same command.
    for i, c in enumerate(packet.commands):
        if c["status"] != "passed":
            later = next(
                (d for d in packet.commands[i + 1 :] if normalize(d["command"]) == normalize(c["command"]) and d["current"] and d["status"] == "passed"),
                None,
            )
            if later:
                c["superseded_by"] = later["id"]

    for i, ev in enumerate(store.events("runtime"), 1):
        after = store.load_snapshot(ev.get("snapshot_after"))
        stale = after is None or bool(snapshot.relevant(snapshot.changed(after, now), ignore))
        packet.runtime.append(
            {"id": f"W{i}", "tool": ev.get("tool"), "input": ev.get("input"), "result": ev.get("status"), "output_tail": ev.get("output_tail", ""), "current": not stale}
        )

    for i, ev in enumerate(store.events("prompt"), 1):
        packet.prompts.append({"id": f"U{i}", "text": ev.get("text", "")})

    # Required validation: decided entirely in code.
    required = [{"command": c, "requirements": [], "source": "config"} for c in config.required_validation]
    if checklist:
        required += [dict(v, source="checklist") for v in checklist.validation]
    for v in required:
        needle = normalize(v["command"])
        runs = [c for c in packet.commands if needle and needle in normalize(c["command"])]
        latest = runs[-1] if runs else None
        if latest is None:
            status = "missing"
        elif not latest["current"]:
            status = "stale"
        elif latest["status"] == "passed":
            status = "passed"
        elif latest["status"] in ("not_run", "denied", "backgrounded"):
            status = "not_run"
        else:
            status = "failed"
        packet.validation.append(dict(v, status=status, evidence=latest["id"] if latest else None))

    # Text that looks like it is talking to the checker is flagged, never obeyed.
    sources = [(f["id"], f["diff"]) for f in packet.files]
    sources += [(c["id"], c["output_tail"] + "\n" + c["command"]) for c in packet.commands if c["current"]]
    sources += [(w["id"], w["output_tail"]) for w in packet.runtime]
    sources.append(("RESPONSE", packet.final_response))
    for evidence_id, text in sources:
        for rx in _INJECTION_RE:
            m = rx.search(text or "")
            if m:
                packet.injection_hits.append({"evidence": evidence_id, "snippet": text[max(0, m.start() - 40) : m.end() + 40]})
                break
    return packet
