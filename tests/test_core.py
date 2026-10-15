import copy
import io
import json
import sqlite3
import time

import pytest
from pydantic import ValidationError

from interview_agent.config import Settings
from interview_agent.db import BudgetExceeded, LostLease, Store
from interview_agent.files import inside, read_json
from interview_agent.pipeline import Pipeline, job_settings
from interview_agent.schemas import Extraction, Segment, Transcript, import_text, validate_extraction
from interview_agent.services import chunk_transcript, export_revision, purge_deleted, request_delete, submit
from conftest import FakeLLM


def test_text_pipeline_preserves_sources_and_exports(settings, store, submitted):
    pipeline = Pipeline(settings, store, llm_factory=FakeLLM)
    assert pipeline.run_once()
    interview_id, job_id = submitted
    job = store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "COMPLETED", job["error"]
    interview = store.one("SELECT * FROM interviews WHERE id=?", (interview_id,))
    revision = store.one("SELECT * FROM revisions WHERE id=?", (interview["active_revision"],))
    result = json.loads(revision["content"])
    assert len(result["items"]) == 1
    assert result["items"][0]["answer_segment_ids"] == ["s00002"]
    assert store.search("幂等")[0]["interview_id"] == interview_id
    note = settings.data_dir / "notes" / interview_id / "interview.md"
    assert "重复请求与执行一次" in note.read_text(encoding="utf-8")
    assert not pipeline.run_once()


def test_retry_export_does_not_repeat_llm(settings, store, submitted, monkeypatch):
    import interview_agent.pipeline as module
    original = module.export_revision
    monkeypatch.setattr(module, "export_revision", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    FakeLLM.calls = 0
    pipeline = Pipeline(settings, store, llm_factory=FakeLLM)
    pipeline.run_once()
    assert FakeLLM.calls == 1
    assert store.one("SELECT status FROM jobs WHERE id=?", (submitted[1],))["status"] == "FAILED"
    monkeypatch.setattr(module, "export_revision", original)
    store.retry(submitted[1])
    pipeline.run_once()
    assert FakeLLM.calls == 1
    assert store.one("SELECT status FROM jobs WHERE id=?", (submitted[1],))["status"] == "COMPLETED"


def test_human_revision_survives_rerun(settings, store, submitted):
    pipeline = Pipeline(settings, store, llm_factory=FakeLLM)
    pipeline.run_once()
    interview_id = submitted[0]
    active = store.one("SELECT active_revision FROM interviews WHERE id=?", (interview_id,))["active_revision"]
    content = json.loads(store.one("SELECT content FROM revisions WHERE id=?", (active,))["content"])
    content["items"][0]["answer_summary"] = "人工补充说明"
    human_id = store.edit_revision(interview_id, active, content)
    with pytest.raises(ValueError, match="其他窗口"):
        store.edit_revision(interview_id, active, content)
    with store.transaction() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (submitted[1],)).fetchone()
        cp = json.loads(row["checkpoint"])
        cp.pop("revision_id")
        con.execute("UPDATE jobs SET status='QUEUED',checkpoint=? WHERE id=?", (json.dumps(cp), submitted[1]))
    pipeline.run_once()
    assert store.one("SELECT active_revision FROM interviews WHERE id=?", (interview_id,))["active_revision"] == human_id
    assert store.search("人工补充说明")


def test_lease_fencing_and_renewal(store, submitted):
    first = store.claim("first")
    assert store.claim("second") is None
    assert store.heartbeat(first)
    with store.transaction() as con:
        con.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, first["id"]))
    second = store.claim("second")
    assert second["epoch"] == first["epoch"] + 1
    with pytest.raises(LostLease):
        store.checkpoint(first)
    store.checkpoint(second)


def test_cancellation_blocks_persist(store, submitted):
    job = store.claim("worker")
    store.cancel(job["id"])
    with pytest.raises(InterruptedError):
        store.checkpoint(job)
    store.finish(job, "COMPLETED")
    assert store.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] == "CANCELLED"


