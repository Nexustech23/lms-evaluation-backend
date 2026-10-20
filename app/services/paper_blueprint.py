"""Allocate exact marks and validate an AI paper before DOCX rendering."""
import json
import math
import re

PAPER_PROMPT_VERSION = "paper-blueprint-v1"

def allocate_sections(sections, total):
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        raise ValueError("Paper total must be a positive integer")
    if not sections or sum(s['percent'] for s in sections) != 100:
        raise ValueError("Section percentages must total 100")
    exact = [total * s['percent'] / 100 for s in sections]
    marks = [math.floor(x) for x in exact]
    for i in sorted(range(len(marks)), key=lambda i: (-(exact[i]-marks[i]), i))[:total-sum(marks)]:
        marks[i] += 1
    if any(m <= 0 for m in marks):
        raise ValueError("Each section needs at least one mark; adjust sections or total")
    return marks


def validate_paper(text, sections, total, co_list, *, allow_choices=False):
    """Choice sections offer equal-mark alternatives; count only required attempts."""
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != {'sections'} or not isinstance(data['sections'],list) or len(data['sections']) != len(sections):
        raise ValueError("Generated paper sections do not match the blueprint")
    allocations = allocate_sections(sections,total)
    allowed_cos = {c['co_code'] for c in co_list}
    lines, seen, normalized_texts = ['INSTRUCTIONS:', '1. Follow the attempt rule printed under each section.'], set(), set()
    required_total = offered_total = 0
    for configured, generated, allocated in zip(sections,data['sections'],allocations):
        if generated.get('label') != configured['label']:
            raise ValueError("Section labels or order changed")
        questions = generated.get('questions')
        attempt = generated.get('attempt_count')
        if not isinstance(questions,list) or not 1 <= len(questions) <= 100 or type(attempt) is not int or not 1 <= attempt <= len(questions):
            raise ValueError("Invalid section questions or attempt count")
        marks=[]; section_lines=[]
        for q in questions:
            n, score = q.get('number'), q.get('marks')
            if type(n) is not int or n != len(seen)+1 or n in seen:
                raise ValueError("Question numbering must be unique and consecutive")
            seen.add(n)
            if type(score) is not int or score <= 0:
                raise ValueError("Question marks must be positive integers")
            content = q.get('text')
            if not isinstance(content,str) or not content.strip():
                raise ValueError("Question text is required")
            if '<<<' in content or re.search(r'(?im)^\s*(?:Q\d+[.)]|SECTION\s+[A-Z])|\[\d+\s*Marks?\]', content):
                raise ValueError("Question text cannot inject numbering, sections or marks")
            key=' '.join(content.casefold().split())
            if key in normalized_texts:
                raise ValueError("Duplicate question text")
            normalized_texts.add(key)
            bloom=q.get('bloom_level')
            if type(bloom) is not int or not 1 <= bloom <= 6 or (configured.get('bloomLevels') and bloom not in configured['bloomLevels']):
                raise ValueError("Question Bloom level violates section blueprint")
            cos=q.get('co_codes')
            if not isinstance(cos,list) or any(not isinstance(c,str) for c in cos) or len(cos)!=len(set(cos)) or not set(cos)<=allowed_cos or (allowed_cos and not cos):
                raise ValueError("Question CO tags do not match approved outcomes")
            section_lines.append(f"Q{n}. {content.strip()} [{score} Marks]" + (f" [{', '.join(cos)}]" if cos else ''))
            diagrams=q.get('diagrams',[])
            if not isinstance(diagrams,list): raise ValueError("Invalid diagrams")
            for spec in diagrams:
                fields={'data_table':['headers','rows'],'math_expression':['expressions'],
                    'chemical_equation':['equations'],'electrical_circuit':['elements'],
                    'graph':['xlabel','ylabel','xrange','curves'],'binary_tree':['nodes'],
                    'network_graph':['nodes','edges'],'mermaid_diagram':['mermaid']}
                if not isinstance(spec,dict) or not spec.get('title') or spec.get('type') not in fields or any(not spec.get(f) for f in fields[spec['type']]):
                    raise ValueError("Diagram requires a supported type and complete payload; faculty must supply unsupported visuals")
                section_lines.extend(['<<<DIAGRAM>>>',json.dumps(spec),'<<<END_DIAGRAM>>>'])
            marks.append(score)
        if attempt < len(questions):
            if not allow_choices: raise ValueError("Optional choices require an explicit faculty request")
            if len(set(marks)) != 1: raise ValueError("Choice alternatives must carry equal marks")
            required=attempt*marks[0]
        else: required=sum(marks)
        if required != allocated: raise ValueError("Section required marks do not match allocated marks")
        required_total+=required;offered_total+=sum(marks)
        lines += ['',configured['label'],f'Attempt {attempt} of {len(questions)} questions. Section total: {required} marks.']+section_lines
    if required_total != total: raise ValueError("Paper required marks do not match total")
    return '\n'.join(lines), {'required_marks':required_total,'offered_marks':offered_total,'sections':data['sections']}
