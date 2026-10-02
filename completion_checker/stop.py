"""The Stop hook: runs when the agent is about to hand back its final response.

Flow: load checklist -> build evidence packet -> ask Decider 2B narrow questions -> decide in
code -> in enforce mode, either send actionable gaps back to the agent (bounded number
of attempts) or let it stop with an honest summary for the user.
"""

import asyncio
import json
import time
from pathlib import Path

from . import checklist as checklist_mod
from . import snapshot
from .config import Config
from .decide import Evaluation, Reason, evaluate
from .evidence import Packet, build
from .decider import DATA_NOTE, DeciderError, DeciderResult, coverage_questions, requirement_questions, response_questions, segment
from .recorder import checklist_path, project_dir
from .store import SessionStore

COVERAGE_BATCH = 40


async def _ask(decider, state: dict, questions: dict) -> DeciderResult:
    try:
        return await decider.ask(state, questions)
    except DeciderError as e:
        return DeciderResult(error=str(e))
    except Exception as e:  # anything unexpected is an error, never an answer
        return DeciderResult(error=f"{type(e).__name__}: {e}"[:300])


async def run_judgments(checklist, packet: Packet, decider, config: Config):
    model_commands = packet.commands_for_model(config.max_commands_in_state)
    failing = [(i, c["id"]) for i, c in enumerate(model_commands) if c["result"].startswith(("failed", "interrupted"))]
    runtime = [w for w in packet.runtime if w["current"]]
    files = [{k: f[k] for k in ("id", "path", "change", "diff")} for f in packet.files]

    tasks: dict[str, asyncio.Task] = {}
    for req in checklist.requirements:
        state = {
            "note": DATA_NOTE,
            "requirement": {"id": req.id, "text": req.text, "verification": req.verification},
            "code_changes": [dict(f) for f in files],
            "commands": model_commands,
            "runtime_checks": runtime,
            "final_response": packet.final_response,
            "declared_blocker": checklist.blockers.get(req.id),
        }
        qs = requirement_questions(len(model_commands) + len(runtime), failing, req.id in checklist.blockers)
        tasks[req.id] = asyncio.ensure_future(_ask(decider, state, qs))

    response_task = asyncio.ensure_future(_ask(decider, {"note": DATA_NOTE, "final_response": packet.final_response}, response_questions()))

    coverage_tasks = []
    if config.coverage_check and packet.prompts:
        segs = [(p["id"], s) for p in packet.prompts for s in segment(p["text"])]
        items = [{"id": r.id, "text": r.text} for r in checklist.requirements]
        for start in range(0, len(segs), COVERAGE_BATCH):
            batch = segs[start : start + COVERAGE_BATCH]
            texts = [s for _, s in batch]
            ids = ",".join(sorted({pid for pid, _ in batch}))
            state = {"note": DATA_NOTE, "checklist": items, "segments": texts}
            coverage_tasks.append((ids, texts, asyncio.ensure_future(_ask(decider, state, coverage_questions(len(texts))))))

    everything = list(tasks.values()) + [response_task] + [t for _, _, t in coverage_tasks]
    done, pending = await asyncio.wait(everything, timeout=config.overall_deadline_s)
    for t in pending:
        t.cancel()

    def result(task) -> DeciderResult:
        return task.result() if task in done else DeciderResult(error="overall checker deadline reached")

    results = {rid: result(t) for rid, t in tasks.items()}
    coverage = [(ids, texts, result(t)) for ids, texts, t in coverage_tasks]
    response = result(response_task)
    # Anything cut to fit the model's window is part of the record, not hidden.
    for label, res in [(rid, r) for rid, r in results.items()] + [("response", response)] + [(f"coverage {ids}", r) for ids, _, r in coverage]:
        if res.trimmed:
            packet.notes.append(f"{label}: trimmed to fit the model window ({'; '.join(res.trimmed[:6])})")
    return results, response, coverage, [cid for _, cid in failing]


def _default_decider(config: Config, deadline: float):
    from .decider import LocalDecider

    return LocalDecider(config.model, config.device, deadline=deadline)


