"""Plan-first paper writing: plan, parallel batches, join. A fake model answers; no AI call, no database, no network."""
import asyncio
import json
import re
import time

import pytest

from app.services import paper_plan
from app.services.cbse_blueprints import get_blueprint
from app.services.paper_blueprint import allocate_sections, validate_paper
from app.services.paper_plan import PlanFirstUnavailable
from tests.test_paper_timing import FakeDb, job_params, physics_reply, run


# ---- a fake model: it plans from the blueprint, and writes whatever batch it is asked for -----------------------------

def blueprint_slots(sections):
    """Every question of a board blueprint as plan items."""
    items, n = [], 0
    for index, sec in enumerate(sections):
        for marks, count in sec["pattern"]:
            for _ in range(count):
                n += 1
                items.append({"number": n, "section": sec["label"], "marks": marks, "kind": "short answer",
                              "topic": f"Subtopic {n}", "bloom_level": 2, "internal_choice": False,
                              "correct_option": "b" if marks == 1 else ""})
    return items


class FakeModel:
    def __init__(self, plan_items, *, delay=0.03, break_batch=None, break_always=None, plan_text=None):
        self.plan_items, self.delay = plan_items, delay
        self.plan_text, self.break_batch, self.break_always = plan_text, break_batch, break_always
        self.prompts, self.kinds, self.running, self.peak, self.calls_for_batch = [], [], 0, 0, {}

    async def __call__(self, prompt, max_tokens, kind):
        self.prompts.append(prompt)
        self.kinds.append(kind)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.running -= 1
        usage = {"input_tokens": 6000, "output_tokens": 900, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        if kind == "plan":
            text = self.plan_text if self.plan_text is not None else json.dumps({"questions": self.plan_items})
            return text, {**usage, "output_tokens": 1800}
        wanted = [int(n) for n in re.search(r"WRITE ONLY THESE QUESTIONS: (.*)", prompt).group(1).replace("Q", "").split(", ")]
        first = wanted[0]
        self.calls_for_batch[first] = self.calls_for_batch.get(first, 0) + 1
        if self.break_always == first or (self.break_batch == first and self.calls_for_batch[first] == 1):
            return "this is not json", usage
        by_number = {item["number"]: item for item in self.plan_items}
        questions = [{"number": n, "text": f"Question {n} about subtopic {n}", "marks": by_number[n]["marks"], "bloom_level": by_number[n].get("bloom_level", 2),
                      "co_codes": [], "diagrams": [], "answer": "b", "working": "checked"} for n in wanted]
        return json.dumps({"questions": questions}), usage


def physics():
    blueprint = get_blueprint("XII-PHY")
    return blueprint["sections"], 70


def write(model, sections, total, **kwargs):
    rules = [f"{i}. {s['label']}" for i, s in enumerate(sections, 1)]
    options = {"batch_size": 5, "max_parallel": 6, **kwargs}
    return run(paper_plan.write_paper(model, "FRONT\n", "BOARD LINE", rules, sections, total, **options))


# ---- the plan ----------------------------------------------------------------------------------------------------------

def test_a_blueprint_plan_is_read_into_slots_in_order():
    sections, total = physics()
    slots = paper_plan.parse_plan(json.dumps({"questions": blueprint_slots(sections)}), sections, total)
    assert [s.number for s in slots] == list(range(1, 34))
    assert slots[0].label == "Section A" and slots[0].correct_option == "b" and slots[-1].label == "Section E"
    assert sum(s.marks for s in slots) == 70


def bad_plan(change):
    sections, total = physics()
    items = blueprint_slots(sections)
    change(items)
    with pytest.raises(PlanFirstUnavailable):
        paper_plan.parse_plan(json.dumps({"questions": items}), sections, total)


def test_a_plan_that_breaks_the_blueprint_is_refused():
    bad_plan(lambda items: items[0].update(number=5))                              # numbering not consecutive
    bad_plan(lambda items: items.pop())                                            # a question missing
    bad_plan(lambda items: items[1].update(marks=2))                               # marks differ from the pattern
    bad_plan(lambda items: items[0].update(section="Section Z"))                   # unknown section
    bad_plan(lambda items: (items[0].update(section="Section B"), items[1].update(section="Section A")))   # out of order
    bad_plan(lambda items: items[0].update(marks=0))
    bad_plan(lambda items: items[0].update(marks="1"))
    bad_plan(lambda items: items.append({**items[-1], "number": 34}))               # one too many


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"error":"conflict"}', '{"questions":[]}', '{"questions":["x"]}'])
def test_an_unusable_plan_is_refused(text):
    sections, total = physics()
    with pytest.raises(PlanFirstUnavailable):
        paper_plan.parse_plan(text, sections, total)


