"""Load the agent's acceptance checklist and hold it to what the user actually said.

Integrity rules (all in code):
- Every requirement should quote the user's words (`source_quote`); quotes are matched
  against the recorded prompts. Untraced requirements are reported.
- Once a requirement has been seen, removing it, changing its text, downgrading it from
  required to optional, or downgrading its verification is only accepted with an
  amendment quoting a *later* user message. Otherwise the original stays in force.
"""

import json
import re
from dataclasses import dataclass, field

KINDS = ("required", "optional")
VERIFICATIONS = ("execution", "runtime", "inspection")
_STRENGTH = {"inspection": 0, "execution": 1, "runtime": 1}
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,31}$")


def normalize(text: str) -> str:
    table = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "`": "'", "*": " "})
    return re.sub(r"\s+", " ", (text or "").translate(table)).strip().lower()


@dataclass
class Requirement:
    id: str
    text: str
    source_quote: str
    kind: str
    verification: str
    traced: bool = False
    flags: list[str] = field(default_factory=list)


@dataclass
class Issue:
    code: str
    detail: str
    requirement: str | None = None
    actionable: bool = True


@dataclass
class Checklist:
    requirements: list[Requirement]
    validation: list[dict]
    blockers: dict[str, str]
    issues: list[Issue]


def _parse(raw: str) -> tuple[dict | None, list[Issue]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, [Issue("CHECKLIST_INVALID", f"checklist is not valid JSON: {e}")]
    if not isinstance(data, dict) or not isinstance(data.get("requirements"), list):
        return None, [Issue("CHECKLIST_INVALID", 'checklist needs a "requirements" list')]
    return data, []


def _req_fields(item: dict) -> dict:
    return {
        "id": str(item.get("id", "")).strip(),
        "text": str(item.get("text", "")).strip(),
        "source_quote": str(item.get("source_quote", "")).strip(),
        "kind": str(item.get("kind", "required")).strip(),
        "verification": str(item.get("verification", "execution")).strip(),
    }


def _weakened(original: dict, current: dict | None) -> str | None:
    if current is None:
        return "removed"
    if normalize(original["text"]) != normalize(current["text"]):
        return "text changed"
    if original["kind"] == "required" and current["kind"] != "required":
        return "downgraded to optional"
    if _STRENGTH.get(current["verification"], 0) < _STRENGTH.get(original["verification"], 0):
        return f"verification downgraded to {current['verification']}"
    return None


def load(current_raw: str | None, history: list[dict], prompts: list[dict]) -> Checklist | None:
    """Build the effective checklist.

    history: earlier `checklist_seen` ledger events (content + time), oldest first.
    prompts: recorded `prompt` events (text + time), oldest first.
    """
    if current_raw is None and not history:
        return None
    issues: list[Issue] = []
    data, parse_issues = _parse(current_raw) if current_raw is not None else ({"requirements": []}, [])
    issues += parse_issues
    if data is None:
        data = {"requirements": []}

    # First-seen version of every requirement id, from the recorded history.
    first_seen: dict[str, tuple[dict, float]] = {}
    for event in history:
        old, _ = _parse(event.get("content", ""))
        for item in (old or {}).get("requirements", []):
            if isinstance(item, dict):
                f = _req_fields(item)
                if f["id"] and f["id"] not in first_seen:
                    first_seen[f["id"]] = (f, event.get("at", 0.0))

    current: dict[str, dict] = {}
    for item in data.get("requirements", []):
        if not isinstance(item, dict):
            issues.append(Issue("CHECKLIST_INVALID", "each requirement must be an object"))
            continue
        f = _req_fields(item)
        problems = []
        if not _ID_RE.match(f["id"]):
            problems.append(f"bad id {f['id']!r}")
        if f["id"] in current:
            problems.append(f"duplicate id {f['id']}")
        if not f["text"]:
            problems.append("empty text")
        if f["kind"] not in KINDS:
            problems.append(f"kind must be one of {KINDS}")
        if f["verification"] not in VERIFICATIONS:
            problems.append(f"verification must be one of {VERIFICATIONS}")
        if problems:
            issues.append(Issue("CHECKLIST_INVALID", "; ".join(problems), f["id"] or None))
            continue
        current[f["id"]] = f

    prompt_texts = [(normalize(p.get("text", "")), p.get("at", 0.0)) for p in prompts]
    amendments: dict[str, list[str]] = {}
    for a in data.get("amendments", []) or []:
        if isinstance(a, dict):
            amendments.setdefault(str(a.get("requirement", "")), []).append(normalize(str(a.get("user_quote", ""))))

    def amended_after(req_id: str, since: float) -> bool:
        for quote in amendments.get(req_id, []):
            if len(quote) >= 8 and any(quote in text and at >= since for text, at in prompt_texts):
                return True
        return False

    effective: dict[str, dict] = {}
    for req_id, (original, seen_at) in first_seen.items():
        change = _weakened(original, current.get(req_id))
        if change and not amended_after(req_id, seen_at):
            effective[req_id] = dict(original, _flag=f"{change} without a user amendment; original kept")
            issues.append(
                Issue(
                    "CHECKLIST_WEAKENED",
                    f"{req_id} was {change} without a recorded user amendment; the original requirement stays in force",
                    req_id,
                )
            )
        elif req_id in current:
            effective[req_id] = current[req_id]
        # removed *with* a valid amendment: dropped
    for req_id, f in current.items():
        effective.setdefault(req_id, f)

    requirements = []
    for f in effective.values():
        quote = normalize(f["source_quote"])
        traced = len(quote) >= 4 and any(quote in text for text, _ in prompt_texts)
        req = Requirement(f["id"], f["text"], f["source_quote"], f["kind"], f["verification"], traced)
        if "_flag" in f:
            req.flags.append(f["_flag"])
        if not traced:
            req.flags.append("source_quote not found in the user's messages")
            issues.append(
                Issue("UNTRACED", f"{req.id}: source_quote does not match the user's words", req.id, actionable=False)
            )
        requirements.append(req)
    if not requirements:
        issues.append(Issue("CHECKLIST_EMPTY", "checklist has no requirements"))

    ids = {r.id for r in requirements}
    validation = []
    for v in data.get("validation", []) or []:
        if isinstance(v, dict) and str(v.get("command", "")).strip():
            validation.append({"command": str(v["command"]).strip(), "requirements": [r for r in v.get("requirements", []) if r in ids]})
    blockers = {}
    for b in data.get("blockers", []) or []:
        if isinstance(b, dict) and b.get("requirement") in ids and str(b.get("description", "")).strip():
            blockers[b["requirement"]] = str(b["description"]).strip()[:1000]
    return Checklist(requirements, validation, blockers, issues)
