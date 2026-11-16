"""Timing and size records for paper generation. Records only: no AI call, no database, no network."""
import asyncio
import json
import logging
import time

import pytest

from app.services import faculty_ai_usage
from app.services.cbse_blueprints import get_blueprint
from app.services.paper_timing import (
    JobTimings, build_summary, composition_of, delivery_lag_seconds, log_delivery, record_job_timings,
)


# ---- stage durations

def test_a_stage_adds_up_and_the_blocks_exception_passes_through():
    timings = JobTimings("job1")
    with timings.stage("generate"):
        time.sleep(0.02)
    with timings.stage("generate"):
        time.sleep(0.02)
    assert timings.stages["generate"] >= 0.04
    with pytest.raises(ValueError):
        with timings.stage("validate"):
            raise ValueError("boom")
    assert "validate" in timings.stages                       # a failing stage is still timed
    assert timings.total_seconds() >= timings.stages["generate"]


# ---- tokens, speed and cost of one call

def test_a_call_records_tokens_speed_cap_and_cost():
    timings = JobTimings("job1")
    timings.note_call("qp_generation", {"input_tokens": 6840, "output_tokens": 44428}, model="claude-sonnet-5-5",
                      seconds=296.96, max_tokens=50000)
    call = timings.calls["qp_generation"]
    assert call["output_tokens"] == 44428 and call["input_tokens"] == 6840
    assert call["tokens_per_second"] == 149.6 and call["cap_percent"] == 88.9
    assert call["cost_usd"] == pytest.approx(6840 / 1e6 * 2 + 44428 / 1e6 * 10, abs=1e-4)      # $2 in / $10 out per million


@pytest.mark.parametrize("usage", [None, {}, "x", {"input_tokens": "a", "output_tokens": None}, {"output_tokens": -5}])
def test_a_call_with_unusable_usage_is_noted_without_error(usage):
    timings = JobTimings("job1")
    timings.note_call("qp_generation", usage, model="claude-sonnet-5-5", seconds=10, max_tokens=100)
    assert "tokens_per_second" not in timings.calls["qp_generation"]


def test_zero_seconds_does_not_divide_by_zero():
    timings = JobTimings("job1")
    timings.note_call("x", {"input_tokens": 1, "output_tokens": 5}, model="claude-sonnet-5-5", seconds=0)
    assert "tokens_per_second" not in timings.calls["x"]


# ---- where the tokens are

def question(number, text="Q" * 100, answer="b", working="w" * 50, diagrams=None, marks=1):
    return {"number": number, "text": text, "marks": marks, "bloom_level": 2, "co_codes": [], "diagrams": diagrams or [],
            "answer": answer, "working": working}


def paper(*sections):
    return json.dumps({"sections": [{"label": f"Section {chr(65 + i)}", "attempt_count": len(qs), "questions": qs}
                                    for i, qs in enumerate(sections)]})


