# ============================================================
# Plain-text maths in a generated paper -> proper symbols.
# The model writes "5*3", "m/s^2", "sqrt(750)", "30 degrees"; a paper should read 5 × 3, m/s², √(750), 30°.
# Only forms that are clearly maths are changed. A star is never touched in a question about regular expressions,
# languages or automata, where it is the Kleene star: (0+1)*00 must stay as written.
# ============================================================

import re

_SUPERSCRIPT = str.maketrans("0123456789+-n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ")
_SUBSCRIPT = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")

# Words that show a star may be a Kleene star or a pointer, not multiplication.
_STAR_CONTEXT = re.compile(
    r"\b(regex|regular expression|regular language|language|automaton|automata|DFA|NFA|grammar|Kleene|alphabet|"
    r"string|pointer|wildcard|glob|markdown)\b",
    re.IGNORECASE,
)

_STAR = r"(?<!\*)\*(?!\*)"
_TIMES_AFTER_NUMBER = re.compile(rf"(?<=[0-9])[ \t]*{_STAR}[ \t]*(?=[\w(\[.])")
_TIMES_BEFORE_NUMBER = re.compile(rf"(?<=[\w)\]])[ \t]*{_STAR}[ \t]*(?=[0-9(\[.])")
_TIMES_SPACED = re.compile(rf"(?<=[\w)\]]) {_STAR} (?=[\w(\[])")
_TIMES_BETWEEN_LETTERS = re.compile(rf"(?<=[A-Za-z0-9)]){_STAR}(?=[A-Za-z(])")

_POWER_BRACED = re.compile(r"\^\{([+-]?\d+|n)\}")
_POWER_PLAIN = re.compile(r"\^([+-]?\d+|n)(?![\w.])")
_SUBSCRIPT_DIGITS = re.compile(r"(?<=[A-Za-z])_(\d+)(?![\w])")
_SQRT = re.compile(r"\bsqrt\(")
_DEGREES = re.compile(r"(?<=\d)\s*degrees?\b")


def _tidy_products(line: str) -> str:
    line = _TIMES_AFTER_NUMBER.sub(" × ", line)
    line = _TIMES_BEFORE_NUMBER.sub(" × ", line)
    line = _TIMES_SPACED.sub(" × ", line)
    if "=" in line or "×" in line:  # a formula line: "F = m*a", "sqrt(2 × g*h)"
        line = _TIMES_BETWEEN_LETTERS.sub(" × ", line)
    return line


def tidy_math(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    if not _STAR_CONTEXT.search(text):
        text = "\n".join(_tidy_products(line) for line in text.split("\n"))
    text = _POWER_BRACED.sub(lambda m: m.group(1).translate(_SUPERSCRIPT), text)
    text = _POWER_PLAIN.sub(lambda m: m.group(1).translate(_SUPERSCRIPT), text)
    text = _SUBSCRIPT_DIGITS.sub(lambda m: m.group(1).translate(_SUBSCRIPT), text)
    text = _SQRT.sub("√(", text)
    text = _DEGREES.sub("°", text)
    return text
