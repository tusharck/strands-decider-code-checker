# How a decision is made

The Jev completion checker for Claude Code, moved to
[Strands Decider 2B](https://huggingface.co/StrandsAgents/strands-decider-2B-hobson-v19),
a local decision model. Before the agent hands back its final answer, the checker compares
the task's acceptance checklist with **trusted records** of what actually happened: file
changes, command exit codes and output, and which code state each run tested. It asks the
model narrow questions about each requirement, then **decides in code**. Work that isn't
finished or isn't shown goes back to the agent, for a bounded number of attempts.

## How a decision is made

For each requirement, Decider 2B gets one request whose state holds the requirement, the diffs
(with context), the current check runs, and the final response. The questions are:

- Choice `implementation`: implemented / partial / not_implemented / cannot_tell.
- Noul `missing_part`: is part of the requirement missing?
- Choice `verification`: did a current run exercise this behavior, and did it pass or fail?
- Noul `overclaim`: does the final response claim more than the evidence shows?
- Noul `failure_<Cn>`: does failed run Cn concern this requirement? (asked once per failure)
- Noul `blocker`: does the evidence show the declared external blocker?
- Noul `needs_execution`: can this be checked by running something, or is reading enough?

One more request reads the final response (`claims_checks`, `response_kind`). Probabilities
and confidence are kept in the decision log. Code then applies these rules
(`completion_checker/decide.py`):

1. **A relevant failure or a failed required check makes the requirement `incomplete`.** A positive judgment cannot override it.
2. **Stale, missing, or never-run evidence cannot establish a pass.** A run is current only if nothing relevant changed during it or after it. Stale runs are not even shown to the model.
3. **Low confidence or conflicting answers leave the requirement `unverified`.**
4. **A model error, a missed deadline, or a malformed answer leaves the requirement `unverified`.** Every answer is validated against its question first. A model failure is reported to you, not sent back to the agent, which cannot fix it.
5. **Optional requirements never block completion.**
6. **Evidence containing text addressed to the checker** caps any pass at `unverified` and names the evidence ID.
7. **A declared blocker becomes `blocked` only when the evidence supports it.** Blockers never cause a retry, so the agent is never stopped from explaining one.

Requirement statuses are `satisfied`, `incomplete`, `unverified`, and `blocked`. Task
outcomes are `completed`, `needs_work` (sent back to the agent), `stopped_with_unresolved`,
`awaiting_user` (the agent asked you a question), and `checker_unavailable`.

[Back to the README](../README.md)
