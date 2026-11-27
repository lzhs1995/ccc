"""Use the original guard's provider ledger for an owned-child recovery.

This adapter neither constructs a store nor changes its policy. Preview uses
the original fair waiter queue; only reservation consumes a remedy. A failed
post-commit readback stays uncertain and is never refunded or retried here.
"""
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time

from ccc_herdr_exit_context import PinnedDirectory, canonical, need
from ccc_original_exit_boundary import OriginalExitAttempts, digest


def native_event_time(value):
    need(isinstance(value, str), 'original_failure_timestamp_missing')
    moment = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    need(moment.tzinfo is not None and moment.utcoffset() is not None,
         'original_failure_timestamp_not_aware')
    return moment.timestamp()


def verify_reserved_provider(config, directory, key, capsule):
    """Host-side readback: an intent alone cannot authorize a process launch.

    Opens only the already registered database read-only. It neither creates
    a store nor refreshes a waiter, reserves, refunds, or marks success.
    """
    stores=config['original_stores'];path=Path(stores['provider_db'])
    expected=Path(config['ccc_config']).parent/'codex-provider-retry.sqlite3'
    need(path==expected and path.resolve(strict=True)==path,'host_provider_path_changed')
    identity=list(stores['provider_identity'])
    def check():
        s=path.lstat()
        need(stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and s.st_nlink==1
             and not s.st_mode&0o022 and [s.st_dev,s.st_ino]==identity,
             'host_provider_identity_changed')
    check()
    with PinnedDirectory(directory) as pinned:
        raw=pinned.read(key+'.provider.json')
        need(raw is not None,'host_provider_reservation_receipt_missing')
        receipt=json.loads(raw)
    event=capsule['source_event'];error=event['error']
    message=error.get('message') if isinstance(error,dict) else error
    import cmux_codex_watch as original
    from ccc_provider_retry import ProviderRetryStore,RETRYABLE
    kind=original._match_error_block(message)
    need(kind in RETRYABLE,'host_provider_error_not_retryable')
    provider=capsule['plan']['expected_model_triple']['provider']
    expected_evidence=dict(identity=ProviderRetryStore.identity(capsule['document']['session_id'],provider),
        provider=provider,error_type=kind,stamp=hashlib.sha256(json.dumps(
            [event['turn_id'],float(native_event_time(event['at'])),kind,message]).encode()).hexdigest())
    need(receipt.get('schema')=='ccc-original-hosted-provider-reservation-v1'
         and receipt.get('original_attempt_key')==key and receipt.get('evidence')==expected_evidence
         and receipt.get('ledger_identity')==identity,'host_provider_reservation_receipt_changed')
    connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)
    try:
        connection.execute('BEGIN')
        row=connection.execute('SELECT record FROM episodes WHERE identity=?',
                               (expected_evidence['identity'],)).fetchone()
        floor=connection.execute('SELECT until FROM server_floors WHERE provider=?',(provider,)).fetchone()
    finally:connection.close()
    check()
    record=json.loads(row[0]) if row else {}
    need(record.get('attempt')==key and record.get('reserved_stamp')==expected_evidence['stamp']
         and record.get('count')==receipt['reservation_count']
         and record.get('last_reserved_at')==receipt['last_reserved_at'],
         'host_provider_reservation_no_longer_current')
    need(not floor or time.time()>=floor[0],'host_provider_server_floor')
    return receipt