def test_queued_cancellation(store, submitted):
    store.cancel(submitted[1])
    assert store.claim("worker") is None
    assert store.one("SELECT status FROM jobs WHERE id=?", (submitted[1],))["status"] == "CANCELLED"


def test_budget_is_reserved_atomically(store, submitted):
    job = store.claim("worker")
    call_id = store.reserve(job, "deepseek", "model", 0.75, 1)
    with pytest.raises(BudgetExceeded):
        store.reserve(job, "deepseek", "model", 0.5, 1)
    store.record_call(call_id, "received", {"total_tokens": 10}, "req")
    store.record_call(call_id, "invalid_output")
    record = store.one("SELECT * FROM calls WHERE id=?", (call_id,))
    assert json.loads(record["usage"])["total_tokens"] == 10
    assert record["reserve_cny"] == 0.75


def test_unknown_submission_not_auto_retried(store, submitted):
    job = store.claim("worker")
    job["checkpoint"]["submission_unknown"] = True
    store.checkpoint(job)
    store.finish(job, "FAILED")
    with pytest.raises(ValueError, match="状态不明"):
        store.retry(job["id"])


def test_delete_waits_for_live_worker_then_purges(settings, store, submitted):
    pipeline = Pipeline(settings, store, llm_factory=FakeLLM)
    pipeline.run_once()
    active = store.one("SELECT active_revision FROM interviews WHERE id=?", (submitted[0],))["active_revision"]
    assert active
    request_delete(store, submitted[0])
    purge_deleted(store)
    assert not store.search()
    assert not store.one("SELECT id FROM revisions WHERE id=?", (active,))
    assert not (settings.data_dir / "notes" / submitted[0]).exists()


def test_backup_includes_wal(store, submitted):
    backup = store.backup()
    with sqlite3.connect(backup) as con:
        assert con.execute("SELECT COUNT(*) FROM interviews").fetchone()[0] == 1


def test_import_no_timestamps_or_media(settings, store, submitted):
    row = store.one("SELECT * FROM interviews WHERE id=?", (submitted[0],))
    assert row["media_id"] is None
    cp = json.loads(store.one("SELECT checkpoint FROM jobs WHERE id=?", (submitted[1],))["checkpoint"])
    t = Transcript.model_validate(read_json(inside(settings.data_dir, cp["transcript"])))
    assert all(s.start_ms is None and s.speaker_id is None for s in t.segments)


@pytest.mark.parametrize("data", [b"", b"{}", b"not json"])
def test_bad_import_rejected(settings, store, data):
    with pytest.raises((ValueError, ValidationError)):
        submit(settings, store, io.BytesIO(data), "bad.json", "bad", {})
    assert store.one("SELECT COUNT(*) n FROM interviews")["n"] == 0


@pytest.mark.parametrize("start,end", [(None, 3), (3, None), (5, 5), (10, 5), (-1, 5)])
def test_invalid_timing(start, end):
    with pytest.raises(ValidationError):
        Segment(id="s1", text_raw="text", start_ms=start, end_ms=end)


def test_overlapping_speakers_allowed():
    Transcript(segments=[Segment(id="s1", text_raw="A", start_ms=0, end_ms=1000), Segment(id="s2", text_raw="B", start_ms=500, end_ms=1200)])


def test_duplicate_segment_ids_rejected():
    with pytest.raises(ValidationError):
        Transcript(segments=[Segment(id="s1", text_raw="A"), Segment(id="s1", text_raw="B")])


def test_source_coverage_and_cycle():
    segments = import_text("问题\n回答").segments
    q = {"local_id": "q1", "question": "问题", "question_segment_ids": ["s00001"], "answer_summary": "回答", "answer_segment_ids": ["s00002"], "answer_status": "answered"}
    result = Extraction(items=[q])
    validate_extraction(result, segments, {s.id for s in segments}, set())
    result.items[0].parent_local_id = "q1"
    with pytest.raises(ValueError, match="环"):
        validate_extraction(result, segments, {s.id for s in segments}, set())
    result.items[0].parent_local_id = None
    result.items[0].answer_segment_ids = ["other-interview"]
    with pytest.raises(ValueError, match="之外"):
        validate_extraction(result, segments, set(), set())


