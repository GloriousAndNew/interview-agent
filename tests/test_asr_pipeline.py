import io
import json
import time

import pytest

from interview_agent.pipeline import Pipeline
from interview_agent.providers import ProviderError
from interview_agent.services import submit
from conftest import FakeLLM


class Storage:
    deleted = []

    def __init__(self, settings):
        pass

    def upload(self, key, path, **kwargs):
        assert path.exists()
        return self.signed_url(key)

    def signed_url(self, key):
        return "https://example.test/" + key

    def delete(self, key):
        type(self).deleted.append(key)


class ASR:
    submits = 0
    fail_poll = False
    fail_submit = False

    def __init__(self, settings):
        pass

    def close(self):
        pass

    def submit(self, url, diarization=True):
        type(self).submits += 1
        if type(self).fail_submit:
            raise ProviderError("timeout", unknown=True)
        return "remote-task-id"

    def poll(self, task_id):
        assert task_id == "remote-task-id"
        if type(self).fail_poll:
            type(self).fail_poll = False
            raise ProviderError("transient", retryable=True)
        return {"output": {"task_status": "SUCCEEDED"}, "usage": {"duration": 1}}

    def result(self, value):
        return {"properties": {"original_duration_in_milliseconds": 1000}, "transcripts": [{"sentences": [{"begin_time": 0, "end_time": 400, "speaker_id": 0, "text": "什么是幂等？"}, {"begin_time": 500, "end_time": 1000, "speaker_id": 1, "text": "重复请求结果相同。"}]}]}


@pytest.fixture
def audio_job(settings, store, monkeypatch):
    import interview_agent.pipeline as module
    monkeypatch.setattr(module, "probe", lambda _: {"duration_ms": 1000, "channels": 1})
    monkeypatch.setattr(module, "prepare", lambda source, target, channel: target.write_bytes(b"audio"))
    ASR.submits, ASR.fail_poll, ASR.fail_submit = 0, False, False
    Storage.deleted = []
    return submit(settings, store, io.BytesIO(b"fake-media"), "source.wav", "音频测试", {})


def test_audio_end_to_end_and_cleanup(settings, store, audio_job):
    pipeline = Pipeline(settings, store, asr_factory=ASR, storage_factory=Storage, llm_factory=FakeLLM)
    pipeline.run_once()
    job = store.one("SELECT * FROM jobs WHERE id=?", (audio_job[1],))
    assert job["status"] == "COMPLETED", job["error"]
    assert ASR.submits == 1
    cp = json.loads(job["checkpoint"])
    assert cp["tracks"][0]["remote_task_id"] == "remote-task-id"
    assert cp["tracks"][0]["path"].endswith(".mp3")
    assert cp["tracks"][0]["object_key"].endswith(".mp3")
    assert cp["upload_audio"]["bitrate_kbps"] == 64
    assert cp["upload_audio"]["prepared_bytes"] == 5
    assert store.search("幂等")
    pipeline.maintenance()
    assert len(Storage.deleted) == 1


def test_remote_task_resume_does_not_resubmit(settings, store, audio_job):
    ASR.fail_poll = True
    pipeline = Pipeline(settings, store, asr_factory=ASR, storage_factory=Storage, llm_factory=FakeLLM)
    pipeline.run_once()
    assert store.one("SELECT status FROM jobs WHERE id=?", (audio_job[1],))["status"] == "RETRY_WAIT"
    with store.transaction() as con:
        con.execute("UPDATE jobs SET retry_at=0 WHERE id=?", (audio_job[1],))
    pipeline.run_once()
    assert ASR.submits == 1
    assert store.one("SELECT status FROM jobs WHERE id=?", (audio_job[1],))["status"] == "COMPLETED"


def test_unknown_submit_remains_blocked_and_keeps_cleanup(settings, store, audio_job):
    ASR.fail_submit = True
    Pipeline(settings, store, asr_factory=ASR, storage_factory=Storage, llm_factory=FakeLLM).run_once()
    row = store.one("SELECT * FROM jobs WHERE id=?", (audio_job[1],))
    assert row["status"] == "FAILED"
    assert json.loads(row["checkpoint"])["submission_unknown"]
    with pytest.raises(ValueError):
        store.retry(audio_job[1])
    assert store.one("SELECT eligible FROM cleanup")["eligible"] > time.time()


def test_stereo_separate_keeps_both_tracks(settings, store, audio_job, monkeypatch):
    import interview_agent.pipeline as module
    monkeypatch.setattr(module, "probe", lambda _: {"duration_ms": 1000, "channels": 2})
    with store.transaction() as con:
        con.execute("UPDATE jobs SET checkpoint=? WHERE id=?", (json.dumps({"stereo_mode": "separate"}), audio_job[1]))
    Pipeline(settings, store, asr_factory=ASR, storage_factory=Storage, llm_factory=FakeLLM).run_once()
    job = store.one("SELECT * FROM jobs WHERE id=?", (audio_job[1],))
    assert job["status"] == "COMPLETED", job["error"]
    assert ASR.submits == 2
    assert len(json.loads(job["checkpoint"])["tracks"]) == 2


def test_known_audio_error_is_not_hidden(settings, store, audio_job, monkeypatch):
    import interview_agent.pipeline as module
    from interview_agent.media import AudioProcessingError
    monkeypatch.setattr(module, "probe", lambda _: (_ for _ in ()).throw(AudioProcessingError("无法解码音频，请检查文件完整性。")))
    Pipeline(settings, store, asr_factory=ASR, storage_factory=Storage, llm_factory=FakeLLM).run_once()
    row = store.one("SELECT status,error FROM jobs WHERE id=?", (audio_job[1],))
    assert row["status"] == "FAILED"
    assert row["error"] == "无法解码音频，请检查文件完整性。"
    assert ASR.submits == 0


