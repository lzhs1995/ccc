"""Admit the original callback's one successor after a proven zero-input return.

This module never sends. The original, pack-pinned controller retains its live
identity/composer checks, persistent lock and two-attempt journal limit.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

from cmux_callback_queue_resume import canonical
from cmux_evidence_io import attempt_paths, read_bytes
from cmux_submission_inputs import literal_command_argv


def allowed(payload, marker, evidence):
    """One exact synchronous original CLI; no new sender or mandatory retry."""
    try:
        if (payload.get('tool_name') != 'Bash'
                or evidence.get('attempt_phase') != 'NO_INPUT'):
            return False
        task = canonical(Path(marker['artifact_root']) / 'task-pack.json')
        raw_pack = read_bytes(task)
        if hashlib.sha256(raw_pack).hexdigest() != evidence['task_pack_sha256']:
            return False
        pack = json.loads(raw_pack)
        skill = canonical(pack['required_skill'])
        controller = canonical(skill.parent / 'scripts/cmux_bridge.py')
        if skill.name != 'SKILL.md' or not controller.is_file():
            return False
        argv = [sys.executable, '-B', str(controller),
                'submit-completion-callback', '--task-pack', str(task)]
        tool = payload.get('tool_input', {})
        if (literal_command_argv(tool.get('command')) != ['rtk', 'proxy', *argv]
                or tool.get('run_in_background')):
            return False
        if literal_command_argv(pack.get('callback_command', shlex.join(argv))) != argv:
            return False
        receipt = Path(pack['completion_receipt'])
        if (receipt.parent != task.parent or os.path.lexists(receipt)
                or os.path.lexists(Path(str(receipt) + '.pending.json'))):
            return False
        journal = canonical(receipt.with_name(receipt.stem + '-attempts'))
        first = canonical(journal / 'attempt-0001.json')
        if attempt_paths(journal) != [first] or str(first) != evidence['attempt']:
            return False
        raw_attempt = read_bytes(first)
        attempt = json.loads(raw_attempt)
        if (hashlib.sha256(raw_attempt).hexdigest() != evidence['attempt_sha256']
                or attempt.get('phase') != 'NO_INPUT' or attempt.get('events') != []):
            return False
        report = canonical(pack['report'])
        return (str(report) == evidence['report']
                and hashlib.sha256(read_bytes(report)).hexdigest() == evidence['report_sha256'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return False
