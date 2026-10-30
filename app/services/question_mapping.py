"""Printed labels are evidence; rubric indices are separate, sequential IDs.

Ambiguous/unrecognised layouts require faculty-selected line boundaries. No AI
call, paper renumbering or inferred mark allocation is involved in mapping.
"""
import hashlib
import json
import re

from fastapi import HTTPException


SECTION = re.compile(r"^\s*(?:#{1,6}\s*)?(?:section|part)\s*[-:]?\s*([A-Z0-9IVX]+)\b", re.I)
LABEL = re.compile(
    r"^\s*(?:\|\s*)?(?:\*\*)?"
    r"(?P<label>(?P<prefix>Q(?:uestion)?\s*(?:No\.?\s*)?\.?\s*)?"
    r"(?P<root>\d+)(?P<parts>(?:\s*\([a-z0-9ivx]+\)|\.[a-z0-9]+)*))"
    r"(?P<end>[.):]|(?=\s|\||$))", re.I)
SUBPART = re.compile(r"^\s*(?:\|\s*)?(?P<label>\([a-zivx]+\)|[a-zivx]+[.)])\s+", re.I)


def source_key(paper):
    fields = [paper.get(k) for k in ("text", "version", "url", "fileId", "no_of_questions")]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, default=str).encode()).hexdigest()


def _lines(text):
    offset = 0
    section = ""
    for line in text.splitlines(keepends=True):
        heading = SECTION.match(line)
        if heading:
            section = heading[0].strip().lstrip("# ")
        yield offset, line.rstrip("\r\n"), section, bool(heading)
        offset += len(line)


def candidates(text):
    rows = []
    for start, line, section, heading in _lines(text):
        if heading:
            continue
        match = LABEL.match(line) or SUBPART.match(line)
        if not match:
            continue
        data = match.groupdict()
        rows.append({"start": start, "question_label": data["label"].strip(),
                     "section": section, "preview": line.strip()[:300],
                     "root": data.get("root"), "subpart": bool(data.get("parts")) or not data.get("root"),
                     "explicit": bool(data.get("prefix"))})
    # Explicit labels take precedence over numbered instructions/content lists.
    # Plain labels may still be selected by faculty when the paper mixes styles.
    return rows


def mapped_blocks(text, selections, count):
    """Validate selected exact line starts and slice the untouched source."""
    if not isinstance(selections, list) or len(selections) != count:
        raise HTTPException(422, "Select exactly one question boundary per evaluation row.")
    lines = {start: (line, section) for start, line, section, _ in _lines(text) if line.strip()}
    starts = [row.get("start") for row in selections if isinstance(row, dict)]
    if (len(starts) != count or any(type(s) is not int or s not in lines for s in starts)
            or starts != sorted(set(starts))):
        raise HTTPException(422, "Question boundaries must be unique source line starts in paper order.")
    blocks = []
    seen = set()
    for index, row in enumerate(selections):
        label = row.get("question_label")
        start = starts[index]
        if not isinstance(label, str) or not label.strip() or len(label) > 120:
            raise HTTPException(422, "Each question needs its printed label (up to 120 characters).")
        label = label.strip()
        section = lines[start][1]
        canonical_label = re.sub(r"^(?:question|q)\s*(?:no\.?\s*)?\.?\s*", "", label, flags=re.I)
        key = (section.casefold(), re.sub(r"\s+", "", canonical_label).casefold())
        if key in seen:
            raise HTTPException(422, "Repeated labels in the same section need distinct subpart/option labels.")
        seen.add(key)
        end = starts[index + 1] if index + 1 < count else len(text)
        # A following section heading belongs to the next section, never to the
        # previous question's evidence (marks/COs in headings must not leak).
        headings = [pos for pos, _, _, heading in _lines(text) if heading and start < pos < end]
        if headings:
            end = headings[0]
        identity = hashlib.sha256(f"{start}:{section}:{label}".encode()).hexdigest()[:24]
        blocks.append({"question_no": index + 1, "question_id": identity,
                       "question_label": label, "section": section,
                       "text": text[start:end].strip(), "start": start})
    return blocks


def resolve_questions(text, count, confirmed=None):
    if confirmed is not None:
        return mapped_blocks(text, confirmed, count)
    options = candidates(text)
    explicit = [q for q in options if q["explicit"]]
    eligible = explicit or options
    # Letter-only rows can be answer options, and numbered instructions can
    # resemble questions. Do not promote either into questions automatically.
    if not explicit:
        eligible = [q for q in eligible if q["root"]]
    # Ignore numbered instruction lists before the first section when present.
    sections = [pos for pos, _, _, heading in _lines(text) if heading]
    if sections and not explicit:
        eligible = [q for q in eligible if q["start"] > sections[0]]
    if not explicit and not sections and re.search(r"(?im)^\s*(?:general\s+)?instructions\s*:", text):
        eligible = []
    # A paper can count 1(i)..1(x) together as Question 1, or count each
    # printed subpart separately. Only choose a grouping matching the configured
    # row count; never discard a root that has no standalone parent heading.
    groups = {}
    for row in eligible:
        key = (row["section"], row["root"])
        groups.setdefault(key, []).append(row)
    grouped = []
    for group in groups.values():
        labels = [re.sub(r"\s+", "", row["question_label"]).casefold() for row in group]
        if sum(not row["subpart"] for row in group) > 1 or len(labels) != len(set(labels)):
            grouped = []
            break
        first = dict(group[0])
        if first["subpart"]:
            first["question_label"] = first["root"]
        grouped.append(first)
    layouts = []
    for layout in (eligible, grouped):
        if layout is eligible and any(len(group) > 1 and any(not row["subpart"] for row in group) for group in groups.values()):
            continue
        if len(layout) != count:
            continue
        try:
            blocks = mapped_blocks(text, layout, count)
        except HTTPException:
            continue
        signature = tuple(q["start"] for q in blocks)
        if not any(signature == sig for sig, _ in layouts):
            layouts.append((signature, blocks))
    if len(layouts) == 1:
        return layouts[0][1]
    raise HTTPException(422, {"code": "QUESTION_MAPPING_REQUIRED",
        "message": "Confirm which printed questions correspond to the evaluation rows. Your paper does not need renumbering.",
        "question_count": count, "candidates": options,
        "lines": [{"start": pos, "text": line, "section": section}
                  for pos, line, section, _ in _lines(text) if line.strip()]})


def legacy_mcq_indices_match(rules):
    """The legacy answer-key/OCR cache is keyed by printed integers only."""
    for index, rule in enumerate(rules, 1):
        label = rule.get("question_label")
        if label is not None and not re.fullmatch(rf"(?:Q(?:uestion)?\s*)?{index}", label, re.I):
            return False
    return True
