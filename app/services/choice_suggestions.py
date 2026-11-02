"""Suggest choice groups from a question paper's own wording ("Answer any two of the following").

Only a suggestion for the faculty to confirm: nothing is saved here and no AI is used. A group is
suggested when a section says "answer any N" and lists more than N separate numbered questions.
Sections that say "answer all", or whose "any N" refers to the parts of one question (Q1 with ten
sub-parts), produce nothing. Pure functions.
"""
import re

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20}

_HEADING = re.compile(r"(?im)^[ \t]*(?:\|[ \t]*)?(group|section|part)[ \t]*[-:]?[ \t]*([A-Z0-9IVX]+)\b[^\n]*")
_ANY = re.compile(r"(?i)\b(?:answer|attempt)\s+any\s+(\d+|[a-z]+)\b[^\n]*")
_QUESTION = re.compile(r"(?m)^[ \t]*(?:\|[ \t]*)?(\d{1,2})[ \t]*[.)]")
_OR_LINE = re.compile(r"(?m)^[ \t]*OR[ \t]*$")


def _number(token):
    token = token.strip().lower()
    return int(token) if token.isdigit() else _WORDS.get(token)


def _question_numbers(text):
    """[(position, number)] for lines that start a printed question.

    A number counts only if it continues the sequence (the next number, or the same one again for an
    OR alternative). That keeps sub-items inside a question ("1. Normality", "2. Molarity") out."""
    found, last = [], None
    for match in _QUESTION.finditer(text):
        number = int(match.group(1))
        if (last is None and number <= 2) or (last is not None and number in (last, last + 1)):
            found.append((match.start(), number))
            last = number
    return found


def suggest_choice_groups(paper_text, row_count=None):
    """Return {"groups": [{id, questions, answerN, rule, source}], "notes": [str]}."""
    notes = []
    if not isinstance(paper_text, str) or not paper_text.strip():
        return {"groups": [], "notes": ["The question-paper text has not been extracted yet."]}
    headings = list(_HEADING.finditer(paper_text))
    groups, last_question = [], 0
    claimed = set()
    printed = _question_numbers(paper_text)
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(paper_text)
        segment = paper_text[heading.end() - len(heading.group(0)):end]
        rule = _ANY.search(segment[:500])
        start = heading.end() - len(heading.group(0))
        numbers = sorted({n for position, n in printed if start <= position < end})
        last_question = max([last_question, *numbers])
        if not rule:
            continue
        answer_n = _number(rule.group(1))
        label = f"{heading.group(1).title()} {heading.group(2).upper()}: answer any {rule.group(1)}"
        if answer_n is None or answer_n < 1:
            continue
        if len(numbers) <= answer_n:
            continue                       # "any N" over the parts of one question, or nothing to choose
        if claimed.intersection(numbers):
            notes.append(f"{label}: some questions already belong to another group, so this one was skipped.")
            continue
        if row_count is not None and numbers[-1] > row_count:
            notes.append(f"{label}: the paper lists up to Q{numbers[-1]} but the rubric has {row_count} questions, so this group was skipped. Check the question numbering.")
            continue
        claimed.update(numbers)
        groups.append({"questions": numbers, "answerN": answer_n, "rule": "best", "source": label})
        if _OR_LINE.search(segment):
            notes.append(f"{label}: this section also has an OR between questions. Check the group before using it.")
    for number, group in enumerate(groups, 1):
        group["id"] = f"g{number}"
    if row_count is not None and last_question and last_question != row_count:
        notes.append(f"The paper's last question is Q{last_question} but the rubric has {row_count} questions. Check that each rubric question matches a printed question.")
    return {"groups": groups, "notes": notes}
