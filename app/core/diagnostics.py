"""Structured testing diagnostics. Never record bodies, prompts, answers or credentials."""
import asyncio
import contextvars
import functools
import inspect
import json
import logging
import math
import os
import re
import socket
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.core.diagnostic_details import build_info, external_error_details, resources, validation_details

_context = contextvars.ContextVar('diagnostic_context', default=None)
_progress = contextvars.ContextVar('diagnostic_progress', default=None)
_logger = logging.getLogger('lms.diagnostics')
_SECRET = re.compile(r'password|passwd|secret|api.?key|authorization|cookie|credential|access.?token|refresh.?token|signature', re.I)
_CONTENT = re.compile(r'^(?:prompt|system|messages|content|html.*|.*base64|file_bytes|answer_text|student_answer|evidence|reasoning|feedback|improvement|description|bio|email|phone|address|full_?name)$', re.I)
_META = {'page','limit','semester','semester_no','credits','totalMarks','total_marks','maxMarks','max_marks',
         'minMarks','weightage','total_final_marks','total_ai_marks','total_max_marks','result_revision',
         'rubric_revision','paper_version','success','status','evaluation_status','saved_count','count',
         'generateTranscriptPdf','review_pending_count','is_archived','is_active','role','prompt_version',
         'transcript_refresh','attempt_count','percent','bloomLevels','percentage','grade',
         'input_tokens','output_tokens','total_tokens','prompt_tokens','candidate_tokens',
         'cache_tokens','cost_usd','usage_status','cost_status','call_status'}
_IDENTIFIER = re.compile(r'(?:^|_)(?:id|ids)$|^(?:folderId|answerId|subjectId|fileId)$')


def context():
    return dict(_context.get() or {})


def bind(**fields):
    _context.set({**context(), **{k: v for k, v in fields.items() if v is not None}})


def clean_text(value):
    text = str(value)
    # Also mask configured secret values when an SDK embeds them without a label.
    for name in type(settings).model_fields:
        if _SECRET.search(name) or name in {'MONGODB_URI', 'REDIS_URL'}:
            secret = getattr(settings, name, None)
            if isinstance(secret, str) and len(secret) >= 8:
                text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(mongodb(?:\+srv)?://)[^\s/@]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'https?://[^\s<>"\']+', '[URL]', text)
    text = re.sub(r'(/[\w/%.:-]+)\?[^\s"\']+', r'\1?[REDACTED]', text)
    text = re.sub(r'(?i)bearer\s+\S+', 'Bearer [REDACTED]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[TOKEN]', text)
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{12,})', '[KEY]', text)
    text = re.sub(r'(?i)((?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|signature)["\']?\s*[:=]\s*)(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,;}]+)', r'\1[REDACTED]', text)
    text = re.sub(r'\b[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}\b', '[EMAIL]', text)
    return text.replace('\x00', '')


