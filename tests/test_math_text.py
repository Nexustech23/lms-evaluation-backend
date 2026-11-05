"""Plain-text maths from the model becomes proper symbols; code and prose are left alone."""
import pytest

from app.services.math_text import tidy_math


@pytest.mark.parametrize("before,after", [
    # the exact strings that were damaged in a real paper
    ("5*(0.6 + 0.5*0.8)*10 N = 50 N", "5 × (0.6 + 0.5 × 0.8) × 10 N = 50 N"),
    ("v0 * M / (M + m)", "v0 × M / (M + m)"),
    ("v0 * m / (M + m)", "v0 × m / (M + m)"),
    # powers
    ("g = 10 m/s^2", "g = 10 m/s²"),
    ("1.2 m/s^2 and 10^-3 kg", "1.2 m/s² and 10⁻³ kg"),
    ("x^{2} + y^{-1}", "x² + y⁻¹"),
    ("m v^2 / R", "m v² / R"),
    ("a^n", "aⁿ"),
    # roots, degrees, subscripts
    ("v0 = sqrt(750) m/s", "v0 = √(750) m/s"),
    ("mu = 1/sqrt(3)", "mu = 1/√(3)"),
    ("inclination 30 degrees", "inclination 30°"),
    ("at 45 degree angle", "at 45° angle"),
    ("m_1 and m_2 with v_0", "m₁ and m₂ with v₀"),
])
def test_clear_maths_is_tidied(before, after):
    assert tidy_math(before) == after


@pytest.mark.parametrize("text", [
    "int *p = &x;",                      # C pointer
    "the regex a*b* matches",            # letters on both sides, no spaces
    "**bold** and ***both***",           # markdown pairs are not multiplication
    "Note: * required fields",           # a bullet-like star
    "e^(-t/RC) decays",                  # not a simple power
    "the T_top and T_bottom tensions",   # names with underscores
    "degrees of freedom",                # the word without a number
    "plain sentence with no maths",
    "",
    # Kleene stars in formal-language questions must survive exactly
    "Give a regular expression for strings ending in 00, such as (0+1)*00",
    "Construct a DFA for the language L = (0+1)*01",
    "Find the automaton for 0*1*0",
    "Is the grammar S -> aS | b* ambiguous?",
])
def test_things_that_are_not_clear_maths_are_unchanged(text):
    assert tidy_math(text) == text


def test_it_is_safe_on_non_text():
    assert tidy_math(None) is None


def test_tidying_twice_changes_nothing_more():
    once = tidy_math("5*(0.6 + 0.5*0.8)*10 m/s^2 and sqrt(2) at 30 degrees")
    assert tidy_math(once) == once


def test_a_multi_line_question_is_tidied_line_by_line():
    text = "Find v (g = 10 m/s^2)\na) v0 * M / (M + m)\nb) sqrt(2*g*h)"
    assert tidy_math(text) == "Find v (g = 10 m/s²)\na) v0 × M / (M + m)\nb) √(2 × g × h)"
