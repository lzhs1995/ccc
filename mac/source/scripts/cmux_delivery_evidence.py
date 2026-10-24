"""Read-only predicates for durable delivery attempts; never send input.

Legacy senders remain unchanged. An unavailable exact-composer checker cannot
authorize adoption of a pending draft.
"""
import hashlib
import re


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def compact(value):
    return re.sub(r'\s+', '', value)


def contains(screen, marker):
    return bool(marker) and compact(marker) in compact(screen)


def delivery_compose_text(bridge, screen):
    lines = screen.splitlines()
    positions = [i for i, line in enumerate(lines) if bridge._PROMPT_GLYPH_RE.match(line)]
    if not positions:
        return None
    index = positions[-1]
    return '\n'.join([bridge._PROMPT_GLYPH_RE.sub('', lines[index], count=1), *lines[index+1:]])


def own_draft(bridge, screen, text):
    checker = getattr(bridge, 'require_exact_composer', None)
    if checker is None:
        return False
    try:
        checker(screen, 'receiver', text)
    except bridge.DispatchUnconfirmed:
        return False
    return True


def queued(bridge, screen, marker):
    return any(contains(screen[match.start():], marker)
               for match in bridge._PENDING_QUEUE_RE.finditer(screen))


def confirmed(bridge, before, screen, marker, text, adopted=False):
    if not marker or (contains(before, marker) and not adopted) or queued(bridge, screen, marker):
        return False
    if contains(delivery_compose_text(bridge, screen) or '', marker):
        return False
    # Match one complete prompt block, not fragments anywhere in history.
    # Whitespace is display-equivalent here because transcript wrapping loses
    # row boundaries. This does not prove byte-exact native message content.
    block = None
    for line in screen.splitlines():
        if bridge._PROMPT_GLYPH_RE.match(line):
            block = [bridge._PROMPT_GLYPH_RE.sub('', line, count=1)]
        elif bridge._ACTIVITY_LINE_RE.match(line):
            if block is not None and compact('\n'.join(block)) == compact(text):
                return True
            block = None
        elif block is not None:
            block.append(line)
    return False