def test_a_bloom_level_outside_the_section_is_set_to_the_sections_level():
    sections = [{"label": "Section A", "bloomLevels": [3, 4], "percent": 100}]
    items = [{"number": 1, "section": "Section A", "marks": 5, "bloom_level": 1, "topic": "t"}]
    (slot,) = paper_plan.parse_plan(json.dumps({"questions": items}), sections, 5)
    assert slot.bloom == 3


def test_a_custom_paper_plan_must_match_each_sections_marks():
    sections = [{"label": "Section A", "bloomLevels": [], "percent": 30}, {"label": "Section B", "bloomLevels": [], "percent": 70}]
    items = [{"number": 1, "section": "Section A", "marks": 3, "topic": "a"},
             {"number": 2, "section": "Section A", "marks": 3, "topic": "b"},
             {"number": 3, "section": "Section B", "marks": 7, "topic": "c"},
             {"number": 4, "section": "Section B", "marks": 7, "topic": "d"}]
    slots = paper_plan.parse_plan(json.dumps({"questions": items}), sections, 20)          # 6 and 14
    assert [s.marks for s in slots] == [3, 3, 7, 7]
    items[0]["marks"] = 4                                                                  # Section A would be 7, not 6
    with pytest.raises(PlanFirstUnavailable):
        paper_plan.parse_plan(json.dumps({"questions": items}), sections, 20)


# ---- the batches ---------------------------------------------------------------------------------------------------------

def test_batches_cover_every_question_once_stay_in_a_section_and_are_even():
    sections, total = physics()
    slots = paper_plan.parse_plan(json.dumps({"questions": blueprint_slots(sections)}), sections, total)
    batches = paper_plan.make_batches(slots, 5)
    assert [s.number for batch in batches for s in batch] == list(range(1, 34))
    assert all(len({s.section for s in batch}) == 1 and 1 <= len(batch) <= 5 for batch in batches)
    assert [len(b) for b in batches] == [4, 4, 4, 4, 5, 4, 3, 2, 3]
    assert [len(b) for b in paper_plan.make_batches(slots, 1)] == [1] * 33
    assert len(paper_plan.make_batches(slots, 100)) == 5                                   # one batch per section


def test_a_batch_must_hold_exactly_the_questions_asked_for():
    sections, total = physics()
    slots = paper_plan.parse_plan(json.dumps({"questions": blueprint_slots(sections)}), sections, total)
    batch = slots[:2]

    def reply(*questions):
        return json.dumps({"questions": list(questions)})

    one = {"number": 1, "text": "q", "marks": 1}
    two = {"number": 2, "text": "q", "marks": 1}
    assert [q["number"] for q in paper_plan.read_batch(reply(two, one), batch)] == [1, 2]      # returned in plan order
    for text in (reply(one), reply(one, two, {"number": 3, "text": "q", "marks": 1}), reply(one, {**two, "number": 4}),
                 reply(one, one), reply(one, {**two, "marks": 2}), "oops", '{"error":"x"}', "[]"):
        with pytest.raises(PlanFirstUnavailable):
            paper_plan.read_batch(text, batch)


# ---- writing the whole paper -----------------------------------------------------------------------------------------------

def test_a_planned_paper_is_joined_in_order_and_passes_the_unchanged_validator():
    sections, total = physics()
    model = FakeModel(blueprint_slots(sections))
    outcome = write(model, sections, total)
    data = json.loads(outcome.text)
    assert [s["label"] for s in data["sections"]] == [s["label"] for s in sections]
    assert [q["number"] for s in data["sections"] for q in s["questions"]] == list(range(1, 34))
    assert all(s["attempt_count"] == len(s["questions"]) for s in data["sections"])
    paper_text, blueprint = validate_paper(outcome.text, sections, total, [])               # the same checks as the single call
    assert blueprint["required_marks"] == 70 and len(blueprint["answer_key"]) == 33
    assert model.kinds.count("plan") == 1 and model.kinds.count("batch") == 9


