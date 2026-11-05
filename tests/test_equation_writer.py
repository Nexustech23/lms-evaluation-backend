"""Words -> LaTeX for the equation box: reply cleaning, the endpoint, its limits and its accounting."""
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.api.routers.question_paper as qp
from app.api.deps import get_current_identity
from app.db.mongodb import get_database
from app.models.ai_usage_event import FACULTY_FEATURES, FEATURE_LABELS, Feature
from app.services import equation_writer
from app.services.equation_writer import clean_latex, parse_equation_reply, write_equation
from app.utils.ai_pricing import _PRICES


# ---------------- cleaning the reply ----------------

@pytest.mark.parametrize("raw,expected", [
    (r"E = \frac{1}{2} m v^2", r"E = \frac{1}{2} m v^2"),
    (r"$E = mc^2$", "E = mc^2"),
    (r"$$E = mc^2$$", "E = mc^2"),
    (r"\[E = mc^2\]", "E = mc^2"),
    (r"\(E = mc^2\)", "E = mc^2"),
    (r"\begin{equation} E = mc^2 \end{equation}", "E = mc^2"),
    ("  \n E = mc^2 \n ", "E = mc^2"),
    ("E = mc^2\x00\x07", "E = mc^2"),
    (r"\ce{2H2 + O2 -> 2H2O}", r"\ce{2H2 + O2 -> 2H2O}"),
])
def test_the_equation_is_returned_without_delimiters(raw, expected):
    assert clean_latex(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, 5, ["x"], "x" * 2001, "$$"])
def test_nothing_usable_returns_nothing(raw):
    assert clean_latex(raw) == ""


def test_a_reply_inside_a_code_block_is_read():
    reply = "```json\n" + json.dumps({"latex": r"F = ma", "note": ""}) + "\n```"
    assert parse_equation_reply(reply) == {"latex": "F = ma", "note": ""}


def test_a_refusal_reply_carries_the_reason():
    assert parse_equation_reply(json.dumps({"latex": "", "note": "That is not an equation"})) == {"latex": "", "note": "That is not an equation"}


@pytest.mark.parametrize("reply", ["not json", "[1, 2]", ""])
def test_an_unreadable_reply_is_an_error(reply):
    with pytest.raises((ValueError, TypeError)):
        parse_equation_reply(reply)


def test_the_model_sees_the_description_and_a_failed_attempt_but_no_paper(monkeypatch):
    seen = {}

    def fake(prompt, model, max_tokens, system=None, effort=None):
        seen.update(prompt=json.loads(prompt), model=model, max_tokens=max_tokens, system=system, effort=effort)
        return "{}", {}

    monkeypatch.setattr(equation_writer, "generate_text", fake)
    write_equation("kinetic energy", r"E_k = \frac{1}{2", "Missing close brace", equation_writer.EQUATION_MODEL, 400)
    assert seen["prompt"] == {"description": "kinetic energy", "previous_latex": r"E_k = \frac{1}{2", "previous_error": "Missing close brace"}
    assert seen["model"] == "claude-haiku-4-5-20251001" and seen["max_tokens"] == 400 and seen["effort"] is None
    write_equation("kinetic energy", None, None, equation_writer.EQUATION_MODEL, 400)
    assert seen["prompt"] == {"description": "kinetic energy"}


def test_the_small_model_is_priced_and_the_feature_is_a_faculty_feature():
    assert equation_writer.EQUATION_MODEL in _PRICES
    assert Feature.EQUATION_ASSIST in FACULTY_FEATURES and Feature.EQUATION_ASSIST in FEATURE_LABELS


# ---------------- the endpoint ----------------

