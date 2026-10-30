"""Recognise printed maxima without treating arbitrary numbers as marks."""
import re
from decimal import Decimal


NUMBER = r"\d+(?:\.\d+)?"


def quoted_maximum_matches(value, quote, block, context):
    expected = Decimal(str(value))
    # Labelled maxima. A minimum/pass threshold is not a maximum, even when
    # it happens to have the same numeric value as the proposed maximum.
    if re.search(r"\b(?:minimum|min\.?|pass(?:ing)?)\b", quote, re.I):
        return False
    labelled = re.findall(rf"({NUMBER})\s*marks?\b|\bmarks?\s*[:=]\s*({NUMBER})", quote, re.I)
    if any(Decimal(a or b) == expected for a, b in labelled):
        return True
    # Conventional bracketed marks must occur at the end of a question line,
    # optionally followed by CO tags. Never accept a plain number in prose.
    token = re.fullmatch(rf"\s*[\[(]\s*({NUMBER})\s*[\])]\s*", quote)
    if token and Decimal(token[1]) == expected:
        suffix = r"(?:\s*\[?\s*CO\d+(?:\s*,\s*CO\d+)*\s*\]?)*\s*$"
        if any(re.search(re.escape(quote.strip()) + suffix, line, re.I) for line in block["text"].splitlines()):
            return True
    # Markdown table columns require an explicit Marks/Max Marks header. The
    # quote must be present in this row's marks cell, not merely another cell.
    paper = context.get("paper_text", "")
    start = block.get("start", paper.find(block["text"]))
    headers = []
    for line in paper[:max(start, 0)].splitlines():
        cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
        indices = [i for i, cell in enumerate(cells) if re.fullmatch(r"(?:max(?:imum)?\.?\s*)?marks?", cell, re.I)]
        if len(cells) > 1 and indices:
            headers = (cells, indices)
    if headers:
        heading, indices = headers
        for line in block["text"].splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) != len(heading):
                continue
            for index in indices:
                cell = cells[index]
                if re.fullmatch(NUMBER, cell) and Decimal(cell) == expected and quote.strip() in (cell, line.strip()):
                    return True
    return False