def check(payload: dict, config: Config, decider=None) -> tuple[Evaluation | None, Packet | None, dict]:
    """Evaluate the current state. Returns (evaluation, packet, context)."""
    deadline = time.monotonic() + config.overall_deadline_s
    store = SessionStore(payload.get("session_id", "unknown"))
    project = project_dir(payload)
    path = checklist_path(project, store.session_id)
    raw = path.read_text() if path.exists() else None
    history = store.events("checklist_seen")
    prompts = store.events("prompt")
    checklist = checklist_mod.load(raw, history, prompts)
    if raw is not None:
        store.append("checklist_seen", content=raw[:100_000])
    ctx = {"store": store, "project": project, "checklist_path": path}

    state = store.read_state()
    baseline = store.load_snapshot(state.get("baseline_snapshot"))
    changed_files = snapshot.relevant(snapshot.changed(baseline, snapshot.take(project)), config.staleness_ignore) if baseline is not None else []

    if checklist is None:
        if changed_files and config.require_checklist_when_files_change:
            issue = Reason("CHECKLIST_MISSING", f"files changed ({', '.join(changed_files[:5])}) but there is no acceptance checklist at {path}")
            return Evaluation([], [issue], [], False), None, ctx
        return None, None, ctx  # not a coding task report: nothing to check

    packet = build(store, project, config, checklist, payload.get("last_assistant_message", ""))
    if not checklist.requirements:
        ev = evaluate(checklist, packet, {}, None, [], [], config.thresholds, config.raise_verification)
        return ev, packet, ctx

    owned = decider is None
    try:
        decider = decider or _default_decider(config, deadline)
    except Exception as e:  # a model that will not load is an error, never a pass
        failed = DeciderResult(error=f"{type(e).__name__}: {e}"[:300])
        results = {r.id: failed for r in checklist.requirements}
        return evaluate(checklist, packet, results, failed, [], [], config.thresholds, config.raise_verification), packet, ctx

    async def go():
        try:
            return await run_judgments(checklist, packet, decider, config)
        finally:
            if owned:
                await decider.aclose()

    results, response, coverage, failing_ids = asyncio.run(go())
    ev = evaluate(checklist, packet, results, response, coverage, failing_ids, config.thresholds, config.raise_verification)
    return ev, packet, ctx


# --- presentation -------------------------------------------------------------------------


def _line(r) -> str:
    reasons = "; ".join(f"{x.code}: {x.detail}" + (f" [{', '.join(x.evidence)}]" if x.evidence else "") for x in r.reasons)
    return f"- {r.id} [{r.status}] {r.text[:120]}" + (f" — {reasons}" if reasons else "")


def feedback(ev: Evaluation, attempt: int, max_attempts: int, path: Path) -> str:
    lines = [f"Completion check (correction attempt {attempt} of {max_attempts}) found work that is unfinished or not shown by the evidence:"]
    for r in ev.unresolved:
        lines.append(_line(r))
    for i in ev.issues:
        if i.actionable:
            lines.append(f"- {i.code}: {i.detail}" + (f" [{', '.join(i.evidence)}]" if i.evidence else ""))
    lines += [
        "",
        "Next: finish or fix these items, re-run the relevant checks after your last edit, and make the final message match the evidence.",
        f"Do not remove or weaken checklist items in {path} to pass. If an external prerequisite blocks you, record it under \"blockers\" and explain it plainly in your final message.",
        "Evidence ids: C = trusted command runs, F = changed files, W = runtime/browser checks, U = user messages, RESPONSE = your final message.",
    ]
    return "\n".join(lines)


def summary(ev: Evaluation, outcome: str, attempts: int) -> str:
    required = [r for r in ev.requirements if r.kind == "required"]
    ok = sum(r.status == "satisfied" for r in required)
    head = {
        "completed": "COMPLETED",
        "stopped_with_unresolved": "STOPPED WITH UNRESOLVED ITEMS",
        "checker_unavailable": "NOT VERIFIED (checker unavailable)",
        "awaiting_user": "PAUSED FOR USER INPUT",
    }[outcome]
    lines = [f"Completion checker: {head} — {ok}/{len(required)} required items satisfied" + (f" after {attempts} correction attempt(s)" if attempts else "") + (f" ({ev.model})" if ev.model else "")]
    for r in ev.requirements:
        if r.status != "satisfied" or r.kind == "optional":
            lines.append(f"  {r.id} {r.kind} {r.status}: " + "; ".join(x.detail for x in r.reasons[:2]))
    for i in ev.issues:
        lines.append(f"  {i.code}: {i.detail}")
    for e in ev.checker_errors[:3]:
        lines.append(f"  checker error: {e}")
    return "\n".join(lines)[:9000]


