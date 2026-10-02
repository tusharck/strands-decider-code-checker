"""The final decision, in code. Decider 2B supplies judgments; these rules decide.

Hard rules:
- A relevant failed check makes a requirement incomplete; no positive judgment overrides it.
- Missing or stale evidence cannot establish a pass.
- Optional requirements never block completion.
- Low-confidence or conflicting judgments leave a requirement unverified.
- A model error leaves the affected requirement unverified. It is never a pass.
"""

from dataclasses import asdict, dataclass, field

from .checklist import Checklist, Issue
from .config import Thresholds
from .evidence import Packet
from .decider import DeciderResult

RANK = {"satisfied": 0, "blocked": 1, "unverified": 2, "incomplete": 3}


@dataclass
class Reason:
    code: str
    detail: str
    evidence: list[str] = field(default_factory=list)
    actionable: bool = True


@dataclass
class RequirementResult:
    id: str
    text: str
    kind: str
    verification: str
    status: str = "satisfied"
    reasons: list[Reason] = field(default_factory=list)
    judgments: dict = field(default_factory=dict)  # raw model answers, probabilities preserved

    def add(self, status: str, code: str, detail: str, evidence=None, actionable: bool = True):
        self.reasons.append(Reason(code, detail, list(evidence or []), actionable))
        if self.status == "blocked":
            return  # a supported external blocker explains the rest; it is still not a pass
        if status == "blocked" or RANK[status] > RANK[self.status]:
            self.status = status


@dataclass
class Evaluation:
    requirements: list[RequirementResult]
    issues: list[Reason]
    checker_errors: list[str]
    asks_user: bool
    model: str | None = None

    @property
    def unresolved(self) -> list[RequirementResult]:
        return [r for r in self.requirements if r.kind == "required" and r.status != "satisfied"]

    @property
    def actionable(self) -> bool:
        """Is there work the agent itself can still do?"""
        for r in self.unresolved:
            if r.status in ("incomplete", "unverified") and any(x.actionable for x in r.reasons):
                return True
        return any(i.actionable for i in self.issues)

    @property
    def completed(self) -> bool:
        return not self.unresolved and not self.checker_errors and not any(i.actionable for i in self.issues)

    def to_dict(self) -> dict:
        return {
            "requirements": [asdict(r) for r in self.requirements],
            "issues": [asdict(i) for i in self.issues],
            "checker_errors": self.checker_errors,
            "asks_user": self.asks_user,
            "model": self.model,
            "completed": self.completed,
            "actionable": self.actionable,
        }


def evaluate_requirement(req, packet: Packet, checklist: Checklist, result: DeciderResult | None, failing_ids: list[str], t: Thresholds, raise_verification: bool = True) -> RequirementResult:
    r = RequirementResult(req.id, req.text, req.kind, req.verification)
    for flag in req.flags:
        r.judgments.setdefault("flags", []).append(flag)

    # 1. Code-only: required checks linked to this requirement.
    for v in packet.validation:
        if req.id not in v.get("requirements", []):
            continue
        ev = [v["evidence"]] if v.get("evidence") else []
        if v["status"] == "failed":
            r.add("incomplete", "CHECK_FAILED", f"required check `{v['command']}` failed", ev)
        elif v["status"] in ("missing", "stale", "not_run"):
            r.add("unverified", "CHECK_" + v["status"].upper(), f"required check `{v['command']}` has no current run ({v['status']})", ev)

    # 2. Model judgments; any error means nothing here can count as a pass.
    if result is None or result.error:
        r.add("unverified", "CHECKER_ERROR", f"Decider judgment unavailable: {result.error if result else 'not run'}", actionable=False)
        return r
    a = result.answers
    r.judgments.update(a)

    if req.id in checklist.blockers:
        p = a.get("blocker", {}).get("p", 0.0)
        if p >= t.blocker_support:
            r.add("blocked", "BLOCKED", f"declared blocker supported by evidence (p={p:.2f}): {checklist.blockers[req.id]}")
        else:
            r.add("unverified", "BLOCKER_UNSUPPORTED", f"declared blocker not shown by the evidence (p={p:.2f}); show the failing command or error")

    for cid in failing_ids:
        p = a.get(f"failure_{cid}", {}).get("p")
        if p is not None and p >= t.failure_relevance:
            r.add("incomplete", "RELEVANT_FAILURE", f"{cid} failed and concerns this requirement (p={p:.2f})", [cid])

    impl = a["implementation"]
    missing = a["missing_part"]["p"]
    if impl["choice"] in ("partial", "not_implemented") and impl["confidence"] >= t.incomplete_confidence:
        r.add("incomplete", "NOT_IMPLEMENTED" if impl["choice"] == "not_implemented" else "PARTIAL", f"implementation judged {impl['choice']} (confidence {impl['confidence']:.2f}; missing-part p={missing:.2f})", [f["id"] for f in packet.files])
    elif impl["choice"] == "implemented" and impl["confidence"] >= t.implementation_confidence:
        if missing >= t.missing_part:
            r.add("unverified", "CONFLICT", f"judged implemented but a part may be missing (p={missing:.2f})")
    else:
        r.add("unverified", "IMPLEMENTATION_UNCLEAR", f"implementation judgment {impl['choice']} at confidence {impl['confidence']:.2f}")

    # The agent picks the verification level; it cannot pick "just read it" for behavior
    # that running something could check.
    needs = a.get("needs_execution")
    if raise_verification and r.verification == "inspection" and needs and needs["p"] >= t.needs_execution:
        r.verification = "execution"
        r.reasons.append(Reason("VERIFICATION_RAISED", f"marked inspection, but running code can check this (p={needs['p']:.2f}); held to execution", [], False))

    if r.verification in ("execution", "runtime"):
        ver = a.get("verification")
        if ver is None:
            r.add("unverified", "NO_CURRENT_RUN", "no current command or check exercised this requirement")
        elif ver["choice"] == "failing" and ver["confidence"] >= t.incomplete_confidence:
            r.add("incomplete", "CHECK_SHOWS_FAILURE", f"current results show this behavior failing (confidence {ver['confidence']:.2f})")
        elif ver["choice"] != "passing" or ver["confidence"] < t.verification_confidence:
            r.add("unverified", "NOT_VERIFIED", f"verification judged {ver['choice']} at confidence {ver['confidence']:.2f}")

    if packet.injection_hits and r.status == "satisfied":
        ids = sorted({h["evidence"] for h in packet.injection_hits})
        r.add("unverified", "POSSIBLE_INJECTION", "evidence contains text addressed to the checker; a person should confirm this pass", ids)

    return r


