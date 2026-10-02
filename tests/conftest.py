"""Test harness: a throwaway git project driven through the real hook handlers.

Tool events are simulated (same payload shapes Claude Code sends); the model is a fake that
returns validated answers, so every decision is deterministic.
"""

import json
import subprocess

import pytest

from completion_checker.config import Config
from completion_checker.decider import DeciderError, DeciderResult, validate
from completion_checker.recorder import checklist_path, on_post_tool, on_pre_tool, on_user_prompt
from completion_checker.stop import on_stop
from completion_checker.store import SessionStore

GOOD = {
    "implementation": ("implemented", 0.95),
    "missing_part": 0.05,
    "overclaim": 0.05,
    "verification": ("passing", 0.92),
    "blocker": 0.1,
    "needs_execution": 0.2,
    "claims_checks": 0.9,
    "response_kind": ("reports_complete", 0.9),
}


def _answer(question, value):
    if hasattr(question, "criteria") and question.type == "choice":
        choice, conf = value
        options = list(question.criteria)
        rest = (1 - conf) / (len(options) - 1)
        return {"type": "choice", "choice": choice, "confidence": conf, "probabilities": {o: (conf if o == choice else rest) for o in options}}
    return {"type": "noul", "noul": value}


class FakeDecider:
    """Answers like Decider would. `per_req` overrides answers per requirement id."""

    def __init__(self, per_req=None, response=None, coverage=None, error=None, raw=None):
        self.per_req = per_req or {}
        self.response = response or {}
        self.coverage = coverage or {}
        self.error = error
        self.raw = raw  # raw (unvalidated) answers, to test malformed responses
        self.calls = []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise DeciderError(self.error)
        if "requirement" in state:
            over = self.per_req.get(state["requirement"]["id"], {})
        elif "segments" in state:
            over = self.coverage
        else:
            over = self.response
        raw = {}
        for name, q in questions.items():
            if name in over:
                value = over[name]
            elif name in GOOD:
                value = GOOD[name]
            elif name.startswith("failure_"):
                value = 0.05
            elif name.startswith("request_"):
                value = 0.1
            elif name.startswith("covered_"):
                value = 0.9
            else:
                raise AssertionError(f"unexpected question {name}")
            raw[name] = _answer(q, value)
        if self.raw is not None:
            raw = self.raw
        return DeciderResult(validate(questions, raw), "decider-fake", {"input_tokens": 0})

    async def aclose(self):
        pass


class Project:
    def __init__(self, root, session_id="s1"):
        self.root = root
        self.sid = session_id
        self.config = Config()
        self.n = 0
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (root / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=root, check=True)

    def base(self, **kw):
        return {"session_id": self.sid, "cwd": str(self.root), **kw}

    def prompt(self, text):
        return on_user_prompt(self.base(hook_event_name="UserPromptSubmit", prompt=text), self.config)

    def edit(self, rel, content):
        (self.root / rel).write_text(content)
        on_post_tool(self.base(hook_event_name="PostToolUse", tool_name="Write", tool_input={"file_path": str(self.root / rel), "content": content}, tool_response={}), self.config, failed=False)

    def run(self, command, exit_code=0, output="", mutate=None):
        self.n += 1
        tid = f"toolu_{self.n}"
        on_pre_tool(self.base(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id=tid), self.config)
        if mutate:
            mutate()
        if exit_code == 0:
            on_post_tool(self.base(hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id=tid, tool_response={"stdout": output, "stderr": "", "interrupted": False, "isImage": False}), self.config, failed=False)
        else:
            on_post_tool(self.base(hook_event_name="PostToolUseFailure", tool_name="Bash", tool_input={"command": command}, tool_use_id=tid, error=f"Exit code {exit_code}\n{output}", is_interrupt=False), self.config, failed=True)

    def checklist(self, requirements, **extra):
        path = checklist_path(self.root, self.sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"requirements": requirements, **extra}, indent=2)
        path.write_text(content)
        on_post_tool(self.base(hook_event_name="PostToolUse", tool_name="Write", tool_input={"file_path": str(path), "content": content}, tool_response={}), self.config, failed=False)

    def stop(self, response, decider):
        return on_stop(self.base(hook_event_name="Stop", stop_hook_active=False, last_assistant_message=response), self.config, decider=decider)

    def last_decision(self):
        lines = (SessionStore(self.sid).dir / "decisions.jsonl").read_text().splitlines()
        return json.loads(lines[-1])

    def status(self):
        return {r["id"]: r["status"] for r in self.last_decision()["evaluation"]["requirements"]}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("DECIDER_CHECKER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    p = Project(root)
    p.config.mode = "enforce"
    return p


PROMPT = "Add a subtract(a, b) function to calc.py. Keep add() backward compatible. Run the tests. Maybe also add a docstring."
REQS = [
    {"id": "R1", "text": "calc.py has subtract(a, b) returning a - b", "source_quote": "Add a subtract(a, b) function to calc.py", "kind": "required", "verification": "execution"},
    {"id": "R2", "text": "add() keeps its signature and behavior", "source_quote": "Keep add() backward compatible", "kind": "required", "verification": "execution"},
    {"id": "R3", "text": "subtract has a docstring", "source_quote": "Maybe also add a docstring", "kind": "optional", "verification": "inspection"},
]
NEW_CALC = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n"
