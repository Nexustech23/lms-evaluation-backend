"""Read a question paper's own marking scheme so printed marks can be checked, never guessed.

Many papers print marks awkwardly: a bare digit at the end of a line (`... verified? 3`),
a sum (`2+1`), one digit after part I only, or on option A of an OR pair. Those layouts are
ambiguous on their own, but the paper usually also states its scheme in plain words:

    Maximum Marks: 70
    Section B contains five questions of two marks each ...      (count style)
    In Section C, Question no. 26 to 31 ... carry 3 marks each   (range style)
    SECTION A (16 x 1 = 16)                                      (heading style)

A question's marks are trusted ONLY when the whole scheme is consistent: every question is
covered, each section's declared count matches the questions found in it, styles agree with
each other, and the marks add up to the printed maximum. Anything less returns None and the
caller keeps its existing (strict) behaviour. Pure functions: no I/O, no AI.
"""
import re
from decimal import Decimal

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
          "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
          "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}

_NUM = r"(?:\d+(?:\.\d+)?|[a-z]+(?:-[a-z]+)?)"
_SECTION_KEY = r"([A-Z0-9IVX]+)"


def to_number(token):
    """Digits or English number words ('five', 'twenty-one') to Decimal, else None."""
    if token is None:
        return None
    token = token.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return Decimal(token)
    parts = re.split(r"[-\s]+", token)
    if len(parts) == 1:
        word = parts[0]
        if word in _UNITS:
            return Decimal(_UNITS[word])
        if word in _TENS:
            return Decimal(_TENS[word])
        return None
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _UNITS and 1 <= _UNITS[parts[1]] <= 9:
        return Decimal(_TENS[parts[0]] + _UNITS[parts[1]])
    return None


def parse_max_marks(text):
    """The paper's printed maximum ('Maximum Marks: 70'), looked for in its header only."""
    head = text[:3000]
    match = re.search(r"(?i)\b(?:max(?:imum)?\.?\s*marks?|total\s+marks?|full\s+marks?)\s*[:=\-–]?\s*(\d+(?:\.\d+)?)", head)
    return Decimal(match.group(1)) if match else None


def section_key(label):
    """'SECTION A', 'Section - B', 'PART II' -> 'A', 'B', 'II'. None when there is no label."""
    if not label:
        return None
    match = re.match(r"(?i)\s*(?:section|part)\s*[-:]?\s*" + _SECTION_KEY, label)
    return match.group(1).upper() if match else None


def question_root(label):
    """Printed label to its root question number: 'Q25' -> 25, '27(A)' -> 27."""
    match = re.search(r"(\d+)", label or "")
    return int(match.group(1)) if match else None


def _instruction_region(text):
    """Text before the first 'SECTION X' heading line, where general instructions live."""
    heading = re.search(r"(?im)^\s*(?:\|\s*)?SECTION\s*[-:]?\s*[A-Z0-9IVX]+\b(?!\s+(?:contains?|carr))", text)
    return text[:heading.start()] if heading else text[:4000]


def _marks_each(segment):
    match = re.search(r"(?i)(" + _NUM + r")\s*[- ]?marks?\s+each", segment)
    return to_number(match.group(1)) if match else None


def _count(segment):
    match = re.search(r"(?i)\bcontains?\s+(" + _NUM + r")\s+(?:[a-z\- ]{0,40}?)questions?", segment)
    return int(to_number(match.group(1))) if match and to_number(match.group(1)) is not None else None


def _ranges(segment):
    found = [(int(a), int(b)) for a, b in re.findall(
        r"(?i)questions?\s*(?:no\.?|nos\.?|numbers?)?\s*(\d+)\s*(?:to|-|–)\s*(\d+)", segment)]
    for a, b in re.findall(r"(?i)questions?\s*(?:no\.?|nos\.?|numbers?)?\s*(\d+)\s+and\s+(\d+)\b", segment):
        found.append((int(a), int(b)))
    return [(lo, hi) for lo, hi in found if 1 <= lo <= hi]


def parse_scheme(text):
    """Return {'sections': {key: {'marks', 'count'}}, 'ranges': [(lo, hi, marks)], 'conflict': bool}."""
    sections, ranges, conflict = {}, [], False

    def put(key, marks, count):
        nonlocal conflict
        entry = sections.setdefault(key, {"marks": None, "count": None})
        for field, value in (("marks", marks), ("count", count)):
            if value is None:
                continue
            if entry[field] is not None and entry[field] != value:
                conflict = True
            entry[field] = value

    region = _instruction_region(text)
    mentions = list(re.finditer(r"\bSection\s*[-:]?\s*" + _SECTION_KEY + r"\b", region))
    for index, match in enumerate(mentions):
        end = mentions[index + 1].start() if index + 1 < len(mentions) else min(len(region), match.end() + 500)
        segment = region[match.start():end]
        marks = _marks_each(segment)
        if marks is None:
            continue
        key = match.group(1).upper()
        put(key, marks, _count(segment))
        ranges.extend((lo, hi, marks) for lo, hi in _ranges(segment))

    # Heading style: "SECTION A (16 x 1 = 16)". Count x marks must equal the stated total.
    for match in re.finditer(r"(?im)^\s*(?:\|\s*)?SECTION\s*[-:]?\s*" + _SECTION_KEY +
                             r"\b[^\n]{0,60}?\(\s*(" + _NUM + r")\s*[x×*]\s*(\d+(?:\.\d+)?)\s*=\s*(\d+(?:\.\d+)?)\s*\)", text):
        count, marks, total = to_number(match.group(2)), Decimal(match.group(3)), Decimal(match.group(4))
        if count is None or count * marks != total:
            conflict = True
            continue
        put(match.group(1).upper(), marks, int(count))
    return {"sections": sections, "ranges": ranges, "conflict": conflict}


def expected_marks(blocks, text):
    """{question_no: Decimal marks} when the paper's scheme is complete and consistent, else None."""
    total = parse_max_marks(text)
    if total is None or not blocks:
        return None
    scheme = parse_scheme(text)
    if scheme["conflict"]:
        return None
    per_section = {}
    for block in blocks:
        per_section.setdefault(section_key(block.get("section")), []).append(block)
    result = {}
    for block in blocks:
        root = question_root(block.get("question_label"))
        from_range = {marks for lo, hi, marks in scheme["ranges"] if root is not None and lo <= root <= hi}
        entry = scheme["sections"].get(section_key(block.get("section")))
        from_section = entry["marks"] if entry and entry["marks"] is not None else None
        candidates = from_range | ({from_section} if from_section is not None else set())
        if len(candidates) != 1:
            return None                      # uncovered question, or the two styles disagree
        result[block["question_no"]] = candidates.pop()
    for key, entry in scheme["sections"].items():
        if entry["count"] is not None and len(per_section.get(key, [])) != entry["count"]:
            return None                      # the paper says N questions; the mapping found another number
    if sum(result.values()) != total:
        return None                          # the scheme does not add up to the printed maximum
    return result


def apply_scheme(blocks, text):
    """Attach resolved_max_marks to every block when the scheme is trusted. Returns True if applied."""
    marks = expected_marks(blocks, text)
    if not marks:
        return False
    for block in blocks:
        block["resolved_max_marks"] = float(marks[block["question_no"]])
        block["resolved_max_source"] = "paper_scheme"
    return True