def test_the_batches_run_at_the_same_time_and_the_limit_is_respected():
    sections, total = physics()
    fast = FakeModel(blueprint_slots(sections), delay=0.1)
    began = time.perf_counter()
    outcome = write(fast, sections, total, max_parallel=9)
    elapsed = time.perf_counter() - began
    assert fast.peak == 9 and elapsed < 0.1 * (1 + 9) * 0.6                                  # far less than one after another
    assert outcome.wall_seconds >= 0.2

    limited = FakeModel(blueprint_slots(sections), delay=0.03)
    write(limited, sections, total, max_parallel=2)
    assert limited.peak == 2


def test_each_batch_prompt_shows_the_whole_plan_but_asks_for_only_its_questions():
    sections, total = physics()
    model = FakeModel(blueprint_slots(sections))
    write(model, sections, total)
    plan_prompt = next(p for p, k in zip(model.prompts, model.kinds) if k == "plan")
    assert plan_prompt.startswith("FRONT\n") and "PLAN THE PAPER" in plan_prompt and "BOARD LINE" in plan_prompt
    batch_prompts = [p for p, k in zip(model.prompts, model.kinds) if k == "batch"]
    asked = []
    for prompt in batch_prompts:
        assert prompt.startswith("FRONT\n") and "BOARD LINE" in prompt
        assert all(f"Q{n} | " in prompt for n in range(1, 34))                               # every question is in the plan shown
        asked += [int(n) for n in re.search(r"WRITE ONLY THESE QUESTIONS: (.*)", prompt).group(1).replace("Q", "").split(", ")]
        assert "topic: Subtopic 1 |" in prompt
    assert sorted(asked) == list(range(1, 34)) and len(asked) == len(set(asked))
    first = batch_prompts[0]
    assert "1. Section A" in first and "2. Section B" not in first                             # only the sections it writes


def test_a_batch_that_fails_once_is_retried_and_the_paper_is_still_complete():
    sections, total = physics()
    model = FakeModel(blueprint_slots(sections), break_batch=5)
    outcome = write(model, sections, total)
    assert outcome.retries == 1 and model.calls_for_batch[5] == 2
    assert next(b for b in outcome.batches if b["first"] == 5)["attempts"] == 2
    validate_paper(outcome.text, sections, total, [])


def test_a_batch_that_fails_twice_gives_up_so_the_paper_is_written_the_old_way():
    sections, total = physics()
    model = FakeModel(blueprint_slots(sections), break_always=5)
    with pytest.raises(PlanFirstUnavailable):
        write(model, sections, total)


def test_a_bad_plan_stops_before_any_batch_is_paid_for():
    sections, total = physics()
    model = FakeModel(blueprint_slots(sections), plan_text="no plan today")
    with pytest.raises(PlanFirstUnavailable):
        write(model, sections, total)
    assert model.kinds == ["plan"]


def test_a_custom_paper_is_planned_and_written_the_same_way():
    sections = [{"label": "Section A", "bloomLevels": [1, 2], "percent": 30}, {"label": "Section B", "bloomLevels": [3, 4], "percent": 40},
                {"label": "Section C", "bloomLevels": [5, 6], "percent": 30}]
    marks = allocate_sections(sections, 50)                                                  # 15, 20, 15
    items, n = [], 0
    for sec, total in zip(sections, marks):
        for _ in range(total // 5):
            n += 1
            items.append({"number": n, "section": sec["label"], "marks": 5, "kind": "long answer", "topic": f"t{n}",
                          "bloom_level": sec["bloomLevels"][0]})
    outcome = write(FakeModel(items), sections, 50, batch_size=2)
    _, blueprint = validate_paper(outcome.text, sections, 50, [])
    assert blueprint["required_marks"] == 50


def test_stopping_the_job_stops_the_batches():
    sections, total = physics()
    model = FakeModel(blueprint_slots(sections), delay=0.2)

    async def go():
        rules = ["r"] * 5
        task = asyncio.ensure_future(paper_plan.write_paper(model, "F", "B", rules, sections, total, batch_size=5, max_parallel=9))
        await asyncio.sleep(0.35)                                                            # the plan is done, the batches are running
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        return model.running

    assert run(go()) == 0


def test_the_total_usage_adds_the_plan_and_every_batch():
    sections, total = physics()
    outcome = write(FakeModel(blueprint_slots(sections)), sections, total)
    usage = paper_plan.total_usage(outcome)
    assert usage["input_tokens"] == 10 * 6000 and usage["output_tokens"] == 1800 + 9 * 900
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]


