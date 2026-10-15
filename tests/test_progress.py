from interview_agent.db import Store
from interview_agent.progress import duration, task_progress


def test_persisted_clocks_freeze_and_survive_retry(store, submitted, monkeypatch):
    import interview_agent.db as module
    clock = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    job = store.claim("worker")
    assert job["started_at"] == 1000
    clock[0] = 1010
    store.checkpoint(job, "EXTRACTING")
    clock[0] = 1015
    store.checkpoint(job)
    row = store.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
    assert row["stage_started_at"] == 1010
    assert task_progress(row, {}, 1018)["elapsed"] == 18
    assert task_progress(row, {}, 1018)["stage_elapsed"] == 8
    clock[0] = 1020
    store.finish(job, "FAILED", "failed")
    row = Store(store.root).one("SELECT * FROM jobs WHERE id=?", (job["id"],))
    assert row["finished_at"] == 1020
    assert task_progress(row, {}, 9999)["elapsed"] == 20
    store.retry(job["id"])
    clock[0] = 1030
    resumed = store.claim("worker-2")
    assert resumed["started_at"] == 1000 and resumed["finished_at"] is None
    assert resumed["stage_started_at"] == 1030
    clock[0] = 1040
    store.finish(resumed, "COMPLETED")
    row = store.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
    assert task_progress(row, {}, 9999)["elapsed"] == 40
    assert task_progress(row, {}, 9999)["fraction"] == 1


def test_queued_cancellation_and_automatic_retry_clocks(store, submitted):
    job = store.claim("worker")
    store.finish(job, "FAILED", "network", retry=True)
    row = store.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
    assert row["status"] == "RETRY_WAIT" and row["finished_at"] is None
    store.cancel(job["id"])
    store.claim("worker-2")
    row = store.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
    assert row["status"] == "CANCELLED" and row["finished_at"] is not None


def test_segment_progress_does_not_regress_when_pending_batch_splits():
    row = {"status": "RUNNING", "stage": "EXTRACTING", "created": 0, "media_id": "audio"}
    before = {"chunk_plan": [{"required": ["a", "b"]}, {"required": ["c", "d"]}], "chunks": ["first.json"]}
    after = {"chunk_plan": [{"required": ["a", "b"]}, {"required": ["c"]}, {"required": ["d"]}], "chunks": ["first.json"]}
    a, b = task_progress(row, before, 10), task_progress(row, after, 10)
    assert a["fraction"] == b["fraction"]
    assert a["saved_segments"] == b["saved_segments"] == 2
    assert a["total_segments"] == b["total_segments"] == 4
    assert b["done_chunks"] == 1 and b["total_chunks"] == 3
    assert b["elapsed"] is None
    assert duration(3661) == "01:01:01" and duration(None) == "—"


def test_legacy_migration_labels_estimated_times(store, submitted):
    job = store.claim("worker")
    store.record_call(store.reserve(job, "deepseek", "model", 0, 1), "received")
    store.finish(job, "COMPLETED")
    with store.transaction() as con:
        for column in ("started_at", "finished_at", "stage_started_at", "timing_estimated"):
            con.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
        con.execute("PRAGMA user_version=2")
    migrated = Store(store.root)
    row = migrated.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
    assert row["timing_estimated"] == 1 and row["started_at"] is not None
    assert row["finished_at"] == row["updated"]
    assert row["stage_started_at"] is None
    # Opening the database again does not overwrite persisted timing data.
    assert Store(store.root).one("SELECT * FROM jobs WHERE id=?", (job["id"],)) == row
