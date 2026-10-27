"""Read-only viewport evidence for the single original ASCII probe cleanup.

Inverse cursor styling alone proves no editor bytes. The caller must observe
Home then End at distinct positions before deleting even one character.
"""
import json


def read(bridge, binding):
    identity = binding['identity']
    raw = bridge._run('rpc', 'terminal.replay', json.dumps(dict(
        workspace_id=identity['workspace_uuid'],
        surface_id=identity['target_surface_uuid'])))
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 8 * 1024 * 1024:
        raise ValueError('PROBE_CLEAR_GRID_SIZE_UNKNOWN')
    return json.loads(raw)


def inspect(bridge, reply, binding, screen_body):
    """Validate a complete same-surface viewport, not a terminal receipt."""
    identity = binding['identity']
    grid = reply['render_grid']
    if (str(reply.get('workspace_id')).upper() != identity['workspace_uuid'].upper()
            or str(reply.get('surface_id')).upper() != identity['target_surface_uuid'].upper()
            or str(grid.get('surface_id')).upper() != identity['target_surface_uuid'].upper()
            or grid.get('format') != 'cmux.render-grid.v1'
            or grid.get('full') is not True or grid.get('active_screen') != 'primary'
            or grid.get('anchor') != 'viewport'):
        raise ValueError('PROBE_CLEAR_GRID_BINDING_OR_COMPLETENESS_CHANGED')
    columns, rows = grid['columns'], grid['rows']
    if (type(columns) is not int or type(rows) is not int
            or not 16 <= columns <= 1000 or not 4 <= rows <= 500
            or reply.get('columns') != columns or reply.get('rows') != rows):
        raise ValueError('PROBE_CLEAR_GRID_GEOMETRY_UNKNOWN')
    styles = {v['id']: v for v in grid['styles']}
    if len(styles) != len(grid['styles']):
        raise ValueError('PROBE_CLEAR_GRID_STYLE_UNKNOWN')
    spans = [[] for _ in range(rows)]
    for span in grid['row_spans']:
        row, col, width = (span[k] for k in ('row', 'column', 'cell_width'))
        if (any(type(v) is not int for v in (row, col, width))
                or not 0 <= row < rows or not 0 <= col < columns
                or not 1 <= width <= columns - col or not isinstance(span['text'], str)
                or any(c in span['text'] for c in ('\n', '\r', '\x1b', '\t'))
                or span['style_id'] not in styles):
            raise ValueError('PROBE_CLEAR_GRID_SPAN_UNKNOWN')
        spans[row].append(span)
    lines = []
    for line in spans:
        end, text = 0, ''
        line.sort(key=lambda v: v['column'])
        for span in line:
            if span['column'] < end:
                raise ValueError('PROBE_CLEAR_GRID_OVERLAPPING_SPANS')
            text += ' ' * (span['column'] - end) + span['text']
            end = span['column'] + span['cell_width']
        lines.append(text)
    snapshot = '\n'.join(lines)
    frame = bridge._claude_bordered_compose(snapshot)
    if (frame is None or frame['end'] != frame['start'] + 1
            or bridge._queued_or_active_input(snapshot) is not False):
        raise ValueError('PROBE_CLEAR_GRID_BUSY_OR_INCOMPLETE')
    row = frame['start']
    if (frame['body'] != screen_body
            or lines[row] not in ('❯ ' + screen_body, '❯\u00a0' + screen_body)
            or not screen_body.isascii()
            or lines[row - 1] != '─' * columns or lines[row + 1] != lines[row - 1]):
        raise ValueError('PROBE_CLEAR_GRID_COMPOSER_CHANGED')
    # ASCII probe and one-cell prompt only: no width normalization or trimming.
    inverse, filled = [], []
    for span in spans[row]:
        style = styles[span['style_id']]
        if (len(span['text']) != span['cell_width'] or style.get('invisible') is not False
                or type(style.get('inverse')) is not bool):
            raise ValueError('PROBE_CLEAR_GRID_CURSOR_STYLE_UNKNOWN')
        cells = list(range(span['column'], span['column'] + span['cell_width']))
        filled.extend(cells)
        if style['inverse']:
            inverse.extend(cells)
    cursor = grid['cursor']
    col = cursor.get('column')
    if (type(col) is not int or type(cursor.get('row')) is not int
            or cursor['row'] != row or inverse != [col]
            or not 2 <= col < columns
            or filled != list(range(len(lines[row])))):
        raise ValueError('PROBE_CLEAR_GRID_CURSOR_UNPROVEN')
    return dict(position=col - 2, body=frame['body'],
                geometry=[columns, rows, row, lines[row - 1]],
                structure=lines[row - 1:row + 2], cursor=cursor)
