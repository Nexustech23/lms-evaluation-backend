"""Read-only workflow observations. Never mutate marks, job state or retry policy."""
import asyncio
import hashlib
import functools
import json
import logging
import math
import re
import threading
import time
from app.core.config import settings


def access_denied(reason,identity,resource,resource_id=None):
    from app.core.diagnostics import event
    value=str(resource_id) if resource_id is not None else ''
    event('authorization.denied',reason_code=reason,resource_type=resource,
          resource_id=value if re.fullmatch(r'[0-9a-fA-F]{24}',value) else None,
          user_id=identity.get('user_id'),role=identity.get('role'))


def configuration_snapshot(exam, answer, evaluation, *, ocr_model, grading_model, prompt_version, transcript_pdf, job_id, mcq_model=None):
    from app.core.diagnostics import event
    try:
        rules = evaluation.get('questionEvaluationDetails', [])
        fingerprint = hashlib.sha256(json.dumps(rules,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()
        event('evaluation.configuration', job_id=job_id, exam_id=str(exam['_id']),answer_id=str(answer['_id']),
              ocr_model=ocr_model,grading_model=grading_model,prompt_version=prompt_version,
              mcq_answer_key_model=mcq_model,transcript_model=grading_model if transcript_pdf else None,
              paper_version=(exam.get('question_paper') or {}).get('version'),
              rubric_revision=exam.get('rubric_revision'),evaluation_id=str(evaluation.get('_id')) if evaluation.get('_id') else None,
              rubric_fingerprint=fingerprint,question_count=len(rules),total_marks=evaluation.get('totalMarks'),
              previous_result_revision=answer.get('result_revision',0),transcript_pdf=bool(transcript_pdf))
    except Exception:
        event('evaluation.configuration_unavailable',job_id=job_id)


def trace_report_refresh(function):
    @functools.wraps(function)
    async def wrapped(db,exam,answer,*args,**kwargs):
        from app.core.diagnostics import event
        ids={'exam_id':str(exam.get('_id')),'answer_id':str(answer.get('_id'))}
        try:
            result=await function(db,exam,answer,*args,**kwargs)
        except BaseException as exc:
            event('report.refresh_outcome',level=logging.WARNING,outcome='failed_or_unconfirmed',
                  error_type=type(exc).__name__,**ids)
            raise
        fields=result if isinstance(result,dict) else {}
        event('report.refresh_outcome',outcome='published',result_revision=fields.get('result_revision'),
              transcript_refresh=fields.get('transcript_refresh'),**ids)
        return result
    return wrapped


def check_result_consistency(rows, *, total_ai, total_final, total_max, phase, **ids):
    from app.core.diagnostics import event
    try:
        def number(value):
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError()
            return float(value)
        def same(a,b):return math.isclose(number(a),number(b),abs_tol=0.011,rel_tol=0)
        ai_sum=final_sum=max_sum=0
        mismatches=[]
        for index,q in enumerate(rows,1):
            ai=number(q['ai_awarded_marks']);final=number(q['final_marks']);cap=number(q['max_marks'])
            ai_sum+=ai;final_sum+=final;max_sum+=cap
            reasons=[]
            if not 0<=ai<=cap or not 0<=final<=cap:reasons.append('SCORE_OUT_OF_RANGE')
            if not same(ai+number(q.get('grace_marks',0)),final):reasons.append('ADJUSTMENT_TOTAL_MISMATCH')
            cos=q.get('cos') or []
            if cos:
                if not same(sum(number(c['ai_marks']) for c in cos),ai):reasons.append('CO_AI_TOTAL_MISMATCH')
                if not same(sum(number(c.get('final_co_marks',number(c['ai_marks'])+number(c.get('grace_marks',0)))) for c in cos),final):reasons.append('CO_FINAL_TOTAL_MISMATCH')
                if not same(sum(number(c['max_marks']) for c in cos),cap):reasons.append('CO_MAXIMUM_TOTAL_MISMATCH')
            if reasons:mismatches.append({'question_no':q.get('question_no',index),'codes':reasons})
        totals={'ai_total_matches':same(ai_sum,total_ai),'final_total_matches':same(final_sum,total_final),
                'maximum_total_matches':same(max_sum,total_max)}
        passed=all(totals.values()) and not mismatches
        event('results.consistency',level=logging.INFO if passed else logging.WARNING,
              phase=phase,outcome='passed' if passed else 'mismatch',question_count=len(rows),
              computed_ai_total=round(ai_sum,4),computed_final_total=round(final_sum,4),computed_maximum=round(max_sum,4),
              checks=totals,question_mismatches=mismatches,**ids)
    except Exception:
        event('results.consistency',level=logging.WARNING,phase=phase,outcome='unavailable',**ids)


class DependencyHealth:
    def __init__(self):
        self.states={}
        self.lock=threading.Lock()

    def observe(self,name,state,*,error_type=None,source='probe'):
        from app.core.diagnostics import event
        with self.lock:
            previous=self.states.get(name,'unknown')
            self.states[name]=state
        if previous!=state:
            event('dependency.transition',level=logging.WARNING if state=='unavailable' else logging.INFO,
                  dependency=name,previous_state=previous,state=state,error_type=error_type,source=source)


dependency_health=DependencyHealth()


async def probe_dependencies():
    from app.db import mongodb
    from app.core import redis_client
    async def probe(name,client,command):
        if client is None:
            dependency_health.observe(name,'uninitialized')
            return
        try:
            await asyncio.wait_for(command(),timeout=settings.DEPENDENCY_PROBE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            dependency_health.observe(name,'unavailable',error_type=type(exc).__name__)
        else:
            dependency_health.observe(name,'connected')
    await asyncio.gather(
        probe('mongodb',mongodb._client,lambda:mongodb._client.admin.command('ping')),
        probe('redis',redis_client._client,lambda:redis_client._client.ping()))


async def dependency_monitor():
    while True:
        try:
            await probe_dependencies()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from app.core.diagnostics import event
            event('dependency.monitor_unavailable',level=logging.WARNING,error_type=type(exc).__name__)
        await asyncio.sleep(settings.DEPENDENCY_LOG_INTERVAL_SECONDS)


async def job_heartbeat(job_id,tracker):
    from app.core.diagnostics import event
    started=time.monotonic()
    suspected=False
    while True:
        await asyncio.sleep(settings.JOB_HEARTBEAT_SECONDS)
        if tracker.get('terminal'):return
        age=max(0,time.monotonic()-tracker.get('updated_at',started))
        stalled=age>=settings.JOB_STALL_WARNING_SECONDS
        event('job.heartbeat',job_id=job_id,stage=tracker.get('step'),
              worker_responsive=True,progress_source='job_store_updates',seconds_since_progress=round(age,2),suspected_stall=stalled,
              stage_duration_seconds=round(max(0,time.perf_counter()-tracker['started']),2) if tracker.get('started') else None)
        if stalled and not suspected:
            event('job.stall_suspected',level=logging.WARNING,job_id=job_id,stage=tracker.get('step'),
                  seconds_since_progress=round(age,2),threshold_seconds=settings.JOB_STALL_WARNING_SECONDS,
                  diagnostic_only=True)
        elif suspected and not stalled:
            event('job.progress_resumed',job_id=job_id,stage=tracker.get('step'))
        suspected=stalled
