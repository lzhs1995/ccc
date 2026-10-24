"""Real native Goal-state evidence for the private-check delivery fixtures.

`goal_state_for_turn` refuses with `native Goal identity unavailable` unless it
can authenticate the writer itself: a live process whose surface, workspace and
birth match the turn, that process's own writable-file inventory holding exactly
one `<sid>.lock` under `thread-writer-locks`, and a real `goals_1.sqlite` beside
it whose dev/inode do not move while it is read twice.

So this helper builds that evidence for real instead of mocking the gate:
a temporary writer directory, an actual lock file, an actual SQLite database
with the `thread_goals` schema, and OS-observation stubs (`scope.process` and
`writer_files`) that report the true dev/inode of those files.  The gate code,
its two re-reads and every identity comparison run unchanged.

`absent=True` (the default) leaves `thread_goals` empty, which is what the
no-Goal delivery cases need: an authenticated *absent* row is the only thing
that permits the ordinary non-Goal route.
"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import sqlite3
import tempfile
import uuid

SCHEMA = ("CREATE TABLE thread_goals ("
          "thread_id TEXT PRIMARY KEY, goal_id TEXT, status TEXT, "
          "updated_at_ms INTEGER)")


def _identity(path: Path) -> dict:
    info = path.lstat()
    return {"device": info.st_dev, "inode": info.st_ino}


def terminal_frame(surface_id, error, *, draft=None, columns=180):
    """A complete screen-anchored Goal input region, including padding cells.

    The normal grid helper omits background blanks and models an ordinary
    composer. Goal terminal admission deliberately requires the full physical
    region, so its socket fixture must supply that region rather than waive
    the reader's anchoring or draft checks.
    """
    from tests.test_watch import grid_payload, span
    frame = grid_payload([], error=error, columns=columns)
    grid = frame['render_grid']
    row = grid['cursor']['row']
    grid.update(surface_id=surface_id, full=True, anchor='screen')
    grid['styles'].append({'id': 5, 'foreground': '#FFFFFF',
                           'background': '#393939', 'faint': True})
    grid['row_spans'] = [s for s in grid['row_spans'] if s['row'] < row - 1]
    grid['row_spans'].extend([span(row - 1, 0, ' ' * columns, 1),
                              span(row + 1, 0, ' ' * columns, 1)])
    if draft is None:
        placeholder = 'Improve documentation in @filename'
        grid['row_spans'].extend([span(row, 0, '› ', 1),
                                  span(row, 2, placeholder, 5),
                                  span(row, 2 + len(placeholder),
                                       ' ' * (columns - 2 - len(placeholder)), 1)])
        grid['cursor']['column'] = 2
    else:
        text = '› ' + draft
        grid['row_spans'].append(span(row, 0, text.ljust(columns), 1))
        grid['cursor']['column'] = len(text)
    grid['row_spans'].append(span(row + 2, 0,
        'gpt-6-astra · Goal stalled (/goal resume)', 0))
    return frame


def install(case, turn, *, absent=True, status="blocked", goal_id=None,
            updated_at_ms=1_700_000_000_000, wrong_process=False,
            wrong_workspace=False, no_lock=False, extra_lock=False,
            no_database=False):
    """Give `turn` authenticated Goal evidence; returns the state dict.

    The keyword flags are for negative controls only: each one breaks exactly
    one piece of the identity chain so a test can show the refusal returns.
    `case` must be a unittest TestCase (its `addCleanup` owns the temp dir).
    """
    import ccc_codex_goal as goal_module
    import ccc_guard_scope as scope
    from unittest.mock import patch

    stack = ExitStack()
    case.addCleanup(stack.close)
    root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
    writer = root / "writer"
    locks = writer / "thread-writer-locks"
    locks.mkdir(parents=True)

    sid = str(uuid.UUID(turn["session_id"]))
    lock = locks / (sid + ".lock")
    if not no_lock:
        lock.write_text("")
    if extra_lock:
        (locks / (str(uuid.uuid4()) + ".lock")).write_text("")
        # A second lock for the *same* sid is impossible on one filesystem, so
        # the ambiguity control instead reports a duplicate inventory entry.

    database = writer / "goals_1.sqlite"
    if not no_database:
        with sqlite3.connect(database) as db:
            db.execute(SCHEMA)
            if not absent:
                db.execute("INSERT INTO thread_goals VALUES (?,?,?,?)",
                           (sid, goal_id or str(uuid.uuid4()), status,
                            updated_at_ms))

    files = {}
    for path in (p for p in (lock, database) if p.exists()):
        files[path] = _identity(path)
    if extra_lock:
        for path in sorted(locks.glob("*.lock")):
            files[path] = _identity(path)

    pid = turn["pid"]
    process = {
        "pid": pid,
        "surface_id": ("00000000-0000-4000-8000-000000000000" if wrong_process
                       else case.target["surface_id"]),
        "environment_workspace_id": ("00000000-0000-4000-8000-000000000001"
                                     if wrong_workspace
                                     else case.target["workspace_id"]),
        "birth": (turn.get("process_start"), 0),
        "agent_kind": "codex",
    }

    stack.enter_context(patch.object(
        scope, "process", side_effect=lambda value: dict(process)
        if value == pid else None))
    stack.enter_context(patch.object(
        goal_module, "writer_files", side_effect=lambda value: dict(files)
        if value == pid else {}))
    return {"root": root, "writer": writer, "lock": lock,
            "database": database, "files": files, "process": process,
            "session_id": sid}
