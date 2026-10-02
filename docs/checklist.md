# The checklist

## The checklist (written by the agent, held to your words)

```json
{"requirements": [{"id": "R1", "text": "...", "source_quote": "<your exact words>",
                   "kind": "required|optional", "verification": "execution|runtime|inspection"}],
 "validation": [{"command": "uv run pytest", "requirements": ["R1"]}],
 "blockers":   [{"requirement": "R2", "description": "..."}],
 "amendments": [{"requirement": "R1", "user_quote": "<your later words>"}]}
```

- Each `source_quote` must appear in a recorded user message. If it doesn't, the
  requirement is reported as `UNTRACED`.
- A requirement cannot be silently removed, reworded, made optional, or given weaker
  verification. The checker keeps the first version it saw unless an amendment quotes a
  *later* user message.
- The checklist must exist before the agent changes project files, so it reflects the
  request rather than the finished result. If files change anyway (for example through
  Bash), the agent is asked for the checklist once. That request does not use up a
  correction attempt.
- The agent picks each item's verification level, but a Decider Noul (`needs_execution`)
  checks that choice. Behavior that running code can check is held to `execution`, even
  when the agent marked it `inspection`.
- A Decider coverage check flags sentences in your messages that ask for something no
  checklist item covers (`COVERAGE_GAP`).

[Back to the README](../README.md)
