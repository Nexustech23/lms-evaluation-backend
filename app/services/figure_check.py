"""Does each figure match its question? Checked by code, with no extra AI call.

The numbers and names in a question's text are compared with the figure the AI described, and the drawing
itself is checked (angles and lengths against the coordinates). A mismatch becomes a flag for the faculty in the same
"Check these questions" list as the answer check: {"number": n, "issue": "..."}.

Everything here is conservative: it only flags when the text states something plainly (for example "AB = 5 cm" or
"concave mirror") and the figure contradicts it, so a flag is worth a look and a quiet paper stays quiet.
"""
import math
import re
from typing import Any, Dict, List, Optional

from app.services import math_figures, physics_figures
from app.services.library_figures import LIBRARY_TYPE, check_figure as check_library, lettered_parts

_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"(?:\s*(?:cm|mm|km|m|units?))?"
_REFERS = re.compile(
    r"\b(?:in|from|of|using|see|refer to)\s+the\s+(?:given\s+|following\s+|above\s+|below\s+)?(?:figure|diagram|graph|circuit)\b"
    r"|\b(?:given|following|above|below|adjoining|accompanying)\s+(?:figure|diagram|graph|circuit)\b"
    r"|\b(?:figure|diagram|graph|circuit)\s+(?:shown|given|below|above)\b|\bshown\s+(?:in|below|above)\b", re.I)
_SEGMENT_TEXT = re.compile(r"\b([A-Z])([A-Z])\s*(?:=|is|of length|equals|measures)\s*" + _NUM + _UNIT)
_ANGLE_TEXT = re.compile(r"(?:∠|\bangle\s+)([A-Z])([A-Z])([A-Z])\s*(?:=|is|equals|measures)\s*" + _NUM + r"\s*°")
_POINT_TEXT = re.compile(r"\b([A-Z])\s*\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)")
_ASKS_TO_FIND = re.compile(r"\b(?:draw|construct|trace|locate|complete the (?:ray|diagram|path)|(?:find|determine|calculate|state) the "
                           r"(?:position|nature|size|angle|image|deviation|refracted|emergent|lateral))", re.I)
_ASKS_TO_DRAW = re.compile(r"\b(?:draw|construct|trace|complete the (?:ray|diagram|path))", re.I)
_ELEMENT_PHRASES = {"convex_lens": "convex lens", "concave_lens": "concave lens", "concave_mirror": "concave mirror",
                    "convex_mirror": "convex mirror", "plane_mirror": "plane mirror"}
_LIBRARY_PHRASES = {"plant_cell": "plant cell", "animal_cell": "animal cell", "human_heart": "heart", "human_eye": "human eye",
                    "flower_section": "flower", "digestive_system": "digestive system", "neuron": "neuron",
                    "leaf_section": "leaf", "simple_distillation": "distillation", "electrolysis_of_water": "electrolysis"}
_LETTERS_TEXT = re.compile(r"\b(?i:parts?|labelled|labeled|marked)\s+([A-H](?:\s*(?:,|and|&|to|-|–)\s*[A-H])*)\b")


def _number(text) -> Optional[float]:
    found = re.fullmatch(r"\s*" + _NUM + r"\s*(?:°|cm|mm|km|m|units?)?\s*", str(text or ""))
    return float(found.group(1)) if found else None


def _close(a: float, b: float, tolerance: float = 0.01) -> bool:
    return abs(a - b) <= tolerance * max(1.0, abs(a), abs(b))


def _clean(text: str) -> str:
    return str(text or "").replace("−", "-").replace("–", "-")


def _drawn_angle(points: Dict[str, Any], vertex: str, a: str, b: str) -> Optional[float]:
    v, p, q = (list(map(float, points[k])) for k in (vertex, a, b))
    u1, u2 = (p[0] - v[0], p[1] - v[1]), (q[0] - v[0], q[1] - v[1])
    n1, n2 = math.hypot(*u1), math.hypot(*u2)
    if n1 < 1e-9 or n2 < 1e-9:
        return None
    return math.degrees(math.acos(max(-1.0, min(1.0, (u1[0] * u2[0] + u1[1] * u2[1]) / (n1 * n2)))))


def _length(points: Dict[str, Any], a: str, b: str) -> float:
    return math.hypot(float(points[a][0]) - float(points[b][0]), float(points[a][1]) - float(points[b][1]))


# ------------------------------------------------------------------ one check per kind of figure

