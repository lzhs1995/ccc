"""Opt-in native terminal evidence for tests of downstream delivery layers."""
from unittest.mock import Mock


def bind_native_failure(daemon, message):
    """Keep one failed turn per surface; never derive authority from a banner."""
    reader = Mock(side_effect=lambda target: {
        'kind': 'task_complete', 'session_id': 'native-' + target['surface_id'],
        'turn_id': 'failed-one', 'at': 100, 'error': {'message': message},
    })
    daemon.codex_queue_recovery.current_turn = reader
    return reader
