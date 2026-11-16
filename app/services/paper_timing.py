"""Where the minutes and the tokens go when a paper is generated.

Records only: nothing here changes a paper, a prompt or a result, and nothing here can raise into the job.
One summary per job is logged (`qp.job.summary`) and saved in Mongo (`paperGenerationTimings`), because container logs
vanish at every redeploy and a number that cannot be looked up later cannot be improved.
Only sizes and durations are recorded, never the words of a paper.
"""
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from app.core.diagnostics import event
from app.utils.ai_pricing import estimate_cost_usd

logger = logging.getLogger(__name__)

TIMINGS_COLLECTION = "paperGenerationTimings"
DEFAULT_CHARS_PER_TOKEN = 3.5      # used only when the real token count is not known


def _whole(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


class JobTimings:
    """Stage durations (seconds), the AI calls' tokens and speed, and the paper's size breakdown for one job."""

    def __init__(self, job_id):
        self.job_id = job_id
        self.started = time.perf_counter()
        self.stages = {}
        self.calls = {}
        self.composition = None
        self.facts = {}

    @contextmanager
    def stage(self, name):
        """Time a block, adding to the stage's total; the block's own exceptions pass through untouched."""
        began = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = round(self.stages.get(name, 0.0) + (time.perf_counter() - began), 3)

    def note_call(self, feature, usage, *, model, seconds, max_tokens=None):
        """Tokens, speed and cost of one AI call. `seconds` is that call's stage time."""
        try:
            usage = usage if isinstance(usage, dict) else {}
            tokens_in, tokens_out = _whole(usage.get("input_tokens")), _whole(usage.get("output_tokens"))
            call = {"model": model, "seconds": round(seconds, 3), "input_tokens": tokens_in, "output_tokens": tokens_out}
            if tokens_out and seconds and seconds > 0:
                call["tokens_per_second"] = round(tokens_out / seconds, 1)
            if tokens_out and max_tokens:
                call["cap_percent"] = round(100.0 * tokens_out / max_tokens, 1)
            if tokens_in is not None and tokens_out is not None:
                cost = estimate_cost_usd(model, tokens_in, tokens_out)
                if cost is not None:
                    call["cost_usd"] = round(cost, 4)
            self.calls[feature] = call
        except Exception:
            logger.exception("Could not note the %s call's timing", feature)

    def total_seconds(self):
        return round(time.perf_counter() - self.started, 3)


# ---- the paper's size: which part of the reply holds the tokens ------------------------------------------------

def _parse(raw_text):
    """Read the model's JSON the way the validator does, but quietly: this is only a measurement."""
    if not isinstance(raw_text, str):
        return None
    try:
        return json.loads(raw_text)
    except ValueError:
        pass
    start, end = raw_text.find("{"), raw_text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(raw_text[start:end + 1])
        except ValueError:
            return None
    return None


def _compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _length(value):
    return len(value) if isinstance(value, str) else 0


def composition_of(raw_text, output_tokens=None):
    """How the model's reply splits into question text, answers, working and figure specs (characters, and estimated
    tokens). Never raises. The reply's own words are not kept, only counts."""
    try:
        raw_chars = len(raw_text) if isinstance(raw_text, str) else 0
        out = {"raw_chars": raw_chars}
        tokens = _whole(output_tokens)
        chars_per_token = (raw_chars / tokens) if tokens and raw_chars else DEFAULT_CHARS_PER_TOKEN
        if tokens and raw_chars:
            out["chars_per_token"] = round(chars_per_token, 2)
        data = _parse(raw_text)
        sections = data.get("sections") if isinstance(data, dict) else None
        if not isinstance(sections, list):
            out["parsed"] = False
            return out
        out["parsed"] = True

        parts = {"question_text_chars": 0, "answer_chars": 0, "working_chars": 0, "diagram_chars": 0, "other_chars": 0}
        diagrams, per_section, largest = 0, [], []
        for section in sections:
            questions = [q for q in (section.get("questions") if isinstance(section, dict) else None) or [] if isinstance(q, dict)]
            section_chars = 0
            for question in questions:
                whole_chars = len(_compact(question))
                section_chars += whole_chars
                sizes = {
                    "question_text_chars": _length(question.get("text")),
                    "answer_chars": _length(question.get("answer")),
                    "working_chars": _length(question.get("working")),
                    "diagram_chars": len(_compact(question.get("diagrams"))) if question.get("diagrams") else 0,
                }
                sizes["other_chars"] = max(whole_chars - sum(sizes.values()), 0)
                for name, value in sizes.items():
                    parts[name] += value
                diagrams += len(question.get("diagrams") or []) if isinstance(question.get("diagrams"), list) else 0
                biggest = max(sizes, key=sizes.get)
                largest.append({"number": question.get("number"), "marks": question.get("marks"), "json_chars": whole_chars,
                                "biggest_part": biggest.replace("_chars", "")})
            label = str(section.get("label", ""))[:40] if isinstance(section, dict) else ""
            per_section.append({"label": label, "questions": len(questions), "json_chars": section_chars})

        question_count = sum(item["questions"] for item in per_section)
        question_chars = sum(item["json_chars"] for item in per_section)
        out.update(parts)
        out.update({
            "question_count": question_count,
            "diagram_count": diagrams,
            "question_json_chars": question_chars,
            "structure_chars": max(raw_chars - question_chars, 0),          # section wrappers and spacing
            "estimated_tokens": {name.replace("_chars", ""): round(value / chars_per_token) for name, value in parts.items()},
            "sections": per_section,
            "largest_questions": sorted(largest, key=lambda item: item["json_chars"], reverse=True)[:3],
        })
        if question_count:
            out["estimated_tokens_per_question"] = round(question_chars / chars_per_token / question_count)
        return out
    except Exception:
        logger.exception("Could not measure the paper's composition")
        return {"parsed": False, "error": "composition_failed"}


# ---- the job summary ---------------------------------------------------------------------------------------------

def build_summary(timings, params, outcome, error_type=None):
    board = (params or {}).get("board") or {}
    composition = timings.composition or {}
    summary = {
        "job_id": timings.job_id,
        "outcome": outcome,
        "total_seconds": timings.total_seconds(),
        "stages": dict(timings.stages),
        "calls": dict(timings.calls),
        "composition": composition,
        "question_count": composition.get("question_count"),
        "institute_type": (params or {}).get("institute_type"),
        "board_key": board.get("key"),
        "customised": board.get("customised"),
        "total_marks": (params or {}).get("total_marks"),
        "section_count": len((params or {}).get("sections") or []),
        "check_answers": bool((params or {}).get("check_answers")),
        **timings.facts,
    }
    if error_type:
        summary["error_type"] = error_type
    return summary


async def record_job_timings(db, timings, params, outcome, error_type=None):
    """Log the job's summary and keep it in Mongo. Always safe to call: a failure here is logged and ignored."""
    try:
        summary = build_summary(timings, params, outcome, error_type)
        event("qp.job.summary", **summary)
        document = {**summary, "user_id": (params or {}).get("user_id"), "faculty_id": (params or {}).get("faculty_id"),
                    "created_at": datetime.now(timezone.utc)}
        await db[TIMINGS_COLLECTION].insert_one(document)
        return summary
    except Exception:
        logger.exception("Could not record the timings of job %s", getattr(timings, "job_id", "?"))
        return None


# ---- how long the finished paper waited for the page to ask ------------------------------------------------------

def delivery_lag_seconds(job, now=None):
    """Seconds between the job finishing and a status request being answered with the result, or None."""
    try:
        finished = float((job or {}).get("finished_at"))
    except (TypeError, ValueError):
        return None
    lag = (time.time() if now is None else now) - finished
    return round(lag, 2) if lag >= 0 else None


def log_delivery(job_id, job, now=None):
    lag = delivery_lag_seconds(job, now)
    if lag is not None:
        event("qp.result.delivered", job_id=job_id, lag_seconds=lag)
    return lag