def test_the_composition_splits_a_reply_by_part():
    figure = {"type": "geometry_figure", "title": "t", "points": {"A": [0, 0], "B": [1, 0]}}
    raw = paper([question(1), question(2, working="w" * 400)], [question(3, diagrams=[figure], marks=5)])
    got = composition_of(raw, output_tokens=len(raw) // 3)
    assert got["parsed"] is True and got["question_count"] == 3 and got["diagram_count"] == 1
    assert got["question_text_chars"] == 300 and got["working_chars"] == 50 + 400 + 50
    assert got["diagram_chars"] > 40 and got["answer_chars"] == 3
    assert got["largest_questions"][0]["number"] in (2, 3)
    assert got["largest_questions"][0]["biggest_part"] in {"working", "diagram", "question_text"}
    assert [s["questions"] for s in got["sections"]] == [2, 1]
    assert got["chars_per_token"] == pytest.approx(3.0, abs=0.1)
    total_estimated = sum(got["estimated_tokens"].values())
    assert total_estimated == pytest.approx(got["question_json_chars"] / got["chars_per_token"], rel=0.02)
    assert got["structure_chars"] + got["question_json_chars"] == got["raw_chars"]


def test_the_composition_names_the_part_that_holds_the_tokens():
    raw = paper([question(i, working="long working " * 200) for i in range(1, 6)])
    got = composition_of(raw, output_tokens=2000)
    assert got["working_chars"] > 5 * got["question_text_chars"]
    assert got["estimated_tokens"]["working"] > got["estimated_tokens"]["question_text"]
    assert got["estimated_tokens_per_question"] > 100


def test_a_fenced_or_wrapped_reply_is_still_measured():
    raw = "```json\n" + paper([question(1)]) + "\n```"
    assert composition_of(raw)["parsed"] is True
    assert composition_of("Here is the paper: " + paper([question(1)]) + " done")["question_count"] == 1


@pytest.mark.parametrize("raw", [None, "", "not json at all", 5, "[1, 2]", '{"sections": "x"}', '{"error": "conflict"}'])
def test_unreadable_replies_never_raise(raw):
    got = composition_of(raw, output_tokens=10)
    assert got.get("parsed") is False


def test_odd_questions_do_not_break_the_measurement():
    raw = json.dumps({"sections": [{"label": 7, "questions": [None, "x", {"number": 1}, {"number": 2, "text": None, "diagrams": "oops"}]},
                                   "not a section"]})
    got = composition_of(raw)
    assert got["parsed"] is True and got["question_count"] == 2


def test_the_reply_words_are_not_kept():
    raw = paper([question(1, text="SECRET QUESTION WORDING " * 5)])
    assert "SECRET" not in json.dumps(composition_of(raw, output_tokens=100))


# ---- the job summary

def test_the_summary_carries_stages_calls_and_the_job_facts():
    timings = JobTimings("job9")
    with timings.stage("generate_paper"):
        pass
    timings.note_call("qp_generation", {"input_tokens": 10, "output_tokens": 100}, model="claude-sonnet-5-5", seconds=1.0, max_tokens=1000)
    timings.composition = {"question_count": 33}
    timings.facts["prompt_chars"] = 15000
    params = {"institute_type": "school", "total_marks": 70, "sections": [1, 2, 3], "check_answers": True,
              "board": {"key": "XII-PHY", "customised": False}}
    summary = build_summary(timings, params, "completed")
    assert summary["job_id"] == "job9" and summary["outcome"] == "completed" and summary["question_count"] == 33
    assert summary["board_key"] == "XII-PHY" and summary["section_count"] == 3 and summary["prompt_chars"] == 15000
    assert "generate_paper" in summary["stages"] and "qp_generation" in summary["calls"]
    assert build_summary(timings, params, "failed", "RuntimeError")["error_type"] == "RuntimeError"
    assert build_summary(timings, None, "failed")["board_key"] is None                      # no params is fine


class FakeCollection:
    def __init__(self, fail=False):
        self.docs, self.fail = [], fail

    async def insert_one(self, doc):
        if self.fail:
            raise RuntimeError("db down")
        self.docs.append(doc)


class FakeDb:
    def __init__(self, fail=False):
        self.collections = {}
        self.fail = fail

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection(self.fail))


def run(coro):
    return asyncio.run(coro)


def test_the_summary_is_logged_and_saved(caplog):
    caplog.set_level(logging.INFO, logger="lms.diagnostics")
    db, timings = FakeDb(), JobTimings("job1")
    summary = run(record_job_timings(db, timings, {"user_id": "u1", "faculty_id": "f1", "total_marks": 70}, "completed"))
    (doc,) = db["paperGenerationTimings"].docs
    assert doc["job_id"] == "job1" and doc["user_id"] == "u1" and doc["faculty_id"] == "f1" and doc["outcome"] == "completed"
    assert doc["created_at"] is not None and summary["outcome"] == "completed"
    assert any(getattr(r, "diagnostic_event", "") == "qp.job.summary" for r in caplog.records)


def test_a_failing_database_never_breaks_the_job(caplog):
    assert run(record_job_timings(FakeDb(fail=True), JobTimings("job1"), {}, "completed")) is None
    assert run(record_job_timings(object(), JobTimings("job1"), {}, "completed")) is None


# ---- how long the result waited for the page

def test_delivery_lag():
    assert delivery_lag_seconds({"finished_at": 1000.0}, now=1007.5) == 7.5
    assert delivery_lag_seconds({}, now=5) is None and delivery_lag_seconds(None) is None
    assert delivery_lag_seconds({"finished_at": "bad"}) is None
    assert delivery_lag_seconds({"finished_at": 2000.0}, now=1000.0) is None             # a clock skew is not a negative lag


def test_delivery_is_logged_only_when_known(caplog):
    caplog.set_level(logging.INFO, logger="lms.diagnostics")
    assert log_delivery("job1", {"finished_at": 100.0}, now=112.0) == 12.0
    assert log_delivery("job1", {}, now=112.0) is None
    events = [r for r in caplog.records if getattr(r, "diagnostic_event", "") == "qp.result.delivered"]
    assert len(events) == 1 and events[0].diagnostic_fields["lag_seconds"] == 12.0


# ---- the provider call's own time, kept on its usage record

class FakeEvents:
    def __init__(self, fail=False):
        self.updates, self.fail = [], fail

    async def update_one(self, query, update):
        if self.fail:
            raise RuntimeError("db down")
        self.updates.append((query, update))


class EventsDb:
    def __init__(self, fail=False):
        self.events = FakeEvents(fail)

    def __getitem__(self, name):
        assert name == "aiUsageEvents"
        return self.events