def report_markdown(ev: Evaluation, packet: Packet | None, outcome: str, mode: str) -> str:
    out = [f"# Completion check: {outcome}", "", f"Mode: {mode}. Model: {ev.model or 'n/a'}.", "", "| ID | Kind | Status | Reasons |", "|---|---|---|---|"]
    for r in ev.requirements:
        why = "<br>".join(f"{x.code}: {x.detail} {' '.join(x.evidence)}" for x in r.reasons) or "evidence supports it"
        out.append(f"| {r.id} | {r.kind} | {r.status} | {why.replace('|', '/')} |")
    if ev.issues:
        out += ["", "## Issues"] + [f"- {i.code}: {i.detail} {' '.join(i.evidence)}" for i in ev.issues]
    if ev.checker_errors:
        out += ["", "## Checker errors"] + [f"- {e}" for e in ev.checker_errors]
    if packet and packet.notes:
        out += ["", "## Notes"] + [f"- {n}" for n in packet.notes]
    if packet:
        out += ["", "## Evidence"]
        out += [f"- {c['id']} `{c['command'][:100]}` → {c['result']}" + ("" if c["current"] else f" (stale: {c['stale_reason']})") + (f" (superseded by {c['superseded_by']})" if c.get("superseded_by") else "") for c in packet.commands]
        out += [f"- {f['id']} {f['change']} `{f['path']}`" for f in packet.files]
    return "\n".join(out) + "\n"


def on_stop(payload: dict, config: Config, decider=None) -> dict | None:
    started = time.time()
    ev, packet, ctx = check(payload, config, decider)
    if ev is None:
        return None
    store: SessionStore = ctx["store"]

    missing_checklist = any(i.code == "CHECKLIST_MISSING" for i in ev.issues)
    with store.state() as state:
        attempts = state.get("attempts", 0)
        nudges = state.get("checklist_nudges", 0)
        nudge = False
        if ev.completed:
            outcome, action = "completed", "allow"
        elif ev.asks_user:
            outcome, action = "awaiting_user", "allow"
        elif missing_checklist and nudges < 1:
            # Asking for the checklist is setup, not a correction: it has its own budget of one.
            outcome, action, nudge = "needs_work", "block", True
        elif ev.actionable and attempts < config.max_attempts:
            outcome, action = "needs_work", "block"
        elif ev.checker_errors and not ev.actionable:
            outcome, action = "checker_unavailable", "allow"
        else:
            outcome, action = "stopped_with_unresolved", "allow"
        if config.mode == "enforce" and action == "block":
            if nudge:
                state["checklist_nudges"] = nudges + 1
            else:
                state["attempts"] = attempts + 1

    record = {
        "at": started,
        "seconds": round(time.time() - started, 2),
        "mode": config.mode,
        "outcome": outcome,
        "action": action if config.mode == "enforce" else "observe_only",
        "attempts_before": attempts,
        "checklist_nudge": nudge,
        "thresholds": config.to_dict()["thresholds"],
        "evaluation": ev.to_dict(),
        "evidence": {
            "commands": [{k: c.get(k) for k in ("id", "command", "result", "current", "stale_reason", "superseded_by")} for c in packet.commands] if packet else [],
            "files": [{k: f[k] for k in ("id", "path", "change")} for f in packet.files] if packet else [],
            "validation": packet.validation if packet else [],
            "injection_hits": packet.injection_hits if packet else [],
            "notes": packet.notes if packet else [],
        },
    }
    store.log_decision(record)
    report_dir = ctx["checklist_path"].parent
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.md").write_text(report_markdown(ev, packet, outcome, config.mode))

    if config.mode == "observe":
        return None
    if action == "block" and nudge:
        from .recorder import PROTOCOL

        reason = f"Completion check: files changed but there is no acceptance checklist. Write it now from the user's messages (this does not use a correction attempt).\n{PROTOCOL.format(path=ctx['checklist_path'])}"
        return {"decision": "block", "reason": reason}
    if action == "block":
        return {"decision": "block", "reason": feedback(ev, attempts + 1, config.max_attempts, ctx["checklist_path"])}
    return {"systemMessage": summary(ev, outcome, attempts)}
