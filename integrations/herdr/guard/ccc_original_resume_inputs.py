"""Minimal same-SID launch normalization; no alternate config loader.

Uses Codex's existing RestoreFromThread branch. Current provider/config
inspection belongs to the original execution adapter, not this pure planner.
"""
from ccc_public_protection import PUBLIC_PROTECTED_PID, PUBLIC_PROTECTED_SURFACE
import hashlib
import json
from pathlib import Path
import re

from ccc_herdr_exit_context import SCHEMA, PROTECTED, need
from ccc_resume_metadata import logical_fidelity
from ccc_resume_branch import MODEL_KEYS


def normalized_plan(document, settings, metadata):
    need(document.get('schema') == SCHEMA and document.get('capture_format') == 2,
         'original_configuration_context_missing')
    binding = document['binding']
    need(binding.get('pid') != PUBLIC_PROTECTED_PID and binding.get('cmux_surface_id', '').upper() != PROTECTED,
         'protected_business_surface')
    logical = logical_fidelity(document, settings, metadata)
    need(logical.get('logical_session_fidelity') is True, logical.get('reason', 'logical_settings_unknown'))
    session_id = document['session_id']
    proof = document['selected_reader_evidence']
    need(proof.get('session_id') == session_id and proof.get('binding') == binding,
         'saved_current_selection_unverified')
    original = document['original_launch_observation']
    argv = original['argv']
    need(isinstance(argv, list) and argv and all(isinstance(x, str)
         and not any(c in x for c in '\0\r\n') for x in argv), 'unreviewed_original_argv')
    flags = list(argv[1:])
    # Strip only the exact parsed trailing resume selector. The selected SID
    # comes from the authenticated capture, not from this historical argument.
    if len(flags) >= 2 and flags[-2] == 'resume':
        from uuid import UUID
        need(str(UUID(flags[-1])) == flags[-1], 'unreviewed_old_resume_selector')
        flags = flags[:-2]
    retained, removed, i = [], [], 0
    while i < len(flags):
        flag = flags[i]
        if flag in {'--no-daemon', '--no-alt-screen'}:
            retained.append(flag); i += 1
        elif flag in {'-m', '--model'} and i+1 < len(flags):
            removed.append(flags[i:i+2]); i += 2
        elif flag in {'-C', '--cd'} and i+1 < len(flags):
            # Launch from the current persisted cwd. Retaining a historical
            # -C forces ResumeCwdMode::Current and can restore the wrong cwd.
            removed.append(flags[i:i+2]); i += 2
        elif flag in {'-c', '--config'} and i+1 < len(flags):
            raw = flags[i+1]
            key, sep, value = raw.partition('=')
            need(sep and re.fullmatch(r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*',key),
                 'unreviewed_config_override')
            if key in MODEL_KEYS:
                removed.append(flags[i:i+2])
            else:
                # Table-form model overrides can also affect the triple. Do
                # not silently claim that removing leaf keys removes those.
                need(key not in {'profile', 'profiles', 'harness'}
                     and not key.startswith(('profiles.', 'harness.')), 'profile_override_needs_review')
                retained.extend(flags[i:i+2])
            i += 2
        else:
            raise ValueError('unreviewed_launch_flag_profile_or_initial_prompt')
    cwd = settings['snapshot']['settings']['cwd']
    need(Path(cwd).is_absolute() and Path(cwd).resolve(strict=True) == Path(cwd)
         and Path(cwd).is_dir(), 'persisted_working_directory_unavailable')
    need(metadata['metadata']['cwd'] == cwd, 'persisted_working_directory_conflict')
    binary = document['executable']
    need(Path(binary).is_absolute(), 'original_client_path_not_absolute')
    return dict(schema='ccc-original-resume-normalized-inputs-v1',
        session_id=session_id, binding=binding, argv=[binary, *retained, 'resume', session_id],
        cwd=cwd, environment=original['environment'],
        binary_sha256=document['executable_sha256'],
        original_argv_sha256=hashlib.sha256(json.dumps(argv, separators=(',', ':')).encode()).hexdigest(),
        removed_startup_overrides=removed, model_restore_mode='RestoreFromThread',
        expected_model_triple={k: logical[k] for k in ('model', 'provider', 'effort')},
        history_identity=[document['history']['device'], document['history']['inode']],
        history_header_sha256=document['history']['header_sha256'],
        settings_snapshot_sha256=settings['snapshot']['sha256'],
        historical_route='UNKNOWN_EXCEPT_ORIGINAL_EXPLICIT_OVERRIDES',
        current_launch_inputs_verified=False, execution_authorized=False)
