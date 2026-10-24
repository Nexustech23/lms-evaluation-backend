"""Local-only diagnostic metadata. No provider requests or subprocess output dumps."""
import asyncio
import os
import re
import subprocess
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def build_info():
    commit = os.environ.get('APP_GIT_COMMIT', '')
    dirty = os.environ.get('APP_SOURCE_DIRTY', 'unknown')
    source = 'image'
    if not re.fullmatch(r'[0-9a-fA-F]{7,64}', commit):
        commit, source = 'unknown', 'unavailable'
        root = Path(__file__).resolve().parents[2]
        # Docker excludes .git; do not accidentally identify an unrelated parent repo.
        if (root / '.git').exists():
            try:
                options = dict(cwd=root, capture_output=True, text=True, timeout=2,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                result = subprocess.run(['git','rev-parse','HEAD'], **options)
                if result.returncode == 0 and re.fullmatch(r'[0-9a-fA-F]{40,64}',result.stdout.strip()):
                    commit, source = result.stdout.strip(), 'local_git'
                    status = subprocess.run(['git','status','--porcelain'], **options)
                    dirty = str(bool(status.stdout.strip())).lower() if status.returncode == 0 else 'unknown'
            except (OSError, subprocess.SubprocessError):
                pass
    return {'git_commit':commit, 'version_source':source,
            'source_dirty':{'true':True,'false':False}.get(dirty.lower()),
            'python_version':sys.version.split()[0]}


def _safe_code(value):
    if not isinstance(value, str): return None
    return value if re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value) else None


def external_error_details(error):
    """Inspect typed SDK metadata through wrappers; never read a response body."""
    seen = set()
    try:
        while isinstance(error, BaseException) and id(error) not in seen and len(seen) < 8:
            seen.add(id(error))
            module, name = type(error).__module__, type(error).__name__
            supported = module.startswith(('anthropic','google','httpx','httpcore','requests','urllib3'))
            if supported or name in {'TimeoutError','ConnectionError'}:
                status = getattr(error,'status_code',None)
                if not isinstance(status,int): status = getattr(error,'code',None)
                if not isinstance(status,int) or isinstance(status,bool): status = None
                api_status = _safe_code(getattr(error,'status',None))
                category = 'external_error'
                if status == 429 or name in {'RateLimitError','ResourceExhausted','TooManyRequests'} or api_status == 'RESOURCE_EXHAUSTED': category = 'rate_limit'
                elif status == 401 or name in {'AuthenticationError','Unauthenticated','Unauthorized'}: category = 'authentication'
                elif status == 403 or name in {'PermissionDeniedError','PermissionDenied','Forbidden'}: category = 'permission_denied'
                elif status in {408,504} or 'Timeout' in name or name == 'DeadlineExceeded': category = 'timeout'
                elif 'Connection' in name or name in {'ConnectError','NetworkError'}: category = 'connection'
                elif status is not None and status >= 500: category = 'service_unavailable'
                elif status is not None and status >= 400: category = 'invalid_request'
                details = {'category':category,'cause_type':name,
                           'provider':'claude' if module.startswith('anthropic') else 'gemini' if module.startswith('google') else 'http'}
                if status is not None and 100 <= status <= 599: details['http_status'] = status
                if api_status: details['api_status'] = api_status
                response = getattr(error,'response',None)
                headers = getattr(response,'headers',{}) or {}
                request_id = _safe_code(getattr(error,'request_id',None))
                for key in ('request-id','x-request-id','x-goog-request-id'):
                    request_id = request_id or _safe_code(headers.get(key))
                if request_id: details['provider_request_id'] = request_id
                for key, multiplier in (('retry-after',1),('retry-after-ms',0.001)):
                    try:
                        delay = float(headers.get(key)) * multiplier
                        if 0 <= delay <= 86400: details['retry_after_seconds'] = delay; break
                    except (TypeError,ValueError,OverflowError): pass
                return details
            error = error.__cause__ or error.__context__
    except Exception:
        pass
    return {}


def validation_error(code, message, *, question_no=None, field=None, section_index=None):
    error = ValueError(message)  # Preserve callers' existing exception type/message contract.
    error.reason_code = code
    error.validation_details = {k:v for k,v in dict(question_no=question_no,field=field,section_index=section_index).items() if v is not None}
    return error


