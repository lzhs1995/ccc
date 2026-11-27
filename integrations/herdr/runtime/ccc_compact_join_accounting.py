"""Exact post-join Goal accounting adoption from the original native receipt."""
import copy

GOAL_IDENTITY = ('threadId', 'objective', 'status', 'tokenBudget', 'createdAt')
REVISION_FIELDS = ('goal_id', 'created_at_ms', 'updated_at_ms')
COUNTER_FIELDS = ('tokens_used', 'time_used_seconds')


def goal_after_join(episode, snapshot, receipt):
    join = receipt.get('joinAccounting')
    if not isinstance(join, dict):
        raise ValueError('native compact receipt lacks join accounting')
    if join == {'status': 'not_accounted'}:
        if (snapshot.get('goal') != episode.get('goal')
                or snapshot.get('goal_revision') != episode.get('goal_revision')):
            raise ValueError('Goal changed without original join accounting')
        return copy.deepcopy(episode)
    if (set(join) != {'status', 'turnId', 'attemptId', 'before', 'after'}
            or join.get('status') != 'accounted'
            or join.get('turnId') != episode.get('turn_id')
            or join.get('attemptId') != episode.get('key')):
        raise ValueError('join accounting belongs to a different original turn or attempt')
    before, after = join['before'], join['after']
    fields = set(REVISION_FIELDS + COUNTER_FIELDS)
    if (not isinstance(before, dict) or not isinstance(after, dict)
            or set(before) != fields or set(after) != fields
            or any(type(row.get(k)) is not int or row[k] < 0
                   for row in (before, after)
                   for k in ('created_at_ms', 'updated_at_ms', *COUNTER_FIELDS))
            or any(not isinstance(row.get('goal_id'), str) or not row['goal_id']
                   for row in (before, after))):
        raise ValueError('invalid exact Goal accounting revision')
    old, current = episode.get('goal'), snapshot.get('goal')
    revision = episode.get('goal_revision')
    if (not isinstance(old, dict) or not isinstance(current, dict)
            or not isinstance(revision, dict)
            or {k: before[k] for k in REVISION_FIELDS} != revision
            or before['tokens_used'] != old.get('tokensUsed')
            or before['time_used_seconds'] != old.get('timeUsedSeconds')
            or before['created_at_ms'] // 1000 != old.get('createdAt')
            or before['updated_at_ms'] // 1000 != old.get('updatedAt')
            or old.get('status') != 'active' or current.get('status') != 'active'
            or any(current.get(k) != old.get(k) for k in GOAL_IDENTITY)
            or after['goal_id'] != before['goal_id']
            or after['created_at_ms'] != before['created_at_ms']
            or after['updated_at_ms'] < before['updated_at_ms']
            or any(after[k] < before[k] for k in COUNTER_FIELDS)
            or all(after[k] == before[k] for k in COUNTER_FIELDS)):
        raise ValueError('Goal changed outside the original join accounting')
    expected = dict(old, tokensUsed=after['tokens_used'],
                    timeUsedSeconds=after['time_used_seconds'],
                    updatedAt=after['updated_at_ms'] // 1000)
    after_revision = {k: after[k] for k in REVISION_FIELDS}
    if current != expected or snapshot.get('goal_revision') != after_revision:
        raise ValueError('current Goal does not equal the exact native accounting receipt')
    adopted = copy.deepcopy(episode)
    adopted.update(goal=copy.deepcopy(current), goal_full=copy.deepcopy(snapshot.get('goal_full')),
                   goal_revision=after_revision)
    return adopted