def _geometry(spec, text) -> List[str]:
    issues: List[str] = []
    points = spec.get("points") or {}
    for angle in spec.get("angles") or []:
        trio = (angle.get("vertex"), angle.get("from"), angle.get("to"))
        drawn = _drawn_angle(points, trio[0], trio[1], trio[2])
        if drawn is None:
            continue
        name = f"∠{trio[1]}{trio[0]}{trio[2]}"
        if angle.get("right") and abs(drawn - 90) > 2:
            issues.append(f"{name} is marked as a right angle but is drawn as {drawn:.0f}°")
        said = _number(angle.get("text"))
        if said is not None and abs(drawn - said) > 3:
            issues.append(f"{name} is labelled {angle['text']} but is drawn as {drawn:.0f}°")
    scale = None
    for label in spec.get("labels") or []:
        at = label.get("at")
        value = _number(label.get("text"))
        if isinstance(at, list) and value and at[0] in points and at[1] in points:
            drawn = _length(points, at[0], at[1])
            if drawn < 1e-9:
                continue
            per_unit = value / drawn
            if scale is None:
                scale = (per_unit, f"{at[0]}{at[1]}", label.get("text"))
            elif abs(per_unit / scale[0] - 1) > 0.12:
                issues.append(f"The figure is not to scale: {scale[1]} is labelled {scale[2]} and {at[0]}{at[1]} is labelled "
                              f"{label.get('text')}, but they are drawn in a different ratio")
                break
    for a, b, value in ((m.group(1), m.group(2), float(m.group(3))) for m in _SEGMENT_TEXT.finditer(text)):
        for label in spec.get("labels") or []:
            at = label.get("at")
            said = _number(label.get("text"))
            if isinstance(at, list) and {at[0], at[1]} == {a, b} and said is not None and not _close(said, value):
                issues.append(f"The question says {a}{b} = {value:g} but the figure labels it {label['text']}")
    for m in _ANGLE_TEXT.finditer(text):
        a, vertex, b, value = m.group(1), m.group(2), m.group(3), float(m.group(4))
        if all(k in points for k in (a, vertex, b)):
            drawn = _drawn_angle(points, vertex, a, b)
            if drawn is not None and abs(drawn - value) > 3:
                issues.append(f"The question says ∠{a}{vertex}{b} = {value:g}° but the figure draws it as {drawn:.0f}°")
    return issues


def _plane(spec, text) -> List[str]:
    issues: List[str] = []
    plotted = {p["name"]: (float(p["x"]), float(p["y"])) for p in spec.get("points") or []}
    (x0, x1), (y0, y1) = spec.get("xrange", [0, 0]), spec.get("yrange", [0, 0])
    for name, (x, y) in plotted.items():
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            issues.append(f"Point {name} ({x:g}, {y:g}) is outside the drawn area of the graph")
    for m in _POINT_TEXT.finditer(_clean(text)):
        name, x, y = m.group(1), float(m.group(2)), float(m.group(3))
        if name in plotted and not (_close(plotted[name][0], x) and _close(plotted[name][1], y)):
            issues.append(f"The question gives {name}({x:g}, {y:g}) but the figure plots {name} at ({plotted[name][0]:g}, {plotted[name][1]:g})")
    return issues


def _ray(spec, text) -> List[str]:
    issues: List[str] = []
    low = text.lower()
    shown = _ELEMENT_PHRASES.get(spec.get("element"), "")
    named = [phrase for phrase in _ELEMENT_PHRASES.values() if phrase in low]
    if named and shown not in named:
        issues.append(f"The question mentions a {named[0]} but the figure shows a {shown}")
    focal = re.search(r"focal length(?:\s+of(?:\s+the\s+\w+)?)?\s*(?:is|=|of)?\s*" + _NUM + r"\s*cm", low)
    if focal and spec.get("focal_length") is not None and not _close(float(focal.group(1)), float(spec["focal_length"])):
        issues.append(f"The question gives a focal length of {focal.group(1)} cm but the figure uses {float(spec['focal_length']):g} cm")
    distance = re.search(_NUM + r"\s*cm\s*(?:in front of|from|away from|before|to the left of)\b", low)
    if distance and spec.get("object_distance") is not None and not _close(float(distance.group(1)), float(spec["object_distance"])):
        issues.append(f"The question puts the object {distance.group(1)} cm away but the figure uses {float(spec['object_distance']):g} cm")
    if _ASKS_TO_FIND.search(text) and (spec.get("show_rays") or spec.get("show_image")):
        issues.append("The figure already shows the rays or the image that the question asks the student to draw or find")
    return issues


def _refraction(spec, text) -> List[str]:
    issues: List[str] = []
    low = text.lower()
    if spec.get("kind") == "prism" and "slab" in low and "prism" not in low:
        issues.append("The question mentions a glass slab but the figure shows a prism")
    if spec.get("kind") == "slab" and "prism" in low and "slab" not in low:
        issues.append("The question mentions a prism but the figure shows a glass slab")
    pairs = (("angle_of_incidence", r"angle of incidence\s*(?:is|=|of)?\s*" + _NUM + r"\s*°", "an angle of incidence of {v:g}°"),
             ("n", r"refractive index\s*(?:of\s+\w+\s*)?(?:is|=|of)?\s*" + _NUM, "a refractive index of {v:g}"),
             ("prism_angle", r"(?:angle of (?:the )?prism|prism angle|apex angle)\s*(?:is|=|of)?\s*" + _NUM + r"\s*°", "a prism angle of {v:g}°"))
    for key, pattern, phrase in pairs:
        found = re.search(pattern, low)
        if found and spec.get(key) is not None and not _close(float(found.group(1)), float(spec[key])):
            issues.append(f"The question gives {phrase.format(v=float(found.group(1)))} but the figure uses {float(spec[key]):g}")
    if _ASKS_TO_DRAW.search(text) and spec.get("show_path", True) is not False:
        issues.append("The figure already shows the refracted path that the question asks the student to draw or find")
    return issues