def test_the_call_time_and_speed_are_kept_on_the_usage_record():
    db = EventsDb()
    run(faculty_ai_usage._record_call_timing(db, "ev1", time.perf_counter() - 2.0, {"output_tokens": 300}, "qp_generation",
                                             "claude-sonnet-5-5", "job1", "completed"))
    ((query, update),) = db.events.updates
    fields = update["$set"]
    assert query == {"_id": "ev1"} and 1900 <= fields["duration_ms"] <= 2600
    assert 100 <= fields["tokens_per_second"] <= 160 and fields["ended_at"] is not None


def test_a_timing_failure_is_swallowed():
    run(faculty_ai_usage._record_call_timing(EventsDb(fail=True), "ev1", time.perf_counter(), {}, "f", "m", "j", "completed"))
    run(faculty_ai_usage._record_call_timing(object(), "ev1", time.perf_counter(), None, "f", "m", "j", "completed"))


def patch_accounting(monkeypatch):
    recorded = []

    async def fake_record(db, **kwargs):
        recorded.append(kwargs)

    async def fake_unknown(db, event_id, status):
        recorded.append({"unknown": status})

    monkeypatch.setattr(faculty_ai_usage, "record_ai_usage", fake_record)
    monkeypatch.setattr(faculty_ai_usage, "_mark_unknown", fake_unknown)
    return recorded


def test_a_finished_call_gets_its_timing(monkeypatch):
    patch_accounting(monkeypatch)
    db = EventsDb()
    usage = {"input_tokens": 10, "output_tokens": 500, "total_tokens": 510}
    result = run(faculty_ai_usage._execute_call(db, lambda *a: ("text", usage), (), "u1", "claude", "claude-sonnet-5-5",
                                                "qp_generation", "job1", "inst1", "ev1"))
    assert result == ("text", usage)
    assert len(db.events.updates) == 1 and "duration_ms" in db.events.updates[0][1]["$set"]


def test_a_failed_call_gets_its_timing_and_still_raises(monkeypatch):
    patch_accounting(monkeypatch)
    db = EventsDb()

    def failing(*args):
        error = RuntimeError("provider down")
        error.usage = {"input_tokens": 10, "output_tokens": 0, "total_tokens": 10}
        raise error

    with pytest.raises(RuntimeError, match="provider down"):
        run(faculty_ai_usage._execute_call(db, failing, (), "u1", "claude", "claude-sonnet-5-5", "qp_generation", "job1", "inst1", "ev1"))
    assert len(db.events.updates) == 1


def test_a_database_failure_while_timing_does_not_change_the_calls_result(monkeypatch):
    patch_accounting(monkeypatch)
    usage = {"input_tokens": 10, "output_tokens": 500, "total_tokens": 510}
    result = run(faculty_ai_usage._execute_call(EventsDb(fail=True), lambda *a: ("text", usage), (), "u1", "claude",
                                                "claude-sonnet-5-5", "qp_generation", "job1", "inst1", "ev1"))
    assert result == ("text", usage)


# ---- the whole job, with every outside service faked

def physics_reply():
    blueprint = get_blueprint("XII-PHY")
    n, sections = 0, []
    for sec in blueprint["sections"]:
        questions = []
        for marks, count in sec["pattern"]:
            for _ in range(count):
                n += 1
                questions.append({"number": n, "text": f"Question {n} about topic {n}", "marks": marks, "bloom_level": 2,
                                  "co_codes": [], "diagrams": [], "answer": "b", "working": "w" * 80})
        sections.append({"label": sec["label"], "attempt_count": len(questions), "questions": questions})
    return json.dumps({"sections": sections}), blueprint


def job_params(blueprint, check=True):
    return {"prompt": "Ray optics", "institute_name": "School", "department_name": "", "subject_name": "Physics",
            "exam_type": "Annual Examination", "semester": "Class 12", "academic_year": "2026-27", "total_marks": 70,
            "duration": "3 Hours", "sections": blueprint["sections"], "co_list": [], "faculty_id": "f1", "user_id": "u1",
            "check_answers": check, "institute_type": "school",
            "board": {"key": "XII-PHY", "code": "042", "class": 12, "subject": "Physics", "customised": False}}


