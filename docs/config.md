# Configuration

## Configure (`.completion/config.json`)

| Key | Default | Meaning |
|---|---|---|
| `mode` | `observe` | `observe` records decisions and never changes the agent's behavior. `enforce` returns gaps to the agent |
| `max_attempts` | `2` | Correction rounds per user message before the agent may stop with unresolved items |
| `required_validation` | `[]` | Commands that must have a current passing run for every task, e.g. `["uv run pytest"]` |
| `model` | `StrandsAgents/strands-decider-2B-hobson-v19` | Hugging Face repo, cached locally |
| `device` | `auto` | `auto` picks cuda, then mps, then cpu. If MPS fails to load, it falls back to CPU |
| `overall_deadline_s` | `60` | Requests not started by then come back as errors (never as passes). Model load counts against it |
| `checklist_first` | `true` | Enforce mode: Edit/Write to project files is refused until the checklist exists |
| `raise_verification` | `true` | Holds an `inspection` item to `execution` when the model judges it can be checked by running code |
| `thresholds.*` | see `config.py` | **Unchanged from the Jev version.** See the eval before you trust them |

`DECIDER_CHECKER_MODE` and `DECIDER_CHECKER_MAX_ATTEMPTS` override the file.

[Back to the README](../README.md)