def evaluate(
    checklist: Checklist,
    packet: Packet,
    results: dict[str, DeciderResult],
    response: DeciderResult | None,
    coverage: list[tuple[str, list[str], DeciderResult]],
    failing_ids: list[str],
    t: Thresholds,
    raise_verification: bool = True,
) -> Evaluation:
    reqs = [evaluate_requirement(req, packet, checklist, results.get(req.id), failing_ids, t, raise_verification) for req in checklist.requirements]
    issues: list[Reason] = []
    errors: list[str] = []

    for issue in checklist.issues:
        issues.append(Reason(issue.code, issue.detail, [issue.requirement] if issue.requirement else [], issue.actionable))

    for v in packet.validation:
        if v["source"] == "config" and v["status"] != "passed":
            issues.append(Reason("REQUIRED_CHECK_" + v["status"].upper(), f"`{v['command']}` is required for every task and is {v['status']}", [v["evidence"]] if v["evidence"] else []))

    for r in reqs:
        o = r.judgments.get("overclaim")
        if o and o["p"] >= t.overclaim:
            issues.append(Reason("OVERCLAIM", f"final response claims more about {r.id} than the evidence shows (p={o['p']:.2f})", [r.id, "RESPONSE"]))

    asks_user = False
    if response is None or response.error:
        errors.append(f"final-response check unavailable: {response.error if response else 'not run'}")
    else:
        c = response.answers["claims_checks"]["p"]
        if c >= t.claims_checks:
            current_tests = [x for x in packet.commands if x["is_test"] and x["current"] and x["status"] == "passed" and not x.get("superseded_by")]
            failing_tests = [x for x in packet.commands if x["is_test"] and x["current"] and x["status"] != "passed" and not x.get("superseded_by")]
            stale_tests = [x for x in packet.commands if x["is_test"] and not x["current"]]
            if not current_tests:
                detail = "final response says tests/checks passed, but there is no current passing test run"
                if stale_tests:
                    detail += f" (only stale runs: {', '.join(x['id'] for x in stale_tests[-3:])} — {stale_tests[-1]['stale_reason']})"
                issues.append(Reason("UNSUPPORTED_TEST_CLAIM", detail, ["RESPONSE"] + [x["id"] for x in stale_tests[-3:]]))
            elif failing_tests:
                issues.append(Reason("UNSUPPORTED_TEST_CLAIM", "final response says tests passed, but a current test run failed", ["RESPONSE"] + [x["id"] for x in failing_tests]))
        kind = response.answers["response_kind"]
        asks_user = kind["choice"] == "asks_user" and kind["confidence"] >= t.asks_user

    for prompt_id, segments, res in coverage:
        if res.error:
            errors.append(f"coverage check unavailable for {prompt_id}: {res.error}")
            continue
        for i, seg in enumerate(segments):
            is_req = res.answers[f"request_{i}"]["p"]
            covered = res.answers[f"covered_{i}"]["p"]
            if is_req >= t.segment_is_request and covered < t.segment_covered:
                issues.append(Reason("COVERAGE_GAP", f'user asked "{seg[:160]}" but no checklist item covers it (request p={is_req:.2f}, covered p={covered:.2f})', [prompt_id]))

    for r in reqs:
        for reason in r.reasons:
            if reason.code == "CHECKER_ERROR":
                errors.append(f"{r.id}: {reason.detail}")
    model = next((x.model for x in list(results.values()) + [response] if x and x.model), None)
    return Evaluation(reqs, issues, errors, asks_user, model)