def _magnetic(spec, text) -> List[str]:
    low = text.lower()
    names = {"bar_magnet": "bar magnet", "solenoid": "solenoid", "straight_wire": "straight"}
    mentioned = [kind for kind, word in names.items() if word in low]
    if mentioned and spec.get("kind") not in mentioned:
        return [f"The question mentions a {names[mentioned[0]].replace('straight', 'straight wire')} but the figure shows a {spec.get('kind', '').replace('_', ' ')}"]
    return []


def _library(spec, text) -> List[str]:
    issues: List[str] = []
    low = text.lower()
    own = _LIBRARY_PHRASES.get(spec.get("figure"), "")
    mentioned = [phrase for phrase in _LIBRARY_PHRASES.values() if phrase in low]
    comparing = any(word in low for word in ("differ", "compar", "distinguish", "between", " versus ", " vs "))
    names_own_cell = spec.get("figure") in ("plant_cell", "animal_cell") and re.search(r"" + own.split()[0] + r"", low)
    if mentioned and own not in mentioned and not comparing and not names_own_cell:
        issues.append(f"The question is about the {mentioned[0]} but the figure shows the {own}")
    lettered = lettered_parts(spec)
    wanted = _LETTERS_TEXT.search(text)
    if wanted:
        letters = {ch for ch in wanted.group(1) if ch.isalpha() and ch.isupper()}
        if len(letters) >= 2 and wanted.group(1).count("to") + wanted.group(1).count("-") + wanted.group(1).count("–"):
            ends = sorted(letters)
            letters = {chr(c) for c in range(ord(ends[0]), ord(ends[-1]) + 1)}
        have = {letter for letter, _ in lettered}
        if not lettered:
            issues.append("The question asks about lettered parts but the figure shows every part by name; use letters or hide")
        elif letters - have:
            issues.append(f"The question refers to part {sorted(letters - have)[0]} but the figure only has letters {lettered[0][0]} to {lettered[-1][0]}")
    for letter, name in lettered:
        if re.search(r"\b" + re.escape(name.lower()) + r"\b", low):
            issues.append(f"The question names the {name}, which the figure hides as {letter}, so the answer is given away")
    return issues


# ------------------------------------------------------------------ whole paper

def check_figures(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flags {number, issue} for every question whose figure does not match its text."""
    found: Dict[int, List[str]] = {}
    for section in sections or []:
        for question in section.get("questions") or []:
            number, text = question.get("number"), _clean(question.get("text"))
            diagrams = [d for d in question.get("diagrams") or [] if isinstance(d, dict)]
            issues: List[str] = []
            if not diagrams and _REFERS.search(text):
                issues.append("The question refers to a figure but none is attached; insert one or reword the question")
            for spec in diagrams:
                kind = spec.get("type")
                reason = (math_figures.check_figure(spec) if kind in math_figures.FIGURE_TYPES
                          else physics_figures.check_figure(spec) if kind in physics_figures.PHYSICS_FIGURE_TYPES
                          else check_library(spec) if kind == LIBRARY_TYPE else None)
                if reason:
                    issues.append(f"A {kind.replace('_', ' ')} could not be drawn automatically ({reason}); insert the figure while editing")
                    continue
                if kind == "geometry_figure":
                    issues += _geometry(spec, text)
                elif kind == "coordinate_plane":
                    issues += _plane(spec, text)
                elif kind == "ray_diagram":
                    issues += _ray(spec, text)
                elif kind == "refraction":
                    issues += _refraction(spec, text)
                elif kind == "magnetic_field":
                    issues += _magnetic(spec, text)
                elif kind == LIBRARY_TYPE:
                    issues += _library(spec, text)
            if issues and isinstance(number, int):
                found.setdefault(number, []).extend(i[:300] for i in issues)
    return [{"number": n, "issue": "; ".join(found[n])} for n in sorted(found)]


def merge_flags(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Combine several flag lists into one, joining the issues for a question that appears in more than one."""
    merged: Dict[int, List[str]] = {}
    for flags in groups:
        for flag in flags or []:
            merged.setdefault(flag["number"], []).append(flag["issue"])
    return [{"number": n, "issue": "; ".join(merged[n])} for n in sorted(merged)]