class OriginalHostedProviderBudget:
    def __init__(self, guard, endpoint, state, *, ledger_identity, owner_recheck):
        self.guard = guard
        self.endpoint = endpoint
        self.directory = Path(state) / 'original-exit-recovery'
        self.store = guard.codex_provider_retry
        self.path = Path(guard.guard_config['ccc_config']).parent / 'codex-provider-retry.sqlite3'
        self.identity = list(ledger_identity)
        self.owner_recheck = owner_recheck
        self.check_store()

    def check_store(self):
        self.owner_recheck()
        need(self.guard.codex_provider_retry is self.store and self.store.path == self.path,
             'original_provider_store_changed')
        need(self.path.resolve(strict=True) == self.path, 'original_provider_store_redirected')
        info = self.path.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
             and info.st_nlink == 1 and not info.st_mode & 0o022
             and [info.st_dev, info.st_ino] == self.identity,
             'original_provider_store_identity_changed')

    def source(self, plan):
        need(plan.get('actuation_kind') == 'owned_child_host', 'provider_budget_not_hosted')
        key = OriginalExitAttempts.attempt_key(plan)
        with PinnedDirectory(self.directory) as directory:
            path = Path(plan['capsule'])
            need(path.parent == self.directory and path.name == plan['capsule_sha256']+'.capsule.json',
                 'original_budget_capsule_outside_state')
            raw = directory.read(path.name)
            need(raw is not None and hashlib.sha256(raw).hexdigest() == plan['capsule_sha256'],
                 'original_budget_capsule_changed')
        capsule = json.loads(raw)
        event = plan['source_event']
        need(capsule.get('attempt_key') == key and capsule.get('source_event') == event
             and capsule['document']['session_id'] == plan['session_id']
             and capsule['document']['binding'] == plan['binding']
             and capsule['plan']['session_id'] == plan['session_id'],
             'original_budget_source_changed')
        need(event.get('kind') == 'task_complete' and event.get('turn_id'),
             'original_budget_requires_failed_turn')
        error = event.get('error')
        message = error.get('message') if isinstance(error, dict) else error
        need(isinstance(message, str) and bool(message.strip()), 'original_budget_error_missing')
        at = native_event_time(event.get('at'))
        born = plan['binding']['birth'][0] + plan['binding']['birth'][1] / 1e6
        now = self.store.clock()
        need(math.isfinite(at) and born <= at <= now+1 and now-at <= 24*3600,
             'original_budget_failure_outside_lifecycle')
        provider = capsule['plan']['expected_model_triple']['provider']
        need(capsule['current_inputs'].get('provider_id') == provider,
             'original_budget_provider_changed')
        # Reuse exactly the main CCC classifier and evidence stamp. Neither a
        # new process generation nor this recovery route gets a new quota key.
        import cmux_codex_watch as original
        from ccc_provider_retry import RETRYABLE
        kind = original._match_error_block(message)
        need(kind in RETRYABLE, 'original_budget_error_not_retryable')
        turn = dict(event, session_id=plan['session_id'], at=at,
                    model_provider=provider, error={'message': message})
        return key, turn, kind

    def __call__(self, plan, *, reserve):
        self.check_store()
        key, turn, kind = self.source(plan)
        if reserve:
            # No provider consumption before the original process intent is
            # durable. The owning driver is the only caller that may submit.
            with PinnedDirectory(self.directory) as directory:
                raw = directory.read(key+'.json')
                need(raw is not None, 'original_budget_intent_missing')
                intent = json.loads(raw)
                need(intent.get('phase') == 'input_intent' and intent.get('plan') == plan
                     and intent.get('attempt_key') == key and intent.get('plan_sha256') == digest(plan),
                     'original_budget_intent_changed')
        from ccc_herdr_codex import provider_evidence
        evidence = provider_evidence(self.guard, self.endpoint, turn, kind)
        decision = self.store.evaluate(evidence, key, reserve=reserve)
        self.check_store()
        result = dict(allowed=decision.allowed, reason=decision.reason_code,
                      retry_at=decision.retry_at, original_attempt_key=key,
                      durable_reservation=False, evidence=evidence,
                      ledger_identity=self.identity)
        if not reserve or not decision.allowed:
            return result
        # evaluate() commits with the original store's FULL synchronous policy.
        # Read back that exact reservation, without restoring any prior image.
        connection = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=2)
        try:
            row = connection.execute('SELECT record FROM episodes WHERE identity=?',
                                     (evidence['identity'],)).fetchone()
        finally:
            connection.close()
        self.check_store()
        record = json.loads(row[0]) if row else {}
        need(record.get('attempt') == key and record.get('reserved_stamp') == evidence['stamp']
             and record.get('count', 0) > 0, 'original_provider_reservation_readback_unknown')
        result['durable_reservation'] = True
        receipt = dict(schema='ccc-original-hosted-provider-reservation-v1',
                       original_attempt_key=key, evidence=evidence, ledger_identity=self.identity,
                       reservation_count=record['count'], last_reserved_at=record['last_reserved_at'])
        with PinnedDirectory(self.directory) as directory:
            name = key+'.provider.json'
            raw = canonical(receipt)+b'\n'
            before = directory.read(name)
            need(before is None or before == raw, 'original_provider_receipt_changed')
            if before is None:
                need(directory.publish(name, raw), 'original_provider_receipt_created_elsewhere')
            need(directory.read(name) == raw, 'original_provider_receipt_not_durable')
        return result