@pytest.mark.parametrize("already_submitted", [False, True])
def test_legacy_wav_checkpoint_not_reencoded_or_resubmitted(settings, store, audio_job, monkeypatch, already_submitted):
    import interview_agent.pipeline as module
    directory = settings.data_dir / "jobs" / audio_job[0] / audio_job[1]
    directory.mkdir(parents=True)
    wave = directory / "audio-0.wav"
    wave.write_bytes(b"existing-wave")
    job = store.claim("old-worker")
    track = {"path": str(wave.relative_to(settings.data_dir)), "channel": None, "object_key": "original-object.wav"}
    if already_submitted:
        call = store.reserve(job, "aliyun", settings.asr_model, 0.1, 50)
        track.update(uploaded=True, remote_task_id="remote-task-id", submitted_at=time.time(), call_id=call)
    job["checkpoint"].update(tracks=[track], duration_ms=1000)
    store.checkpoint(job)
    store.finish(job, "FAILED", "interrupted")
    store.retry(job["id"])
    monkeypatch.setattr(module, "prepare", lambda *args: pytest.fail("Existing WAV must be reused"))
    class LegacyStorage(Storage):
        uploads = 0
        def upload(self, key, path, **kwargs):
            type(self).uploads += 1
            assert key.endswith(".wav") and path == wave
            return super().upload(key, path, **kwargs)
    Pipeline(settings, store, asr_factory=ASR, storage_factory=LegacyStorage, llm_factory=FakeLLM).run_once()
    result = store.one("SELECT status,error,checkpoint FROM jobs WHERE id=?", (job["id"],))
    assert result["status"] == "COMPLETED", result["error"]
    assert wave.read_bytes() == b"existing-wave"
    assert ASR.submits == (0 if already_submitted else 1)
    assert LegacyStorage.uploads == (0 if already_submitted else 1)
    if already_submitted:
        assert json.loads(result["checkpoint"])["tracks"][0]["object_key"] == "original-object.wav"


def test_multipart_pipeline_resume_pins_storage_and_cleans(settings, store, audio_job, monkeypatch):
    from dataclasses import replace
    from test_uploads import Bucket, storage
    from interview_agent.uploads import PART_SIZE
    import interview_agent.pipeline as module
    monkeypatch.setattr(module, "prepare", lambda source, target, channel: target.write_bytes(b"a" * PART_SIZE + b"b" * PART_SIZE + b"end"))
    bucket = Bucket()
    bucket.fail_part = 2
    endpoints = []
    def factory(config):
        endpoints.append(config.oss_endpoint)
        return storage(bucket)
    pipeline = Pipeline(settings, store, asr_factory=ASR, storage_factory=factory, llm_factory=FakeLLM)
    pipeline.run_once()
    row = store.one("SELECT * FROM jobs WHERE id=?", (audio_job[1],))
    assert row["status"] == "RETRY_WAIT", row["error"]
    first = json.loads(row["checkpoint"])["tracks"][0]
    assert first["upload_state"]["confirmed_bytes"] == PART_SIZE
    assert ASR.submits == 0
    assert json.loads(store.one("SELECT config FROM cleanup")["config"])["upload_id"]
    with store.transaction() as con:
        con.execute("UPDATE jobs SET retry_at=0 WHERE id=?", (audio_job[1],))
    Pipeline(replace(settings, oss_endpoint="https://oss-cn-shanghai.aliyuncs.com"), store, asr_factory=ASR, storage_factory=factory, llm_factory=FakeLLM).run_once()
    row = store.one("SELECT * FROM jobs WHERE id=?", (audio_job[1],))
    assert row["status"] == "COMPLETED", row["error"]
    last = json.loads(row["checkpoint"])["tracks"][0]
    assert last["object_key"] == first["object_key"]
    assert last["upload_state"]["resume_count"] == 1
    assert endpoints == [settings.oss_endpoint, settings.oss_endpoint]
    assert bucket.sent == [1, 2, 2, 3] and ASR.submits == 1
    from interview_agent.diagnostics import report
    diagnostic = json.dumps(report(store, audio_job[1]))
    assert "upload_id" not in diagnostic and "SECRET" not in diagnostic
    pipeline.maintenance()
    assert not bucket.objects and not bucket.sessions
    assert store.one("SELECT status FROM cleanup")["status"] == "done"


def test_maintenance_retains_active_parts_then_cleans_failed_job(settings, store, audio_job):
    from test_uploads import Bucket, storage
    from interview_agent.db import uid
    bucket = Bucket()
    session = bucket.init_multipart_upload("partial", {}).upload_id
    with store.transaction() as con:
        con.execute("INSERT INTO cleanup(id,job_id,object_key,config,eligible) VALUES (?,?,?,?,0)", (uid(), audio_job[1], "partial", json.dumps({"oss_bucket": settings.oss_bucket, "oss_endpoint": settings.oss_endpoint, "upload_id": session})))
    pipeline = Pipeline(settings, store, storage_factory=lambda _: storage(bucket))
    pipeline.maintenance()
    assert session in bucket.sessions
    with store.transaction() as con:
        con.execute("UPDATE jobs SET status='FAILED' WHERE id=?", (audio_job[1],))
    pipeline.maintenance()
    assert not bucket.sessions
    assert store.one("SELECT status FROM cleanup")["status"] == "done"
