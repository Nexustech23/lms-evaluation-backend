# ============================================================
# Detects which questions in an already-extracted question-paper text are
# multiple-choice (have lettered options), so the MCQ pipeline
# (app/services/mcq_grading.py) knows which questions to resolve an answer
# key for and grade deterministically — with no faculty step required.
#
# Pure text parsing, no AI/network calls — fully unit-testable.
# Deliberately mirrors the block-splitting regexes in
# app/services/docx_from_text.py (_QUESTION_RE / _SUBPART_RE) so detection
# stays consistent with how the app already understands question-paper
# structure, rather than inventing a second, divergent parser.
# ============================================================

import re
from typing import Dict, List

_QUESTION_START_RE = re.compile(r"^Q(\d+)[.\)]\s*(.*)")
_OPTION_LINE_RE = re.compile(r"^\s*([a-dA-D])[.\)]\s+(.+)")

# A question counts as multiple-choice only with >=2 lettered option lines
# directly under it — a single "a)" is far more likely a sub-part label
# (e.g. "(a) Explain X. (b) Explain Y.") than an MCQ option.
_MIN_OPTIONS_FOR_MCQ = 2


def detect_mcq_questions(paper_text: str) -> Dict[int, List[str]]:
    """Returns {question_no: [option_text, ...]} for every question in
    paper_text that has at least _MIN_OPTIONS_FOR_MCQ lettered option lines
    immediately under it (before the next Q<n>. line). Question numbers with
    fewer/no option lines (short-answer, essay, numerical) are omitted."""
    if not paper_text or not isinstance(paper_text, str):
        return {}

    lines = paper_text.splitlines()
    mcq: Dict[int, List[str]] = {}

    current_q: int | None = None
    current_options: List[str] = []

    def _flush():
        if current_q is not None and len(current_options) >= _MIN_OPTIONS_FOR_MCQ:
            mcq[current_q] = list(current_options)

    for raw_line in lines:
        line = raw_line.rstrip()
        q_match = _QUESTION_START_RE.match(line.strip())
        if q_match:
            _flush()
            current_q = int(q_match.group(1))
            current_options = []
            continue

        if current_q is None:
            continue

        opt_match = _OPTION_LINE_RE.match(line)
        if opt_match:
            current_options.append(opt_match.group(2).strip())
        elif line.strip() == "":
            continue
        elif re.match(r"^\s*\(?[ivxIVX]+[.\)]", line):
            # roman-numeral sub-part (e.g. "(i) ..."), not an MCQ option —
            # ignore it but don't reset current_options; a stray descriptive
            # line under an MCQ stem (rare) shouldn't break detection either.
            continue

    _flush()
    return mcq