def test_uncovered_source_rejected():
    segments = import_text("问题\n回答").segments
    with pytest.raises(ValueError, match="未处理"):
        validate_extraction(Extraction(), segments, {s.id for s in segments}, set())


def test_unanswered_cannot_invent_answer():
    with pytest.raises(ValidationError):
        Extraction(items=[{"local_id": "q", "question": "Q", "question_segment_ids": ["s1"], "answer_status": "unanswered", "answer_summary": "invented"}])


def test_chunking_overlap_only_not_lost():
    t = import_text("aaaa\nbbbb\ncccc\ndddd")
    chunks = chunk_transcript(t, char_limit=8)
    assert len(chunks) == 2
    assert [s.id for s in chunks[1]["segments"]][:2] == ["s00001", "s00002"]
    assert set(sum([c["required"] for c in chunks], [])) == {s.id for s in t.segments}


def test_path_traversal_rejected(tmp_path):
    with pytest.raises(ValueError):
        inside(tmp_path, "../outside")


def test_percent_search_is_literal(settings, store, submitted):
    Pipeline(settings, store, llm_factory=FakeLLM).run_once()
    assert not store.search("%")


def test_settings_secrets_only_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AliApiKey", " fake-ali ")
    monkeypatch.setenv("deepseek", "fake-deepseek")
    (tmp_path / "settings.json").write_text(json.dumps({"deepseek_key": "injected", "asr_key": "injected"}))
    settings = Settings.load(tmp_path)
    assert settings.asr_key == "fake-ali"
    assert settings.deepseek_key == "fake-deepseek"
    settings.save()
    assert "fake-" not in (tmp_path / "settings.json").read_text()
    assert "fake-" not in repr(settings)


@pytest.mark.parametrize("base", ["http://api.deepseek.com", "https://api.deepseek.com.evil.com", "https://evil.com", "https://api.deepseek.com/?token=abc"])
def test_model_host_validation(base):
    with pytest.raises(ValueError):
        Settings(llm_base=base).validate()


def test_retry_refreshes_storage_when_no_remote_object(settings):
    snapshot = {"oss_bucket": "", "oss_endpoint": "", "llm_model": "deepseek-flash"}
    resolved = job_settings(settings, snapshot, {"stereo_mode": "mix"})
    assert resolved.oss_bucket == settings.oss_bucket
    assert resolved.oss_endpoint == settings.oss_endpoint


def test_remote_object_keeps_original_storage_on_resume(settings):
    snapshot = {"oss_bucket": "original-bucket", "oss_endpoint": "https://oss-cn-shanghai.aliyuncs.com"}
    resolved = job_settings(settings, snapshot, {"tracks": [{"uploaded": True, "remote_task_id": "task-1"}]})
    assert resolved.oss_bucket == "original-bucket"
    assert resolved.oss_endpoint == snapshot["oss_endpoint"]


def test_invalid_process_key_falls_back_to_valid_system_value(tmp_path, monkeypatch):
    import interview_agent.config as config
    monkeypatch.setenv("AliApiKey", "旧的无效值")
    monkeypatch.setattr(config, "registry_values", lambda name: ["旧用户值", "system-key-test"] if name == "AliApiKey" else [])
    assert config.credential_environment("AliApiKey") == "system-key-test"
    assert Settings.load(tmp_path).asr_key == "system-key-test"


def test_explicit_empty_credential_disables_fallback(monkeypatch):
    import interview_agent.config as config
    monkeypatch.setenv("AliApiKey", "")
    monkeypatch.setattr(config, "registry_values", lambda _: ["system-key-test"])
    assert config.credential_environment("AliApiKey") == ""
