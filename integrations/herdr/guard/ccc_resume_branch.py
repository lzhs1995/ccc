"""Preserve Codex's native RestoreFromThread branch when its inputs agree.

This calculates a review plan only. No CLI, app-server or terminal is invoked.
Base/project model defaults do not imply a model override in the TUI. Explicit
session model/provider/effort flags and a selected profile's model fields do.
"""
from ccc_public_protection import PUBLIC_PROTECTED_PID, PUBLIC_PROTECTED_SURFACE
from pathlib import Path
import uuid

MODEL_KEYS={'model','model_provider','model_reasoning_effort'}


def need(value,reason):
    if not value:raise ValueError(reason)


def launch_flags(document):
    sid=document['session_id'];need(str(uuid.UUID(sid))==sid,'noncanonical_sid')
    argv=document['original_launch_observation']['argv']
    need(isinstance(argv,list) and argv and all(isinstance(x,str) and
         not any(c in x for c in '\0\r\n') for x in argv),'invalid_original_argv')
    flags=argv[1:]
    if len(flags)>=2 and flags[-2]=='resume':
        need(flags[-1]==sid,'startup_sid_differs_from_saved_selected_sid')
        flags=flags[:-2]
    result=dict(flags=flags,model_override_keys=[],profile=None)
    i=0
    while i<len(flags):
        flag=flags[i]
        if flag in {'--no-daemon','--no-alt-screen'}:i+=1
        elif flag in {'-c','--config'} and i+1<len(flags):
            key,sep,_=flags[i+1].partition('=');need(sep,'malformed_config_flag')
            if key.strip() in MODEL_KEYS:result['model_override_keys'].append(key.strip())
            i+=2
        elif flag in {'-m','--model'} and i+1<len(flags):
            result['model_override_keys'].append('model');i+=2
        elif flag in {'-p','--profile'} and i+1<len(flags):
            need(result['profile'] is None,'multiple_profile_flags')
            result['profile']=flags[i+1];i+=2
        else:raise ValueError('unreviewed_launch_flag_or_prompt')
    return result


def select_branch(document, logical, route, *, selected_profile):
    """selected_profile explicitly records no selection or its observed keys.

The route is a separate current-definition observation, never an inference from
SQLite's provider ID. Missing historical endpoint proof is not a veto.
"""
    need(document['binding'].get('pid')!=PUBLIC_PROTECTED_PID and document['binding'].get('cmux_surface_id','').upper()
         !=PUBLIC_PROTECTED_SURFACE,'protected_business_surface')
    launch=launch_flags(document)
    need(logical.get('logical_session_fidelity') is True
         and logical.get('session_id')==document['session_id'],'logical_session_settings_unverified')
    need(route.get('provider_id')==logical['provider']
         and route.get('current_definition_present') is True
         and route.get('required_inputs_available') is True,'current_provider_missing_or_conflicting')
    need(route.get('provenance') in {'original_explicit_argv','captured_config','current_config_only'},
         'current_provider_route_unknown')
    need(selected_profile.get('observed') is True and selected_profile.get('name')==launch['profile'],
         'selected_profile_unverified')
    profile_keys=selected_profile.get('model_fields')
    need(isinstance(profile_keys,list) and set(profile_keys)<=MODEL_KEYS,'invalid_profile_projection')
    explicit=bool(launch['model_override_keys'] or profile_keys)
    result=dict(schema='ccc-original-resume-branch-v1',session_id=document['session_id'],
        logical_session_fidelity=True,provider_route_fidelity=route['provenance'],
        historical_endpoint=route.get('historical_endpoint','UNKNOWN'),
        startup_model_override_keys=launch['model_override_keys'],profile_model_fields=profile_keys,
        default_config_model_does_not_override_persisted=True,
        input_operations=0,execution_authorized=False,
        required_at_execution=['current_original_recovery_policy','original_process_gone',
            'same_original_empty_terminal','native_no_other_writer',
            'current_original_abort_and_goal_checks','original_single_use_intent',
            'same_sid_new_native_receipt_and_natural_turn_settings'])
    if explicit:
        return dict(result,resume_mode='OverrideFromCurrentConfig',
            status='FULL_TRIPLE_OVERRIDE_PLAN_REQUIRED',
            nullable_effort_needs_native_handling=logical.get('effort') is None)
    need(Path(document['executable']).is_absolute(),'original_client_not_absolute')
    return dict(result,resume_mode='RestoreFromThread',status='DEFAULT_BRANCH_REVIEWABLE',
        argv=[document['executable'],*launch['flags'],'resume',document['session_id']],
        injected_model_flags=0, nullable_effort_preserved_by_native_resume=True)
