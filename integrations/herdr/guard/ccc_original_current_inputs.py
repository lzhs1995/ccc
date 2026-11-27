"""Finite local-input validation for the original, simple custom-provider case.

This is not a configuration loader. Nonempty system/project/profile/managed
layers are returned as specific unsupported cases, not approximated or ignored.
No credential helper, login, probe or alternate client is started.
"""
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from urllib.parse import urlsplit

from ccc_herdr_exit_context import canonical, need


MAX_CONFIG = 2 * 1024 * 1024


def file_input(path):
    path=Path(path)
    need(path.is_absolute(), 'configuration_path_not_absolute')
    try:
        before=path.lstat()
    except FileNotFoundError:
        return None, dict(path=str(path),exists=False)
    need(path.resolve(strict=True)==path and stat.S_ISREG(before.st_mode)
         and before.st_uid in {0,os.getuid()} and not before.st_mode&0o022
         and before.st_size<=MAX_CONFIG, 'configuration_file_not_owned_regular')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        opened=os.fstat(stream.fileno());raw=stream.read(MAX_CONFIG+1);after=os.fstat(stream.fileno())
    ident=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode,s.st_uid)
    need(ident(before)==ident(opened)==ident(after)==ident(path.lstat())
         and len(raw)<=MAX_CONFIG,'configuration_changed_during_read')
    return tomllib.loads(raw.decode('utf-8')),dict(path=str(path),exists=True,
        device=before.st_dev,inode=before.st_ino,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())


def forced_preferences():
    lib=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    make=lib.CFStringCreateWithCString;make.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32];make.restype=ctypes.c_void_p
    forced=lib.CFPreferencesAppValueIsForced;forced.argtypes=[ctypes.c_void_p,ctypes.c_void_p];forced.restype=ctypes.c_bool
    release=lib.CFRelease;release.argtypes=[ctypes.c_void_p];release.restype=None
    application=make(None,b'com.openai.codex',0x08000100)
    need(bool(application),'managed_preferences_application_unavailable')
    result={}
    try:
        for name in ('config_toml_base64','requirements_toml_base64'):
            key=make(None,name.encode(),0x08000100)
            need(bool(key),'managed_preferences_key_unavailable')
            try:result[name]=bool(forced(key,application))
            finally:release(key)
    finally:release(application)
    return dict(source='CFPreferencesAppValueIsForced',forced=result)