def redact(value, key='', depth=0):
    if _SECRET.search(key) or _CONTENT.match(key):
        return '[REDACTED]'
    if depth > 10:
        return '[MAX_DEPTH]'
    if isinstance(value, dict):
        return {str(k): redact(v, str(k), depth+1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(v, key, depth+1) for v in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        return {'byte_count': len(value)}
    return clean_text(value)


def metadata(value, depth=0):
    try:
        return _metadata(value, depth)
    except Exception:
        return {'type':type(value).__name__, 'metadata_unavailable':True}


def _metadata(value, depth=0):
    """Allowlisted domain values plus shape, not a recursive body dump."""
    if depth > 4:
        return {'type': type(value).__name__}
    if hasattr(value, 'model_dump'):
        value = value.model_dump(exclude_unset=True)
    if isinstance(value, dict):
        out = {'fields': sorted(str(k) for k in value)}
        for k, v in value.items():
            name = str(k)
            if _SECRET.search(name) or _CONTENT.match(name):
                continue
            if name in _META or _IDENTIFIER.search(name):
                if isinstance(v, (dict,list)):
                    out[name] = metadata(v, depth+1)
                else:
                    out[name] = redact(v, name)
            elif isinstance(v, list):
                out[name+'_count'] = len(v)
            elif isinstance(v, dict):
                out[name] = metadata(v, depth+1)
        return out
    if isinstance(value, (list, tuple)):
        return {'count':len(value), 'items':[metadata(v,depth+1) for v in value[:3]]}
    if isinstance(value, bytes):
        return {'type':'bytes','byte_count':len(value)}
    if isinstance(value, str):
        return {'type':'str','character_count':len(value)}
    return {'type':type(value).__name__}


def event(name, *, level=logging.INFO, exc_info=None, **fields):
    """Diagnostics must never change the business outcome, even if logging fails."""
    try:
        _logger.log(level, name, extra={'diagnostic_event': name,
                    'diagnostic_fields': redact(fields), 'diagnostic_context':context()}, exc_info=exc_info)
    except Exception:
        pass


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {'timestamp':datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                'level':record.levelname, 'logger':record.name, 'pid':record.process,
                'instance':socket.gethostname(), **build_info(), **getattr(record, 'diagnostic_context', context()),
                'event':getattr(record,'diagnostic_event','log'),
                'message':clean_text(record.getMessage()),
                **getattr(record,'diagnostic_fields',{})}
        if record.exc_info and record.exc_info[0]:
            data['error_type'] = record.exc_info[0].__name__
            external = external_error_details(record.exc_info[1])
            if external: data['external_error'] = external
            data.update(validation_details(record.exc_info[1]))
            # Full stack, no local variable capture. Redaction also covers legacy messages.
            data['traceback'] = clean_text(''.join(traceback.format_exception(*record.exc_info)))
        return json.dumps(redact(data),ensure_ascii=True,default=str)


class DiagnosticFileHandler(logging.FileHandler):
    """Report disk-write failure directly to stderr without recursively logging a record."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._failure_count=0
        self._write_failed=False
        self._last_notice=None

    def _notice(self,name,**fields):
        try:
            notice={'timestamp':datetime.now(timezone.utc).isoformat(), 'event':name,
                    'level':'ERROR' if name=='logging.file_write_failed' else 'INFO',
                    'pid':os.getpid(),**fields}
            os.write(2,(json.dumps(notice)+'\n').encode())
        except Exception:
            pass

    def handleError(self,record):
        self._failure_count+=1
        self._write_failed=True
        now=time.monotonic()
        if self._last_notice is None or now-self._last_notice>=30:
            error=sys.exc_info()[1]
            self._notice('logging.file_write_failed',error_type=type(error).__name__,
                         errno=getattr(error,'errno',None),failure_count=self._failure_count)
            self._last_notice=now

    def emit(self,record):
        previous=self._failure_count
        try:
            super().emit(record)
        except Exception:
            self.handleError(record)
        if self._write_failed and previous==self._failure_count:
            self._notice('logging.file_recovered',failure_count=self._failure_count)
            self._write_failed=False
            self._last_notice=None


def configure_logging():
    root = logging.getLogger()
    if any(getattr(h,'_lms_diagnostic',False) for h in root.handlers):
        return
    root.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    formatter = JsonFormatter()
    console = logging.StreamHandler(sys.stdout);console.setFormatter(formatter)
    console._lms_diagnostic = True
    root.handlers[:] = [console]
    if settings.LOG_FILE:
        try:
            path = Path(settings.LOG_FILE.format(pid=os.getpid(),hostname=socket.gethostname()))
            path.parent.mkdir(parents=True,exist_ok=True)
            handler = DiagnosticFileHandler(path,encoding='utf-8')
            handler.setFormatter(formatter);handler._lms_diagnostic=True
            root.addHandler(handler)
            try: path.chmod(0o600)
            except OSError: pass
        except Exception:
            event('logging.file_unavailable',level=logging.ERROR,exc_info=True)
    # SDK debug traces may include full HTTP payloads; application diagnostics remain detailed.
    for name in ('pymongo','motor','httpx','httpcore','anthropic','google','urllib3'):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


@contextmanager
def scope(**fields):
    token = _context.set({**context(), **fields})
    try: yield
    finally: _context.reset(token)


@contextmanager
def stage(name, **fields):
    started = time.perf_counter()
    event('stage.started',stage=name,**fields,
          **({'resources':resources.sample()} if name.startswith('job.') else {}))
    try:
        yield
    except BaseException as exc:
        detail = validation_details(exc)
        if name == 'paper.validate' and isinstance(exc,json.JSONDecodeError):
            detail = {'reason_code':'PAPER_JSON_INVALID','field':'paper_json',
                      'line':exc.lineno,'column':exc.colno}
        if detail:
            event('validation.rejected', level=logging.WARNING, stage=name, **detail)
        event('stage.failed',stage=name,duration_ms=round((time.perf_counter()-started)*1000,2),
              error_type=type(exc).__name__,level=logging.ERROR,exc_info=True,**fields,
              resources=resources.sample(), **detail)
        raise
    else:
        event('stage.completed',stage=name,duration_ms=round((time.perf_counter()-started)*1000,2),**fields,
              **({'resources':resources.sample()} if name.startswith('job.') else {}))


def trace_function(name):
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def wrapped(*args,**kwargs):
                with stage(name):
                    result = await function(*args,**kwargs)
                    event('stage.result', stage=name, result=metadata(result))
                    return result
        else:
            @functools.wraps(function)
            def wrapped(*args,**kwargs):
                with stage(name):
                    result = function(*args,**kwargs)
                    event('stage.result', stage=name, result=metadata(result))
                    return result
        return wrapped
    return decorate


def install_route_diagnostics(app):
    from fastapi.routing import APIRoute
    for route in app.routes:
        if not isinstance(route,APIRoute) or getattr(route.dependant.call,'_lms_traced',False):continue
        def wrap(function, path):
            def inputs(kwargs):
                return metadata({k:v for k,v in kwargs.items() if k not in {'db','request','response','background_tasks','identity','user','_identity'}})
            if inspect.iscoroutinefunction(function):
                @functools.wraps(function)
                async def call(*args,**kwargs):
                    with stage('api.'+function.__name__,route=path,parameters=inputs(kwargs)):
                        result = await function(*args,**kwargs)
                        event('api.result',route=path,result=metadata(result),status=getattr(result,'status_code',None))
                        return result
            else:
                @functools.wraps(function)
                def call(*args,**kwargs):
                    with stage('api.'+function.__name__,route=path,parameters=inputs(kwargs)):
                        result=function(*args,**kwargs)
                        event('api.result',route=path,result=metadata(result))
                        return result
            call._lms_traced=True
            return call
        route.dependant.call=wrap(route.dependant.call,route.path)


def trace_job(function):
    @functools.wraps(function)
    async def call(ctx,*args,_lms_trace=None,**kwargs):
        inherited={k:v for k,v in (_lms_trace or context()).items() if k in {'request_id','user_id','role'}}
        identity = str(args[0]) if args else None
        with scope(**inherited,job_id=identity,job_kind=function.__name__):
            dispatched=(_lms_trace or {}).get("dispatch_epoch")
            delay=(time.time()-dispatched)*1000 if isinstance(dispatched,(int,float)) else None
            event("job.worker_started",dispatch_to_worker_ms=round(delay,2) if delay is not None and delay>=0 else None,
                  queue_clock_skew=bool(delay is not None and delay<0),
                  queue_job_id=ctx.get("job_id") if isinstance(ctx,dict) else None)
            from app.core.workflow_diagnostics import job_heartbeat
            tracker={'updated_at':time.monotonic()}
            token=_progress.set(tracker)
            heartbeat=asyncio.create_task(job_heartbeat(identity,tracker))
            try:
                with stage('job.'+function.__name__):
                    return await function(ctx,*args,**kwargs)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                except Exception:
                    event('job.heartbeat_unavailable',level=logging.WARNING,error_type='MonitorFailure')
                tracker=_progress.get() or {}
                if tracker.get('step'):
                    event('job.stage.closed',stage=tracker['step'],outcome='worker_exit',
                          duration_ms=round((time.perf_counter()-tracker['started'])*1000,2))
                _progress.reset(token)
    return call


def job_progress(job_id, patch):
    tracker=_progress.get()
    if tracker is None:
        return event('job.updated',job_id=job_id,status=patch.get('status'),progress=patch.get('progress'),stage=patch.get('step'))
    next_step=patch.get('step'); terminal=patch.get('status') in {'done','completed','error','failed','interrupted'}
    changed=(bool(next_step and next_step!=tracker.get('step')) or
             any(k in patch and patch[k]!=tracker.get(k) for k in ('progress','status')))
    if tracker.get('step') and (terminal or (next_step and next_step!=tracker['step'])):
        event('job.stage.completed',job_id=job_id,stage=tracker['step'],outcome=patch.get('status','advanced'),
              duration_ms=round((time.perf_counter()-tracker['started'])*1000,2))
        tracker.clear()
    if next_step and not terminal and not tracker.get('step'):
        tracker.update(step=next_step,started=time.perf_counter())
        event('job.stage.started',job_id=job_id,stage=next_step)
    if changed or terminal:tracker['updated_at']=time.monotonic()
    tracker.update({k:patch[k] for k in ('progress','status') if k in patch})
    tracker['terminal']=terminal
    event('job.updated',job_id=job_id,status=patch.get('status'),progress=patch.get('progress'),
          error_type='JobFailure' if patch.get('status') in {'error','failed'} else None)


def question_marks(rows, phase):
    if not settings.DETAILED_LOGGING:return
    for q in rows or []:
        if not isinstance(q, dict): continue
        event('marks.question',phase=phase,question_no=q.get('question_no'),max_marks=q.get('max_marks'),
              ai_marks=q.get('ai_awarded_marks'),adjustment=q.get('grace_marks'),final_marks=q.get('final_marks'),
              flags=q.get('flags'),cos=[{k:c.get(k) for k in ('co_code','max_marks','ai_marks','grace_marks','final_co_marks')} for c in (q.get('cos') or []) if isinstance(c,dict)],
              parameters=[{k:p.get(k) for k in ('name','weight_percentage','ai_score')} for p in (q.get('parameters') or []) if isinstance(p,dict)])
