"""Request tracing and MongoDB command outcomes without document/body dumps."""
import logging
import re
import time
from uuid import uuid4
from pymongo import monitoring
from app.core.config import settings
from app.core.diagnostics import context, event, scope as diagnostic_scope


class RequestTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        if not settings.PERF_LOG_ENABLED and not settings.DETAILED_LOGGING:
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        supplied = headers.get(b'x-request-id', b'').decode('ascii', 'ignore')
        request_id = supplied if re.fullmatch(r'[A-Za-z0-9._:-]{1,80}', supplied) else uuid4().hex
        started = time.perf_counter()
        status, received, sent, completed, response_started = 500, 0, 0, False, False
        scope.setdefault('state', {})
        scope['state']['diagnostic_request_id'] = request_id

        async def traced_receive():
            nonlocal received
            message = await receive()
            received += len(message.get('body', b''))
            return message

        async def traced_send(message):
            nonlocal status, sent, completed, response_started
            if message['type'] == 'http.response.start':
                status = message['status']
                message = dict(message)
                message['headers'] = [(k,v) for k,v in message.get('headers', [])
                                      if k.lower() not in {b'x-request-id', b'x-response-time-ms'}]
                message['headers'] += [(b'x-request-id', request_id.encode()),
                    (b'x-response-time-ms', str(round((time.perf_counter()-started)*1000,2)).encode())]
            await send(message)
            if message['type'] == 'http.response.start':
                response_started = True
            if message['type'] == 'http.response.body':
                sent += len(message.get('body', b''))
                if not message.get('more_body', False):
                    completed = True
                    event('request.response_sent', status=status, response_bytes=sent,
                          duration_ms=round((time.perf_counter()-started)*1000,2),
                          **scope['state'].get('diagnostic_identity', {}))

        with diagnostic_scope(request_id=request_id, method=scope['method'], path=scope['path']):
            event('request.started', content_type=headers.get(b'content-type', b'').decode('ascii','replace'))
            try:
                await self.app(scope, traced_receive, traced_send)
            except BaseException as exc:
                event('request.failed', level=logging.ERROR, error_type=type(exc).__name__,
                      status=status, response_started=response_started, exc_info=True,
                      **scope['state'].get('diagnostic_identity', {}))
                raise
            finally:
                duration = round((time.perf_counter()-started)*1000,2)
                event('request.completed', status=status, response_complete=completed,
                      level=logging.WARNING if duration >= settings.PERF_LOG_SLOW_MS else logging.INFO,
                      request_bytes=received, response_bytes=sent,
                      duration_ms=duration,
                      **scope['state'].get('diagnostic_identity', {}))


class _SlowQueryListener(monitoring.CommandListener):
    _INTERESTING = {'find','getMore','aggregate','count','distinct','insert','update','delete',
                    'findAndModify','commitTransaction','abortTransaction'}

    def __init__(self, threshold_ms):
        self._threshold_us = threshold_ms * 1000
        self._pending = {}

    def started(self, command):
        if command.command_name not in self._INTERESTING:
            return
        try:
            doc = command.command
            details = {'operation':command.command_name, 'database':command.database_name,
                       'collection':str(doc.get('collection', doc.get(command.command_name))),
                       'mongo_request_id':command.request_id,
                       'filter_fields':sorted((doc.get('filter') or doc.get('query') or {}).keys()),
                       'transaction':bool(doc.get('autocommit') is False)}
            for field in ('updates','deletes','documents','pipeline'):
                if field in doc: details[field+'_count'] = len(doc[field])
            filters = [doc.get('filter') or doc.get('query') or {}]
            filters += [item.get('q', {}) for key in ('updates','deletes') for item in doc.get(key, [])]
            details['filter_fields'] = sorted({str(key) for filt in filters for key in filt})
            ids = {}
            for filt in filters[:10]:
                for key, value in filt.items():
                    if key in {'_id','exam_id','answer_id','faculty_id','institute_id','subject_id','batch_id'}:
                        text = str(value)
                        if re.fullmatch(r'[A-Fa-f0-9-]{24,36}', text): ids[key] = text
            details['record_ids'] = ids
            if 'updates' in doc:
                details['update_fields'] = sorted({str(k) for item in doc['updates']
                    for changes in (item.get('u', {}).values() if isinstance(item.get('u'),dict) else [])
                    if isinstance(changes,dict) for k in changes})
            self._pending[(command.connection_id,command.request_id)] = (context(), details)
            if settings.DETAILED_LOGGING: event('database.started', **details)
        except Exception:
            pass  # Monitoring must never interfere with database operations.

    def succeeded(self, command):
        self._finish(command, True)

    def failed(self, command):
        self._finish(command, False)

    def _finish(self, command, ok):
        try:
            pending = self._pending.pop((command.connection_id,command.request_id), None)
            if not pending: return
            inherited, details = pending
            if not settings.DETAILED_LOGGING and ok and command.duration_micros < self._threshold_us: return
            reply = getattr(command,'reply', {})
            counts = {k:reply[k] for k in ('n','nModified','ok') if k in reply}
            failure = getattr(command, 'failure', {})
            if isinstance(failure, dict):
                counts.update({k:failure[k] for k in ('code','codeName') if k in failure})
            cursor = reply.get('cursor', {})
            for batch in ('firstBatch','nextBatch'):
                if batch in cursor: counts['returned_count'] = len(cursor[batch])
            with diagnostic_scope(**inherited):
                slow = self._threshold_us > 0 and command.duration_micros >= self._threshold_us
                event('database.completed' if ok else 'database.failed',
                      level=(logging.WARNING if slow else logging.INFO) if ok else logging.ERROR,
                      slow=slow,
                      duration_ms=round(command.duration_micros/1000,2), **details, **counts)
        except Exception:
            pass


_registered = False


def install_slow_query_logging():
    global _registered
    if _registered or (not settings.DETAILED_LOGGING and settings.SLOW_QUERY_MS <= 0): return
    monitoring.register(_SlowQueryListener(settings.SLOW_QUERY_MS))
    _registered = True
    event('database.monitoring_enabled', detailed=settings.DETAILED_LOGGING,
          slow_threshold_ms=settings.SLOW_QUERY_MS)