def current_inputs(document, plan, *, system_root=Path('/etc/codex'), managed_reader=forced_preferences):
    environment=plan['environment'];home=Path(document['codex_home']);cwd=Path(plan['cwd'])
    need(home.resolve(strict=True)==home and cwd.resolve(strict=True)==cwd,
         'original_home_or_cwd_changed')
    if 'CODEX_HOME' in environment:
        need(Path(environment['CODEX_HOME']).resolve(strict=True)==home,'original_home_environment_conflict')
    else:
        need(Path(environment['HOME'])/'.codex'==home,'original_default_home_conflict')
    files=[]
    config,pin=file_input(home/'config.toml');files.append(pin)
    need(isinstance(config,dict),'original_current_config_missing')
    need(not config.get('profile') and not config.get('project_root_markers'),
         'selected_profile_or_custom_project_discovery_requires_review')
    for name in ('config.toml','managed_config.toml','requirements.toml'):
        layer,pin=file_input(system_root/name);files.append(pin)
        need(not layer,'nonempty_system_configuration_requires_review:'+name)
    managed=managed_reader()
    need(managed.get('source')=='CFPreferencesAppValueIsForced'
         and managed.get('forced')=={'config_toml_base64':False,'requirements_toml_base64':False},
         'managed_configuration_requires_review')
    # Empty files along the complete ancestor chain are a sufficient simple
    # case regardless of Git root/trust. A nonempty layer is not merged here.
    for parent in (cwd,*cwd.parents):
        path=parent/'.codex/config.toml'
        if path==home/'config.toml':continue
        layer,pin=file_input(path);files.append(pin)
        need(not layer,'nonempty_project_configuration_requires_review:'+str(path))
    provider_id=plan['expected_model_triple']['provider']
    need(re.fullmatch(r'[A-Za-z0-9_-]+',provider_id),'unreviewed_provider_identifier')
    provider=copy.deepcopy(config.get('model_providers',{}).get(provider_id))
    need(isinstance(provider,dict),'current_provider_definition_missing')
    prefix='model_providers.'+provider_id+'.'
    flags=plan['argv'][1:-2];i=0
    sqlite_home=config.get('sqlite_home')
    sqlite_origin='config'
    while i<len(flags):
        flag=flags[i]
        if flag in {'--no-daemon','--no-alt-screen'}:i+=1;continue
        need(flag in {'-c','--config'} and i+1<len(flags),'unreviewed_normalized_flags')
        key,_,value=flags[i+1].partition('=');i+=2
        try:parsed=tomllib.loads('v='+value)['v']
        except tomllib.TOMLDecodeError:parsed=value.strip().strip('"\'')
        if key.startswith(prefix):
            leaf=key[len(prefix):]
            need(re.fullmatch(r'[A-Za-z0-9_]+',leaf),'nested_provider_override_requires_review')
            provider[leaf]=parsed
        elif key=='sqlite_home':
            sqlite_home=parsed
            sqlite_origin='argv'
        elif key in {'model_providers','model_providers.'+provider_id,'features','config',
                     'requirements','project_root_markers'}:
            raise ValueError('table_override_requires_review')
    # The provider itself is passed intact to the native loader. Only the
    # simple request-auth branch already used by this fleet is supported.
    need(provider.get('requires_openai_auth') is False and provider.get('wire_api')=='responses',
         'provider_auth_or_wire_type_requires_review')
    need(set(provider)<={'name','base_url','wire_api','requires_openai_auth',
        'env_key','env_key_instructions','experimental_bearer_token','http_headers',
        'env_http_headers','request_max_retries','stream_max_retries','stream_idle_timeout_ms',
        'websocket_connect_timeout_ms','supports_websockets'},
        'provider_definition_extension_requires_review')
    endpoint=provider.get('base_url');url=urlsplit(endpoint) if isinstance(endpoint,str) else None
    need(url and url.scheme in {'http','https'} and url.hostname and not url.username
         and not url.password and not url.query and not url.fragment,'provider_endpoint_unverified')
    credential={}
    if provider.get('env_key'):
        key=provider['env_key'];value=environment.get(key)
        need(isinstance(value,str) and bool(value.strip()),'required_original_provider_environment_missing')
        credential=dict(source='original_environment',name=key,sha256=hashlib.sha256(value.encode()).hexdigest())
    else:
        value=provider.get('experimental_bearer_token')
        need(isinstance(value,str) and bool(value.strip()),'provider_bearer_token_missing')
        credential=dict(source='current_provider.experimental_bearer_token',sha256=hashlib.sha256(value.encode()).hexdigest())
    env_headers=provider.get('env_http_headers') or {}
    need(isinstance(env_headers,dict),'unreviewed_provider_environment_headers')
    for key,name in env_headers.items():
        need(isinstance(name,str) and environment.get(name),'required_original_header_environment_missing')
    if sqlite_home is None:
        sqlite_home=environment.get('CODEX_SQLITE_HOME','').strip() or str(home)
        sqlite_origin='environment'
    need(isinstance(sqlite_home,str),'invalid_current_sqlite_home')
    database_home=Path(sqlite_home)
    if not database_home.is_absolute():
        need(sqlite_origin!='config','relative_file_sqlite_home_requires_review')
        database_home=cwd/database_home
    database_home=database_home.resolve(strict=True)
    original_db=Path(document['configuration_evidence']['database_locator']['path'])
    need(database_home/'state_5.sqlite'==original_db,'next_launch_database_namespace_changed')
    # Verify the same finite inputs again before returning. The execution
    # adapter repeats this after acquiring the original input locks.
    for pin in files:
        _,again=file_input(pin['path']);need(again==pin,'current_configuration_changed')
    need(managed_reader()==managed,'managed_configuration_changed')
    return dict(schema='ccc-original-current-launch-inputs-v1',state='SIMPLE_LOCAL_INPUTS_VERIFIED',
        session_id=plan['session_id'],provider_id=provider_id,
        provider_definition_sha256=hashlib.sha256(canonical(provider)).hexdigest(),
        credential_source=credential,endpoint=endpoint,files=files,managed=managed,
        sqlite_home=str(database_home),original_environment_sha256=hashlib.sha256(canonical(environment)).hexdigest(),
        profile=None,historical_runtime_configuration='UNKNOWN',
        local_configuration_scope=True,model_or_auth_probe=0,execution_authorized=False)