def test_when_plan_first_applies():
    sections, _ = physics()
    ok = dict(enabled=True, min_marks=40, choices_requested=False)
    assert paper_plan.applies(sections, 70, "x", **ok)
    assert not paper_plan.applies(sections, 70, "x", **{**ok, "enabled": False})                 # the switch is off
    assert not paper_plan.applies(sections, 30, "x", **ok)                                       # a small paper stays one call
    assert not paper_plan.applies(sections, 70, "x", **{**ok, "choices_requested": True})
    assert not paper_plan.applies([], 70, "x", **ok)
    assert not paper_plan.applies(sections, True, "x", **ok)


# ---- inside the whole job ----------------------------------------------------------------------------------------------------

def patch_job(monkeypatch, model, single_reply, *, plan_first=True, min_marks=40):
    from app.api.routers import question_paper as qp
    patches, db, seen = [], FakeDb(), []

    async def claim(prefix, job_id):
        return True

    async def update(prefix, job_id, patch):
        patches.append(patch)

    async def tracked(database, function, *args, feature=None, **kwargs):
        seen.append(feature)
        if feature in ("qp_plan",) or (feature == "qp_generation" and args[0].find("WRITE ONLY THESE QUESTIONS") >= 0):
            return await model(args[0], args[2], "plan" if feature == "qp_plan" else "batch")
        if feature == "qp_generation":
            return single_reply, {"input_tokens": 3700, "output_tokens": 21000, "total_tokens": 24700}
        return '{"checks": []}', {"input_tokens": 4200, "output_tokens": 3000, "total_tokens": 7200}

    async def release(*args, **kwargs):
        return None

    monkeypatch.setattr(qp, "get_database", lambda: db)
    monkeypatch.setattr(qp, "claim_job_execution", claim)
    monkeypatch.setattr(qp, "update_job", update)
    monkeypatch.setattr(qp, "tracked_faculty_call", tracked)
    monkeypatch.setattr(qp, "release_submission", release)
    monkeypatch.setattr(qp, "upload_file_to_imagekit", lambda *a, **k: {"url": "https://x/y.docx", "file_id": "f"})
    monkeypatch.setattr(qp.settings, "PAPER_PLAN_FIRST", plan_first)
    monkeypatch.setattr(qp.settings, "PAPER_PLAN_MIN_MARKS", min_marks)
    return qp, db, patches, seen


def test_a_whole_job_with_plan_first_delivers_the_paper_and_records_the_plan_and_batches(monkeypatch):
    single, blueprint = physics_reply()
    model = FakeModel(blueprint_slots(blueprint["sections"]))
    qp, db, patches, seen = patch_job(monkeypatch, model, single)
    run(qp._run_generation_job("job1", job_params(blueprint), {}))
    (doc,) = db["paperGenerationTimings"].docs
    assert doc["outcome"] == "completed" and doc["mode"] == "plan_first" and doc["question_count"] == 33
    assert seen.count("qp_plan") == 1 and seen.count("qp_generation") == 9 and seen.count("qp_answer_check") == 1
    assert set(doc["stages"]) >= {"plan_and_write", "plan", "write_batches", "validate_paper", "check_answers", "build_docx", "upload"}
    assert "generate_paper" not in doc["stages"]
    batches = doc["calls"]["qp_generation_batches"]
    assert batches["batches"] == 9 and batches["output_tokens"] == 9 * 900 and batches["slowest_seconds"] > 0
    assert doc["calls"]["qp_plan"]["output_tokens"] == 1800
    (done,) = [p for p in patches if p.get("status") == "completed"]
    assert done["answerCheck"] == "done" and done["tokenUsage"]["output_tokens"] == 1800 + 9 * 900
    assert "Question 33 about subtopic 33" in done["paperText"] and done["filename"].endswith(".docx")