def patch_job(monkeypatch, reply, check_reply='{"checks": []}', fail_generate=False, fail_db=False):
    from app.api.routers import question_paper as qp
    patches, db = [], FakeDb(fail=fail_db)

    async def claim(prefix, job_id):
        return True

    async def update(prefix, job_id, patch):
        patches.append(patch)

    async def tracked(database, function, *args, feature=None, **kwargs):
        if feature == "qp_generation":
            if fail_generate:
                raise RuntimeError("provider down")
            time.sleep(0.02)
            return reply, {"input_tokens": 3700, "output_tokens": 21000, "total_tokens": 24700}
        time.sleep(0.01)
        return check_reply, {"input_tokens": 4200, "output_tokens": 3000, "total_tokens": 7200}

    async def release(*args, **kwargs):
        return None

    monkeypatch.setattr(qp, "get_database", lambda: db)
    monkeypatch.setattr(qp, "claim_job_execution", claim)
    monkeypatch.setattr(qp, "update_job", update)
    monkeypatch.setattr(qp, "tracked_faculty_call", tracked)
    monkeypatch.setattr(qp, "release_submission", release)
    monkeypatch.setattr(qp, "upload_file_to_imagekit", lambda *a, **k: {"url": "https://x/y.docx", "file_id": "f"})
    return qp, db, patches


def test_a_whole_job_records_every_stage_and_both_calls(monkeypatch):
    reply, blueprint = physics_reply()
    qp, db, patches = patch_job(monkeypatch, reply)
    run(qp._run_generation_job("job1", job_params(blueprint), {}))
    (doc,) = db["paperGenerationTimings"].docs
    assert doc["outcome"] == "completed" and doc["board_key"] == "XII-PHY" and doc["question_count"] == 33
    assert set(doc["stages"]) >= {"generate_paper", "validate_paper", "check_answers", "check_figures", "build_docx", "upload"}
    assert doc["stages"]["generate_paper"] >= 0.02 and doc["total_seconds"] >= doc["stages"]["generate_paper"]
    assert doc["calls"]["qp_generation"]["output_tokens"] == 21000 and doc["calls"]["qp_generation"]["tokens_per_second"] > 0
    assert doc["calls"]["qp_generation"]["cap_percent"] == 42.0
    assert doc["calls"]["qp_answer_check"]["output_tokens"] == 3000
    assert doc["composition"]["question_count"] == 33 and doc["composition"]["working_chars"] == 33 * 80
    assert doc["prompt_chars"] > 5000
    done = [p for p in patches if p.get("status") == "completed"]
    assert len(done) == 1 and done[0]["finished_at"] > 0 and done[0]["answerCheck"] == "done"       # the paper itself is unchanged


def test_a_job_without_the_answer_check_has_no_check_call(monkeypatch):
    reply, blueprint = physics_reply()
    qp, db, _ = patch_job(monkeypatch, reply)
    run(qp._run_generation_job("job1", job_params(blueprint, check=False), {}))
    (doc,) = db["paperGenerationTimings"].docs
    assert "check_answers" not in doc["stages"] and "qp_answer_check" not in doc["calls"] and doc["outcome"] == "completed"


def test_a_failed_job_is_still_recorded_with_the_stages_that_ran(monkeypatch):
    reply, blueprint = physics_reply()
    qp, db, patches = patch_job(monkeypatch, reply, fail_generate=True)
    run(qp._run_generation_job("job1", job_params(blueprint), {}))
    (doc,) = db["paperGenerationTimings"].docs
    assert doc["outcome"] == "failed" and doc["error_type"] == "RuntimeError" and "generate_paper" in doc["stages"]
    assert [p for p in patches if p.get("status") == "failed"]


def test_an_unreadable_reply_is_recorded_with_what_the_model_wrote(monkeypatch):
    qp, db, patches = patch_job(monkeypatch, "I cannot write this paper because the request is unclear. " * 20)
    _, blueprint = physics_reply()
    run(qp._run_generation_job("job1", job_params(blueprint), {}))
    (doc,) = db["paperGenerationTimings"].docs
    assert doc["outcome"] == "failed" and doc["composition"]["parsed"] is False and doc["composition"]["raw_chars"] > 500


def test_a_database_failure_while_recording_does_not_fail_the_job(monkeypatch):
    reply, blueprint = physics_reply()
    qp, db, patches = patch_job(monkeypatch, reply, fail_db=True)
    run(qp._run_generation_job("job1", job_params(blueprint), {}))
    assert [p for p in patches if p.get("status") == "completed"] and not [p for p in patches if p.get("status") == "failed"]


def test_the_status_route_logs_the_delivery_lag(monkeypatch, caplog):
    from app.api.routers import question_paper as qp
    caplog.set_level(logging.INFO, logger="lms.diagnostics")

    async def owned(prefix, job_id, identity):
        return {"status": "completed", "finished_at": time.time() - 9, "paperText": "x", "answerKey": [], "reviewFlags": []}

    monkeypatch.setattr(qp, "get_owned_job", owned)
    result = run(qp.generate_question_paper_status("job1", identity={"user_id": "u"}))
    assert result["status"] == "completed"
    lags = [r.diagnostic_fields["lag_seconds"] for r in caplog.records if getattr(r, "diagnostic_event", "") == "qp.result.delivered"]
    assert len(lags) == 1 and 8.5 <= lags[0] <= 15
