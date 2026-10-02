"""Checker settings: defaults, overridden by `.completion/config.json`, then by env vars.

Thresholds are cut-offs on Decider 2B's probabilities and confidence. They decide how much
certainty the checker wants before it acts; they are not guarantees of correctness. They
were set for Jev and kept unchanged for this port: measure on your own traffic before
trusting them with Decider.
"""

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

MODES = ("observe", "enforce")


@dataclass
class Thresholds:
    # Choice confidence needed before an "implemented" / "passing_check" answer counts.
    implementation_confidence: float = 0.7
    verification_confidence: float = 0.7
    # Choice confidence needed before "partial" / "not_implemented" marks work incomplete.
    incomplete_confidence: float = 0.6
    # Noul: a required part is missing. Above this, "implemented" is not trusted.
    missing_part: float = 0.5
    # Noul: a failed command concerns this requirement. Kept low on purpose, so a
    # doubtful relevance call still counts the failure against the requirement.
    failure_relevance: float = 0.3
    # Noul: the final response claims more than the evidence shows.
    overclaim: float = 0.5
    # Noul: the evidence supports the agent's declared external blocker.
    blocker_support: float = 0.6
    # Noul: the final response says tests/checks ran and passed.
    claims_checks: float = 0.5
    # Coverage of user instructions by the checklist.
    segment_is_request: float = 0.5
    segment_covered: float = 0.5
    # Choice confidence that the final response is a question to the user.
    asks_user: float = 0.8
    # Noul: the requirement can be checked by running something. At or above this, an
    # item the agent marked "inspection" is held to "execution" instead.
    needs_execution: float = 0.7


@dataclass
class Config:
    mode: str = "observe"
    max_attempts: int = 2
    model: str = "StrandsAgents/strands-decider-2B-hobson-v19"  # Hugging Face repo, cached locally
    device: str = "auto"  # auto | cuda | mps | cpu
    overall_deadline_s: float = 60.0  # must stay below the Stop hook timeout
    # Ask the agent for a checklist when files changed and none exists.
    require_checklist_when_files_change: bool = True
    # Enforce mode: refuse Edit/Write to project files until the checklist exists.
    checklist_first: bool = True
    # Raise "inspection" items to "execution" when the model judges they can be run.
    raise_verification: bool = True
    coverage_check: bool = True
    # Commands whose runs must pass for the whole task, e.g. ["uv run pytest"].
    required_validation: list[str] = field(default_factory=list)
    # Regexes marking a command as a check (test, lint, build, run).
    check_command_patterns: list[str] = field(
        default_factory=lambda: [
            r"\b(pytest|unittest|tox|nox)\b",
            r"\b(npm|pnpm|yarn|bun)\s+(run\s+)?(test|lint|build|typecheck|check)\b",
            r"\b(vitest|jest|mocha|playwright|cypress)\b",
            r"\b(tsc|eslint|ruff|mypy|pyright|flake8|black\s+--check)\b",
            r"\b(go\s+(test|build|vet)|cargo\s+(test|build|check|clippy))\b",
            r"\b(make|gradle|mvn)\b",
            r"\bcurl\b",
            r"\b(python3?|node|uv\s+run)\b",
        ]
    )
    # Regexes for commands that count as *tests* for "tests passed" claims.
    test_command_patterns: list[str] = field(
        default_factory=lambda: [
            r"\b(pytest|unittest|tox|nox)\b",
            r"\b(npm|pnpm|yarn|bun)\s+(run\s+)?test\b",
            r"\b(vitest|jest|mocha|playwright\s+test|cypress\s+run)\b",
            r"\b(go\s+test|cargo\s+test)\b",
            r"\bmake\s+(test|check)\b",
        ]
    )
    # Non-Bash tools whose results count as runtime/browser evidence.
    runtime_tool_patterns: list[str] = field(default_factory=lambda: [r"^mcp__.*(chrome|playwright|browser).*"])
    # Files whose change does not make earlier runs stale (docs, the checklist itself).
    staleness_ignore: list[str] = field(default_factory=lambda: ["*.md", "docs/*", ".completion/*"])
    max_diff_chars: int = 40_000
    max_output_tail_chars: int = 1_500
    max_commands_in_state: int = 12
    thresholds: Thresholds = field(default_factory=Thresholds)

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(project_dir: Path) -> Config:
    config = Config()
    path = project_dir / ".completion" / "config.json"
    if path.exists():
        data = json.loads(path.read_text())
        thresholds = data.pop("thresholds", {}) or {}
        known = {f.name for f in fields(Config)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown config keys in {path}: {sorted(unknown)}")
        for key, value in data.items():
            setattr(config, key, value)
        known_t = {f.name for f in fields(Thresholds)}
        unknown_t = set(thresholds) - known_t
        if unknown_t:
            raise ValueError(f"Unknown threshold keys in {path}: {sorted(unknown_t)}")
        for key, value in thresholds.items():
            setattr(config.thresholds, key, float(value))

    if os.environ.get("DECIDER_CHECKER_MODE"):
        config.mode = os.environ["DECIDER_CHECKER_MODE"].strip()
    if os.environ.get("DECIDER_CHECKER_MAX_ATTEMPTS"):
        config.max_attempts = int(os.environ["DECIDER_CHECKER_MAX_ATTEMPTS"])
    if config.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {config.mode!r}")
    for f in fields(Thresholds):
        value = getattr(config.thresholds, f.name)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"threshold {f.name} must be within [0, 1], got {value}")
    return config
