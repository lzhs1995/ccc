"""Bounded adoption receipts for real task-hook entrypoints; never a task gate."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid


HOOKS = {'cmux_executor_closeout_guard.py', 'cmux_consensus_stop_guard.py',
         'cmux_lease_guard.py'}


def _entrypoint():
    main = getattr(sys.modules.get('__main__'), '__file__', '')
    path = Path(main).resolve()
    scripts = Path(__file__).resolve().parent
    if path.parent != scripts:
        return None
    if path.name == 'cmux_workflow_advisory.py':
        from cmux_workflow_advisory import hook_from_argv
        return hook_from_argv()
    return path.name if path.name in HOOKS else None


def _parent_chain():
    # Collect only immediate kernel process provenance. A manual test launched
    # through a tool shell has an intermediate process and is not automatic
    # client adoption. Never copy environment, command text or credentials.
    import cmux_daemon_identity as native
    result = []
    pid = os.getppid()
    for _ in range(3):
        try:
            row = native.process(pid, validate_argv=False)
            result.append({k: row[k] for k in ('pid', 'ppid', 'birth', 'executable')})
            pid = row['ppid']
        except Exception:
            break
    return result


def record(payload, scope_status, *, original_hook=None, mode=None):
    """Keep one private latest receipt per session/event/hook, without stdin."""
    temporary = None
    try:
        hook = _entrypoint()
        entrypoint = Path(getattr(sys.modules.get('__main__'), '__file__', '')).name
        if original_hook is not None and original_hook != hook:
            return
        advisory = entrypoint == 'cmux_workflow_advisory.py'
        if mode is not None and mode != ('advisory' if advisory else 'legacy-gate'):
            return
        session = str(uuid.UUID(payload.get('session_id') or payload.get('sessionId')))
        import cmux_hook_thread
        thread = cmux_hook_thread.describe(payload)
        event = payload.get('hook_event_name')
        if not hook or event not in {'PreToolUse', 'PostToolUse', 'Stop', 'SubagentStop'}:
            return
        source = Path(__file__).resolve().parents[1]
        version = (source / 'VERSION').read_text().strip()
        root = Path.home() / '.local/state/multi-agent-collaboration/runtime-adoption-v1'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        st = root.lstat()
        if root.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
            return
        receipt = dict(schema='hook-runtime-adoption-v1', source=str(source),
                       version=version, session_id=session, hook=hook, event=event,
                       original_hook=hook, entrypoint=entrypoint,
                       mode='advisory' if advisory else 'legacy-gate',
                       native_receipt=False,
                       scope_status=scope_status, pid=os.getpid(), ppid=os.getppid(),
                       time_ns=time.time_ns(), parents=_parent_chain(), **thread)
        actual = thread.get('actual_thread_id')
        key = actual if actual and thread['thread_scope'] != 'UNVERIFIED' else (
            session + '.unverified-' + thread.get('unverified_key', 'no-transcript'))
        target = root / (key + '.' + event + '.' + hook + '.json')
        fd, temporary = tempfile.mkstemp(prefix='.adoption-', dir=root)
        with os.fdopen(fd, 'w') as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.write('\n')
        os.replace(temporary, target)
        temporary = None
    except Exception:
        # Observability cannot recreate the outage it exists to diagnose.
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