def test_the_switch_is_off_by_default_so_the_paper_is_written_in_one_call(monkeypatch):
    single, blueprint = physics_reply()
    model = FakeModel(blueprint_slots(blueprint["sections"]))
    qp, db, patches, seen = patch_job(monkeypatch, model, single, plan_first=False)
    run(qp._run_generation_job("job1", job_params(blueprint), {}))
    (doc,) = db["paperGenerationTimings"].docs
    assert doc["mode"] == "single_call" and "generate_paper" in doc["stages"] and "qp_plan" not in seen
    assert model.prompts == []


def test_a_small_paper_stays_one_call_even_with_the_switch_on(monkeypatch):
    single, blueprint = physics_reply()
    model = FakeModel(blueprint_slots(blueprint["sections"]))
    qp, db, patches, seen = patch_job(monkeypatch, model, single, min_marks=100)
    run(qp._run_generation_job("job1", job_params(blueprint), {}))
    assert db["paperGenerationTimings"].docs[0]["mode"] == "single_call" and model.prompts == []


def test_a_failed_plan_falls_back_to_one_call_and_the_paper_is_delivered(monkeypatch):
    single, blueprint = physics_reply()
    model = FakeModel(blueprint_slots(blueprint["sections"]), plan_text="garbage")
    qp, db, patches, seen = patch_job(monkeypatch, model, single)
    run(qp._run_generation_job("job1", job_params(blueprint), {}))
    (doc,) = db["paperGenerationTimings"].docs
    assert doc["outcome"] == "completed" and doc["mode"] == "single_call" and "plan_first_fallback" in doc
    assert "generate_paper" in doc["stages"] and seen.count("qp_generation") == 1
    assert any(p.get("status") == "completed" for p in patches)


def test_optional_choices_use_the_single_call(monkeypatch):
    single, blueprint = physics_reply()
    params = job_params(blueprint)
    params["prompt"] = "Ray optics, with optional questions in the long answers"
    model = FakeModel(blueprint_slots(blueprint["sections"]))
    qp, db, patches, seen = patch_job(monkeypatch, model, single)
    run(qp._run_generation_job("job1", params, {}))
    assert db["paperGenerationTimings"].docs[0]["mode"] == "single_call" and model.prompts == []


# ---- what the reply held: text or reasoning (counts only) --------------------------------------------------------------------

class Block:
    def __init__(self, type, **fields):
        self.type = type
        for key, value in fields.items():
            setattr(self, key, value)


class Message:
    def __init__(self, content, stop_reason="end_turn"):
        self.content, self.stop_reason = content, stop_reason


def test_the_content_breakdown_tells_text_from_reasoning_without_keeping_words():
    from app.services.claude import content_breakdown
    message = Message([Block("thinking", thinking="x" * 120), Block("text", text="y" * 30)])
    assert content_breakdown(message) == {"blocks": {"thinking": 1, "text": 1}, "text_chars": 30, "thinking_chars": 120,
                                         "thinking_tokens": None, "stop_reason": "end_turn"}
    assert content_breakdown(Message([Block("text", text="abc")]))["blocks"] == {"text": 1}
    assert content_breakdown(Message([Block("redacted_thinking", data="zz")]))["blocks"] == {"redacted_thinking": 1}
    assert content_breakdown(object())["blocks"] == {}                                        # an odd reply never raises
    assert "x" * 5 not in json.dumps(content_breakdown(message))


def test_the_batch_summary_reports_wall_time_the_slowest_batch_tokens_and_cost():
    from app.services.paper_timing import JobTimings
    sections, total = physics()
    outcome = write(FakeModel(blueprint_slots(sections)), sections, total)
    timings = JobTimings("j")
    timings.note_batches(outcome, model="claude-sonnet-5-5")
    batches, plan = timings.calls["qp_generation_batches"], timings.calls["qp_plan"]
    assert batches["batches"] == 9 and batches["retries"] == 0 and batches["output_tokens"] == 9 * 900
    assert batches["slowest_seconds"] >= batches["fastest_seconds"] > 0 and batches["cost_usd"] > 0
    assert len(batches["per_batch"]) == 9 and batches["per_batch"][0]["questions"] == "1-4"
    assert plan["output_tokens"] == 1800
