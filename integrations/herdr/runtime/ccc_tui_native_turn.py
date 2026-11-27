"""Official Codex TUI continuity for one explicitly enrolled live client.

The official recorder omits selected thread IDs. A fresh native /status is
therefore the initial anchor; every later selection operation invalidates it.
This module never writes a foreground observer record or submits terminal input.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

ROOT = Path.home() / 'Library/Application Support/cmux-codex-continue/native-tui-bindings'
MAX_LOG_BYTES = 8 * 1024 * 1024
SAFE_EVENTS = frozenset('''
AccountEmailLoaded SecuritySetupLoaded AgentsOverviewThreadsLoaded
AgentsOverviewSearchPage AgentPickerThreadsLoaded ThreadTitleStarted
GeneratedThreadTitle ThreadHistoryEntryResponse RequestOlderScrollbackHistory
OlderThreadHistoryLoaded OpenTranscriptExportFilePrompt OpenWarnings CopyWarning
UpdateWarnings ExportTranscript SelectTranscriptCopy TranscriptCopyClosed
CopySelection AppendMessageHistoryEntry SyncThreadGitBranch
LookupMessageHistoryEntry LookupMessageHistoryBatch StartupThreadStarted
ImagesPrepared StartFileSearch FileSearchResult TaskSearchResult
RefreshRateLimits RateLimitsLoaded ApplyBackendBannerFallback
OpenAnalytics OpenRateLimitResetCredits OpenRateLimitResetConfirmation
RestoreBackgroundTerminals BackgroundTerminalsLoaded RefreshThreadUsage ThreadUsageLoaded
AgentsOverviewUsageLoaded RefreshStatusLineWorkspaceHeadline
CommitPendingUsageOutput CommitPendingUsageOutputAfterStreamShutdown
ConnectorsLoaded InstalledConnectorMentionsLoaded DiffResult OpenAppLink
OpenUrlInBrowser PetSelected PetDisabled PetPreviewRequested PetPreviewLoaded
PetSelectionLoaded ConfiguredPetLoaded RefreshConnectors FetchConnectorsList
FetchInstalledConnectorMentions FetchPluginsList FetchHooksList PluginsLoaded
OpenPluginsList PluginRemoteSectionsLoaded HooksLoaded RefreshPluginMentions
PluginMentionsLoaded FetchMcpInventory McpInventoryLoaded SkillsListLoaded
BeginInitialHistoryReplayBuffer EndInitialHistoryReplayBuffer FollowTranscript
TurnTipReady ConsolidateAgentMessage CorrectFlushedAgentMessage
ConsolidateProposedPlan StartCommitAnimation StopCommitAnimation CommitTick
UpdateReasoningEffort UpdateLunaReserveReasoning UpdateModel ScopedModelPicker
ApplyModelSelection AstraSelectedFromModelPicker SettingsSelectionClosed
SettingsSelectionSettled PersistModelSelection SelectSessionModel
PersistServiceTierSelection PersistDaybreakSelection DisableUnavailableDaybreak
FetchModels ModelsLoaded ApiKeyCompatModelsLoaded FetchPermissionProfiles
PermissionProfilesLoaded OpenReasoningPopup OpenAdvancedReasoningPopup
ApplyAdvancedReasoning OpenPlanReasoningScopePrompt OpenAllModelsPopup
OpenFullAccessConfirmation ApplyPermissionShortcut UpdateAskForApprovalPolicy
UpdateActivePermissionProfile SelectPermissionProfile UpdateApprovalsReviewer
UpdatePlanModeReasoningEffort PersistPlanModeReasoningEffort
PersistRateLimitSwitchPromptHidden UpdateRateLimitSwitchPromptHidden
PersistModelMigrationPromptAcknowledged OpenApprovalsPopup OpenSkillsList
OpenManageSkillsPopup SetSkillEnabled SetAppEnabled SetHookEnabled TrustHook
TrustHooks HookEnabledSet HookTrusted ManageSkillsClosed OpenPermissionsPopup
ResolvePermissionProfile FullScreenApprovalRequest FullScreenUserVerificationRequest
OpenFeedback OpenFeedbackNote OpenFeedbackConsent SubmitFeedback FeedbackSubmitted
LaunchExternalEditor StatusLineBranchUpdated StatusLineGitSummaryUpdated
StatusLineWorkspaceHeadlineUpdated StatusLineSetup StatusLineSetupCancelled
TerminalTitleSetup TerminalTitleSetupPreview TerminalTitleSetupCancelled
PersistAgentsOverviewGrouping OpenConfigEditor TestConfigNotification
SendConfigTestNotification SaveConfigPreference FullscreenTranscriptSelected
SyntaxThemeSelected SyntaxThemePreviewed OpenKeymapActionMenu
OpenKeymapReplaceBindingMenu OpenKeymapCapture OpenKeymapDebug KeymapCaptured
KeymapCleared CodexOp SubmitThreadOp SubmitUserMessageWithMode
ConfirmSafetyBufferedRetry UserVerificationApproved UserVerificationFinished
RetrySafetyBufferedTurn ContinueSafetyRecourse ApproveRecentAutoReviewDenial
OpenThreadGoalMenu OpenThreadGoalEditor SetThreadGoalDraft SetThreadGoalStatus
ClearThreadGoal ConsumeRateLimitResetCredit RateLimitResetCreditConsumed
SendAddCreditsNudgeEmail AddCreditsNudgeEmailFinished DynamicToolCallCompleted
TaskToolsAvailable InsertHistoryCell OpenReviewBranchPicker
OpenReviewCommitPicker OpenReviewCustomPrompt GenerateRecap CheckRecap
RecapStarted RecapGenerated
'''.split())
# All of these produce a native command. Unknown commands are rejected, and
# every non-read command requires a newer native lifecycle before a retry.
READ_OPS = frozenset({'ListSkills'})
ACTIVITY_OPS = frozenset('''Interrupt CleanBackgroundTerminals RunUserShellCommand
UserTurn OverrideTurnContext ExecApproval PatchApproval ResolveElicitation
ResolveUserVerification UserInputAnswer RequestPermissionsResponse
ReloadUserConfig Compact SetThreadName Review ApproveGuardianDeniedAction'''.split())


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
        raise ValueError('invalid UUID')
    return value


def _stamp(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _read(path, limit, *, private=True):
    path = Path(path)
    first = path.lstat()
    if (not path.is_absolute() or path.resolve(strict=True) != path
            or not stat.S_ISREG(first.st_mode) or first.st_uid != os.getuid()
            or first.st_nlink != 1 or private and first.st_mode & 0o077
            or first.st_size > limit):
        raise ValueError('private canonical native evidence required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb', buffering=0) as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (_stamp(first) != _stamp(before) or _stamp(before) != _stamp(after)
            or _stamp(after) != _stamp(path.lstat()) or len(raw) > limit):
        raise ValueError('native evidence changed during read')
    return raw, _stamp(after)


def _json(path):
    raw, identity = _read(path, 65536)
    value = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(value, dict):
        raise ValueError('native evidence is not an object')
    return value, {
        'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'identity': identity}


def _time(value):
    from ccc_codex_queue import epoch
    result = epoch(value)
    if not math.isfinite(result):
        raise ValueError('nonfinite native timestamp')
    return result


def _log(raw, *, cwd, born, created):
    if not raw or not raw.endswith(b'\n'):
        raise ValueError('native TUI record missing or torn')
    rows = [json.loads(line, object_pairs_hook=_object) for line in raw.splitlines()]
    first = rows[0]
    if (not isinstance(first, dict) or first.get('dir') != 'meta'
            or first.get('kind') != 'session_start' or first.get('cwd') != cwd
            or not created <= _time(first.get('ts')) + .001
            or _time(first.get('ts')) + .001 < born):
        raise ValueError('native TUI header does not bind launch')
    latest, starts, pending = _time(first['ts']), 0, 0.0
    for event in rows[1:]:
        if not isinstance(event, dict):
            raise ValueError('invalid native TUI record')
        at = _time(event.get('ts'))
        if at < latest or at > time.time() + 1:
            raise ValueError('native TUI clock regression')
        latest = at
        route = event.get('dir'), event.get('kind')
        if route == ('to_tui', 'app_event'):
            variant = event.get('variant')
            if variant not in SAFE_EVENTS:
                raise ValueError('native selection or unsupported app event: ' + str(variant))
            starts += variant == 'StartupThreadStarted'
            if starts > 1:
                raise ValueError('native startup repeated')
            if variant in {'CodexOp', 'SubmitThreadOp', 'SubmitUserMessageWithMode'}:
                pending = at
        elif route == ('to_tui', 'insert_history_cell'):
            if type(event.get('lines')) is not int or event['lines'] < 0:
                raise ValueError('invalid history record')
        elif route in {('to_tui', 'file_search_start'), ('to_tui', 'file_search_result')}:
            pass
        elif route == ('from_tui', 'op'):
            payload = event.get('payload')
            if isinstance(payload, str):
                operation = payload
            elif isinstance(payload, dict) and len(payload) == 1:
                operation = next(iter(payload))
            else:
                raise ValueError('ambiguous native operation')
            if operation in ACTIVITY_OPS:
                pending = at
            elif operation not in READ_OPS:
                raise ValueError('unsupported native operation: ' + str(operation))
        else:
            raise ValueError('native session ended, changed or unrecognized')
    return {'last_activity_at': pending, 'header_at': _time(first['ts']),
            'records': len(rows), 'last_record_at': latest}


def _process(target, pid):
    from ccc_guard_scope import process, arguments
    from ccc_native_processes import display_process, _display_tty_device
    client = process(pid)
    screen = display_process(pid)
    argv, env = arguments(pid)
    if (not client or not screen or screen.get('agent_kind') != 'codex'
            or client.get('remote') or client.get('backend')
            or client.get('surface_id') != target['surface_id']
            or client.get('environment_workspace_id') != target['workspace_id']
            or screen.get('surface_id') != target['surface_id']
            or screen.get('environment_workspace_id') != target['workspace_id']
            or tuple(screen.get('birth', ())) != tuple(client['birth'])
            or not screen.get('pgid', 0) > 0 or screen.get('pgid') != screen.get('tpgid')
            or target.get('tty') and _display_tty_device(target) != screen.get('tdev')
            or env.get('CODEX_CLIENT_THREAD_OBSERVER') is not None
            or env.get('CODEX_TUI_RECORD_SESSION') != '1'
            or not env.get('CODEX_TUI_SESSION_LOG_PATH')):
        raise ValueError('not the enrolled local official TUI client')
    return client, screen, argv, env


def _record_path(pid, birth):
    return ROOT / (f'{pid}-{birth[0]}-{birth[1]}.json')


def _native_log(target, pid, receipt):
    from ccc_shared_codex_turn import writer_files
    client, screen, argv, env = _process(target, pid)
    if client['birth'] != receipt['birth'] or client['pid'] != receipt['pid']:
        raise ValueError('enrolled PID generation changed')
    path = Path(env['CODEX_TUI_SESSION_LOG_PATH'])
    if str(path) != receipt['log_path']:
        raise ValueError('native TUI log redirected')
    raw, signature = _read(path, MAX_LOG_BYTES)
    if signature[:2] != receipt['log_identity']:
        raise ValueError('native TUI log replaced')
    files = writer_files(pid)
    if files.get(path) != {'device': signature[0], 'inode': signature[1]}:
        raise ValueError('enrolled TUI no longer holds its event log')
    born = client['birth'][0] + client['birth'][1] / 1e6
    parsed = _log(raw, cwd=receipt['cwd'], born=born, created=receipt['created_at'])
    if (len(raw) < receipt.get('prefix_bytes', 0)
            or hashlib.sha256(raw[:receipt.get('prefix_bytes', 0)]).hexdigest()
                != receipt.get('prefix_sha256', hashlib.sha256(b'').hexdigest())):
        raise ValueError('native TUI initial prefix changed')
    if _process(target, pid) != (client, screen, argv, env) or _stamp(path.lstat()) != signature:
        raise ValueError('native TUI generation changed during observation')
    return raw, signature, parsed, client


def enroll(target, pid, session_id, prelaunch_path, status_path):
    """One O_EXCL enrollment from an actual post-launch /status capture."""
    session_id = _uuid(session_id)
    launch, launch_ref = _json(prelaunch_path)
    status, status_ref = _json(status_path)
    if (launch.get('schema') != 'ccc-native-tui-launch-v1'
            or any(launch.get(k) != target[k] or status.get(k) != target[k]
                   for k in ('surface_id', 'workspace_id'))
            or launch.get('expected_session_id') != session_id
            or status.get('session_id') != session_id or status.get('pid') != pid):
        raise ValueError('original launch and current status disagree')
    found = re.findall(r'\bSession\s*:\s*([0-9a-fA-F-]{36})\b', str(status.get('status_text', '')))
    if found != [session_id]:
        raise ValueError('current native status has no unique enrolled session')
    birth = status['birth']
    born = birth[0] + birth[1] / 1e6
    if (not math.isfinite(launch['created_at']) or not launch['created_at'] <= born
            or not born <= status['observed_at'] <= time.time() + 1
            or time.time() - status['observed_at'] > 120):
        raise ValueError('native status capture is not current')
    receipt = {'schema': 'ccc-official-tui-binding-v1', 'pid': pid, 'birth': birth,
        'surface_id': target['surface_id'], 'workspace_id': target['workspace_id'],
        'session_id': session_id, 'log_path': launch['log_path'],
        'log_identity': launch['log_identity'], 'cwd': launch['cwd'],
        'created_at': launch['created_at'], 'launch_evidence': launch_ref,
        'status_evidence': status_ref, 'enrolled_at': time.time()}
    raw, _, _, _ = _native_log(target, pid, receipt)
    receipt.update(prefix_bytes=len(raw), prefix_sha256=hashlib.sha256(raw).hexdigest())
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _record_path(pid, birth)
    data = (json.dumps(receipt, sort_keys=True, separators=(',', ':')) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(data); out.flush(); os.fsync(out.fileno())
    return path


def selection(target, pid):
    """Return advisory selection proof; existing native writer gates follow."""
    from ccc_guard_scope import birth
    try:
        identity = birth(pid, codex=True)
        if not identity:
            return 'invalid', None, None
        path = _record_path(pid, identity)
        if not path.exists():
            from ccc_guard_scope import arguments
            _, env = arguments(pid)
            log = Path(env.get('CODEX_TUI_SESSION_LOG_PATH') or '.')
            owned_log = (env.get('CODEX_TUI_RECORD_SESSION') == '1'
                         and log.is_absolute() and log.is_relative_to(ROOT))
            return ('invalid' if owned_log else 'absent'), None, None
        receipt, enrollment = _json(path)
        if (receipt.get('schema') != 'ccc-official-tui-binding-v1'
                or any(receipt.get(k) != target[k] for k in ('surface_id', 'workspace_id'))):
            raise ValueError('native enrollment target changed')
        sid = _uuid(receipt['session_id'])
        for field in ('launch_evidence', 'status_evidence'):
            expected = receipt[field]
            _, actual = _json(Path(expected['path']))
            if actual != expected:
                raise ValueError('original enrollment evidence changed')
        raw, signature, parsed, _ = _native_log(target, pid, receipt)
        proof = {'kind': 'official_tui_record', 'enrollment': enrollment,
            'log_signature': signature, 'log_sha256': hashlib.sha256(raw).hexdigest(), **parsed}
        return 'ok', sid, proof
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError):
        return 'invalid', None, None


def lifecycle_matches(selection, snapshot, path=None):
    """New accepted input must never reuse a completion seen before that input."""
    proof = selection[2] if selection and len(selection) == 3 else None
    if not isinstance(proof, dict) or proof.get('kind') != 'official_tui_record':
        return True
    at = snapshot.get('at') if isinstance(snapshot, dict) else None
    pending = proof['last_activity_at']
    if not (type(at) in (int, float) and math.isfinite(at) and at > pending):
        return False
    if not pending or snapshot.get('kind') not in {'task_complete', 'turn_aborted'}:
        return True
    # A delayed completion from the old turn may arrive after a new TUI op.
    # Require the matching native turn start, not merely a later error clock.
    if path is None or not snapshot.get('turn_id'):
        return False
    try:
        path = Path(path)
        before = _stamp(path.stat())
        with path.open('rb') as stream:
            size = before[2]
            offset = max(0, size - 1024 * 1024)
            stream.seek(offset)
            raw = stream.read(1024 * 1024 + 1)
            if offset:
                raw = raw.split(b'\n', 1)[1] if b'\n' in raw else b''
            if _stamp(os.fstat(stream.fileno())) != before:
                return False
        if not raw or not raw.endswith(b'\n') or _stamp(path.stat()) != before:
            return False
        for line in reversed(raw.splitlines()):
            event = json.loads(line, object_pairs_hook=_object)
            payload = event.get('payload', {})
            if (event.get('type') == 'event_msg' and payload.get('type') == 'task_started'
                    and payload.get('turn_id') == snapshot['turn_id']):
                return pending < _time(event['timestamp']) <= at
        return False
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False
