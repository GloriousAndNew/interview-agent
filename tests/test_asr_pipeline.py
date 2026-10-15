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

    def upload(self, key, path):
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
