"""Known generated transcript records cannot prove a new user input."""


def generated_user_record(record):
    # Claude marks compaction replay as type=user with the original text. The
    # user role/marker alone therefore does not establish another reception.
    return any(record.get(key) is True for key in (
        'isCompactSummary', 'isVisibleInTranscriptOnly'))