def validation_details(error):
    if getattr(error,'reason_code',None):
        return {'reason_code':error.reason_code, **getattr(error,'validation_details',{})}
    return {}


def _kv_file(path):
    return {line.split()[0].rstrip(':'):line.split()[1] for line in Path(path).read_text().splitlines() if len(line.split()) >= 2}


def _integer_file(path):
    try:
        value = Path(path).read_text().strip()
        return int(value) if value != 'max' else None
    except (OSError,ValueError): return None


def _windows_memory():
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)] + [(name,ctypes.c_size_t) for name in (
            'PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
            'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    counters = Counters(); counters.cb = ctypes.sizeof(counters)
    get_process = ctypes.windll.kernel32.GetCurrentProcess
    get_process.restype = wintypes.HANDLE
    query = ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    if not query(get_process(),ctypes.byref(counters),counters.cb): return {}
    return {'process_rss_bytes':counters.WorkingSetSize, 'process_peak_rss_bytes':counters.PeakWorkingSetSize}


class ResourceSampler:
    def __init__(self):
        self._previous = None
        self._previous_host_cpu = None
        self._previous_cgroup_cpu = None
        self._lock = threading.Lock()

    def sample(self):
        """Non-blocking CPU counters: percentages describe time since the last sample."""
        try:
            with self._lock:
                now, cpu = time.monotonic(), time.process_time()
                result = {'process_cpu_seconds':round(cpu,4), 'cpu_count':os.cpu_count(),
                          'process_cpu_percent_one_core':None}
                if self._previous:
                    wall = now-self._previous[0]
                    if wall > 0:
                        result.update(sample_window_seconds=round(wall,4),
                            process_cpu_percent_one_core=round(max(0,cpu-self._previous[1])*100/wall,2))
                if sys.platform == 'win32':
                    result.update(_windows_memory())
                elif sys.platform.startswith('linux'):
                    status = _kv_file('/proc/self/status')
                    result.update({key:int(status[field])*1024 for key,field in (
                        ('process_rss_bytes','VmRSS'),('process_peak_rss_bytes','VmHWM')) if field in status})
                    memory = _kv_file('/proc/meminfo')
                    result.update({key:int(memory[field])*1024 for key,field in (
                        ('host_memory_total_bytes','MemTotal'),('host_memory_available_bytes','MemAvailable')) if field in memory})
                    try:
                        ticks = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
                        total, idle = sum(ticks), ticks[3]+ticks[4]
                        if self._previous_host_cpu:
                            elapsed = total-self._previous_host_cpu[0]
                            if elapsed > 0:
                                result['host_cpu_percent'] = round(max(0,min(1,1-(idle-self._previous_host_cpu[1])/elapsed))*100,2)
                        self._previous_host_cpu = total,idle
                    except (OSError,ValueError,IndexError): pass
                    # Docker's cgroup v2 namespace exposes this container at the root.
                    for key, filename in (('cgroup_memory_bytes','memory.current'),('cgroup_memory_limit_bytes','memory.max')):
                        result[key] = _integer_file('/sys/fs/cgroup/'+filename)
                    try:
                        events = _kv_file('/sys/fs/cgroup/memory.events')
                        result['cgroup_memory_events'] = {k:int(events[k]) for k in ('high','max','oom','oom_kill') if k in events}
                    except (OSError,ValueError): pass
                    try:
                        usage = int(_kv_file('/sys/fs/cgroup/cpu.stat')['usage_usec'])/1000000
                        quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()
                        result['cgroup_cpu_limit_cores'] = None if quota == 'max' else int(quota)/int(period)
                        result['cgroup_scope'] = 'mounted_cgroup_v2_root'
                        if self._previous_cgroup_cpu:
                            wall = now-self._previous_cgroup_cpu[0]
                            if wall > 0:
                                result['cgroup_cpu_percent_one_core'] = round(max(0,usage-self._previous_cgroup_cpu[1])*100/wall,2)
                        self._previous_cgroup_cpu = now,usage
                    except (OSError,KeyError,ValueError,ZeroDivisionError): pass
                self._previous = now,cpu
                return result
        except Exception:
            return {'resource_status':'unavailable'}


resources = ResourceSampler()


async def resource_monitor():
    from app.core.config import settings
    from app.core.diagnostics import event
    while True:
        event('resources.snapshot', resources=resources.sample())
        await asyncio.sleep(settings.RESOURCE_LOG_INTERVAL_SECONDS)
