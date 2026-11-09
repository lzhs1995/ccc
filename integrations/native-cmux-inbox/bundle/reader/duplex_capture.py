"""Bounded capture of the original CLI bytes, including failed/UNKNOWN calls."""
import base64
import os
import selectors
import subprocess
import time
import fixture_common as f


def capture(argv, directory, label, limit, seconds):
    f.save_once(directory/(label+'-INTENT.json'), dict(argv=argv, invocations=1, retries=0,
                bytes_per_stream_limit=limit, seconds_limit=seconds, stdin='DEVNULL'))
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    began = time.monotonic()
    deadline = began+seconds
    status, child, error = 'UNKNOWN', None, None
    launcher_pid, popen_pid = os.getpid(), None
    cleanup_errors = []
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        popen_pid = child.pid
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                selector.register(getattr(child, name), selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('original CLI exceeded capture deadline')
                ready = selector.select(remaining)
                if not ready:
                    raise TimeoutError('original CLI output deadline')
                for key, _ in ready:
                    buf = buffers[key.data]
                    part = os.read(key.fileobj.fileno(), min(65536, limit+1-len(buf)))
                    if part:
                        buf.extend(part)
                        if len(buf) > limit:
                            raise ValueError('original CLI output overflow')
                    else:
                        selector.unregister(key.fileobj)
            code = child.wait(timeout=max(.001, deadline-time.monotonic()))
            status = 'COMPLETE' if code == 0 else 'REFUSED'
    except BaseException as exc:
        error = dict(type=type(exc).__name__, error=str(exc))
    finally:
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()  # Only the subprocess just spawned by this function.
                    child.wait(timeout=1)
            except BaseException as exc:
                cleanup_errors.append(dict(operation='reap-original-child',type=type(exc).__name__,error=str(exc)))
            for name in buffers:
                try:
                    getattr(child, name).close()
                except BaseException as exc:
                    cleanup_errors.append(dict(operation='close-'+name,type=type(exc).__name__,error=str(exc)))
        if cleanup_errors:
            status = 'UNKNOWN'
    row = dict(status=status, returncode=child.returncode if child else None,
               began_monotonic=began, ended_monotonic=time.monotonic(), error=error,
               cleanup_errors=cleanup_errors, replay_allowed=False,
               launcher_pid=launcher_pid,popen_pid=popen_pid,
               pid_scope='actual Popen handle identity; no separate kernel child birth/ppid sample')
    for name, raw in buffers.items():
        row[name+'_bytes'] = len(raw)
        row[name+'_base64'] = base64.b64encode(raw).decode()
        row[name+'_sha256'] = f.sha(raw)
    f.save_once(directory/(label+'-RESULT.json'), row)
    f.need(status == 'COMPLETE', 'original CLI refused/UNKNOWN: '+str(error))
    return bytes(buffers['stdout']), bytes(buffers['stderr'])


def inbox(surface, directory, original):
    argv = [original.CMUX, 'agent', 'inbox', '--surface', surface,
            '--state', 'queued', '--limit', str(original.MAX_ROWS), '--json']
    raw, unused_stderr = capture(argv, directory, 'INBOX-RAW', original.MAX_QUERY, 2)
    parsed = original.strict_json(raw)
    f.save_once(directory/'INBOX-PARSED.json', parsed)
    return parsed
