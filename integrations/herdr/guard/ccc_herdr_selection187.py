"""Guard-only native selection barrier around the complete original reader.

Native foreground publication is a veto before any legacy reader is entered.
The complete reader still owns process, placement, writer, rollout and Goal
validation. This module neither chooses a transcript nor submits input.
"""
from __future__ import annotations


def unknown(status, reason):
    return ({'kind': 'unknown', 'native_foreground_status': status,
             'reason': reason}, {'status': 'unknown'})


def guarded_current(process, original_read, foreground, opt_in, *, standalone_read=None):
    record = {}
    before = foreground.read_foreground(process['pid'], opt_in, _record=record)
    status, selected, evidence = before
    if status not in {'ok', 'absent'}:
        # A complete optional schema1 file from an earlier PID birth is only
        # a veto exception for the original, independently proved direct writer.
        # Required/current/schema2, shared writers, clear and changing evidence
        # remain refusals. This neither removes a file nor restores an old SID.
        if status == 'invalid' and standalone_read is not None:
            return stale_standalone_current(process, original_read, standalone_read,
                                            foreground, opt_in)
        return unknown(status, 'Native foreground observation invalid or expired')
    if status == 'ok' and selected is None:
        return unknown('clear', 'Native foreground selection is cleared')
    if status == 'ok' and record.get('schema') not in {1, 2}:
        return unknown('invalid', 'Native foreground schema is unavailable')

    # Include Goal acquisition inside the native lease fence. A successful
    # optional legacy read must remain optional; any publication veto wins.
    turn, goal = original_read()
    after_record = {}
    after = foreground.read_foreground(process['pid'], opt_in, _record=after_record)
    if status == 'absent':
        if after[0] != 'absent':
            return unknown(after[0], 'Native foreground changed during legacy observation')
    elif (after[0] != 'ok' or after_record.get('schema') != record['schema']
          or before[1:] != after[1:]):
        return unknown(after[0], 'Native selection changed or expired during observation')
    if (not isinstance(turn, dict) or turn.get('kind') == 'unknown'
            or turn.get('pid') != process['pid']
            or turn.get('birth') != process['birth'] or not turn.get('session_id')):
        reason = turn.get('reason') if isinstance(turn, dict) else None
        return unknown(status, reason or 'Complete original native thread reader is unbound')
    if status == 'ok' and turn['session_id'] != selected:
        return unknown(status, 'Complete original reader disagrees with current selection')

    # Schema2 evidence intentionally excludes heartbeat timestamps and file
    # inode replacement. The producer's selection/connection generation stays
    # part of the proof, so same-SID republication is not an old input authority.
    current = dict(turn)
    current.update(native_foreground_status=status,
                   native_selection_schema=record.get('schema'),
                   native_selection_evidence=evidence,
                   native_selection_source='schema2_committed_foreground'
                       if record.get('schema') == 2 else
                       'schema1_authenticated_foreground' if status == 'ok' else
                       'independent_optional_legacy_reader',
                   native_startup_resume_id=process.get('native_resume_id'))
    return current, goal


def stale_standalone_current(process, original_read, standalone_read, foreground, opt_in):
    """Preserve the original stale-metadata exception inside the Herdr barrier.

    Only QueueRecovery._stale_foreground_standalone_turn may supply this callback.
    It proves actual native process/placement, an exclusive held writer lock,
    unchanged rollout identity and full lifecycle independently of startup argv.
    Repeat that complete proof after Goal acquisition and never accept a shared
    writer or a transition to a new/cleared foreground record in this interval.
    """
    refusal = lambda: unknown('invalid', 'Native foreground observation invalid or expired')
    stale_reader = getattr(foreground, 'stale_optional_foreground', None)
    if not callable(stale_reader):
        return refusal()
    stale = stale_reader(process['pid'], opt_in)
    if stale is None:
        return refusal()
    standalone = standalone_read()
    if (not isinstance(standalone, dict) or standalone.get('kind') == 'unknown'
            or 'shared_writer' in standalone or standalone.get('pid') != process['pid']
            or standalone.get('birth') != process['birth']
            or not standalone.get('session_id')):
        return refusal()
    turn, goal = original_read()
    if (turn != standalone or foreground.read_foreground(process['pid'], opt_in)[0] != 'invalid'
            or stale_reader(process['pid'], opt_in) != stale
            or standalone_read() != standalone
            or stale_reader(process['pid'], opt_in) != stale
            or foreground.read_foreground(process['pid'], opt_in)[0] != 'invalid'):
        return refusal()
    current = dict(turn)
    current.update(native_foreground_status='stale_optional_standalone',
                   native_selection_schema=None,
                   native_selection_evidence=stale,
                   native_selection_source='original_standalone_writer_with_stale_optional_metadata',
                   native_startup_resume_id=process.get('native_resume_id'))
    return current, goal
