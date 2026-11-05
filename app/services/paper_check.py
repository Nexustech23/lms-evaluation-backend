# ============================================================
# A second opinion on a generated paper: a separate AI call solves every question without seeing the answer key,
# and any disagreement or "no option matches" becomes a flag for the faculty. Advisory only; it never changes the paper.
# ============================================================

import json
import re
from typing import Any, Dict, List, Optional

from app.services.claude import generate_text
from app.services.paper_blueprint import parse_paper_json

CHECK_SYSTEM = """You check examination questions for a faculty member. Solve every question below yourself,
carefully and independently. No answer key is given.

For each question report:
- "number": the question number.
- "my_answer": for a multiple-choice question the correct option letter(s), for example "b" or "a, c"; for a
  numerical question your result with its unit; otherwise a short key point.
- "problem": empty when the question is sound. Otherwise one short sentence naming the defect, for example
  "No option matches 3.16 m/s", "Two options are correct but one is stated", "The wording does not say which way
  the force acts", "The data given are inconsistent". Report a problem only when you are confident.

Return only a JSON object: {"checks":[{"number":1,"my_answer":"b","problem":""}]}
Include every question once, in order. No markdown."""


def check_paper(questions: List[Dict[str, Any]], model: str, max_tokens: int, effort: Optional[str]):
    """Blocking (run through asyncio.to_thread by tracked_faculty_call). Returns (reply text, usage)."""
    prompt = "QUESTIONS (JSON):\n" + json.dumps(questions, ensure_ascii=False)
    return generate_text(prompt, model, max_tokens, system=CHECK_SYSTEM, effort=effort)


def parse_checks(reply: str) -> List[Dict[str, Any]]:
    data = parse_paper_json(reply)
    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, list):
        raise ValueError("The answer check did not return a list of checks")
    return [c for c in checks if isinstance(c, dict) and isinstance(c.get("number"), int)]


_LETTERS = re.compile(r"\b([a-e])\b", re.IGNORECASE)
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _letters(answer: Optional[str]) -> Optional[frozenset]:
    """Option letters in an answer such as "b" or "a, c"; None when the answer is not option letters."""
    if not answer:
        return None
    text = answer.strip().strip(".()")
    if re.fullmatch(r"(?:[a-eA-E](?:\s*(?:,|and|&|/)?\s*)?)+", text):
        return frozenset(m.group(1).lower() for m in _LETTERS.finditer(text))
    return None


def _first_number(answer: Optional[str]) -> Optional[float]:
    match = _NUMBER.search(answer or "")
    return float(match.group()) if match else None


def _same_number(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.01, 0.01 * max(abs(a), abs(b)))


def compare_answers(answer_key: List[Dict[str, Any]], checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flags for the faculty: the checker's own objections plus any difference from the written answer."""
    key = {k["number"]: k.get("answer") for k in answer_key if isinstance(k.get("number"), int)}
    flags: Dict[int, List[str]] = {}
    for check in checks:
        number = check["number"]
        problem = str(check.get("problem") or "").strip()
        if problem:
            flags.setdefault(number, []).append(problem[:300])
        written, solved = key.get(number), str(check.get("my_answer") or "")
        written_letters, solved_letters = _letters(written), _letters(solved)
        if written_letters and solved_letters:
            different = written_letters != solved_letters
        else:
            a, b = _first_number(written), _first_number(solved)
            different = a is not None and b is not None and not _same_number(a, b)
        if written and solved and different:
            flags.setdefault(number, []).append(f"The written answer is {written} but the check found {solved}")
    return [{"number": n, "issue": "; ".join(issues)} for n, issues in sorted(flags.items())]
