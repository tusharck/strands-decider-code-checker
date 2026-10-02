"""Run the labelled scenarios against Strands Decider 2B on this machine.

    uv run python eval/run_eval.py [--repeats 3] [--cold 3]

Writes eval/results/decider.json and prints a summary. Latency is measured here, on
this machine: per check (one Stop-hook evaluation, model already loaded), per model
request, model load, and a cold Stop hook (new process: Python start + load + check),
which is what a Claude Code session waits for.
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "eval"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")  # after the one-time download: no network

from scenarios import SCENARIOS, Sim, run_scenario  # noqa: E402

from completion_checker.config import Config  # noqa: E402
from completion_checker.decider import LocalDecider  # noqa: E402


class Timed:
    """Wraps the model to record every request's latency."""

    def __init__(self, inner):
        self.inner, self.requests = inner, []

    async def ask(self, state, questions):
        started = time.perf_counter()
        try:
            return await self.inner.ask(state, questions)
        finally:
            self.requests.append({"seconds": time.perf_counter() - started, "questions": len(questions)})

    async def aclose(self):
        pass


def cold_hook(workdir: Path) -> float:
    """One Stop hook in a fresh process, exactly as Claude Code launches it."""
    sim = Sim(workdir / "project", "cold")
    os.environ["DECIDER_CHECKER_STATE_DIR"] = str(workdir / "state")
    sc = SCENARIOS[0]
    sim.prompt(sc["prompt"])
    sim.checklist(sc["checklist"])
    for step in sc["steps"]:
        sim.edit(step[1], step[2]) if step[0] == "edit" else sim.run(step[1], step[2], step[3])
    payload = json.dumps(sim.base(hook_event_name="Stop", last_assistant_message=sc["final"]))
    started = time.perf_counter()
    subprocess.run([str(REPO / ".venv" / "bin" / "python"), str(REPO / "hook.py")], input=payload, text=True, capture_output=True, check=True, env=os.environ)
    seconds = time.perf_counter() - started
    lines = (workdir / "state" / "sessions" / "cold" / "decisions.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["outcome"], "cold hook did not record a decision"
    return seconds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--cold", type=int, default=3)
    parser.add_argument("--set", action="append", default=[], metavar="THRESHOLD=VALUE",
                        help="sensitivity run with a changed threshold, e.g. implementation_confidence=0.6")
    parser.add_argument("--out", default="decider.json")
    args = parser.parse_args()

    config = Config()
    overrides = {}
    for item in args.set:
        key, value = item.split("=")
        setattr(config.thresholds, key, float(value))
        overrides[key] = float(value)
    import scenarios

    scenarios.Sim.config_overrides = overrides
    started = time.perf_counter()
    model = LocalDecider(config.model, config.device)
    load_seconds = time.perf_counter() - started
    print(f"loaded {config.model} on {model.device} in {load_seconds:.1f}s (window {model.window} tokens)")
    timed = Timed(model)

    passes = []
    for rep in range(args.repeats):
        with tempfile.TemporaryDirectory() as tmp:
            results = [run_scenario(sc, timed, Path(tmp), i) for i, sc in enumerate(SCENARIOS)]
        passes.append(results)
        print(f"pass {rep + 1}: {sum(r['passed'] for r in results)}/{len(results)} scenarios right")

    first = passes[0]
    deterministic = all(
        [r["judgments"] for r in p] == [r["judgments"] for r in first] for p in passes[1:]
    )
    checks = [r["seconds"] for p in passes for r in p]
    cold = []
    for _ in range(args.cold):
        with tempfile.TemporaryDirectory() as tmp:
            cold.append(cold_hook(Path(tmp)))

    import torch

    report = {
        "model": config.model,
        "threshold_overrides": overrides,
        "device": model.device,
        "window_tokens": model.window,
        "machine": {"platform": platform.platform(), "processor": platform.processor(), "torch": torch.__version__},
        "scenarios_right": sum(r["passed"] for r in first),
        "scenarios_total": len(first),
        "deterministic_across_repeats": deterministic,
        "latency_s": {
            "model_load": round(load_seconds, 2),
            "check_median": round(statistics.median(checks), 3),
            "check_p90": round(sorted(checks)[int(0.9 * (len(checks) - 1))], 3),
            "request_median": round(statistics.median(r["seconds"] for r in timed.requests), 3),
            "cold_stop_hook_median": round(statistics.median(cold), 2) if cold else None,
            "checks_measured": len(checks),
            "requests_measured": len(timed.requests),
        },
        "cost_usd": 0.0,
        "results": first,
    }
    out = REPO / "eval" / "results" / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")

    print(f"\n{report['scenarios_right']}/{report['scenarios_total']} scenarios right on {model.device}; deterministic: {deterministic}")
    for r in first:
        mark = "ok  " if r["passed"] else "MISS"
        print(f"  {mark} {r['name']:<45} {r['seconds']:>6.2f}s  {r['statuses']}  {r['issues']}" + (f"  <- {'; '.join(r['misses'])}" if r["misses"] else ""))
    print("\nlatency:", json.dumps(report["latency_s"]))
    print(f"wrote {out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
