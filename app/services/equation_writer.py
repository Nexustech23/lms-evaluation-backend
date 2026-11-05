# ============================================================
# Words -> LaTeX for the paper editor's equation box. A small model writes the LaTeX; the browser draws it
# and refuses it if it does not draw, so the faculty always sees the equation before anything is inserted.
# ============================================================

import json
import re
from typing import Any, Dict, Optional

from app.services.claude import generate_text
from app.services.paper_blueprint import parse_paper_json

EQUATION_MODEL = "claude-haiku-4-5-20251001"
EQUATION_MAX_TOKENS = 400
MAX_LATEX = 2000

SYSTEM = r"""You write LaTeX for an equation editor (MathJax) used by teachers to write exam papers.
The user describes an equation or formula in words. Write it as LaTeX.

Rules:
- Return only a JSON object: {"latex": "...", "note": ""}. No markdown.
- "latex" is the equation itself: no $ signs, no \[ \] or \( \), no \begin{equation}, no explanation.
- You may use standard TeX maths, amsmath, \vec, \hat, \dot, the physics package (\dv, \pdv, \qty, \abs) and
  \ce{...} for chemical formulas and reactions (for example \ce{2H2 + O2 -> 2H2O}).
- When the description names a well-known law or formula, write it in its standard textbook form.
- Write exactly what was asked. Do not solve it, simplify it or add anything.
- If the description is not an equation or formula, return {"latex": "", "note": "a short reason"}.
- If a previous attempt is given with the error it produced, return a corrected version that fixes that error."""


def write_equation(description: str, previous_latex: Optional[str], previous_error: Optional[str],
                   model: str, max_tokens: int):
    """Blocking (run through asyncio.to_thread by tracked_faculty_call). Returns (reply text, usage)."""
    request: Dict[str, Any] = {"description": description}
    if previous_latex:
        request["previous_latex"] = previous_latex
        request["previous_error"] = previous_error or "It did not draw"
    return generate_text(json.dumps(request, ensure_ascii=False), model, max_tokens, system=SYSTEM)


_WRAPPERS = (
    (re.compile(r"^\s*\$\$(.*?)\$\$\s*$", re.DOTALL), 1),
    (re.compile(r"^\s*\$(.*?)\$\s*$", re.DOTALL), 1),
    (re.compile(r"^\s*\\\[(.*?)\\\]\s*$", re.DOTALL), 1),
    (re.compile(r"^\s*\\\((.*?)\\\)\s*$", re.DOTALL), 1),
    (re.compile(r"^\s*\\begin\{(?:equation|align|displaymath)\*?\}(.*?)\\end\{(?:equation|align|displaymath)\*?\}\s*$", re.DOTALL), 1),
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_latex(value: Any) -> str:
    """The equation without delimiters or stray characters; "" when it cannot be used."""
    if not isinstance(value, str):
        return ""
    text = _CONTROL.sub("", value).strip()
    for pattern, group in _WRAPPERS:
        match = pattern.match(text)
        if match:
            text = match.group(group).strip()
    if not text or len(text) > MAX_LATEX:
        return ""
    return text


def parse_equation_reply(reply: str) -> Dict[str, str]:
    """{"latex", "note"} from the model's reply; raises ValueError when there is no usable reply."""
    data = parse_paper_json(reply)
    if not isinstance(data, dict):
        raise ValueError("The equation writer did not return an object")
    note = data.get("note")
    return {"latex": clean_latex(data.get("latex")), "note": note.strip()[:200] if isinstance(note, str) else ""}
