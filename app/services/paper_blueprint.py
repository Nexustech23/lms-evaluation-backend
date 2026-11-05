"""Allocate exact marks and validate an AI paper before DOCX rendering."""
from app.core.diagnostics import event, trace_function
from app.core.diagnostic_details import validation_error
from app.services.math_text import tidy_math
import json
import logging
import math
import re

PAPER_PROMPT_VERSION = "paper-blueprint-v2"

_OPTION_LINE = re.compile(r"^\s*\(?[A-Ea-e][\).]\s+\S")


def place_marks_tag(content: str, tag: str) -> str:
    """Put the marks tag at the end of the question stem, before options a) to d); else at the end."""
    lines = content.split("\n")
    first_option = next((i for i, line in enumerate(lines) if _OPTION_LINE.match(line)), None)
    if first_option:
        j = first_option - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j >= 0:
            lines[j] = lines[j].rstrip() + " " + tag
            return "\n".join(lines)
    return content.rstrip() + " " + tag


def _clean_answer(value):
    if isinstance(value, str):
        return value.strip()[:200] or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        items = [str(v).strip()[:60] for v in value if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
        return ", ".join(i for i in items if i)[:200] or None
    return None

def allocate_sections(sections, total):
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        raise validation_error('PAPER_TOTAL_INVALID', "Paper total must be a positive integer")
    if not sections or sum(s['percent'] for s in sections) != 100:
        raise validation_error('PAPER_SECTION_PERCENTAGES_INVALID', "Section percentages must total 100")
    exact = [total * s['percent'] / 100 for s in sections]
    marks = [math.floor(x) for x in exact]
    for i in sorted(range(len(marks)), key=lambda i: (-(exact[i]-marks[i]), i))[:total-sum(marks)]:
        marks[i] += 1
    if any(m <= 0 for m in marks):
        raise validation_error('PAPER_SECTION_ALLOCATION_INVALID', "Each section needs at least one mark; adjust sections or total")
    return marks


def _reply_shape(text):
    """Describe a reply that could not be read without recording any of its words."""
    text = text if isinstance(text, str) else ""
    stripped = text.lstrip()
    if not stripped:
        first = "empty"
    elif stripped.startswith("```"):
        first = "fence"
    elif stripped.startswith("{"):
        first = "brace"
    else:
        first = "letter" if stripped[0].isalpha() else "other"
    return {"reply_length": len(text), "first_char_kind": first, "first_brace_index": text.find("{"),
            "ends_with_brace": stripped.endswith("}")}


def parse_paper_json(text):
    """The model is asked for bare JSON; also accept it inside a code fence or with a sentence around it."""
    text = text if isinstance(text, str) else ""
    try:
        return json.loads(text)
    except json.JSONDecodeError as first_error:
        cleaned = re.sub(r"^\s*```[A-Za-z0-9_-]*[ \t]*\n?", "", text)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        for candidate in (cleaned, cleaned[start:end + 1] if 0 <= start < end else None):
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
            except ValueError:
                continue
            event("paper.reply_cleaned", level=logging.WARNING, **_reply_shape(text))
            return data
        event("paper.reply_unreadable", level=logging.WARNING, **_reply_shape(text))
        raise first_error


@trace_function('paper.validate')
def validate_paper(text, sections, total, co_list, *, allow_choices=False):
    """Choice sections offer equal-mark alternatives; count only required attempts."""
    data = parse_paper_json(text)
    if not isinstance(data, dict) or set(data) != {'sections'} or not isinstance(data['sections'],list) or len(data['sections']) != len(sections):
        raise validation_error('PAPER_SECTIONS_MISMATCH', "Generated paper sections do not match the blueprint")
    allocations = allocate_sections(sections,total)
    allowed_cos = {c['co_code'] for c in co_list}
    lines, seen, normalized_texts = ['INSTRUCTIONS:', '1. Follow the attempt rule printed under each section.'], set(), set()
    required_total = offered_total = 0
    answer_key, dropped_formulas = [], 0
    for section_index, (configured, generated, allocated) in enumerate(zip(sections,data['sections'],allocations), 1):
        if generated.get('label') != configured['label']:
            raise validation_error('PAPER_SECTION_LABEL_MISMATCH', "Section labels or order changed", section_index=section_index)
        questions = generated.get('questions')
        attempt = generated.get('attempt_count')
        if not isinstance(questions,list) or not 1 <= len(questions) <= 100 or type(attempt) is not int or not 1 <= attempt <= len(questions):
            raise validation_error('PAPER_ATTEMPT_COUNT_INVALID', "Invalid section questions or attempt count", section_index=section_index)
        marks=[]; section_lines=[]
        for q in questions:
            n, score = q.get('number'), q.get('marks')
            if type(n) is not int or n != len(seen)+1 or n in seen:
                raise validation_error('PAPER_QUESTION_NUMBER_INVALID', "Question numbering must be unique and consecutive", section_index=section_index)
            seen.add(n)
            if type(score) is not int or score <= 0:
                raise validation_error('PAPER_QUESTION_MARKS_INVALID', "Question marks must be positive integers", question_no=n, section_index=section_index)
            content = q.get('text')
            if not isinstance(content,str) or not content.strip():
                raise validation_error('PAPER_QUESTION_TEXT_MISSING', "Question text is required", question_no=n, section_index=section_index)
            content = tidy_math(content)
            if '<<<' in content or re.search(r'(?im)^\s*(?:Q\d+[.)]|SECTION\s+[A-Z])|\[\d+\s*Marks?\]', content):
                raise validation_error('PAPER_QUESTION_STRUCTURE_INJECTION', "Question text cannot inject numbering, sections or marks", question_no=n, section_index=section_index)
            key=' '.join(content.casefold().split())
            if key in normalized_texts:
                raise validation_error('PAPER_DUPLICATE_QUESTION', "Duplicate question text", question_no=n, section_index=section_index)
            normalized_texts.add(key)
            bloom=q.get('bloom_level')
            if type(bloom) is not int or not 1 <= bloom <= 6 or (configured.get('bloomLevels') and bloom not in configured['bloomLevels']):
                raise validation_error('PAPER_BLOOM_LEVEL_INVALID', "Question Bloom level violates section blueprint", question_no=n, section_index=section_index)
            cos=q.get('co_codes')
            if not isinstance(cos,list) or any(not isinstance(c,str) for c in cos) or len(cos)!=len(set(cos)) or not set(cos)<=allowed_cos or (allowed_cos and not cos):
                raise validation_error('PAPER_CO_TAGS_INVALID', "Question CO tags do not match approved outcomes", question_no=n, section_index=section_index)
            tags = f"[{score} Marks]" + (f" [{', '.join(cos)}]" if cos else '')
            section_lines.append(f"Q{n}. " + place_marks_tag(content.strip(), tags))
            answer, working = _clean_answer(q.get('answer')), _clean_answer(q.get('working'))
            if answer or working:
                answer_key.append({"number": n, "answer": answer, "working": working})
            diagrams=q.get('diagrams',[])
            if not isinstance(diagrams,list): raise validation_error('PAPER_DIAGRAMS_INVALID', "Invalid diagrams", question_no=n, section_index=section_index)
            for spec in diagrams:
                if isinstance(spec, dict) and spec.get('type') == 'math_expression':
                    dropped_formulas += 1  # a formula picture under a question gives the solution away
                    continue
                fields={'data_table':['headers','rows'],'math_expression':['expressions'],
                    'chemical_equation':['equations'],'electrical_circuit':['elements'],
                    'graph':['xlabel','ylabel','xrange','curves'],'binary_tree':['nodes'],
                    'network_graph':['nodes','edges'],'mermaid_diagram':['mermaid']}
                if not isinstance(spec,dict) or not spec.get('title') or spec.get('type') not in fields or any(not spec.get(f) for f in fields[spec['type']]):
                    raise validation_error('PAPER_DIAGRAM_PAYLOAD_INVALID', "Diagram requires a supported type and complete payload; faculty must supply unsupported visuals", question_no=n, section_index=section_index)
                section_lines.extend(['<<<DIAGRAM>>>',json.dumps(spec),'<<<END_DIAGRAM>>>'])
            marks.append(score)
        if attempt < len(questions):
            if not allow_choices: raise validation_error('PAPER_CHOICES_NOT_APPROVED', "Optional choices require an explicit faculty request", section_index=section_index)
            if len(set(marks)) != 1: raise validation_error('PAPER_CHOICE_MARKS_UNEQUAL', "Choice alternatives must carry equal marks", section_index=section_index)
            required=attempt*marks[0]
        else: required=sum(marks)
        if required != allocated: raise validation_error('PAPER_SECTION_MARKS_MISMATCH', "Section required marks do not match allocated marks", section_index=section_index)
        required_total+=required;offered_total+=sum(marks)
        lines += ['',configured['label'],f'Attempt {attempt} of {len(questions)} questions. Section total: {required} marks.']+section_lines
    if required_total != total: raise validation_error('PAPER_TOTAL_MISMATCH', "Paper required marks do not match total")
    if dropped_formulas:
        event('paper.formula_diagrams_dropped', level=logging.WARNING, count=dropped_formulas)
    public_sections = [{**sec, 'questions': [{k: v for k, v in q.items() if k not in ('answer', 'working')}
                                             for q in sec['questions']]} for sec in data['sections']]
    return '\n'.join(lines), {'required_marks':required_total,'offered_marks':offered_total,'sections':public_sections,
                              'answer_key':answer_key}
