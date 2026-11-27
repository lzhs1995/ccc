"""Pure receipt shape and pinned selector matching; never performs transport."""


def matches(row, member):
    return bool(row.get('executor') and row['executor'] in
                (member.get('surface_ref'), member.get('surface_uuid')))


def aggregate(rows, members=None):
    receipt = dict(rows[0], executors=rows, executor_count=len(rows))
    missing = list((members or [])[len(rows):])
    all_acked = all(row.get('executor_ack') is True for row in rows)
    receipt['all_executors_acked'] = all_acked and not missing
    if not all_acked:
        unproven = [row for row in rows if row.get('executor_ack') is not True]
        receipt['executor_ack'] = False
        receipt['unproven_executors'] = [row['executor'] for row in unproven]
        failed = next((row for row in unproven
                       if row.get('lifecycle') != 'PENDING' or row.get('status') == 'FAIL'), None)
        if failed is None:
            receipt.update(status='AWAITING_EXECUTOR_ACK', lifecycle='PENDING', terminal_error_at=None)
            receipt.pop('error', None)
        else:
            receipt.update(status='FAIL', lifecycle=failed.get('lifecycle', 'UNKNOWN'),
                terminal_error_at=failed.get('terminal_error_at'),
                error=f"handshake incomplete — terminal or invalid state for "
                      f"{failed['executor']}: {failed.get('error', 'see executor record')}")
    if missing:
        receipt.update(expected_executor_count=len(members),
                       unstarted_executors=[peer['surface_ref'] for peer in missing])
        receipt['executor_ack'] = False
        receipt['unproven_executors'] = receipt.get('unproven_executors', []) + receipt['unstarted_executors']
        if all_acked:
            receipt.update(status='AWAITING_EXECUTOR_ACK', lifecycle='PENDING', terminal_error_at=None)
            receipt.pop('error', None)
    return receipt
