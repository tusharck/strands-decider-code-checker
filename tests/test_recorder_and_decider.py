"""Trusted-record capture, redaction, window fitting, and the Decider adapter over a stand-in engine."""

import asyncio
import json

import pytest
from strands_decider.schema import ChoiceAnswer, NoulAnswer, SystemOneResponse, Usage

from completion_checker.decider import DeciderError, LocalDecider, fit_state, requirement_questions, response_questions, segment
from completion_checker.recorder import on_pre_tool
from completion_checker.redact import redact
from completion_checker.store import SessionStore, state_root


def test_failed_command_exit_code_and_output_recorded(project):
    project.prompt("x")
    project.run("uv run pytest -q", 2, "ERROR collecting test_calc.py")
    cmd = SessionStore(project.sid).events("command")[0]
    assert cmd["exit_code"] == 2 and cmd["status"] == "failed" and "ERROR collecting" in cmd["output_tail"]
    assert cmd["source"] == "claude_code_hook" and cmd["snapshot_before"] and cmd["snapshot_after"]


def test_background_command_is_not_a_pass(project):
    project.prompt("x")
    from completion_checker.recorder import on_post_tool

    on_pre_tool(project.base(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": "pytest", "run_in_background": True}, tool_use_id="t9"), project.config)
    on_post_tool(project.base(hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "pytest", "run_in_background": True}, tool_use_id="t9", tool_response={"stdout": "", "backgroundTaskId": "b1"}), project.config, failed=False)
    cmd = SessionStore(project.sid).events("command")[0]
    assert cmd["status"] == "backgrounded" and cmd["exit_code"] is None


def test_agent_cannot_edit_trusted_records(project):
    from conftest import REQS

    project.prompt("x")
    project.checklist(REQS)
    target = state_root() / "sessions" / project.sid / "ledger.jsonl"
    out = on_pre_tool(project.base(hook_event_name="PreToolUse", tool_name="Write", tool_input={"file_path": str(target)}), project.config)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    ok = on_pre_tool(project.base(hook_event_name="PreToolUse", tool_name="Write", tool_input={"file_path": str(project.root / "calc.py")}), project.config)
    assert ok is None


def test_secrets_are_redacted_in_records(project, monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "tsk_live_abcdefghijklmnop1234")
    project.prompt("x")
    project.run("env | grep KEY", 0, "SERVICE_API_KEY=tsk_live_abcdefghijklmnop1234\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz")
    raw = (SessionStore(project.sid).dir / "ledger.jsonl").read_text()
    assert "tsk_live_abcdefghijklmnop1234" not in raw and "abcdefghijklmnopqrstuvwxyz" not in raw


def test_redact_keeps_ordinary_text():
    assert redact("2 passed in 0.03s") == "2 passed in 0.03s"
    assert "hunter2secret" not in redact("password = hunter2secret")


def test_segment_splits_user_instructions():
    segs = segment("**1. Setup**\n* Keep add() backward compatible. Run the tests afterwards please.\nThanks!")
    assert segs == ["Keep add() backward compatible.", "Run the tests afterwards please."]


# --- the Decider adapter over a stand-in engine -------------------------------------------
#
# The real engine needs ~4 GB of weights; these tests swap in an object with the same
# surface (ask, tok, model.config.max_length, cfg.max_question_fraction) so every failure
# path of the adapter runs in milliseconds. tests/test_live.py runs the real model.


class _Tok:
    """Whitespace 'tokenizer' with the call signature the adapter uses."""

    def __call__(self, text, add_special_tokens=True):
        return {"input_ids": text.split()}


class _Engine:
    def __init__(self, answers=None, error=None, window=4096):
        self.tok = _Tok()
        self.model = type("M", (), {"config": type("C", (), {"max_length": window})()})()
        self.cfg = type("Cfg", (), {"max_question_fraction": 0.5})()
        self.answers, self.error, self.states = answers, error, []

    def ask(self, state, questions):
        self.states.append(state)
        if self.error:
            raise self.error
        return SystemOneResponse(model="stand-in", answers=self.answers(questions), usage=Usage(input_tokens=7))


def _good(questions):
    out = {}
    for name, q in questions.items():
        if q.type == "noul":
            out[name] = NoulAnswer(noul=0.8)
        else:
            opts = list(q.criteria)
            out[name] = ChoiceAnswer(choice=opts[0], confidence=0.9, probabilities={o: (0.91 if i == 0 else 0.09 / (len(opts) - 1)) for i, o in enumerate(opts)})
    return out


def _run(decider, state=None, questions=None):
    return asyncio.run(decider.ask(state or {"final_response": "done"}, questions or response_questions()))


def test_adapter_returns_validated_answers():
    res = _run(LocalDecider.from_engine(_Engine(_good), "mps"))
    assert res.answers["claims_checks"]["p"] == 0.8
    assert res.answers["response_kind"]["choice"] == "reports_complete" and res.answers["response_kind"]["confidence"] == 0.9
    assert res.usage["device"] == "mps" and res.trimmed == []


@pytest.mark.parametrize(
    "engine",
    [
        _Engine(error=RuntimeError("MPS backend out of memory")),
        _Engine(lambda q: {}),
        _Engine(lambda q: {"claims_checks": NoulAnswer(noul=1.7), "response_kind": NoulAnswer(noul=0.2)}),
        _Engine(lambda q: {"claims_checks": NoulAnswer(noul=0.5), "response_kind": ChoiceAnswer(choice="maybe", confidence=0.9, probabilities={"maybe": 1.0})}),
        _Engine(lambda q: {**_good(q), "claims_checks": NoulAnswer(noul=float("nan"))}),
    ],
    ids=["engine-error", "missing-answers", "wrong-types", "unknown-option", "nan"],
)
def test_adapter_failures_raise_decider_error(engine):
    with pytest.raises(DeciderError):
        _run(LocalDecider.from_engine(engine, "mps"))


def test_deadline_refuses_new_requests():
    engine = _Engine(_good)
    with pytest.raises(DeciderError, match="deadline"):
        _run(LocalDecider.from_engine(engine, "mps", deadline=0.0))
    assert engine.states == []  # the model was never asked


def test_long_state_is_trimmed_before_the_engine_sees_it():
    diff = " ".join(f"line{i}" for i in range(3000))
    output = " ".join(f"out{i}" for i in range(2000)) + " 2 passed"
    state = {
        "requirement": {"id": "R1", "text": "keep me"},
        "code_changes": [{"id": "F1", "path": "calc.py", "diff": diff}],
        "commands": [{"id": "C1", "command": "pytest", "output_tail": output}],
        "final_response": "Done; tests pass.",
    }
    engine = _Engine(_good, window=1200)
    res = _run(LocalDecider.from_engine(engine, "mps"), state, requirement_questions(1, [], False))
    sent = engine.states[0]
    assert sent["requirement"]["text"] == "keep me" and sent["final_response"] == "Done; tests pass."
    assert sent["commands"][0]["output_tail"].endswith("2 passed")  # the summary at the end survives
    assert sent["code_changes"][0]["diff"].startswith("line0")  # a diff keeps its start
    assert any(c.startswith("F1 diff") for c in res.trimmed) and any(c.startswith("C1 output_tail") for c in res.trimmed)
    assert state["code_changes"][0]["diff"] == diff  # the caller's packet is not mutated


def test_fit_state_leaves_small_states_alone():
    state = {"final_response": "short", "code_changes": [{"id": "F1", "diff": "x"}]}
    out, cuts = fit_state(state, lambda s: 10, budget=100)
    assert out == state and cuts == []


def test_fit_state_reports_when_it_cannot_fit():
    out, cuts = fit_state({"requirement": {"text": "x " * 500}}, lambda s: len(str(s)), budget=10)
    assert cuts and "engine cut the end" in cuts[0]


def test_questions_only_ask_what_applies():
    qs = requirement_questions(0, [], False)
    assert set(qs) == {"implementation", "missing_part", "overclaim", "needs_execution"}
    qs = requirement_questions(2, [(1, "C4")], True)
    assert {"verification", "failure_C4", "blocker"} <= set(qs)
    assert "`commands[1]` (id C4)" in qs["failure_C4"].instructions
