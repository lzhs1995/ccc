-- Revision survives Goal deletion/replacement and is advanced by every writer,
-- including an older binary that does not understand recovery receipts.
CREATE TABLE thread_goal_revisions (
    thread_id TEXT PRIMARY KEY NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0)
);
INSERT INTO thread_goal_revisions(thread_id, revision)
SELECT thread_id, 1 FROM thread_goals;
CREATE TRIGGER goal_revision_insert AFTER INSERT ON thread_goals BEGIN
  INSERT INTO thread_goal_revisions(thread_id, revision) VALUES(NEW.thread_id, 1)
  ON CONFLICT(thread_id) DO UPDATE SET revision = revision + 1;
END;
CREATE TRIGGER goal_revision_update AFTER UPDATE ON thread_goals BEGIN
  UPDATE thread_goal_revisions SET revision = revision + 1 WHERE thread_id = NEW.thread_id;
END;
CREATE TRIGGER goal_revision_delete AFTER DELETE ON thread_goals BEGIN
  UPDATE thread_goal_revisions SET revision = revision + 1 WHERE thread_id = OLD.thread_id;
END;
CREATE TABLE thread_goal_recovery_receipts (
    attempt_id TEXT PRIMARY KEY NOT NULL,
    thread_id TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('intent','admitted','started','rejected')),
    turn_id TEXT,
    admitted_goal_revision INTEGER,
    reason TEXT,
    fence_active INTEGER NOT NULL DEFAULT 1 CHECK(fence_active IN (0,1)),
    CHECK((phase IN ('intent','rejected') AND turn_id IS NULL)
       OR (phase IN ('admitted','started') AND turn_id IS NOT NULL))
);
CREATE UNIQUE INDEX unique_goal_recovery_active_fence
ON thread_goal_recovery_receipts(thread_id) WHERE fence_active = 1;