class Harness:
    def __init__(self, monkeypatch, reply=None, budget=None, error=None):
        self.calls = []

        async def faculty(identity, db):
            return {"_id": "user-1"}, "faculty-1"

        async def allowed(db, faculty_id, providers):
            self.budget_check = (faculty_id, providers)
            return budget or {"allowed": True, "message": None, "warnings": []}

        async def tracked(db, function, *args, **kwargs):
            self.calls.append((function, args, kwargs))
            if error:
                raise error
            return reply, {}

        monkeypatch.setattr(qp, "_require_faculty", faculty)
        monkeypatch.setattr(qp, "check_institute_token_budget", allowed)
        monkeypatch.setattr(qp, "tracked_faculty_call", tracked)
        app = FastAPI()
        app.include_router(qp.router)
        app.dependency_overrides[get_current_identity] = lambda: {"user_id": "u", "role": 3}
        app.dependency_overrides[get_database] = lambda: object()
        app.dependency_overrides[qp.ai_rate_limit] = lambda: None
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def post(self, body):
        async with self.client as c:
            return await c.post("/question-paper/equation/from-text", json=body)


async def test_a_description_returns_the_equation(monkeypatch):
    harness = Harness(monkeypatch, reply=json.dumps({"latex": r"$E_k = \frac{1}{2} m v^2$", "note": ""}))
    response = await harness.post({"description": "kinetic energy is half mass times velocity squared"})
    assert response.status_code == 200 and response.json() == {"latex": r"E_k = \frac{1}{2} m v^2", "note": ""}
    function, args, kwargs = harness.calls[0]
    assert function is write_equation and args[0] == "kinetic energy is half mass times velocity squared"
    assert kwargs["feature"] == Feature.EQUATION_ASSIST and kwargs["model"] == "claude-haiku-4-5-20251001"
    assert harness.budget_check == ("faculty-1", ["claude"])


async def test_the_failed_attempt_is_passed_back_for_the_one_retry(monkeypatch):
    harness = Harness(monkeypatch, reply=json.dumps({"latex": r"\frac{a}{b}", "note": ""}))
    response = await harness.post({"description": "a over b", "previousLatex": r"\frac{a}{", "previousError": "Missing close brace"})
    assert response.status_code == 200
    _, args, _ = harness.calls[0]
    assert args[1] == r"\frac{a}{" and args[2] == "Missing close brace"


async def test_a_description_that_is_not_an_equation_comes_back_empty_with_a_reason(monkeypatch):
    harness = Harness(monkeypatch, reply=json.dumps({"latex": "", "note": "That is not an equation"}))
    response = await harness.post({"description": "write me a poem"})
    assert response.status_code == 200 and response.json() == {"latex": "", "note": "That is not an equation"}


@pytest.mark.parametrize("body", [{}, {"description": ""}, {"description": "ab"}, {"description": "x" * 501},
                                  {"description": "ok text", "previousLatex": "x" * 2001}, {"description": "ok text", "previousError": "x" * 301}])
async def test_bad_requests_are_refused_without_calling_the_ai(monkeypatch, body):
    harness = Harness(monkeypatch, reply="{}")
    assert (await harness.post(body)).status_code == 422 and harness.calls == []


async def test_an_exhausted_institute_budget_stops_the_call(monkeypatch):
    harness = Harness(monkeypatch, reply="{}", budget={"allowed": False, "message": "AI budget used up", "warnings": []})
    response = await harness.post({"description": "kinetic energy"})
    assert response.status_code == 402 and response.json() == {"error": "AI budget used up"} and harness.calls == []


@pytest.mark.parametrize("failure", [dict(error=RuntimeError("provider down")), dict(reply="not json")])
async def test_a_failure_is_a_friendly_error_not_a_crash(monkeypatch, failure):
    harness = Harness(monkeypatch, **failure)
    response = await harness.post({"description": "kinetic energy"})
    assert response.status_code == 502 and "could not write" in response.json()["error"]


def test_the_endpoint_is_rate_limited_like_the_other_ai_calls():
    route = next(r for r in qp.router.routes if getattr(r, "path", "") == "/question-paper/equation/from-text")
    assert any(dep.call is qp.ai_rate_limit for dep in route.dependant.dependencies)
