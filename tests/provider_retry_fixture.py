"""Original provider evidence and elapsed SQLite cooldown for UI-only tests."""
import cmux_codex_watch as core
from ccc_provider_retry import ProviderRetryStore

def bind_ready_provider(test, daemon, error):
    """Bind the UI test to an original failed turn and elapsed real-store cooldown."""
    now = [1000.0]
    turn = {'kind': 'task_complete', 'session_id': 'original', 'turn_id': 'failed',
            'at': 200.0, 'model_provider': 'synthetic-provider', 'error': {'message': error}}
    daemon.codex_queue_recovery.current_turn = lambda _: dict(turn)
    daemon._provider_retry = ProviderRetryStore(daemon._provider_retry.path,
        clock=lambda: now[0], jitter=lambda: 0)
    daemon._provider_retry.observe('original', 'synthetic-provider', 'failed',
        core._match_error_block(error), error, 200.0)
    now[0] += 15
    test.addCleanup(daemon._process_snapshots.close)

